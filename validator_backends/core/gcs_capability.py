"""Consume one attempt-scoped Google Cloud Storage capability.

Cloud Run validator jobs must not use their attached service account for
bucket access. Django instead passes a short-lived Credential Access Boundary
token whose permissions and resource condition are limited to one execution
attempt prefix. This module keeps that bearer token out of logs and makes the
same prefix check locally before the Google client sees any request.

The first token is sufficient to load ``input.json``. After the envelope has
been authenticated and parsed, its callback nonce authorizes token renewal
through the worker service. Renewal is denied once Django has fenced the
attempt into a terminal state.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from google.oauth2.credentials import Credentials

from validator_backends.core.callback_auth import get_callback_auth


if TYPE_CHECKING:
    from pydantic import BaseModel


CAPABILITY_REQUIRED_ENV = "VALIDIBOT_GCS_CAPABILITY_REQUIRED"
CAPABILITY_TOKEN_ENV = "VALIDIBOT_GCS_ACCESS_TOKEN"
CAPABILITY_EXPIRY_ENV = "VALIDIBOT_GCS_ACCESS_TOKEN_EXPIRY"
CAPABILITY_PREFIX_ENV = "VALIDIBOT_GCS_ALLOWED_PREFIX"
CAPABILITY_PROJECT_ENV = "VALIDIBOT_GCS_PROJECT_ID"
CAPABILITY_REFRESH_URL_ENV = "VALIDIBOT_GCS_CAPABILITY_REFRESH_URL"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class GCSCapabilityError(RuntimeError):
    """Raised when an attempt capability is absent, malformed, or out of scope."""


@dataclass(frozen=True, slots=True)
class GCSCapabilityConfig:
    """Non-secret limits accompanying one short-lived access token."""

    project_id: str
    allowed_prefix: str
    refresh_url: str


@dataclass(slots=True)
class _TokenState:
    """Mutable in-memory token state, with secret values excluded from repr."""

    access_token: str = field(repr=False)
    expiry: datetime


@dataclass(frozen=True, slots=True)
class _RefreshContext:
    """Attempt proof learned only after the trusted input envelope is parsed."""

    run_id: str
    callback_id: str
    callback_nonce: str = field(repr=False)


_state_lock = threading.Lock()
_config: GCSCapabilityConfig | None = None
_token_state: _TokenState | None = None
_refresh_context: _RefreshContext | None = None
_environment_loaded = False


def capability_is_required() -> bool:
    """Return whether this process must reject ordinary ambient GCS access."""
    return os.getenv(CAPABILITY_REQUIRED_ENV, "").strip().lower() in _TRUE_VALUES


def assert_gcs_uri_allowed(uri: str) -> None:
    """Fail before I/O when a URI falls outside the injected attempt prefix."""
    config, _ = _load_environment()
    if config is None:
        if capability_is_required():
            raise GCSCapabilityError(
                "Attempt-scoped GCS capability is required but was not provided"
            )
        return

    if not uri.startswith(config.allowed_prefix):
        raise GCSCapabilityError("GCS URI is outside this execution attempt's allowed prefix")


def build_gcs_credentials() -> tuple[Credentials | None, str | None]:
    """Return explicit downscoped credentials and project for a storage client.

    ``None`` preserves local/manual legacy behavior only when capability mode
    was not marked required. Cloud Run executions launched by current Django
    always set the required marker and therefore fail closed if token delivery
    is incomplete.
    """
    config, state = _load_environment()
    if config is None or state is None:
        if capability_is_required():
            raise GCSCapabilityError("Attempt-scoped GCS capability is required but incomplete")
        return None, None

    refresh_handler = _refresh_access_token if _refresh_context is not None else None
    credentials = Credentials(
        token=state.access_token,
        expiry=state.expiry,
        refresh_handler=refresh_handler,
    )
    return credentials, config.project_id


def configure_capability_refresh(envelope: BaseModel) -> None:
    """Bind future token renewal to the parsed attempt callback credential."""
    global _refresh_context

    config, _ = _load_environment()
    if config is None:
        return

    context = getattr(envelope, "context", None)
    run_id = str(getattr(envelope, "run_id", "") or "")
    callback_id = str(getattr(context, "callback_id", "") or "")
    callback_nonce = str(getattr(context, "callback_nonce", "") or "")
    if not config.refresh_url or not run_id or not callback_id or not callback_nonce:
        raise GCSCapabilityError(
            "Attempt-scoped GCS token renewal requires callback-bound context"
        )

    _refresh_context = _RefreshContext(
        run_id=run_id,
        callback_id=callback_id,
        callback_nonce=callback_nonce,
    )


def _load_environment() -> tuple[GCSCapabilityConfig | None, _TokenState | None]:
    """Load and validate the immutable process capability environment once."""
    global _config
    global _environment_loaded
    global _token_state

    if _environment_loaded:
        return _config, _token_state

    with _state_lock:
        if _environment_loaded:
            return _config, _token_state

        token = os.getenv(CAPABILITY_TOKEN_ENV, "").strip()
        expiry_value = os.getenv(CAPABILITY_EXPIRY_ENV, "").strip()
        prefix = os.getenv(CAPABILITY_PREFIX_ENV, "").strip()
        project_id = os.getenv(CAPABILITY_PROJECT_ENV, "").strip()
        refresh_url = os.getenv(CAPABILITY_REFRESH_URL_ENV, "").strip()

        supplied = any((token, expiry_value, prefix, project_id, refresh_url))
        if not supplied:
            _environment_loaded = True
            return None, None
        if not all((token, expiry_value, prefix, project_id, refresh_url)):
            raise GCSCapabilityError("Attempt-scoped GCS capability environment is incomplete")
        if not prefix.startswith("gs://") or not prefix.endswith("/"):
            raise GCSCapabilityError(
                "VALIDIBOT_GCS_ALLOWED_PREFIX must be a gs:// URI ending in '/'"
            )
        if not refresh_url.startswith("https://"):
            raise GCSCapabilityError("GCS capability refresh URL must use HTTPS")

        _config = GCSCapabilityConfig(
            project_id=project_id,
            allowed_prefix=prefix,
            refresh_url=refresh_url,
        )
        _token_state = _TokenState(
            access_token=token,
            expiry=_parse_expiry(expiry_value),
        )
        _environment_loaded = True
        return _config, _token_state


def _refresh_access_token(request, scopes=None) -> tuple[str, datetime]:
    """Obtain another prefix-identical token from the attempt-fencing broker."""
    config, _ = _load_environment()
    context = _refresh_context
    if config is None or context is None:
        raise GCSCapabilityError("GCS capability refresh context is unavailable")

    auth = get_callback_auth()
    headers = {"Content-Type": "application/json"}
    headers.update(auth.build_headers(config.refresh_url))
    with httpx.Client(timeout=30) as client:
        response = client.post(
            config.refresh_url,
            json={
                "run_id": context.run_id,
                "callback_id": context.callback_id,
                "callback_nonce": context.callback_nonce,
            },
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

    returned_prefix = str(payload.get("allowed_prefix", ""))
    if returned_prefix != config.allowed_prefix:
        raise GCSCapabilityError("Capability broker returned a different attempt prefix")
    token = str(payload.get("access_token", ""))
    expiry = _parse_expiry(str(payload.get("expires_at", "")))
    if not token:
        raise GCSCapabilityError("Capability broker returned an empty access token")

    with _state_lock:
        global _token_state
        _token_state = _TokenState(access_token=token, expiry=expiry)
    return token, expiry


def _parse_expiry(value: str) -> datetime:
    """Parse RFC 3339 expiry into google-auth's naive UTC convention."""
    if not value:
        raise GCSCapabilityError("GCS capability token expiry is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GCSCapabilityError("GCS capability token expiry is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(tzinfo=None)


def reset_capability_state_for_tests() -> None:
    """Clear process globals so tests can install a fresh capability env."""
    global _config
    global _environment_loaded
    global _refresh_context
    global _token_state

    with _state_lock:
        _config = None
        _token_state = None
        _refresh_context = None
        _environment_loaded = False
