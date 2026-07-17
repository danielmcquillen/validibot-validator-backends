"""Engine-primitive tests: hardened-XML guard, compile-and-run, hard caps.

Layer C of the ADR-2026-07-01 test plan — the ONLY tests that run the real
SaxonC-HE runtime. The fixture ``.sch`` declares ``queryBinding="xslt2"``
with XPath 2.0 expressions, so a passing compile-and-run test proves the
full production path: SchXslt2 transpile under Saxon, then the compiled
rules under Saxon.

The Saxon tests additionally require the vendored SchXslt2 transpiler
(``schxslt2/transpile.xsl`` — see that directory's README) and skip with a
pointer to it when absent, so the suite stays green even on a checkout
missing the vendored tooling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from validator_backends.schematron import engine, saxon_worker


FIXTURES = Path(__file__).parent / "fixtures"

XXE_PAYLOAD = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    "<foo>&xxe;</foo>"
)

TINY_MAX_BYTES = 64
TINY_MAX_DEPTH = 3
GENEROUS_LIMIT = 10_000_000

requires_transpiler = pytest.mark.skipif(
    not engine.transpiler_available(),
    reason=(
        "SchXslt2 transpiler not vendored — see validator_backends/schematron/schxslt2/README.md"
    ),
)


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

    Django's guards run at authoring and dispatch time, but this container
    must not trust that the bytes it downloaded are the bytes Django saw.
    """
    evil = tmp_path / "xxe.xml"
    evil.write_text(XXE_PAYLOAD, encoding="utf-8")
    with pytest.raises(engine.SchematronEngineError, match="forbidden constructs"):
        engine.guard_submission(
            evil,
            max_bytes=GENEROUS_LIMIT,
            max_depth=engine.HARD_MAX_INPUT_DEPTH,
        )


def test_guard_rejects_xinclude_before_saxon_parses_the_submission(tmp_path):
    """XInclude cannot make an otherwise DTD-free document read local files.

    SaxonC 13 no longer exposes the old ``xInclude-aware`` configuration
    feature. Rejecting the instruction in the deterministic pre-guard keeps
    source-node loading safe before URI protocols are disabled in the worker.
    """
    evil = tmp_path / "xinclude.xml"
    evil.write_text(
        '<root xmlns:xi="http://www.w3.org/2001/XInclude">'
        '<xi:include href="file:///etc/passwd" parse="text"/>'
        "</root>",
        encoding="utf-8",
    )

    with pytest.raises(engine.SchematronEngineError, match="forbidden XInclude"):
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


def test_guard_clamps_envelope_limits_to_hard_maxima():
    """A hand-crafted envelope cannot widen the safety net beyond hard caps.

    Django clamps before shipping, but the container re-clamps: an absurd
    ``max_bytes`` in the envelope must still be bounded by
    ``HARD_MAX_INPUT_BYTES``.
    """
    assert (
        engine.clamp(10**12, engine.HARD_MAX_INPUT_BYTES, default=1) == engine.HARD_MAX_INPUT_BYTES
    )
    # Non-positive values fall back to the default, never "unlimited".
    assert engine.clamp(0, engine.HARD_MAX_INPUT_BYTES, default=42) == 42


def test_saxon_worker_decodes_a_declared_non_utf8_xml_encoding():
    """In-memory URI lockdown must not narrow valid XML to UTF-8 documents.

    SaxonC 13's text API requires a Python string. The worker therefore honors
    the XML declaration before passing text into the no-protocol processor,
    preserving submissions such as legacy ISO-8859-1 business documents.
    """
    source = '<?xml version="1.0" encoding="ISO-8859-1"?><root>café</root>'

    assert saxon_worker._decode_xml_bytes(source.encode("iso-8859-1")) == source


# ── Query-binding detection (provenance) ─────────────────────────────────────


def test_detect_query_binding_reads_and_normalises_the_root_attribute(tmp_path):
    """queryBinding detection covers xslt2, the ISO default, and failures.

    The detected binding is provenance only, so failure modes return ""
    rather than raising — a run must never die over a provenance field.
    """
    assert engine.detect_query_binding(FIXTURES / "subset.sch") == "xslt2"

    default_binding = tmp_path / "default.sch"
    default_binding.write_text(
        '<schema xmlns="http://purl.oclc.org/dsdl/schematron"/>',
        encoding="utf-8",
    )
    assert engine.detect_query_binding(default_binding) == "xslt1"

    assert engine.detect_query_binding(tmp_path / "missing.sch") == ""


