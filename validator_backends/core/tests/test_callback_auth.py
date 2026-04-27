"""Tests for callback authentication backends.

Covers:

* The factory (:func:`get_callback_auth`) picks the right backend for
  every ``DEPLOYMENT_TARGET`` and the documented fallbacks.
* :class:`GCPCallbackAuth` derives an origin-only OIDC audience from
  the callback URL, matching what Cloud Tasks / Cloud Scheduler sign.
  This is the compatibility fix that unblocks validator callbacks
  once the Django-side strict verification ships.
* :class:`SharedSecretCallbackAuth` produces the same header format
  the Django ``WorkerKeyAuthentication`` consumes
  (``Authorization: Worker-Key <secret>``).
* Failure modes fail closed: missing google-auth, missing audience,
  and metadata-server errors all log + return an empty header set so
  Django responds 401 rather than silently accepting something.
"""

from __future__ import annotations

import pytest

from validator_backends.core import callback_auth
from validator_backends.core.callback_auth import (
    CallbackAuth,
    GCPCallbackAuth,
    NullCallbackAuth,
    SharedSecretCallbackAuth,
    get_callback_auth,
    reset_callback_auth_cache,
)


# ---------------------------------------------------------------------------
# Factory: DEPLOYMENT_TARGET → backend
#
# The factory is cached with ``lru_cache`` so every test that touches it has
# to clear the cache first. Environment-driven tests use monkeypatch.setenv
# so the shell environment is restored automatically.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_factory_cache():
    """Reset the backend cache around every test.

    Without this, setting DEPLOYMENT_TARGET in one test would leak
    into the next — test order would silently decide which backend
    gets exercised.
    """
    reset_callback_auth_cache()
    yield
    reset_callback_auth_cache()


def test_factory_gcp_returns_gcp_backend(monkeypatch):
    """DEPLOYMENT_TARGET=gcp selects the OIDC backend.

    This is the production path for Cloud Run Jobs; regressing it
    would immediately break validator callbacks.
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "gcp")
    monkeypatch.delenv("TASK_OIDC_AUDIENCE", raising=False)

    backend = get_callback_auth()

    assert isinstance(backend, GCPCallbackAuth)
    assert backend._audience_override is None


def test_factory_gcp_respects_audience_override(monkeypatch):
    """TASK_OIDC_AUDIENCE overrides the per-request origin derivation.

    Useful when Cloud Run is behind a load balancer and the signed
    audience differs from the direct service URL — the operator
    configures the explicit audience to match what IAM actually
    signs.
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "gcp")
    monkeypatch.setenv("TASK_OIDC_AUDIENCE", "https://worker.example.com")

    backend = get_callback_auth()

    assert isinstance(backend, GCPCallbackAuth)
    assert backend._audience_override == "https://worker.example.com"


def test_factory_docker_compose_with_api_key_returns_shared_secret(monkeypatch):
    """Docker Compose deployments use the shared secret when configured.

    Mirrors Django's WorkerKeyAuthentication selection on the same
    target — the two sides must agree on the scheme.
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "docker_compose")
    monkeypatch.setenv("WORKER_API_KEY", "super-secret")

    backend = get_callback_auth()

    assert isinstance(backend, SharedSecretCallbackAuth)


def test_factory_docker_compose_without_api_key_falls_back_to_null(monkeypatch):
    """Without WORKER_API_KEY the Docker target sends no auth header.

    Matches Django's behaviour when the same var is unset — neither
    side rejects, neither side authenticates. Safe for local dev
    clusters; the warning log points operators at the misconfig.
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "docker_compose")
    monkeypatch.delenv("WORKER_API_KEY", raising=False)

    backend = get_callback_auth()

    assert isinstance(backend, NullCallbackAuth)


def test_factory_aws_uses_shared_secret_fallback(monkeypatch):
    """AWS deployments get shared-secret auth until an AWS backend exists.

    AWS support is not implemented end-to-end yet; routing to
    SharedSecret keeps the contract usable while the signature-based
    backend is built out.
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "aws")
    monkeypatch.setenv("WORKER_API_KEY", "aws-secret")

    backend = get_callback_auth()

    assert isinstance(backend, SharedSecretCallbackAuth)


def test_factory_local_docker_compose_returns_null(monkeypatch):
    """Local dev always uses NullCallbackAuth.

    Lets `docker compose up` work without provisioning a worker API
    key. Django's matching target skips the shared-secret check in
    the same mode.
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "local_docker_compose")
    monkeypatch.delenv("WORKER_API_KEY", raising=False)

    backend = get_callback_auth()

    assert isinstance(backend, NullCallbackAuth)


