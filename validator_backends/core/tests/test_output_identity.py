"""Tests for binding validator outputs to an immutable execution attempt.

These checks cover the trust-boundary metadata shared by every backend. A
container output is acceptable only when it echoes the exact attempt identity,
canonical input digest, and pre-authorized output URI from its input envelope.
"""

from __future__ import annotations

import pytest

from validator_backends.core.envelope_loader import get_output_uri
from validator_backends.core.output_identity import output_identity_for
from validibot_shared.canonicalization import sha256_hex_for_model
from validibot_shared.validations.envelopes import (
    ATTEMPT_CONTRACT_VERSION,
    ValidationInputEnvelope,
    ValidatorType,
)


def _input_envelope() -> ValidationInputEnvelope:
    """Build a deterministic input envelope for attempt-identity checks."""
    return ValidationInputEnvelope(
        run_id="run-1",
        validator={"id": "validator-1", "type": ValidatorType.FMU, "version": "1"},
        org={"id": "org-1", "name": "Org"},
        workflow={"id": "workflow-1", "step_id": "step-1", "step_name": "FMU"},
        context={
            "execution_bundle_uri": "gs://bucket/runs/run-1/",
            "execution_attempt_id": "attempt-1",
            "step_run_id": "step-run-1",
            "attempt_contract_version": ATTEMPT_CONTRACT_VERSION,
            "expected_output_uri": "gs://bucket/runs/run-1/output.json",
        },
    )


def test_output_identity_echoes_attempt_and_canonical_input_digest():
    """Every output must prove which exact input and attempt produced it."""
    envelope = _input_envelope()
    output_uri = get_output_uri(envelope)

    identity = output_identity_for(envelope, output_uri)

    assert identity == {
        "step_run_id": "step-run-1",
        "execution_attempt_id": "attempt-1",
        "attempt_contract_version": ATTEMPT_CONTRACT_VERSION,
        "input_envelope_sha256": sha256_hex_for_model(envelope),
        "output_uri": "gs://bucket/runs/run-1/output.json",
    }


def test_get_output_uri_rejects_conflicting_environment_override(monkeypatch):
    """A container environment cannot redirect output outside the attempt URI."""
    monkeypatch.setenv("VALIDIBOT_OUTPUT_URI", "gs://attacker/output.json")

    with pytest.raises(ValueError, match="conflicts with the input envelope"):
        get_output_uri(_input_envelope())


def test_output_identity_rejects_a_different_output_uri():
    """Identity assembly fails closed if a caller bypasses URI resolution."""
    with pytest.raises(ValueError, match="does not match"):
        output_identity_for(_input_envelope(), "gs://attacker/output.json")


def test_shared_attempt_fixture_digest_matches_the_django_contract():
    """Backend canonicalization must match the literal pinned in every repo."""
    envelope = ValidationInputEnvelope(
        run_id="run-fixture",
        validator={"id": "validator-fixture", "type": ValidatorType.FMU, "version": "1"},
        org={"id": "org-fixture", "name": "Fixture Org"},
        workflow={
            "id": "workflow-fixture",
            "step_id": "step-fixture",
            "step_name": "Fixture Step",
        },
        inputs={"alpha": 1},
        context={
            "execution_attempt_id": "attempt-fixture",
            "step_run_id": "step-run-fixture",
            "attempt_contract_version": ATTEMPT_CONTRACT_VERSION,
            "expected_output_uri": "gs://fixture/runs/run-fixture/output.json",
            "execution_bundle_uri": "gs://fixture/runs/run-fixture/",
            "skip_callback": True,
        },
    )

    assert sha256_hex_for_model(envelope) == (
        "0f4f7cd8b38a79dbc2c4ac66c1ed602cb4db59665d52b6df73cd409bdaf765c7"
    )
