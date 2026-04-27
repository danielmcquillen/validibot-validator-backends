"""
Error reporting utilities for validator containers.

We log all exceptions and, when Sentry is installed/configured, forward the
exception for deeper diagnostics. This keeps user-facing messages minimal while
capturing full detail in observability tools.
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def report_fatal(exc: Exception, *, context: dict | None = None) -> None:
    """
    Log a fatal exception and send to Sentry if available.

    Args:
        exc: The exception to report.
        context: Optional extra context to include in logs/Sentry.
    """
    logger.exception("Fatal error in validator container", extra=context or {})

    try:
        import sentry_sdk  # type: ignore

        with sentry_sdk.push_scope() as scope:
            if context:
                for key, value in context.items():
                    scope.set_tag(key, value)
            sentry_sdk.capture_exception(exc)
    except Exception:
        # If Sentry is unavailable, we still have logs. Silently ignore.
        logger.debug("Sentry not available for fatal error reporting", exc_info=True)