def test_factory_test_target_returns_null(monkeypatch):
    """The TEST target forces NullCallbackAuth.

    So unit tests don't try to call the metadata server or require a
    pre-shared key to be set.
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "test")
    monkeypatch.setenv("WORKER_API_KEY", "ignored-in-test")

    backend = get_callback_auth()

    assert isinstance(backend, NullCallbackAuth)


def test_factory_unset_target_with_api_key_uses_shared_secret(monkeypatch):
    """Unset DEPLOYMENT_TARGET + WORKER_API_KEY set → shared secret.

    A safety net for environments that forget to set the target
    variable but do configure the key (common in older
    docker-compose.yml files).
    """
    monkeypatch.delenv("DEPLOYMENT_TARGET", raising=False)
    monkeypatch.setenv("WORKER_API_KEY", "fallback-secret")

    backend = get_callback_auth()

    assert isinstance(backend, SharedSecretCallbackAuth)


def test_factory_unset_target_without_api_key_returns_null(monkeypatch):
    """Nothing configured → NullCallbackAuth (with a loud warning).

    The warning log is the user-visible signal; the returned backend
    keeps the container from crashing so `pytest` can still run
    callback_client tests against an unauthenticated stub.
    """
    monkeypatch.delenv("DEPLOYMENT_TARGET", raising=False)
    monkeypatch.delenv("WORKER_API_KEY", raising=False)

    backend = get_callback_auth()

    assert isinstance(backend, NullCallbackAuth)


def test_factory_is_cached(monkeypatch):
    """Repeat calls must return the same instance.

    Important for GCPCallbackAuth which caches its google-auth
    transport on the instance — rebuilding it per callback would
    defeat the connection-pool optimisation.
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "local_docker_compose")

    first = get_callback_auth()
    second = get_callback_auth()

    assert first is second


def test_factory_target_is_case_insensitive(monkeypatch):
    """DEPLOYMENT_TARGET values are normalised to lowercase.

    Docs show ``DEPLOYMENT_TARGET=GCP`` but the enum value is ``gcp``;
    matching case-insensitively removes a papercut without introducing
    ambiguity (the enum values are disjoint on lowercase).
    """
    monkeypatch.setenv("DEPLOYMENT_TARGET", "GCP")

    backend = get_callback_auth()

    assert isinstance(backend, GCPCallbackAuth)


# ---------------------------------------------------------------------------
# SharedSecretCallbackAuth
# ---------------------------------------------------------------------------


def test_shared_secret_header_format():
    """Header format must match Django's WorkerKeyAuthentication.

    Django parses ``Authorization: Worker-Key <secret>`` exactly; any
    deviation (e.g. ``Bearer``, missing space) silently 401s.
    """
    backend = SharedSecretCallbackAuth("shhh")

    headers = backend.build_headers("http://example.com/callback")

    assert headers == {"Authorization": "Worker-Key shhh"}


def test_shared_secret_rejects_empty_secret():
    """An empty secret is a misconfiguration, not a valid state.

    Fail at construction time so the container crashes loudly
    rather than sending an ``Authorization: Worker-Key `` header that
    Django will reject with no useful diagnostic.
    """
    with pytest.raises(ValueError):
        SharedSecretCallbackAuth("")


# ---------------------------------------------------------------------------
# NullCallbackAuth
# ---------------------------------------------------------------------------


def test_null_auth_returns_empty_headers():
    """NullCallbackAuth is intentionally a no-op.

    Distinct from "we tried and failed": the caller picks this on
    purpose when auth isn't desired.
    """
    backend = NullCallbackAuth()

    assert backend.build_headers("http://example.com/callback") == {}


# ---------------------------------------------------------------------------
# GCPCallbackAuth — audience derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "callback_url,expected_audience",
    [
        # Happy path: origin extracted, path dropped.
        ("https://worker-abc.run.app/api/validations/callback/", "https://worker-abc.run.app"),
        # Query string is also dropped.
        ("https://worker.example.com/callback?x=1", "https://worker.example.com"),
        # Non-default port is preserved (netloc includes it).
        ("http://worker.internal:8080/cb", "http://worker.internal:8080"),
    ],
)
def test_gcp_audience_derivation(callback_url, expected_audience):
    """Audience is origin-only, matching how Cloud Tasks signs tokens.

    This is the compatibility fix. A previous implementation passed
    the full callback URL (with path); Django's strict audience check
    will reject those tokens the moment the worker-side fix ships.
    Keeping a parametric test here guards against accidental revert.
    """
    assert GCPCallbackAuth._derive_audience(callback_url) == expected_audience