# ── Compile-and-run (the production engine, item 7) ─────────────────────────


@requires_transpiler
def test_compile_and_run_flags_the_invalid_invoice(tmp_path):
    """The XSLT-2.0 fixture source compiles under Saxon and emits real SVRL.

    xs:decimal + ``eq`` make the compiled rules XSLT-2.0-only, so this
    passing proves BOTH production stages ran under Saxon: the SchXslt2
    transpile and the transform itself.
    """
    out = tmp_path / "report.svrl"
    svrl = engine.run_schematron(
        FIXTURES / "subset.sch",
        FIXTURES / "invoice_invalid.xml",
        out,
        timeout_seconds=60,
    )
    assert "failed-assert" in svrl
    assert "VB-CO-15" in svrl
    assert "fired-rule" in svrl


@requires_transpiler
def test_compile_and_run_is_clean_for_the_valid_invoice(tmp_path):
    """The reconciling invoice yields SVRL with a fired rule and no asserts."""
    out = tmp_path / "report.svrl"
    svrl = engine.run_schematron(
        FIXTURES / "subset.sch",
        FIXTURES / "invoice_valid.xml",
        out,
        timeout_seconds=60,
    )
    assert "failed-assert" not in svrl
    assert "fired-rule" in svrl


@requires_transpiler
def test_uncompilable_rules_surface_as_rules_invalid(tmp_path):
    """A source Saxon cannot compile maps to error_code="rules_invalid".

    The machine hint is what lets Django render "the workflow author's
    rules are broken" distinctly from a generic engine failure (D9).
    """
    bad = tmp_path / "bad.sch"
    bad.write_text("<schema>this is not schematron", encoding="utf-8")
    out = tmp_path / "report.svrl"
    with pytest.raises(engine.SchematronEngineError, match="failed to compile") as exc:
        engine.run_schematron(
            bad,
            FIXTURES / "invoice_valid.xml",
            out,
            timeout_seconds=60,
        )
    assert exc.value.error_code == "rules_invalid"


@requires_transpiler
def test_uri_retrieval_functions_cannot_read_container_files(tmp_path):
    """Author rules must not read arbitrary file:// URIs through Saxon.

    ``doc()`` used to be able to read any well-formed XML file visible inside
    the container and copy the content into SVRL text. The validator backend
    has no business granting that ambient filesystem authority: uploaded rules
    are executable XSLT and should only see the submitted XML document.
    """
    secret = tmp_path / "secret.xml"
    secret.write_text("<secret>LEAK_VALIDIBOT_TEST_SECRET</secret>", encoding="utf-8")
    malicious_rules = tmp_path / "file_read.sch"
    malicious_rules.write_text(
        f"""
        <schema xmlns="http://purl.oclc.org/dsdl/schematron" queryBinding="xslt2">
          <pattern>
            <rule context="/">
              <assert id="FILE-READ" test="false()">
                secret=<value-of select='doc("file://{secret}")/secret/text()'/>
              </assert>
            </rule>
          </pattern>
        </schema>
        """,
        encoding="utf-8",
    )
    out = tmp_path / "report.svrl"

    with pytest.raises(engine.SchematronEngineError) as exc:
        engine.run_schematron(
            malicious_rules,
            FIXTURES / "invoice_valid.xml",
            out,
            timeout_seconds=60,
        )

    message = str(exc.value)
    assert "LEAK_VALIDIBOT_TEST_SECRET" not in message
    if out.exists():
        assert "LEAK_VALIDIBOT_TEST_SECRET" not in out.read_text(encoding="utf-8")


@requires_transpiler
def test_engine_version_reports_both_toolchain_halves():
    """Provenance (D5) needs the full toolchain identity.

    Both halves matter for reproducibility — SchXslt2 decides how the .sch
    compiles, Saxon decides how the compiled XSLT executes — so the engine
    string must name both, e.g. 'SchXslt2 1.11.1 + SaxonC-HE 12.9'.
    """
    version = engine.engine_version()
    assert "SchXslt2" in version
    assert "Saxon" in version
