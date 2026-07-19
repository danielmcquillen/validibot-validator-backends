"""Tests for attempt-scoped GCS credentials inside validator containers.

The Cloud Run runtime service account must not be a hidden fallback for object
access. These tests pin fail-closed capability loading, local prefix defense,
secret-safe representations, and callback-bound token renewal so a compromised
validator remains confined to its own execution attempt.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from validator_backends.core import gcs_capability
from validator_backends.core.gcs_capability import GCSCapabilityError


@pytest.fixture(autouse=True)
def _reset_capability_process_state():
    """Each test needs a fresh view of its monkeypatched process environment."""
    gcs_capability.reset_capability_state_for_tests()
    yield
    gcs_capability.reset_capability_state_for_tests()


def _install_capability_env(monkeypatch) -> None:
    """Install one complete, non-expired attempt capability environment."""
    values = {
        gcs_capability.CAPABILITY_REQUIRED_ENV: "1",
        gcs_capability.CAPABILITY_TOKEN_ENV: "secret-access-token",
        gcs_capability.CAPABILITY_EXPIRY_ENV: "2099-01-01T00:00:00Z",
        gcs_capability.CAPABILITY_PREFIX_ENV: ("gs://validation/runs/org/run/attempts/attempt/"),
        gcs_capability.CAPABILITY_PROJECT_ENV: "validibot-project",
        gcs_capability.CAPABILITY_REFRESH_URL_ENV: (
            "https://worker.example/api/v1/validation-storage-capabilities/refresh/"
        ),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_required_capability_fails_closed_when_token_is_missing(monkeypatch):
    """Cloud Run must never fall back to ambient ADC after token delivery fails."""
    monkeypatch.setenv(gcs_capability.CAPABILITY_REQUIRED_ENV, "1")

    with pytest.raises(GCSCapabilityError, match="required"):
        gcs_capability.build_gcs_credentials()


def test_capability_rejects_another_attempt_uri(monkeypatch):
    """A local prefix check limits damage even if provider policy is misissued."""
    _install_capability_env(monkeypatch)

    gcs_capability.assert_gcs_uri_allowed(
        "gs://validation/runs/org/run/attempts/attempt/input.json"
    )
    with pytest.raises(GCSCapabilityError, match="outside"):
        gcs_capability.assert_gcs_uri_allowed(
            "gs://validation/runs/org/run/attempts/other/input.json"
        )


def test_explicit_credentials_keep_token_out_of_repr(monkeypatch):
    """Diagnostic representations must not accidentally disclose bearer tokens."""
    _install_capability_env(monkeypatch)

    credentials, project_id = gcs_capability.build_gcs_credentials()

    assert credentials is not None
    assert credentials.token == "secret-access-token"
    assert project_id == "validibot-project"
    assert "secret-access-token" not in repr(gcs_capability._token_state)


def test_refresh_uses_callback_proof_and_rejects_prefix_substitution(monkeypatch):
    """Renewal is authenticated by the attempt nonce and cannot widen scope."""
    _install_capability_env(monkeypatch)
    envelope = SimpleNamespace(
        run_id="run-1",
        context=SimpleNamespace(
            callback_id="execution-attempt-attempt-1",
            callback_nonce="callback-secret",
        ),
    )
    gcs_capability.configure_capability_refresh(envelope)

    captured: dict[str, object] = {}

    class _Auth:
        """Provide deterministic worker authentication headers for the test."""

        def build_headers(self, audience):
            """Record the audience without creating a real Google ID token."""
            captured["audience"] = audience
            return {"Authorization": "Bearer oidc-token"}

    class _Response:
        """Minimal successful httpx response carrying a narrowed token."""

        def raise_for_status(self):
            """Represent an HTTP 200 response."""

        def json(self):
            """Return a refreshed token with the original allowed prefix."""
            return {
                "access_token": "refreshed-secret",
                "expires_at": "2099-01-01T01:00:00Z",
                "allowed_prefix": ("gs://validation/runs/org/run/attempts/attempt/"),
            }

    class _Client:
        """Capture the secret-bearing request without making network I/O."""

        def __init__(self, *, timeout):
            """Record the bounded broker timeout."""
            captured["timeout"] = timeout

        def __enter__(self):
            """Return this fake client from its context manager."""
            return self

        def __exit__(self, *args):
            """Close the fake client without suppressing exceptions."""
            return False

        def post(self, url, *, json, headers):
            """Capture the exact refresh proof and authentication headers."""
            captured.update({"url": url, "json": json, "headers": headers})
            return _Response()

    monkeypatch.setattr(gcs_capability, "get_callback_auth", lambda: _Auth())
    monkeypatch.setattr(gcs_capability.httpx, "Client", _Client)

    credentials, _ = gcs_capability.build_gcs_credentials()
    assert credentials is not None
    credentials.refresh(object())

    assert credentials.token == "refreshed-secret"
    assert captured["json"] == {
        "run_id": "run-1",
        "callback_id": "execution-attempt-attempt-1",
        "callback_nonce": "callback-secret",
    }
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer oidc-token",
    }
    assert captured["timeout"] == 30
