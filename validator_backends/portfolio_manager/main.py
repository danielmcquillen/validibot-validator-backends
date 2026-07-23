"""Cloud Run Job/Service entrypoint for Portfolio Manager report validation."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from validator_backends.core.callback_client import post_callback
from validator_backends.core.envelope_loader import get_output_uri, load_input_envelope
from validator_backends.core.error_reporting import report_fatal
from validator_backends.core.gcs_client import upload_envelope
from validator_backends.core.output_identity import output_identity_for
from validator_backends.core.replay import replay_existing_output
from validator_backends.core.report_artifacts import upload_text_report_artifact
from validator_backends.portfolio_manager.runner import (
    property_results_artifact_json,
    run_portfolio_manager_validation,
)
from validibot_shared.portfolio_manager import (
    PortfolioManagerInputEnvelope,
    PortfolioManagerOutputEnvelope,
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
    """Execute one immutable Portfolio Manager attempt."""
    started_at = datetime.now(UTC)
    try:
        input_envelope = load_input_envelope(PortfolioManagerInputEnvelope)
        if replay_existing_output(input_envelope, PortfolioManagerOutputEnvelope):
            logger.info("Replayed existing Portfolio Manager output without recompute")
            return 0
        result = run_portfolio_manager_validation(input_envelope)
        finished_at = datetime.now(UTC)
        artifact = upload_text_report_artifact(
            content=property_results_artifact_json(result.outputs),
            execution_bundle_uri=str(input_envelope.context.execution_bundle_uri),
            filename="portfolio-manager-property-results.json",
            artifact_type="portfolio-manager-property-results",
            mime_type="application/json",
        )
        output_uri = get_output_uri(input_envelope)
        output_envelope = PortfolioManagerOutputEnvelope(
            run_id=input_envelope.run_id,
            **output_identity_for(input_envelope, output_uri),
            validator=input_envelope.validator,
            status=result.status,
            timing={"started_at": started_at, "finished_at": finished_at},
            messages=result.messages,
            metrics=[],
            artifacts=[artifact] if artifact else [],
            outputs=result.outputs,
        )
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
            callback_nonce=input_envelope.context.callback_nonce,
            skip_callback=input_envelope.context.skip_callback,
        )
        return 0
    except Exception as exc:
        logger.exception("Portfolio Manager validation failed with unexpected error")
        report_fatal(
            exc,
            context={
                "run_id": getattr(locals().get("input_envelope", None), "run_id", None),
                "validator": ValidatorType.PORTFOLIO_MANAGER,
            },
        )
        try:
            if "input_envelope" in locals():
                output_uri = get_output_uri(input_envelope)
                failure = PortfolioManagerOutputEnvelope(
                    run_id=input_envelope.run_id,
                    **output_identity_for(input_envelope, output_uri),
                    validator=input_envelope.validator,
                    status=ValidationStatus.FAILED_RUNTIME,
                    timing={
                        "started_at": started_at,
                        "finished_at": datetime.now(UTC),
                    },
                    messages=[
                        ValidationMessage(
                            severity=Severity.ERROR,
                            code="portfolio_manager.runtime.failed",
                            text=(
                                "Portfolio Manager validator failed. "
                                "Please retry or contact support."
                            ),
                        )
                    ],
                    outputs=None,
                )
                upload_envelope(failure, output_uri)
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
                    callback_nonce=input_envelope.context.callback_nonce,
                    skip_callback=input_envelope.context.skip_callback,
                )
        except Exception:
            logger.exception("Failed to publish Portfolio Manager runtime failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
