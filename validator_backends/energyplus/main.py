"""
EnergyPlus validator container entrypoint.

This Cloud Run Job container:
1. Downloads input.json from GCS
2. Downloads input files (IDF, EPW) from GCS
3. Runs EnergyPlus simulation
4. Uploads output.json to GCS
5. POSTs callback to Django
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from validator_backends.core.callback_client import post_callback
from validator_backends.core.envelope_loader import get_output_uri, load_input_envelope
from validator_backends.core.error_reporting import report_fatal
from validator_backends.core.gcs_client import upload_directory, upload_envelope
from validibot_shared.energyplus.envelopes import (
    EnergyPlusInputEnvelope,
    EnergyPlusOutputEnvelope,
    EnergyPlusOutputs,
)
from validibot_shared.validations.envelopes import (
    RawOutputs,
    Severity,
    ValidationArtifact,
    ValidationMessage,
    ValidationStatus,
    ValidatorType,
)

from .runner import run_energyplus_simulation


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> int:
    """
    Main entrypoint for EnergyPlus validator container.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    started_at = datetime.now(UTC)

    try:
        # Load input envelope from GCS
        logger.info("Loading input envelope...")
        input_envelope = load_input_envelope(EnergyPlusInputEnvelope)

        logger.info(
            "Loaded input envelope for run_id=%s, validator=%s v%s",
            input_envelope.run_id,
            input_envelope.validator.type,
            input_envelope.validator.version,
        )

        # Run EnergyPlus simulation
        logger.info("Running EnergyPlus simulation...")
        outputs, work_dir, parsed_messages = run_energyplus_simulation(input_envelope)

        # Determine status from simulation results
        if outputs.energyplus_returncode == 0:
            status = ValidationStatus.SUCCESS
        else:
            status = ValidationStatus.FAILED_VALIDATION

        # Upload raw outputs to GCS for debugging / artifacts
        artifacts: list[ValidationArtifact] = []
        raw_outputs: RawOutputs | None = None
        try:
            execution_bundle_uri = str(input_envelope.context.execution_bundle_uri)
            artifacts, raw_outputs = _upload_outputs(work_dir, execution_bundle_uri)
            outputs = _rewrite_output_paths(outputs, artifacts)
        except Exception:
            logger.exception("Failed to upload EnergyPlus outputs; continuing without artifacts")

        finished_at = datetime.now(UTC)

        # Convert parsed messages to ValidationMessage objects
        messages: list[ValidationMessage] = []
        for msg in parsed_messages:
            severity_str = msg.get("severity", "error")
            if severity_str == "warning":
                severity = Severity.WARNING
            elif severity_str == "info":
                severity = Severity.INFO
            else:
                severity = Severity.ERROR
            messages.append(
                ValidationMessage(
                    severity=severity,
                    text=msg.get("text", ""),
                    code=msg.get("code"),
                )
            )

        if messages:
            logger.info("Including %d validation messages in output", len(messages))

        # Create output envelope
        logger.info("Creating output envelope...")
        output_envelope = EnergyPlusOutputEnvelope(
            run_id=input_envelope.run_id,
            validator=input_envelope.validator,
            status=status,
            timing={
                "started_at": started_at,
                "finished_at": finished_at,
            },
            messages=messages,
            metrics=[],  # Populated by runner if needed
            outputs=outputs,
            artifacts=artifacts,
            raw_outputs=raw_outputs,
        )

        # Upload output envelope to GCS
        output_uri = get_output_uri(input_envelope)
        logger.info("Uploading output envelope to %s", output_uri)
        upload_envelope(output_envelope, output_uri)

        # POST callback to Django (unless skip_callback is set)
        logger.info("Sending callback to Django...")
        post_callback(
            callback_url=(
                str(input_envelope.context.callback_url)
                if input_envelope.context.callback_url
                else None
            ),
            run_id=input_envelope.run_id,
            status=status,
            result_uri=output_uri,
            callback_id=input_envelope.context.callback_id,
            skip_callback=input_envelope.context.skip_callback,
        )

        logger.info("Validation complete (status=%s)", status.value)
        return 0

    except (FileNotFoundError, ValueError) as exc:
        logger.error("Validation failed due to missing/invalid input: %s", exc)
        return _handle_failure(
            input_envelope=input_envelope if "input_envelope" in locals() else None,
            started_at=started_at,
            message=str(exc),
            report_exception=False,
            exit_code=0,
        )
    except Exception as exc:
        logger.exception("Validation failed with unexpected error")
        return _handle_failure(
            input_envelope=input_envelope if "input_envelope" in locals() else None,
            started_at=started_at,
            message="EnergyPlus validator failed. Please retry or contact support.",
            report_exception=True,
            exit_code=1,
            exception=exc,
        )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _upload_outputs(
    work_dir: Path,
    execution_bundle_uri: str,
) -> tuple[list[ValidationArtifact], RawOutputs | None]:
    """
    Upload all files from the working directory to GCS and build artifact metadata.
    """
    base_uri = execution_bundle_uri.rstrip("/")
    outputs_uri = f"{base_uri}/outputs"
    manifest = upload_directory(
        work_dir,
        outputs_uri,
        manifest_path="manifest.json",
    )

    artifacts: list[ValidationArtifact] = []
    for item in manifest.get("files", []):
        name = item.get("name", "")
        uri = item.get("uri", "")
        size_bytes = item.get("size_bytes")
        artifacts.append(
            ValidationArtifact(
                name=name,
                type=_infer_artifact_type(name),
                mime_type=_guess_mime_type(name),
                uri=uri,
                size_bytes=size_bytes,
            )
        )

    raw_outputs = RawOutputs(
        format=manifest.get("format", "directory"),
        manifest_uri=manifest.get("manifest_uri", f"{outputs_uri}/manifest.json"),
    )
    return artifacts, raw_outputs


