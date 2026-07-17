"""Tests for callback transport, retries, and attempt authentication.

These tests focus on the HTTP transport layer — retry-on-transient,
callback credential propagation, and the skip paths. The attempt nonce proves
that the sender received the exact input envelope; the callback ID remains the
idempotency key. Transport authentication is stubbed out with a trivial
:class:`_StubAuth` backend so the tests don't depend on google-auth or the real
metadata server. Dedicated coverage for the auth backends lives in
``test_callback_auth.py``.
"""

from __future__ import annotations

import httpx
import pytest

from validator_backends.core import callback_client
from validator_backends.core.callback_auth import CallbackAuth
from validibot_shared.validations.envelopes import ValidationStatus


CALLBACK_ID = "execution-attempt-test"
CALLBACK_NONCE = "A" * 43


class _StubAuth(CallbackAuth):
    """Deterministic auth backend for transport-layer tests.

    Returning a fixed bearer token keeps the ``Authorization`` header
    present so we can assert on it without going anywhere near
    google-auth.
    """

    def __init__(self, token: str = "fake-id-token") -> None:
        self._token = token
        self.calls: list[str] = []

    def build_headers(self, callback_url: str) -> dict[str, str]:
        self.calls.append(callback_url)
        return {"Authorization": f"Bearer {self._token}"}


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, raise_for_status=None):
        self.status_code = status_code
        self._json_body = json_body or {}
        self._raise_for_status = raise_for_status

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self._raise_for_status:
            raise self._raise_for_status


def test_post_callback_retries_and_succeeds(monkeypatch):
    """post_callback should retry on transient HTTP errors.

    Matters because Cloud Run cold-starts and transient network
    hiccups are expected failure modes — validator runs are
    long-running and expensive, so losing one to a single 500 would
    be costly.
    """
    calls = {"count": 0}

    class _Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def post(self, *args, **kwargs):
            calls["count"] += 1
            # First attempt fails, second succeeds
            if calls["count"] == 1:
                exc = httpx.HTTPStatusError(
                    "fail",
                    request=httpx.Request("POST", "http://x"),
                    response=httpx.Response(500),
                )
                raise exc
            return _FakeResponse(200, json_body={"ok": True})

    monkeypatch.setattr(callback_client.httpx, "Client", _Client)

    resp = callback_client.post_callback(
        callback_url="http://example.com",
        run_id="1",
        status=ValidationStatus.SUCCESS,
        result_uri="gs://bucket/run/output.json",
        callback_id=CALLBACK_ID,
        callback_nonce=CALLBACK_NONCE,
        max_attempts=2,
        retry_delay_seconds=0,
        auth=_StubAuth(),
    )

    assert resp == {"ok": True}
    assert calls["count"] == 2


def test_post_callback_rebuilds_auth_headers_per_attempt(monkeypatch):
    """Auth headers should be rebuilt on every retry.

    OIDC tokens are short-lived (≈1 hour) but retry_delay_seconds is
    configurable. Rebuilding the header per attempt lets the backend
    refresh an expired token without special-casing at this layer.
    """
    attempt_count = {"n": 0}

    class _Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def post(self, *args, **kwargs):
            attempt_count["n"] += 1
            if attempt_count["n"] < 3:
                raise httpx.HTTPStatusError(
                    "fail",
                    request=httpx.Request("POST", "http://x"),
                    response=httpx.Response(503),
                )
            return _FakeResponse(200, json_body={"ok": True})

    monkeypatch.setattr(callback_client.httpx, "Client", _Client)

    stub = _StubAuth()
    callback_client.post_callback(
        callback_url="http://example.com",
        run_id="r",
        status=ValidationStatus.SUCCESS,
        result_uri="gs://x/y",
        callback_id=CALLBACK_ID,
        callback_nonce=CALLBACK_NONCE,
        max_attempts=3,
        retry_delay_seconds=0,
        auth=stub,
    )

    # One header-build per HTTP attempt, not one per call.
    assert len(stub.calls) == 3


