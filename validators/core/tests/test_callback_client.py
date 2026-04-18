"""Tests for callback client retry behaviour and callback_id handling.

These tests focus on the HTTP transport layer — retry-on-transient,
idempotency key propagation, and the skip paths. Authentication is
stubbed out with a trivial :class:`_StubAuth` backend so the tests
don't depend on google-auth or the real metadata server. Dedicated
coverage for the auth backends lives in
``test_callback_auth.py``.
"""

from __future__ import annotations

import httpx

from validators.core import callback_client
from validators.core.callback_auth import CallbackAuth
from validibot_shared.validations.envelopes import ValidationStatus


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
        max_attempts=3,
        retry_delay_seconds=0,
        auth=stub,
    )

    # One header-build per HTTP attempt, not one per call.
    assert len(stub.calls) == 3


def test_post_callback_includes_callback_id_in_payload(monkeypatch):
    """post_callback should include callback_id in the POST payload.

    Django's ValidationCallbackService uses callback_id as the
    idempotency key to detect duplicate Cloud Tasks deliveries. Losing
    it in transit means duplicate work and duplicate billed usage.
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

    callback_id = "test-idempotency-key-12345"
    callback_client.post_callback(
        callback_url="http://example.com/callback",
        run_id="run-123",
        status=ValidationStatus.SUCCESS,
        result_uri="gs://bucket/run/output.json",
        callback_id=callback_id,
        auth=_StubAuth(),
    )

    assert captured_payload["callback_id"] == callback_id
    assert captured_payload["run_id"] == "run-123"
    assert captured_payload["status"] == "success"
    assert captured_payload["result_uri"] == "gs://bucket/run/output.json"


def test_post_callback_without_callback_id(monkeypatch):
    """post_callback should work without callback_id.

    Older validator envelopes may not carry an idempotency key.
    Dropping the request would regress compatibility; sending null
    tells Django to fall back to best-effort de-duplication.
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
        run_id="run-456",
        status=ValidationStatus.FAILED_VALIDATION,
        result_uri="gs://bucket/run/output.json",
        # callback_id not provided
        auth=_StubAuth(),
    )

    assert captured_payload["callback_id"] is None
    assert captured_payload["run_id"] == "run-456"
    assert captured_payload["status"] == "failed_validation"


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
