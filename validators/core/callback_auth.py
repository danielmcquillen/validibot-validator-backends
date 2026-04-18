"""
Callback authentication backends for validator containers.

Validator containers (EnergyPlus, FMU, …) finish their run and POST a
completion callback to the Django worker service. The authentication
mechanism used on that callback depends on where the validator is
running:

* **GCP (Cloud Run Jobs)** — fetches a Google-signed OIDC identity token
  from the metadata server (``google.oauth2.id_token.fetch_id_token``)
  and sends it as ``Authorization: Bearer <token>``. This is the primary
  production path and is verified by Django's
  ``CloudTasksOIDCAuthentication``.
* **Docker Compose / Celery** — shared-secret API key sent as
  ``Authorization: Worker-Key <key>`` (``WORKER_API_KEY``). Mirrors the
  Django-side ``WorkerKeyAuthentication``.
* **Local dev / tests** — no authentication header; the receiving
  service is expected to be configured to accept unauthenticated
  callbacks in that environment (Django's ``TEST`` /
  ``LOCAL_DOCKER_COMPOSE`` targets skip the shared-secret check when
  ``WORKER_API_KEY`` is unset).

The active backend is selected at container startup by
:func:`get_callback_auth`, which consults the ``DEPLOYMENT_TARGET``
environment variable. A new deployment target (e.g. AWS SQS HTTP
signatures) is added by implementing a new :class:`CallbackAuth`
subclass and extending the factory — nothing else in the validator code
changes.

Audience semantics (important!)
-------------------------------

Google Cloud signs OIDC tokens for Cloud Tasks and Cloud Scheduler with
``aud = <worker service URL origin>`` — scheme + host, **no path**.
Django's ``CloudTasksOIDCAuthentication`` enforces strict audience
matching, so validator callbacks must use the same audience. This
module derives the audience from the callback URL's origin
(``scheme://netloc``) unless overridden via ``TASK_OIDC_AUDIENCE``. A
previous implementation passed the full callback URL (including path)
as the audience, which will start failing once the Django-side strict
verification ships. Aligning on origin-only audience is the
compatibility fix.

Failure modes are deliberately fail-closed: if we can't fetch a token
we log at error level and return an empty header set. The HTTP layer
will then receive a 401/403 from Django and surface it as a retry —
far better than silently sending an unauthenticated callback that
looks successful.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from functools import lru_cache
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


# Mirror the StrEnum values used in the Django config
# (validibot/config/settings/base.py → DEPLOYMENT_TARGET). Keeping a
# plain string set here avoids dragging a Django dependency into the
# validator container image.
_DEPLOYMENT_TARGET_GCP = "gcp"
_DEPLOYMENT_TARGET_AWS = "aws"
_DEPLOYMENT_TARGET_DOCKER_COMPOSE = "docker_compose"
_DEPLOYMENT_TARGET_LOCAL_DOCKER_COMPOSE = "local_docker_compose"
_DEPLOYMENT_TARGET_TEST = "test"


class CallbackAuth(ABC):
    """Abstract base for callback authentication backends.

    Implementations produce the ``Authorization``-style headers that
    should accompany the callback POST. They do not set
    ``Content-Type`` — the HTTP client is responsible for that — so a
    backend returning ``{}`` is legitimate (local dev / open callback
    endpoint).
    """

    @abstractmethod
    def build_headers(self, callback_url: str) -> dict[str, str]:
        """Return auth headers to merge into the callback request.

        ``callback_url`` is passed in rather than captured at
        construction time so the same backend instance can serve
        multiple callback URLs (e.g. a worker promoted between stages)
        and so the GCP backend can derive the OIDC audience from the
        URL at call time.
        """


class NullCallbackAuth(CallbackAuth):
    """No-op backend. Used when no authentication is configured.

    Intentionally distinct from "couldn't fetch a token" — this is a
    conscious decision made at startup based on deployment target.
    """

    def build_headers(self, callback_url: str) -> dict[str, str]:
        return {}


class SharedSecretCallbackAuth(CallbackAuth):
    """Send a pre-shared worker API key.

    Mirrors Django's ``WorkerKeyAuthentication``:
    ``Authorization: Worker-Key <key>``. Used in Docker Compose /
    Celery deployments where infrastructure IAM is not available to
    mint identity tokens.
    """

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("SharedSecretCallbackAuth requires a non-empty secret")
        self._secret = secret

    def build_headers(self, callback_url: str) -> dict[str, str]:
        return {"Authorization": f"Worker-Key {self._secret}"}


class GCPCallbackAuth(CallbackAuth):
    """Fetch a Google-signed OIDC identity token via the metadata server.

    ``audience_override`` is read from ``TASK_OIDC_AUDIENCE`` at
    startup. If unset, the audience is derived per-request from the
    callback URL's origin (``scheme://netloc``) to match how Cloud
    Tasks and Cloud Scheduler sign their tokens. Django's verification
    enforces exact audience match, so getting this right is a hard
    correctness requirement — not a defence-in-depth nicety.

    The ``google.auth`` transport is constructed once and cached on
    the instance so the connection pool is reused across callbacks.
    """

    def __init__(self, audience_override: str | None = None) -> None:
        self._audience_override = audience_override
        # Lazily populated on first call so import of this module never
        # requires google-auth (keeps non-GCP deployments lean).
        self._transport = None
        self._id_token_mod = None

    def build_headers(self, callback_url: str) -> dict[str, str]:
        audience = self._audience_override or self._derive_audience(callback_url)
        if not audience:
            logger.error(
                "GCPCallbackAuth: could not derive audience from callback_url=%r; "
                "sending callback without Authorization header will fail auth",
                callback_url,
            )
            return {}

        try:
            id_token_mod, transport = self._load_google_auth()
        except ImportError as exc:
            logger.error(
                "GCPCallbackAuth: google-auth not installed (%s); "
                "callback will be sent without Authorization header and will "
                "likely be rejected by Django",
                exc,
            )
            return {}

        try:
            token = id_token_mod.fetch_id_token(transport, audience)
        except Exception:
            # fetch_id_token can raise many things — network errors,
            # metadata-server 404s, google.auth.exceptions.*. Log with
            # the audience so 401s from Django are diagnosable.
            logger.exception(
                "GCPCallbackAuth: failed to fetch OIDC id token (audience=%s); "
                "callback will be sent without Authorization header",
                audience,
            )
            return {}

        return {"Authorization": f"Bearer {token}"}

    def _load_google_auth(self):
        """Import google-auth and cache the transport.

        Import is deferred so importing this module doesn't require
        google-auth on non-GCP deployments. The transport is cached so
        repeated callbacks reuse the same TCP pool.
        """
        if self._transport is None or self._id_token_mod is None:
            from google.auth.transport.requests import Request as GoogleAuthRequest
            from google.oauth2 import id_token as id_token_mod

            self._transport = GoogleAuthRequest()
            self._id_token_mod = id_token_mod

        return self._id_token_mod, self._transport

    @staticmethod
    def _derive_audience(callback_url: str) -> str:
        """Return ``scheme://netloc`` for the callback URL.

        Cloud Tasks / Cloud Scheduler sign tokens with ``aud`` set to
        the service URL origin — path and query are NOT included. We
        must match that to pass strict verification. Returns ``""`` if
        the URL is un-parseable, which the caller treats as fail-closed.
        """
        parsed = urlparse(callback_url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"


@lru_cache(maxsize=1)
def get_callback_auth() -> CallbackAuth:
    """Return the process-wide callback auth backend.

    Selection order:

    1. Explicit ``DEPLOYMENT_TARGET`` env var (case-insensitive).
    2. Fallback: if ``WORKER_API_KEY`` is set assume shared-secret,
       else no-op (local dev).

    Cached via :func:`functools.lru_cache` so every callback in a
    container run reuses the same backend instance (and, for GCP, the
    same google-auth transport / connection pool).
    """
    target = os.environ.get("DEPLOYMENT_TARGET", "").strip().lower()

    if target == _DEPLOYMENT_TARGET_GCP:
        audience_override = os.environ.get("TASK_OIDC_AUDIENCE") or None
        return GCPCallbackAuth(audience_override=audience_override)

    if target in {_DEPLOYMENT_TARGET_DOCKER_COMPOSE, _DEPLOYMENT_TARGET_AWS}:
        secret = os.environ.get("WORKER_API_KEY", "")
        if secret:
            return SharedSecretCallbackAuth(secret)
        logger.warning(
            "DEPLOYMENT_TARGET=%s but WORKER_API_KEY is unset; "
            "callbacks will be sent without authentication",
            target,
        )
        return NullCallbackAuth()

    if target in {_DEPLOYMENT_TARGET_LOCAL_DOCKER_COMPOSE, _DEPLOYMENT_TARGET_TEST}:
        return NullCallbackAuth()

    # Unset or unknown target: best-effort fallback. If an API key is
    # configured, use it; otherwise go quiet (local dev against an
    # open Django). An explicit warning helps spot missing config in
    # CI / staging.
    secret = os.environ.get("WORKER_API_KEY", "")
    if secret:
        logger.info(
            "DEPLOYMENT_TARGET not set (got %r); WORKER_API_KEY present, "
            "using shared-secret callback auth",
            target,
        )
        return SharedSecretCallbackAuth(secret)

    logger.warning(
        "DEPLOYMENT_TARGET not set (got %r) and WORKER_API_KEY not set; "
        "callbacks will be sent WITHOUT authentication",
        target,
    )
    return NullCallbackAuth()


def reset_callback_auth_cache() -> None:
    """Drop the cached backend — only used by tests."""
    get_callback_auth.cache_clear()
