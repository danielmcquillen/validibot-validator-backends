"""Tests for replay-safe immutable output and callback handling.

Cloud Tasks may redeliver a request after the validator produced output but
before it received a successful HTTP response.  These tests prove that exact
existing output skips domain compute and retries only the authenticated
callback, while conflicting evidence fails closed.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from validator_backends.core.replay import replay_existing_output


def _input_and_output():
    """Return matching lightweight envelope-shaped objects for replay tests."""
    validator = {"type": "FMU", "version": "1"}
    context = SimpleNamespace(
        expected_output_uri="file:///tmp/output.json",
        callback_url="https://worker.example/callback/",
        callback_id="execution-attempt-1",
        callback_nonce="secret",
        skip_callback=False,
    )
    input_envelope = SimpleNamespace(
        run_id="run-1",
        validator=validator,
        context=context,
    )
    output_envelope = SimpleNamespace(
        run_id="run-1",
        validator=validator,
        status="SUCCESS",
        step_run_id="step-1",
        execution_attempt_id="attempt-1",
    )
    return input_envelope, output_envelope


@patch("validator_backends.core.replay.post_callback")
@patch(
    "validator_backends.core.replay.output_identity_for",
    return_value={
        "step_run_id": "step-1",
        "execution_attempt_id": "attempt-1",
    },
)
@patch("validator_backends.core.replay.download_envelope")
@patch("validator_backends.core.replay.stored_object_exists", return_value=True)
def test_exact_existing_output_retries_callback_without_compute(
    _exists,
    download_envelope,
    _identity,
    post_callback,
):
    """A redelivery after output publication must converge on one domain result."""
    input_envelope, output_envelope = _input_and_output()
    download_envelope.return_value = output_envelope

    replayed = replay_existing_output(input_envelope, object)

    assert replayed is True
    post_callback.assert_called_once()
    download_envelope.assert_called_once_with(
        "file:///tmp/output.json",
        object,
        configure_refresh=False,
    )


@patch(
    "validator_backends.core.replay.output_identity_for",
    return_value={"execution_attempt_id": "different-attempt"},
)
@patch("validator_backends.core.replay.download_envelope")
@patch("validator_backends.core.replay.stored_object_exists", return_value=True)
def test_conflicting_existing_output_fails_closed(
    _exists,
    download_envelope,
    _identity,
):
    """A create-only conflict is reconciliation evidence, never overwrite permission."""
    input_envelope, output_envelope = _input_and_output()
    download_envelope.return_value = output_envelope

    with pytest.raises(ValueError, match="execution_attempt_id"):
        replay_existing_output(input_envelope, object)


@patch("validator_backends.core.replay.stored_object_exists", return_value=False)
def test_missing_output_allows_one_shot_domain_execution(_exists):
    """Only true absence permits the child entrypoint to begin domain compute."""
    input_envelope, _output_envelope = _input_and_output()

    assert replay_existing_output(input_envelope, object) is False
