"""Engine-primitive tests: checksum, hardened-XML guard, Saxon transform.

Layer C of the ADR-2026-07-01 test plan — the ONLY tests that run the real
SaxonC-HE/XSLT-2.0 runtime (Django-side layers A/B never touch it). The
fixture stylesheet uses xs:decimal constructors and the ``ne`` value
comparison, which no XSLT 1.0 processor accepts, so a passing transform
test proves the production engine actually ran.

The security tests cover the container-side D8 posture: the artefact
checksum gate (never execute unverified bytes) and the defusedxml guard
(XXE / entity bombs / DTDs rejected even though Django pre-guarded —
defence in depth).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from validator_backends.schematron import engine


FIXTURES = Path(__file__).parent / "fixtures"

XXE_PAYLOAD = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    "<foo>&xxe;</foo>"
)

TINY_MAX_BYTES = 64
TINY_MAX_DEPTH = 3
GENEROUS_LIMIT = 10_000_000


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── Artefact checksum gate (D4b) ─────────────────────────────────────────────


def test_matching_artifact_checksum_passes():
    """A downloaded artefact whose bytes match the envelope pin is accepted."""
    artifact = FIXTURES / "compiled_subset.xslt"
    engine.verify_artifact_checksum(artifact, _sha256(artifact))


def test_mismatched_artifact_checksum_refuses_with_machine_code():
    """Checksum drift refuses execution and carries the artifact_mismatch code.

    The error_code is what Django maps to its reserved
    ``schematron.artifact_mismatch`` finding (D9) — the machine hint must
    survive the engine boundary, not just the prose.
    """
    artifact = FIXTURES / "compiled_subset.xslt"
    with pytest.raises(engine.SchematronEngineError, match="mismatch") as excinfo:
        engine.verify_artifact_checksum(artifact, "0" * 64)
    assert excinfo.value.error_code == "artifact_mismatch"


# ── Hardened-XML guard (D8a, container side) ─────────────────────────────────


def test_guard_accepts_the_benign_fixture_invoice():
    """A well-formed, within-limits invoice passes the guard untouched."""
    engine.guard_submission(
        FIXTURES / "invoice_valid.xml",
        max_bytes=GENEROUS_LIMIT,
        max_depth=engine.HARD_MAX_INPUT_DEPTH,
    )


def test_guard_rejects_xxe_even_though_django_preguarded(tmp_path):
    """An XXE payload is rejected container-side too (defence in depth).

    Django's preprocess guard runs before dispatch, but this container must
    not trust that the bytes it downloaded are the bytes Django checked.
    """
    evil = tmp_path / "xxe.xml"
    evil.write_text(XXE_PAYLOAD, encoding="utf-8")
    with pytest.raises(engine.SchematronEngineError, match="forbidden constructs"):
        engine.guard_submission(
            evil,
            max_bytes=GENEROUS_LIMIT,
            max_depth=engine.HARD_MAX_INPUT_DEPTH,
        )


def test_guard_enforces_size_and_depth_caps(tmp_path):
    """Oversize and over-deep documents are refused per the D8 table."""
    big = tmp_path / "big.xml"
    big.write_text("<a>" + "x" * TINY_MAX_BYTES + "</a>", encoding="utf-8")
    with pytest.raises(engine.SchematronEngineError, match="too large"):
        engine.guard_submission(
            big,
            max_bytes=TINY_MAX_BYTES,
            max_depth=engine.HARD_MAX_INPUT_DEPTH,
        )

    deep = tmp_path / "deep.xml"
    deep.write_text("<a><b><c><d><e/></d></c></b></a>", encoding="utf-8")
    with pytest.raises(engine.SchematronEngineError, match="nests deeper"):
        engine.guard_submission(
            deep,
            max_bytes=GENEROUS_LIMIT,
            max_depth=TINY_MAX_DEPTH,
        )


def test_guard_clamps_envelope_limits_to_hard_maxima(tmp_path):
    """A hand-crafted envelope cannot widen the safety net beyond hard caps.

    Django clamps before shipping, but the container re-clamps: an absurd
    ``max_bytes`` in the envelope must still be bounded by
    ``HARD_MAX_INPUT_BYTES``.
    """
    assert (
        engine.clamp(10**12, engine.HARD_MAX_INPUT_BYTES, default=1)
        == engine.HARD_MAX_INPUT_BYTES
    )
    # Non-positive values fall back to the default, never "unlimited".
    assert engine.clamp(0, engine.HARD_MAX_INPUT_BYTES, default=42) == 42


# ── The Saxon transform (the production engine, item 7) ─────────────────────


def test_saxon_transform_produces_svrl_for_the_invalid_invoice(tmp_path):
    """The XSLT-2.0 fixture pack runs under Saxon and emits real SVRL.

    xs:decimal + ``ne`` make this stylesheet XSLT-2.0-only, so this test
    failing under a 1.0 processor is by design: passing PROVES SaxonC-HE
    executed the transform. The invalid invoice must yield the VB-CO-15
    failed-assert; ids/severities surviving the round trip is asserted in
    the runner tests via the shared parser.
    """
    out = tmp_path / "report.svrl"
    svrl = engine.run_transform(
        FIXTURES / "compiled_subset.xslt",
        FIXTURES / "invoice_invalid.xml",
        out,
        timeout_seconds=60,
    )
    assert "failed-assert" in svrl
    assert "VB-CO-15" in svrl
    assert "fired-rule" in svrl


def test_saxon_transform_is_clean_for_the_valid_invoice(tmp_path):
    """The reconciling invoice yields SVRL with a fired rule and no asserts."""
    out = tmp_path / "report.svrl"
    svrl = engine.run_transform(
        FIXTURES / "compiled_subset.xslt",
        FIXTURES / "invoice_valid.xml",
        out,
        timeout_seconds=60,
    )
    assert "failed-assert" not in svrl
    assert "fired-rule" in svrl


def test_broken_stylesheet_surfaces_as_engine_error(tmp_path):
    """A stylesheet Saxon cannot compile maps to a clean engine error.

    The worker exits non-zero with detail on stderr; the parent must wrap
    that as SchematronEngineError (→ D9 engine_status="error"), never leak
    a raw subprocess exception.
    """
    bad = tmp_path / "bad.xslt"
    bad.write_text("<xsl:stylesheet>this is not XSLT", encoding="utf-8")
    out = tmp_path / "report.svrl"
    with pytest.raises(engine.SchematronEngineError, match="transform failed"):
        engine.run_transform(
            bad,
            FIXTURES / "invoice_valid.xml",
            out,
            timeout_seconds=60,
        )


def test_saxon_engine_version_reports_name_and_version():
    """Provenance (D5) needs the actual engine identity, e.g. 'SaxonC-HE 12.9'."""
    version = engine.saxon_engine_version()
    assert "Saxon" in version
