"""End-to-end runner tests: envelope in → SchematronOutputs out (layer C).

Drives ``run_schematron_validation`` with real file:// staging, the real
Saxon compile-and-run, and the canonical shared SVRL parser — the full
container path minus storage/network. The rules arrive exactly as in
production: **inline** in ``inputs.schematron_text``. Pins the two
contracts Django depends on:

1. **The signal surface** — counts, ``finding_rule_ids_by_severity``,
   findings with native ids/locations, and the D5 provenance echo (the
   rules' sha256 from the envelope, the detected query binding, and the
   engine that ACTUALLY ran).
2. **The D9 failure taxonomy** — uncompilable rules (``rules_invalid``),
   guard rejection, and transform timeout produce
   ``engine_status``/``engine_error_code`` with ``passed=None`` and NO
   fabricated rule findings, under ``ValidationStatus.FAILED_RUNTIME``.

Saxon-dependent tests skip if the vendored SchXslt2 transpiler is absent
(see ``schxslt2/README.md``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from validator_backends.schematron import engine
from validator_backends.schematron.runner import run_schematron_validation
from validibot_shared.schematron.envelopes import (
    SchematronInputEnvelope,
    SchematronInputs,
    SchematronOutputEnvelope,
)
from validibot_shared.validations.envelopes import (
    InputFileItem,
    SupportedMimeType,
    ValidationStatus,
    ValidatorType,
)


FIXTURES = Path(__file__).parent / "fixtures"
SCH_TEXT = (FIXTURES / "subset.sch").read_text()
SCH_SHA = hashlib.sha256(SCH_TEXT.encode("utf-8")).hexdigest()

XXE_PAYLOAD = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    "<foo>&xxe;</foo>"
)

requires_transpiler = pytest.mark.skipif(
    not engine.transpiler_available(),
    reason=(
        "SchXslt2 transpiler not vendored — see validator_backends/schematron/schxslt2/README.md"
    ),
)


def _envelope(
    submission_path: Path,
    *,
    schematron_text: str = SCH_TEXT,
    xslt_timeout_seconds: int = 60,
) -> SchematronInputEnvelope:
    """Build an input envelope over a file:// submission (no storage backend)."""
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
            schematron_text=schematron_text,
            schematron_sha256=SCH_SHA,
            xslt_timeout_seconds=xslt_timeout_seconds,
        ),
        context={
            "callback_url": "https://example.com/callback",
            "execution_bundle_uri": "file:///tmp/run-1/",
            "skip_callback": True,
        },
    )


@requires_transpiler
def test_valid_invoice_passes_with_full_provenance():
    """A reconciling invoice yields engine ok, passed=True, and D5 provenance.

    The provenance echo is what makes the result reproducible: the rules'
    sha256 comes from the envelope, the query binding is detected from the
    source, and ``engine`` is the identity of what ACTUALLY ran.
    """
    result = run_schematron_validation(_envelope(FIXTURES / "invoice_valid.xml"))

    assert result.status == ValidationStatus.SUCCESS
    outputs = result.outputs
    assert outputs.engine_status == "ok"
    assert outputs.passed is True
    assert outputs.error_count == 0
    assert outputs.fired_rule_count == 1
    assert outputs.findings == []
    assert outputs.schematron_sha256 == SCH_SHA
    assert outputs.query_binding == "xslt2"
    assert "SchXslt2" in outputs.engine
    assert "Saxon" in outputs.engine
    assert outputs.execution_seconds > 0


@requires_transpiler
def test_invalid_invoice_fails_vb_co_15_with_the_d2_signal_shape():
    """The seeded totals defect round-trips compile → Saxon → SVRL → contract.

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


@requires_transpiler
def test_uncompilable_rules_map_to_rules_invalid():
    """Broken uploaded rules are an engine failure, never a rule failure.

    engine_status="error" + engine_error_code="rules_invalid" is what
    Django maps to its reserved ``schematron.rules_invalid`` finding —
    telling the AUTHOR their rules are broken while the submitter's
    document stays un-judged (``passed`` is None, no findings fabricated).
    """
    envelope = _envelope(
        FIXTURES / "invoice_valid.xml",
        schematron_text="<schema>this is not schematron",
    )
    result = run_schematron_validation(envelope)

    assert result.status == ValidationStatus.FAILED_RUNTIME
    outputs = result.outputs
    assert outputs.engine_status == "error"
    assert outputs.engine_error_code == "rules_invalid"
    assert outputs.passed is None
    assert outputs.findings == []
    assert outputs.finding_rule_ids_by_severity == {}


def test_xxe_submission_is_blocked_and_never_reaches_saxon(tmp_path):
    """The container-side guard rejects an XXE payload as an engine error.

    The ADR Phase 3 security test: a malicious invoice is blocked, not
    fetched — and because the guard runs before compile-and-run, Saxon
    never sees the document (this test needs no transpiler for that reason).
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

    monkeypatch.setattr(engine, "run_schematron", _times_out)

    result = run_schematron_validation(_envelope(FIXTURES / "invoice_valid.xml"))

    assert result.status == ValidationStatus.FAILED_RUNTIME
    assert result.outputs.engine_status == "timeout"
    assert result.outputs.passed is None
    assert "wall-clock" in result.outputs.engine_message


# ── The container entrypoint (main.py) ───────────────────────────────────────
# main.py is the only code the container actually boots, yet nothing else
# covers it — a stale field reference there (e.g. the pack-era
# ``inputs.pack_id`` logging that survived into the SchXslt2 retarget) would
# crash every production run while the runner tests stayed green.


@requires_transpiler
def test_main_entrypoint_round_trips_the_envelope(tmp_path, monkeypatch):
    """The real entrypoint runs envelope-in → output.json-out, hermetically.

    Drives ``main.main()`` exactly as a container boot does — input envelope
    resolved from ``VALIDIBOT_INPUT_URI``, output written to
    ``VALIDIBOT_OUTPUT_URI`` — over file:// storage with the callback
    skipped, pinning the glue the runner tests cannot see (envelope
    loading, startup logging, output assembly and upload).
    """
    from validator_backends.schematron import main as schematron_main

    envelope = _envelope(FIXTURES / "invoice_invalid.xml")
    input_path = tmp_path / "input.json"
    input_path.write_text(envelope.model_dump_json(), encoding="utf-8")
    output_path = tmp_path / "output.json"
    monkeypatch.setenv("VALIDIBOT_INPUT_URI", f"file://{input_path}")
    monkeypatch.setenv("VALIDIBOT_OUTPUT_URI", f"file://{output_path}")

    exit_code = schematron_main.main()

    assert exit_code == 0
    output = SchematronOutputEnvelope.model_validate_json(
        output_path.read_text(encoding="utf-8"),
    )
    assert output.run_id == "run-1"
    assert output.status == ValidationStatus.FAILED_VALIDATION
    assert output.outputs.engine_status == "ok"
    assert output.outputs.finding_rule_ids_by_severity == {"VB-CO-15": "ERROR"}
