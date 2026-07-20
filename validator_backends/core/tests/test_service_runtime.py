"""Tests for the reusable Cloud Run Service HTTP/child-process boundary.

These tests prove the security properties that container reuse would otherwise
weaken: immutable revision matching, strict request schemas, child-only bearer
credentials, a fresh scratch root per delivery, hard deadlines, and no secret
retention in the long-lived parent environment.
"""

from __future__ import annotations

import io
import json
import signal
import threading
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from validator_backends.core import storage_client
from validator_backends.core.gcs_capability import CAPABILITY_TOKEN_ENV
from validator_backends.core.service_contract import ServiceExecutionRequest
from validator_backends.core.service_runtime import (
    BACKEND_IMAGE_DIGEST_ENV,
    MAX_REQUEST_BYTES,
    ServiceRequestError,
    ServiceRequestExpired,
    ValidatorServiceHandler,
    _validated_child_timeout,
    execute_service_request,
)


ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
DEPLOYMENT_ID = "22222222-2222-4222-8222-222222222222"
SERVICE_NAME = "validibot-energyplus"
SERVICE_REVISION = "validibot-energyplus-00001-abc"
DIGEST = "sha256:" + "a" * 64
TOKEN = "transient-bearer-value"
DOMAIN_TIMEOUT_SECONDS = 300
EXPECTED_CHILD_TIMEOUT_SECONDS = 300
TIMED_OUT_EXIT_CODE = 124


def _payload(*, token: str = TOKEN) -> dict:
    """Return a valid request whose deadline comfortably fits the child."""
    return {
        "schema_version": 1,
        "attempt_id": ATTEMPT_ID,
        "deployment_id": DEPLOYMENT_ID,
        "deployment_revision": SERVICE_REVISION,
        "provider_resource_name": (
            f"projects/validibot-prod/locations/australia-southeast1/services/{SERVICE_NAME}"
        ),
        "provider_task_name": (
            "projects/validibot-prod/locations/australia-southeast1/queues/"
            f"validator/tasks/{ATTEMPT_ID}"
        ),
        "service_name": SERVICE_NAME,
        "service_revision": SERVICE_REVISION,
        "backend_image_digest": DIGEST,
        "input_uri": f"gs://bucket/runs/{ATTEMPT_ID}/input.json",
        "timeout_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "domain_timeout_seconds": DOMAIN_TIMEOUT_SECONDS,
        "gcs_capability": {
            "access_token": token,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "allowed_prefix": f"gs://bucket/runs/{ATTEMPT_ID}/",
            "project_id": "validibot-prod",
            "refresh_url": "https://worker.example/capabilities/refresh/",
        },
    }


def _install_runtime_identity(monkeypatch) -> None:
    """Make the test process represent the pinned Cloud Run revision."""
    monkeypatch.setenv("K_SERVICE", SERVICE_NAME)
    monkeypatch.setenv("K_REVISION", SERVICE_REVISION)
    monkeypatch.setenv(BACKEND_IMAGE_DIGEST_ENV, DIGEST)


