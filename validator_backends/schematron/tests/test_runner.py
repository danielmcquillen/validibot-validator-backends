"""End-to-end runner tests: envelope in → SchematronOutputs out (layer C).

Drives ``run_schematron_validation`` with real file:// staging, the real
Saxon engine, and the canonical shared SVRL parser — the full container
path minus storage/network. Pins the two contracts Django depends on:

1. **The signal surface** — counts, ``finding_rule_ids_by_severity``,
   findings with native ids/locations, and the D5 provenance echo (pack
   pins from the envelope + the engine that ACTUALLY ran).
2. **The D9 failure taxonomy** — checksum mismatch, guard rejection, and
   transform timeout produce ``engine_status``/``engine_error_code`` with
   ``passed=None`` and NO fabricated rule findings, under
   ``ValidationStatus.FAILED_RUNTIME``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from validator_backends.schematron import engine
from validator_backends.schematron.runner import run_schematron_validation
from validibot_shared.schematron.envelopes import (
    SchematronInputEnvelope,
    SchematronInputs,
)
from validibot_shared.validations.envelopes import (
    InputFileItem,
    SupportedMimeType,
    ValidationStatus,
    ValidatorType,
)


FIXTURES = Path(__file__).parent / "fixtures"
ARTIFACT = FIXTURES / "compiled_subset.xslt"
ARTIFACT_SHA = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()

XXE_PAYLOAD = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    "<foo>&xxe;</foo>"
)


def _envelope(
    submission_path: Path,
    *,
    artifact_sha256: str = ARTIFACT_SHA,
    xslt_timeout_seconds: int = 60,
) -> SchematronInputEnvelope:
    """Build an input envelope over file:// URIs (no storage backend)."""
    return SchematronInputEnvelope(
        run_id="run-1",
        validator={"id": "v1", "type": ValidatorType.SCHEMATRON, "version": "1"},
        org={"id": "org-1", "name": "ValidiBot"},
        workflow={"id": "wf-1", "step_id": "step-1", "step_name": "Peppol rules"},
        input_files=[
            InputFileItem(
                name="submission.xml",
                mime_type=SupportedMimeType.APPLICATION_XML,
                role="primary-model",
                uri=f"file://{submission_path}",
            ),
        ],
        inputs=SchematronInputs(
            pack_id="vb-peppol-subset",
            pack_version="0.1.0",
            artifact_uri=f"file://{ARTIFACT}",
            artifact_sha256=artifact_sha256,
            source_sha256="a" * 64,
            query_binding="xslt2",
            engine="saxonc-he",
            xslt_timeout_seconds=xslt_timeout_seconds,
        ),
        context={
            "callback_url": "https://example.com/callback",
            "execution_bundle_uri": "file:///tmp/run-1/",
            "skip_callback": True,
        },
    )


def test_valid_invoice_passes_with_full_provenance():
    """A reconciling invoice yields engine ok, passed=True, and D5 provenance.

    The provenance echo is what makes the result reproducible: the pack pins
    come from the verified envelope, and ``engine`` is the identity of what
    ACTUALLY ran in this container — not what the pack requested.
    """
    result = run_schematron_validation(_envelope(FIXTURES / "invoice_valid.xml"))

    assert result.status == ValidationStatus.SUCCESS
    outputs = result.outputs
    assert outputs.engine_status == "ok"
    assert outputs.passed is True
    assert outputs.error_count == 0
    assert outputs.fired_rule_count == 1
    assert outputs.findings == []
    assert outputs.pack_id == "vb-peppol-subset"
    assert outputs.pack_version == "0.1.0"
    assert outputs.pack_artifact_sha256 == ARTIFACT_SHA
    assert "Saxon" in outputs.engine
    assert outputs.execution_seconds > 0


def test_invalid_invoice_fails_vb_co_15_with_the_d2_signal_shape():
    """The seeded totals defect round-trips Saxon → SVRL → the D2 contract.

    This is ADR test-plan item 7's substance: ids and severities survive
    the production engine, and the ``finding_rule_ids_by_severity`` map has
    the exact {rule_id: severity} shape CEL gates are written against.
    """
    result = run_schematron_validation(_envelope(FIXTURES / "invoice_invalid.xml"))

    assert result.status == ValidationStatus.FAILED_VALIDATION
    outputs = result.outputs
    assert outputs.engine_status == "ok"
    assert outputs.passed is False
    assert outputs.error_count == 1
    assert outputs.finding_rule_ids_by_severity == {"VB-CO-15": "ERROR"}

    finding = outputs.findings[0]
    assert finding.rule_id == "VB-CO-15"
    assert finding.severity == "ERROR"
    assert finding.flag == "fatal"
    assert "LegalMonetaryTotal" in finding.location_xpath

    # The generic messages list carries the same finding for consumers that
    # never look inside outputs.
    assert any((m.code or "") == "VB-CO-15" for m in result.messages)


def test_artifact_checksum_mismatch_maps_to_the_d9_taxonomy():
    """A drifted artefact is an engine failure, never a rule failure.

    engine_status="error" + engine_error_code="artifact_mismatch" is what
    Django maps to its reserved ``schematron.artifact_mismatch`` finding;
    ``passed`` stays None (unknown) and no rule findings are fabricated.
    """
    envelope = _envelope(
        FIXTURES / "invoice_valid.xml",
        artifact_sha256="0" * 64,
    )
    result = run_schematron_validation(envelope)

    assert result.status == ValidationStatus.FAILED_RUNTIME
    outputs = result.outputs
    assert outputs.engine_status == "error"
    assert outputs.engine_error_code == "artifact_mismatch"
    assert outputs.passed is None
    assert outputs.findings == []
    assert outputs.finding_rule_ids_by_severity == {}


def test_xxe_submission_is_blocked_and_never_reaches_saxon(tmp_path):
    """The container-side guard rejects an XXE payload as an engine error.

    The ADR Phase 3 security test: a malicious invoice is blocked, not
    fetched — and because the guard runs before the transform, Saxon never
    sees the document at all.
    """
    evil = tmp_path / "xxe.xml"
    evil.write_text(XXE_PAYLOAD, encoding="utf-8")

    result = run_schematron_validation(_envelope(evil))

    assert result.status == ValidationStatus.FAILED_RUNTIME
    assert result.outputs.engine_status == "error"
    assert result.outputs.passed is None
    assert "forbidden constructs" in result.outputs.engine_message


def test_transform_timeout_maps_to_engine_status_timeout(monkeypatch):
    """A wall-clock timeout surfaces as engine_status="timeout" (D8/D9).

    The timeout itself is enforced by the subprocess boundary (exercised in
    the engine tests); here the mapping is pinned by simulating the raise —
    real long-running transforms would make the suite slow and flaky.
    """

    def _times_out(*args, **kwargs):
        raise engine.SchematronTransformTimeout(1)

    monkeypatch.setattr(engine, "run_transform", _times_out)

    result = run_schematron_validation(_envelope(FIXTURES / "invoice_valid.xml"))

    assert result.status == ValidationStatus.FAILED_RUNTIME
    assert result.outputs.engine_status == "timeout"
    assert result.outputs.passed is None
    assert "wall-clock" in result.outputs.engine_message


# ADR test-plan item 8 (real-pack smoke test: pinned en16931-ubl /
# peppol-bis-billing-ubl against known-good/bad UBL invoices, asserting real
# BR-* / PEPPOL-EN16931-* ids and the D7 pack boundary) requires the first
# packs to be vendored into the community repo. Add it alongside the
# vendoring of those packs — tracked in the ADR's Phase 3 checklist.