def test_gcp_audience_derivation_rejects_unparseable_url():
    """Empty string → empty audience → caller fails closed.

    The factory path logs an error and returns an empty header set
    rather than calling google-auth with an obviously-wrong audience.
    """
    assert GCPCallbackAuth._derive_audience("not-a-url") == ""


def test_gcp_build_headers_uses_override_audience(monkeypatch):
    """An explicit audience override is passed straight to fetch_id_token.

    Covers the load-balancer case where the signed ``aud`` doesn't
    match the direct service origin.
    """
    captured = {}

    class _FakeIdToken:
        @staticmethod
        def fetch_id_token(_transport, audience):
            captured["audience"] = audience
            return "fake-token"

    backend = GCPCallbackAuth(audience_override="https://signed-as.example.com")
    backend._id_token_mod = _FakeIdToken
    backend._transport = object()

    headers = backend.build_headers("https://direct-worker.example.com/cb")

    assert captured["audience"] == "https://signed-as.example.com"
    assert headers == {"Authorization": "Bearer fake-token"}


def test_gcp_build_headers_derives_audience_when_no_override(monkeypatch):
    """Without an override, audience is derived per-request from the URL.

    Locks in the new behaviour — the previous bug was passing the
    whole URL here.
    """
    captured = {}

    class _FakeIdToken:
        @staticmethod
        def fetch_id_token(_transport, audience):
            captured["audience"] = audience
            return "token"

    backend = GCPCallbackAuth()
    backend._id_token_mod = _FakeIdToken
    backend._transport = object()

    backend.build_headers("https://worker.example.com/api/validations/callback/")

    assert captured["audience"] == "https://worker.example.com"


def test_gcp_build_headers_fails_closed_on_fetch_error(monkeypatch, caplog):
    """Metadata-server errors return ``{}`` and log at ERROR.

    Django will then reject the callback; that's a retry signal for
    the HTTP layer, which is vastly better than sending an
    unauthenticated request and quietly recording a "success".
    """

    class _FakeIdToken:
        @staticmethod
        def fetch_id_token(_transport, _audience):
            raise RuntimeError("metadata server unreachable")

    backend = GCPCallbackAuth()
    backend._id_token_mod = _FakeIdToken
    backend._transport = object()

    with caplog.at_level("ERROR", logger=callback_auth.__name__):
        headers = backend.build_headers("https://worker.example.com/cb")

    assert headers == {}
    assert any("failed to fetch OIDC id token" in r.message for r in caplog.records)


def test_gcp_build_headers_fails_closed_on_unparseable_url(caplog):
    """An un-parseable URL → fail closed with a clear log line.

    Covers the defence-in-depth case where the caller somehow passes
    a non-URL string; we'd rather not call google-auth with ``""``
    as the audience.
    """
    backend = GCPCallbackAuth()

    with caplog.at_level("ERROR", logger=callback_auth.__name__):
        headers = backend.build_headers("not a url at all")

    assert headers == {}
    assert any("could not derive audience" in r.message for r in caplog.records)


def test_gcp_build_headers_fails_closed_when_google_auth_missing(monkeypatch, caplog):
    """Missing google-auth → fail closed rather than 500.

    If a container image is built without the google-auth wheel, we
    still want the ``post_callback`` loop to surface an auth failure
    via Django's 401, not a bare ImportError propagating out of
    ``build_headers``.
    """
    backend = GCPCallbackAuth()

    def _raise_import(*args, **kwargs):
        raise ImportError("No module named 'google.auth'")

    monkeypatch.setattr(backend, "_load_google_auth", _raise_import)

    with caplog.at_level("ERROR", logger=callback_auth.__name__):
        headers = backend.build_headers("https://worker.example.com/cb")

    assert headers == {}
    assert any("google-auth not installed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# CallbackAuth ABC contract
# ---------------------------------------------------------------------------


def test_callback_auth_is_abstract():
    """CallbackAuth cannot be instantiated directly.

    Guards against a subclass being added in the future that forgets
    to implement ``build_headers``.
    """
    with pytest.raises(TypeError):
        CallbackAuth()  # type: ignore[abstract]
