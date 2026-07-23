"""Tests for the Portfolio Manager backend entrypoint artifact contract.

Runner tests cover carrier parsing and collection policy. This suite protects
the container boundary: the carrier-neutral property report must be uploaded
and declared in the output envelope so Django can index its artifact port.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from validator_backends.portfolio_manager import main as portfolio_manager_main
from validibot_shared.portfolio_manager import (
    PortfolioManagerInputs,
    PortfolioManagerOutputs,
    PortfolioManagerPropertyResult,
    build_portfolio_manager_input_envelope,
)
from validibot_shared.validations.envelopes import (
    ATTEMPT_CONTRACT_VERSION,
    ExecutionContext,
    ValidationStatus,
    ValidatorType,
)


class _Validator:
    """Minimal validator identity accepted by the shared envelope builder."""

    id = "validator-1"
    validation_type = ValidatorType.PORTFOLIO_MANAGER
    version = "1"


def _input_envelope(tmp_path):
    """Build a minimal local-file envelope with an artifact output location."""
    submission = tmp_path / "property.xml"
    submission_payload = b"<property><propertyId>1</propertyId></property>"
    submission.write_bytes(submission_payload)
    submission_sha256 = hashlib.sha256(submission_payload).hexdigest()
    return build_portfolio_manager_input_envelope(
        run_id="run-1",
        validator=_Validator(),
        org_id="org-1",
        org_name="Org",
        workflow_id="workflow-1",
        step_id="step-1",
        step_name="Portfolio Manager",
        submission_name="property.xml",
        submission_uri=submission.as_uri(),
        submission_size_bytes=len(submission_payload),
        submission_sha256=submission_sha256,
        submission_storage_version=f"sha256:{submission_sha256}",
        inputs=PortfolioManagerInputs(),
        context=ExecutionContext(
            execution_bundle_uri=(tmp_path / "bundle").as_uri(),
            execution_attempt_id="attempt-1",
            step_run_id="step-run-1",
            attempt_contract_version=ATTEMPT_CONTRACT_VERSION,
            expected_output_uri=(tmp_path / "output.json").as_uri(),
            skip_callback=True,
        ),
    )


def test_main_uploads_property_results_as_declared_output_artifact(
    monkeypatch,
    tmp_path,
) -> None:
    """Successful execution must expose the JSON result through its artifact port."""
    captured = {}
    outputs = PortfolioManagerOutputs(
        submission_structure="single_report",
        profile="generic",
        file_count=1,
        valid_file_count=1,
        invalid_file_count=0,
        property_count=1,
        reporting_cycle_count=1,
        reporting_cycles_match=True,
        property_results=[
            PortfolioManagerPropertyResult(
                member_name="property.xml",
                carrier="xml",
                property_id="1",
            )
        ],
    )

    monkeypatch.setattr(
        portfolio_manager_main,
        "load_input_envelope",
        lambda _model: _input_envelope(tmp_path),
    )
    monkeypatch.setattr(
        portfolio_manager_main,
        "replay_existing_output",
        lambda _input, _output_type: False,
    )
    monkeypatch.setattr(
        portfolio_manager_main,
        "run_portfolio_manager_validation",
        lambda _envelope: SimpleNamespace(
            status=ValidationStatus.SUCCESS,
            messages=[],
            outputs=outputs,
        ),
    )
    monkeypatch.setattr(
        portfolio_manager_main,
        "get_output_uri",
        lambda _envelope: (tmp_path / "output.json").as_uri(),
    )
    monkeypatch.setattr(
        portfolio_manager_main,
        "upload_envelope",
        lambda envelope, uri: captured.update({"envelope": envelope, "uri": uri}),
    )
    monkeypatch.setattr(
        portfolio_manager_main,
        "post_callback",
        lambda **_kwargs: None,
    )

    exit_code = portfolio_manager_main.main()

    assert exit_code == 0
    output = captured["envelope"]
    assert output.execution_attempt_id == "attempt-1"
    assert len(output.input_envelope_sha256) == 64
    assert len(output.artifacts) == 1
    artifact = output.artifacts[0]
    assert artifact.name == "portfolio-manager-property-results.json"
    assert artifact.type == "portfolio-manager-property-results"
    assert artifact.mime_type == "application/json"
    artifact_path = tmp_path / "bundle" / "outputs" / artifact.name
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["summary"]["property_count"] == 1
    assert payload["properties"][0]["property_id"] == "1"