def _infer_artifact_type(name: str) -> str:
    """Best-effort artifact typing based on filename."""
    lowered = name.lower()
    if lowered.endswith(".sql"):
        return "simulation-db"
    if lowered.endswith(".csv"):
        return "timeseries-csv"
    if lowered.endswith(".err"):
        return "err-log"
    if lowered.endswith(".eso"):
        return "eso"
    return "file"


def _guess_mime_type(name: str) -> str | None:
    """Map common EnergyPlus outputs to MIME types."""
    lowered = name.lower()
    if lowered.endswith(".sql"):
        return "application/x-sqlite3"
    if lowered.endswith(".csv"):
        return "text/csv"
    if lowered.endswith(".err") or lowered.endswith(".txt"):
        return "text/plain"
    return None


def _rewrite_output_paths(
    outputs: EnergyPlusOutputs,
    artifacts: list[ValidationArtifact],
) -> EnergyPlusOutputs:
    """
    Replace local file paths in outputs with GCS URIs where available.
    """
    uri_by_name = {Path(a.name).name: a.uri for a in artifacts}
    sim_outputs = outputs.outputs

    def _map(name: str, current: Path | None) -> Path | None | str:
        if name in uri_by_name:
            return uri_by_name[name]
        return current

    if sim_outputs:
        sim_outputs.eplusout_sql = _map("eplusout.sql", sim_outputs.eplusout_sql)
        sim_outputs.eplusout_err = _map("eplusout.err", sim_outputs.eplusout_err)
        sim_outputs.eplusout_csv = _map("eplusout.csv", sim_outputs.eplusout_csv)
        sim_outputs.eplusout_eso = _map("eplusout.eso", sim_outputs.eplusout_eso)
        outputs.outputs = sim_outputs
    return outputs


def _handle_failure(
    *,
    input_envelope: EnergyPlusInputEnvelope | None,
    started_at: datetime,
    message: str,
    report_exception: bool,
    exit_code: int,
    exception: Exception | None = None,
) -> int:
    """
    Serialize a failure envelope and callback without crashing the Job.
    """
    if report_exception and exception is not None:
        report_fatal(
            exception,
            context={
                "run_id": getattr(input_envelope, "run_id", None),
                "validator": ValidatorType.ENERGYPLUS,
            },
        )

    if input_envelope is None:
        # If we failed before reading the input envelope, we cannot upload results.
        return exit_code

    finished_at = datetime.now(UTC)
    failure_envelope = EnergyPlusOutputEnvelope(
        run_id=input_envelope.run_id,
        validator=input_envelope.validator,
        status=ValidationStatus.FAILED_RUNTIME,
        timing={
            "started_at": started_at,
            "finished_at": finished_at,
        },
        messages=[
            ValidationMessage(
                severity=Severity.ERROR,
                text=message,
            ),
        ],
        outputs=EnergyPlusOutputs(
            energyplus_returncode=-1,
            execution_seconds=0,
            invocation_mode="cli",
        ),
    )

    output_uri = get_output_uri(input_envelope)
    upload_envelope(failure_envelope, output_uri)

    post_callback(
        callback_url=(
            str(input_envelope.context.callback_url)
            if input_envelope.context.callback_url
            else None
        ),
        run_id=input_envelope.run_id,
        status=ValidationStatus.FAILED_RUNTIME,
        result_uri=output_uri,
        callback_id=input_envelope.context.callback_id,
        skip_callback=input_envelope.context.skip_callback,
    )

    logger.info("Published failure envelope for run_id=%s", input_envelope.run_id)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
