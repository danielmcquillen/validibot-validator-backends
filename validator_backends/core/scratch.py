"""Attempt-specific scratch roots shared by Job and Service execution."""

from __future__ import annotations

import os
from pathlib import Path


ATTEMPT_SCRATCH_ROOT_ENV = "VALIDIBOT_ATTEMPT_SCRATCH_ROOT"


def attempt_scratch_base(default_name: str) -> Path:
    """Return a parent-owned Service root or the historical Job path."""
    configured = os.getenv(ATTEMPT_SCRATCH_ROOT_ENV, "").strip()
    if configured:
        return Path(configured) / default_name
    return Path("/tmp") / default_name
