"""
HTTP callback client for validator containers.

Provides utilities for POSTing validation completion callbacks back to
the Django worker service. Authentication is delegated to a pluggable
:mod:`validators.core.callback_auth` backend so the transport code
stays deployment-target agnostic.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from validators.core.callback_auth import CallbackAuth, get_callback_auth
from validibot_shared.validations.envelopes import ValidationCallback, ValidationStatus


logger = logging.getLogger(__name__)


def post_callback(
    callback_url: str | None,
    run_id: str,
    status: ValidationStatus,
    result_uri: str,
    *,
    callback_id: str | None = None,
    skip_callback: bool = False,
    timeout_seconds: int = 30,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    auth: CallbackAuth | None = None,
) -> dict[str, Any] | None:
    """
    POST a validation completion callback to the Django worker service.

    Args:
        callback_url: Django callback endpoint URL (can be None if skip_callback=True)
        run_id: Validation run ID
        status: Validation status (SUCCESS, FAILED_VALIDATION, etc.)
        result_uri: GCS URI to output.json
        callback_id: Idempotency key echoed from the input envelope
            (for duplicate detection)
        skip_callback: If True, skip the callback POST (useful for testing)
        timeout_seconds: HTTP request timeout
        max_attempts: Retry attempts for transient failures
        retry_delay_seconds: Delay between retries
        auth: Optional explicit authentication backend. When omitted
            the process-wide backend from :func:`get_callback_auth` is
            used. Tests pass a stub here instead of monkey-patching
            google-auth internals.

    Returns:
        Response JSON from Django, or None if skip_callback=True

    Raises:
        httpx.HTTPStatusError: If callback request fails
    """
    if skip_callback:
        logger.info("Skipping callback for run_id=%s (skip_callback=True)", run_id)
        return None

    if not callback_url:
        logger.warning(
            "No callback_url provided for run_id=%s and skip_callback=False; skipping",
            run_id,
        )
        return None

    logger.info(
        "POSTing callback for run_id=%s to %s (callback_id=%s)",
        run_id,
        callback_url,
        callback_id,
    )

    callback = ValidationCallback(
        run_id=run_id,
        callback_id=callback_id,
        status=status,
        result_uri=result_uri,
    )

    auth_backend: CallbackAuth = auth if auth is not None else get_callback_auth()

    def _build_headers() -> dict[str, str]:
        """Combine content-type with backend-provided auth headers.

        Auth headers are rebuilt per attempt so that a short-lived
        OIDC token doesn't expire between the first attempt and a
        retry after ``retry_delay_seconds``.
        """
        headers = {"Content-Type": "application/json"}
        headers.update(auth_backend.build_headers(callback_url))
        return headers

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            headers = _build_headers()
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    callback_url,
                    json=callback.model_dump(),
                    headers=headers,
                )

                response.raise_for_status()

                logger.info(
                    "Callback successful (run_id=%s, status=%d)",
                    run_id,
                    response.status_code,
                )

                return response.json()
        except httpx.HTTPError as exc:
            last_exc = exc
            logger.warning(
                "Callback attempt %d/%d failed: %s",
                attempt,
                max_attempts,
                exc,
            )
            if attempt < max_attempts:
                time.sleep(retry_delay_seconds)
            else:
                raise

    if last_exc:
        raise last_exc