def _http_response(*, body: bytes, content_type: str = "application/json"):
    """Deliver one real loopback HTTP request through the shared handler."""
    ValidatorServiceHandler.backend_module = "example.backend"
    server = HTTPServer(("127.0.0.1", 0), ValidatorServiceHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    connection = HTTPConnection(*server.server_address)
    try:
        connection.request(
            "POST",
            "/v1/execute",
            body=body,
            headers={"Content-Type": content_type},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()
        thread.join(timeout=2)
        server.server_close()


def test_request_schema_rejects_unknown_authority_fields_and_redacts_token():
    """The transport contract must reject expansion and never reveal bearer data."""
    request = ServiceExecutionRequest.model_validate(_payload())
    invalid = _payload()
    invalid["service_account_key"] = "forbidden"

    with pytest.raises(ValidationError):
        ServiceExecutionRequest.model_validate(invalid)

    assert TOKEN not in repr(request)
    assert TOKEN not in str(request)


def test_runtime_rejects_revision_drift_before_starting_a_child(monkeypatch):
    """A request for another revision cannot masquerade as this deployment."""
    _install_runtime_identity(monkeypatch)
    monkeypatch.setenv("K_REVISION", "drifted-revision")
    request = ServiceExecutionRequest.model_validate(_payload())

    with pytest.raises(ServiceRequestError, match="service revision"):
        _validated_child_timeout(request)


def test_runtime_classifies_late_delivery_as_acknowledge_without_compute(monkeypatch):
    """An expired durable attempt must not trigger retries or a child process."""
    _install_runtime_identity(monkeypatch)
    payload = _payload()
    payload["timeout_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    request = ServiceExecutionRequest.model_validate(payload)

    with pytest.raises(ServiceRequestExpired, match="deadline"):
        _validated_child_timeout(request)


def test_http_contract_enforces_media_body_and_expired_delivery(monkeypatch):
    """All images share bounded transport semantics before domain code can run."""
    _install_runtime_identity(monkeypatch)

    def _validate_without_child(request, **kwargs):
        """Retain parent validation while replacing only child execution."""
        _validated_child_timeout(
            request,
            cloud_task_name=kwargs.get("cloud_task_name", ""),
        )
        return 0

    monkeypatch.setattr(
        "validator_backends.core.service_runtime.execute_service_request",
        _validate_without_child,
    )
    valid_body = json.dumps(_payload()).encode()

    assert _http_response(body=valid_body)[0] == 200
    assert _http_response(body=valid_body, content_type="text/plain")[0] == 415
    assert _http_response(body=b"x" * (MAX_REQUEST_BYTES + 1))[0] == 413

    expired_payload = _payload()
    expired_payload["timeout_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert _http_response(body=json.dumps(expired_payload).encode())[0] == 204


def test_each_delivery_gets_fresh_child_environment_and_removed_scratch(
    monkeypatch,
):
    """A reused HTTP parent cannot expose one request's token or files to the next."""
    _install_runtime_identity(monkeypatch)
    monkeypatch.delenv(CAPABILITY_TOKEN_ENV, raising=False)
    observed_environments: list[dict[str, str]] = []
    observed_scratch_roots: list[Path] = []

    class _Process:
        stdout = io.BytesIO()

        def wait(self, timeout=None):
            assert timeout == EXPECTED_CHILD_TIMEOUT_SECONDS
            return 0

    def _popen(_command, *, env, stdout, stderr, start_new_session):
        assert stdout == __import__("subprocess").PIPE
        assert stderr == __import__("subprocess").STDOUT
        assert start_new_session is True
        observed_environments.append(env)
        observed_scratch_roots.append(Path(env["VALIDIBOT_ATTEMPT_SCRATCH_ROOT"]))
        return _Process()

    monkeypatch.setattr("subprocess.Popen", _popen)

    first = ServiceExecutionRequest.model_validate(_payload(token="first-token"))
    second = ServiceExecutionRequest.model_validate(_payload(token="second-token"))
    assert execute_service_request(first, backend_module="example.first") == 0
    assert execute_service_request(second, backend_module="example.second") == 0

    assert observed_environments[0][CAPABILITY_TOKEN_ENV] == "first-token"
    assert observed_environments[1][CAPABILITY_TOKEN_ENV] == "second-token"
    assert observed_scratch_roots[0] != observed_scratch_roots[1]
    assert all(not path.exists() for path in observed_scratch_roots)
    assert CAPABILITY_TOKEN_ENV not in __import__("os").environ
    assert all(
        environment["VALIDIBOT_VERIFY_ATTEMPT_ACTIVE"] == "true"
        for environment in observed_environments
    )


def test_child_timeout_terminates_then_returns_retryable_failure(monkeypatch):
    """A deadline must signal the child's entire isolated process group."""
    _install_runtime_identity(monkeypatch)
    received_signals = []

    class _Process:
        pid = 4242
        stdout = io.BytesIO()

        def wait(self, timeout=None):
            if not received_signals:
                raise __import__("subprocess").TimeoutExpired("child", timeout)
            return 0

    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: _Process())
    monkeypatch.setattr(
        "os.killpg",
        lambda process_group, sent_signal: received_signals.append(
            (process_group, sent_signal)
        ),
    )
    request = ServiceExecutionRequest.model_validate(_payload())

    assert (
        execute_service_request(request, backend_module="example.backend") == TIMED_OUT_EXIT_CODE
    )
    assert received_signals == [(4242, signal.SIGTERM)]


def test_child_timeout_escalates_the_process_group_to_sigkill(monkeypatch):
    """Native grandchildren that ignore SIGTERM must not survive the request."""
    _install_runtime_identity(monkeypatch)
    received_signals = []

    class _Process:
        pid = 4343
        stdout = io.BytesIO()
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits < 3:
                raise __import__("subprocess").TimeoutExpired("child", timeout)
            return 0

    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: _Process())
    monkeypatch.setattr(
        "os.killpg",
        lambda process_group, sent_signal: received_signals.append(
            (process_group, sent_signal)
        ),
    )
    request = ServiceExecutionRequest.model_validate(_payload())

    assert (
        execute_service_request(request, backend_module="example.backend") == TIMED_OUT_EXIT_CODE
    )
    assert received_signals == [
        (4343, signal.SIGTERM),
        (4343, signal.SIGKILL),
    ]


def test_child_output_is_bounded_and_bearer_value_is_redacted(monkeypatch, caplog):
    """A compromised child cannot exfiltrate its token through retained logs."""
    _install_runtime_identity(monkeypatch)
    caplog.set_level("INFO")

    class _Process:
        stdout = io.BytesIO(f"before {TOKEN} after".encode())

        def wait(self, timeout=None):
            """Finish successfully after the parent starts draining output."""
            return 0

    monkeypatch.setattr("subprocess.Popen", lambda *_args, **_kwargs: _Process())
    request = ServiceExecutionRequest.model_validate(_payload())

    assert execute_service_request(request, backend_module="example.backend") == 0

    assert TOKEN not in caplog.text
    assert "<redacted>" in caplog.text


def test_service_child_reauthorizes_attempt_after_loading_input(monkeypatch, tmp_path):
    """A terminal attempt must be rejected before its domain runner starts."""

    class _Envelope(BaseModel):
        """Minimal input shape needed to exercise the shared loader hook."""

        run_id: str

    input_path = tmp_path / "input.json"
    input_path.write_text('{"run_id":"run-1"}', encoding="utf-8")
    configure = MagicMock()
    refresh = MagicMock()
    monkeypatch.setenv("VALIDIBOT_VERIFY_ATTEMPT_ACTIVE", "true")
    monkeypatch.setattr(storage_client, "configure_capability_refresh", configure)
    monkeypatch.setattr(storage_client, "refresh_attempt_capability", refresh)

    envelope = storage_client.download_envelope(
        f"file://{input_path}",
        _Envelope,
    )

    assert envelope.run_id == "run-1"
    configure.assert_called_once_with(envelope)
    refresh.assert_called_once_with()