def test_post_callback_includes_attempt_credentials_in_payload(monkeypatch):
    """The POST payload must echo both attempt callback credentials.

    Django's ValidationCallbackService uses callback_id as the
    idempotency key to detect duplicate Cloud Tasks deliveries. Losing
    it in transit means duplicate work and duplicate billed usage. The raw
    nonce separately proves the sender received this exact attempt envelope.
    """
    captured_payload = {}

    class _Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def post(self, url, json=None, **kwargs):
            captured_payload.update(json or {})
            return _FakeResponse(200, json_body={"ok": True})

    monkeypatch.setattr(callback_client.httpx, "Client", _Client)

    callback_client.post_callback(
        callback_url="http://example.com/callback",
        run_id="run-123",
        status=ValidationStatus.SUCCESS,
        result_uri="gs://bucket/run/output.json",
        callback_id=CALLBACK_ID,
        callback_nonce=CALLBACK_NONCE,
        auth=_StubAuth(),
    )

    assert captured_payload["callback_id"] == CALLBACK_ID
    assert captured_payload["callback_nonce"] == CALLBACK_NONCE
    assert captured_payload["run_id"] == "run-123"
    assert captured_payload["status"] == "success"
    assert captured_payload["result_uri"] == "gs://bucket/run/output.json"


@pytest.mark.parametrize(
    ("callback_id", "callback_nonce", "expected_error"),
    [
        (None, CALLBACK_NONCE, "callback_id is required"),
        (CALLBACK_ID, None, "callback_nonce is required"),
    ],
)
def test_post_callback_rejects_missing_attempt_credentials(
    callback_id,
    callback_nonce,
    expected_error,
):
    """An active callback must never fall back to unauthenticated attempt data.

    Transport credentials identify the runtime, but they do not bind the
    notification to one dispatched attempt. Both envelope-derived fields are
    therefore mandatory before any HTTP request is created.
    """
    with pytest.raises(ValueError, match=expected_error):
        callback_client.post_callback(
            callback_url="http://example.com/callback",
            run_id="run-456",
            status=ValidationStatus.FAILED_VALIDATION,
            result_uri="gs://bucket/run/output.json",
            callback_id=callback_id,
            callback_nonce=callback_nonce,
            auth=_StubAuth(),
        )


def test_post_callback_sends_auth_header_from_backend(monkeypatch):
    """The Authorization header from the backend must reach the HTTP request.

    Regression guard: if wiring between callback_client and the auth
    backend ever gets dropped, Django will reject every callback in
    production with no obvious failure signal here.
    """
    captured_headers = {}

    class _Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def post(self, url, json=None, headers=None, **kwargs):
            captured_headers.update(headers or {})
            return _FakeResponse(200, json_body={"ok": True})

    monkeypatch.setattr(callback_client.httpx, "Client", _Client)

    callback_client.post_callback(
        callback_url="http://example.com/callback",
        run_id="run-xyz",
        status=ValidationStatus.SUCCESS,
        result_uri="gs://bucket/run/output.json",
        callback_id=CALLBACK_ID,
        callback_nonce=CALLBACK_NONCE,
        auth=_StubAuth(token="deadbeef"),
    )

    assert captured_headers["Authorization"] == "Bearer deadbeef"
    assert captured_headers["Content-Type"] == "application/json"


def test_post_callback_skip_callback_returns_none():
    """post_callback should return None when skip_callback=True.

    Used by local dev / unit tests where we don't want to stand up a
    callback receiver.
    """
    result = callback_client.post_callback(
        callback_url="http://example.com/callback",
        run_id="run-789",
        status=ValidationStatus.SUCCESS,
        result_uri="gs://bucket/run/output.json",
        callback_id="some-id",
        skip_callback=True,
    )

    assert result is None


def test_post_callback_no_url_returns_none():
    """post_callback should return None when callback_url is None.

    Defensive: the caller (``post_callback_from_envelope`` in some
    code paths) may pass None rather than pre-filtering. We don't want
    to crash the container on a missing field.
    """
    result = callback_client.post_callback(
        callback_url=None,
        run_id="run-abc",
        status=ValidationStatus.SUCCESS,
        result_uri="gs://bucket/run/output.json",
        callback_id="some-id",
    )

    assert result is None
