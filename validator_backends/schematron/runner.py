"""Schematron validation runner for the isolated container backend.

Orchestrates one run from the typed input envelope (ADR-2026-07-01 D3/D4b):

1. Download the XML submission (``input_files[0]``) and the staged pack
   artefact (``inputs.artifact_uri``).
2. Verify the artefact checksum against the pinned ``artifact_sha256`` —
   the container never trusts what it fetched.
3. Re-apply the hardened-XML guard to the submission (D8, defence in depth).
4. Run the pack XSLT under Saxon with a hard wall-clock timeout.
5. Parse the SVRL with the canonical shared parser and assemble
   ``SchematronOutputs`` — counts, the ``finding_rule_ids_by_severity``
   map, capped findings with an explicit truncation marker, and the full
   D5 provenance (pack pins + actual engine version).

Every "couldn't run the rules" outcome maps to the D9 taxonomy
(``engine_status`` + ``engine_error_code``) with ``passed=None`` — never to
fabricated rule findings. The CEL/Basic output assertions are intentionally
NOT run here — they operate on the extracted signals and stay in Django,
evaluated by ``SchematronValidator.post_execute_validate``.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from validator_backends.core.storage_client import download_file
from validator_backends.schematron import engine
from validibot_shared.schematron.envelopes import (
    ENGINE_STATUS_ERROR,
    ENGINE_STATUS_OK,
    ENGINE_STATUS_TIMEOUT,
    SchematronFinding,
    SchematronOutputs,
)
from validibot_shared.schematron.svrl import (
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    SvrlParseError,
    parse_svrl,
)
from validibot_shared.validations.envelopes import (
    MessageLocation,
    Severity,
    ValidationMessage,
    ValidationStatus,
)


if TYPE_CHECKING:
    from validibot_shared.schematron.envelopes import SchematronInputEnvelope

logger = logging.getLogger(__name__)

# Findings-severity strings → the shared Severity enum for the generic
# ``messages`` list (Django rebuilds rich findings from outputs.findings;
# messages are the lowest-common-denominator view).
_SEVERITY_TO_ENUM = {
    SEVERITY_ERROR: Severity.ERROR,
    SEVERITY_WARNING: Severity.WARNING,
}


class SchematronRunResult(NamedTuple):
    """What ``main.py`` needs to assemble the output envelope."""

    status: ValidationStatus
    messages: list[ValidationMessage]
    outputs: SchematronOutputs


def run_schematron_validation(
    input_envelope: SchematronInputEnvelope,
) -> SchematronRunResult:
    """Execute one Schematron run per the input envelope."""
    inputs = input_envelope.inputs
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="schematron-") as tmp:
        tmpdir = Path(tmp)
        submission_path = tmpdir / "submission.xml"
        artifact_path = tmpdir / "pack.xslt"
        svrl_path = tmpdir / "report.svrl"

        try:
            download_file(input_envelope.input_files[0].uri, submission_path)
            download_file(inputs.artifact_uri, artifact_path)

            engine.verify_artifact_checksum(artifact_path, inputs.artifact_sha256)
            engine.guard_submission(
                submission_path,
                max_bytes=inputs.max_input_bytes,
                max_depth=inputs.max_input_depth,
            )
            svrl_text = engine.run_transform(
                artifact_path,
                submission_path,
                svrl_path,
                timeout_seconds=inputs.xslt_timeout_seconds,
            )
            summary = parse_svrl(
                svrl_text,
                max_findings=engine.clamp(
                    inputs.max_findings,
                    engine.HARD_MAX_FINDINGS,
                    default=engine.HARD_MAX_FINDINGS,
                ),
            )
        except engine.SchematronTransformTimeout as exc:
            return _engine_failure_result(
                inputs,
                engine_status=ENGINE_STATUS_TIMEOUT,
                engine_message=str(exc),
                execution_seconds=time.monotonic() - started,
            )
        except engine.SchematronEngineError as exc:
            return _engine_failure_result(
                inputs,
                engine_status=ENGINE_STATUS_ERROR,
                engine_message=str(exc),
                engine_error_code=exc.error_code,
                execution_seconds=time.monotonic() - started,
            )
        except (SvrlParseError, ValueError) as exc:
            # Unreadable SVRL or a bad storage URI is equally "the rules
            # were not run" — an engine error, never a rule failure.
            return _engine_failure_result(
                inputs,
                engine_status=ENGINE_STATUS_ERROR,
                engine_message=str(exc),
                execution_seconds=time.monotonic() - started,
            )

    execution_seconds = time.monotonic() - started

    outputs = SchematronOutputs(
        engine_status=ENGINE_STATUS_OK,
        passed=summary.passed,
        error_count=summary.error_count,
        warning_count=summary.warning_count,
        info_count=summary.info_count,
        fired_rule_count=summary.fired_rule_count,
        finding_rule_ids_by_severity=summary.finding_rule_ids_by_severity,
        findings=[
            SchematronFinding(
                rule_id=f.rule_id,
                message=f.message,
                severity=f.severity,
                location_xpath=f.location,
                flag=f.flag,
                role=f.role,
            )
            for f in summary.findings
        ],
        findings_truncated=summary.findings_truncated,
        findings_suppressed_count=summary.findings_suppressed_count,
        **_provenance(inputs),
        execution_seconds=execution_seconds,
    )

    status = (
        ValidationStatus.SUCCESS
        if summary.passed
        else ValidationStatus.FAILED_VALIDATION
    )
    return SchematronRunResult(
        status=status,
        messages=_messages_from_outputs(outputs),
        outputs=outputs,
    )


def _provenance(inputs) -> dict:
    """The D5 provenance echo: pins from the envelope + the ACTUAL engine.

    Pack pins come from the verified input envelope; the engine string is
    what really ran in this container (name + version), not what the pack
    requested — that difference is exactly what provenance exists to catch.
    """
    return {
        "pack_id": inputs.pack_id,
        "pack_version": inputs.pack_version,
        "pack_source_sha256": inputs.source_sha256,
        "pack_artifact_sha256": inputs.artifact_sha256,
        "query_binding": inputs.query_binding,
        "engine": engine.saxon_engine_version(),
    }


def _engine_failure_result(
    inputs,
    *,
    engine_status: str,
    engine_message: str,
    engine_error_code: str = "",
    execution_seconds: float,
) -> SchematronRunResult:
    """Assemble the D9 failure shape: rules NOT run, ``passed`` unknown.

    Counts stay at zero-but-meaningless (Django nulls them from
    ``engine_status``), the findings list stays empty, and the single
    user-facing message carries the engine detail.
    """
    logger.error("Schematron engine failure (%s): %s", engine_status, engine_message)
    outputs = SchematronOutputs(
        engine_status=engine_status,
        engine_message=engine_message,
        engine_error_code=engine_error_code,
        passed=None,
        **_provenance(inputs),
        execution_seconds=execution_seconds,
    )
    return SchematronRunResult(
        status=ValidationStatus.FAILED_RUNTIME,
        messages=[
            ValidationMessage(severity=Severity.ERROR, text=engine_message),
        ],
        outputs=outputs,
    )


def _messages_from_outputs(outputs: SchematronOutputs) -> list[ValidationMessage]:
    """Project findings into the generic envelope ``messages`` list.

    Django rebuilds its rich findings (rule ids, deep links, meta) from
    ``outputs.findings``; the messages list is the generic fallback other
    envelope consumers read.
    """
    messages: list[ValidationMessage] = []
    for finding in outputs.findings:
        messages.append(
            ValidationMessage(
                severity=_SEVERITY_TO_ENUM.get(finding.severity, Severity.INFO),
                code=finding.rule_id or None,
                text=finding.message,
                location=(
                    MessageLocation(path=finding.location_xpath)
                    if finding.location_xpath
                    else None
                ),
            ),
        )
    return messages
