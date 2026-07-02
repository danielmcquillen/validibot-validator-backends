"""Schematron validator container entrypoint for Cloud Run Jobs / Docker.

Loads a ``SchematronInputEnvelope``, runs the curated rule pack (Saxon
XSLT over the submitted XML) in this isolated process, writes a
``SchematronOutputEnvelope`` to storage, and POSTs the callback to Django.

The isolation is the whole point: rule-pack XSLT executes over untrusted
submitted XML only ever in this container, which has no database, no
secrets, and a locked-down service account (ADR-2026-07-01 D4/D8).
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from validator_backends.core.callback_client import post_callback
from validator_backends.core.envelope_loader import get_output_uri, load_input_envelope
from validator_backends.core.error_reporting import report_fatal
from validator_backends.core.gcs_client import upload_envelope
from validator_backends.schematron.runner import run_schematron_validation
from validibot_shared.schematron.envelopes import (
    SchematronInputEnvelope,
    SchematronOutputEnvelope,
)
from validibot_shared.validations.envelopes import (
    Severity,
    ValidationMessage,
    ValidationStatus,
    ValidatorType,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> int:
    started_at = datetime.now(UTC)

    try:
        input_envelope = load_input_envelope(SchematronInputEnvelope)
        logger.info(
            "Loaded Schematron input envelope for run_id=%s pack=%s@%s",
            input_envelope.run_id,
            input_envelope.inputs.pack_id,
            input_envelope.inputs.pack_version,
        )

        result = run_schematron_validation(input_envelope)
        finished_at = datetime.now(UTC)

        output_envelope = SchematronOutputEnvelope(
            run_id=input_envelope.run_id,
            validator=input_envelope.validator,
            status=result.status,
            timing={"started_at": started_at, "finished_at": finished_at},
            messages=result.messages,
            metrics=[],
            artifacts=[],
            outputs=result.outputs,
        )

        output_uri = get_output_uri(input_envelope)
        logger.info("Uploading Schematron output envelope to %s", output_uri)
        upload_envelope(output_envelope, output_uri)

        post_callback(
            callback_url=(
                str(input_envelope.context.callback_url)
                if input_envelope.context.callback_url
                else None
            ),
            run_id=input_envelope.run_id,
            status=result.status,
            result_uri=output_uri,
            callback_id=input_envelope.context.callback_id,
            skip_callback=input_envelope.context.skip_callback,
        )
        logger.info("Schematron validation complete (status=%s)", result.status.value)
        return 0

    except Exception as exc:
        logger.exception("Schematron validation failed with unexpected error")
        report_fatal(
            exc,
            context={
                "run_id": getattr(locals().get("input_envelope", None), "run_id", None),
                "validator": ValidatorType.SCHEMATRON,
            },
        )
        try:
            if "input_envelope" in locals():
                finished_at = datetime.now(UTC)
                failure_envelope = SchematronOutputEnvelope(
                    run_id=input_envelope.run_id,
                    validator=input_envelope.validator,
                    status=ValidationStatus.FAILED_RUNTIME,
                    timing={"started_at": started_at, "finished_at": finished_at},
                    messages=[
                        ValidationMessage(
                            severity=Severity.ERROR,
                            text=(
                                "Schematron validator failed. "
                                "Please retry or contact support."
                            ),
                        ),
                    ],
                    outputs=None,
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
        except Exception:
            logger.exception("Failed to send Schematron failure callback")
        return 1


if __name__ == "__main__":
    sys.exit(main())
