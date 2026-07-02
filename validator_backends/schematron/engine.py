"""Schematron engine primitives: checksum, guards, and the Saxon transform.

Container-side half of the ADR-2026-07-01 D8 posture. Django already
verified and staged the artefact and pre-guarded the submission, but this
container trusts nothing it downloads (defence in depth):

- :func:`verify_artifact_checksum` — the fetched pack XSLT must match the
  pinned sha256 from the input envelope before it is ever compiled.
- :func:`guard_submission` — the defusedxml posture (no DTD, no entities,
  no external references) plus size/depth caps, re-applied here.
- :func:`run_transform` — Saxon runs in a **subprocess** with a hard
  wall-clock timeout (native code cannot be interrupted in-process).

The D8 limits from the envelope are re-clamped to the hard maxima below —
Django clamps before shipping, but a hand-crafted envelope must not be able
to widen the safety net.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from typing import TYPE_CHECKING

from defusedxml import ElementTree as SafeET
from defusedxml.common import DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden


if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# D8 hard maxima — mirror the community table (validators/schematron/
# security.py). Envelope values are clamped to these, never trusted.
HARD_MAX_INPUT_BYTES = 50_000_000
HARD_MAX_INPUT_DEPTH = 500
HARD_MAX_XSLT_TIMEOUT_SECONDS = 300
HARD_MAX_FINDINGS = 2000

# Tail of worker stderr surfaced in engine error messages.
_STDERR_TAIL_CHARS = 500


class SchematronEngineError(Exception):
    """An engine-level failure: the rules were NOT run (D9).

    ``error_code`` carries the machine hint for
    ``SchematronOutputs.engine_error_code`` (e.g. ``artifact_mismatch``) so
    Django can map it to its reserved ``schematron.*`` finding codes.
    """

    def __init__(self, message: str, *, error_code: str = "") -> None:
        super().__init__(message)
        self.error_code = error_code


class SchematronTransformTimeout(SchematronEngineError):
    """The XSLT transform exceeded its wall-clock budget (D8/D9)."""

    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(
            f"Schematron transform exceeded {timeout_seconds}s wall-clock "
            f"limit and was terminated.",
        )
        self.timeout_seconds = timeout_seconds


def clamp(value: int, hard_max: int, *, default: int) -> int:
    """Clamp an envelope-supplied limit to its hard maximum."""
    if value <= 0:
        return default
    return min(value, hard_max)


def verify_artifact_checksum(artifact_path: Path, expected_sha256: str) -> None:
    """Verify the downloaded pack artefact against the pinned checksum.

    The container never trusts what it fetched (D4b): a mismatch means
    tampering or a staging bug, and the rules must not run.

    Raises:
        SchematronEngineError: with ``error_code="artifact_mismatch"``.
    """
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if digest != expected_sha256:
        msg = (
            f"Rule-pack artefact checksum mismatch: fetched sha256 "
            f"{digest[:12]}… but the envelope pins "
            f"{expected_sha256[:12]}… — refusing to execute."
        )
        raise SchematronEngineError(msg, error_code="artifact_mismatch")


def guard_submission(
    submission_path: Path,
    *,
    max_bytes: int,
    max_depth: int,
) -> None:
    """Re-apply the hardened-XML posture to the downloaded submission (D8a).

    Django pre-guarded before dispatch, but this container re-checks —
    the same defusedxml stance (XXE / entity bombs / DTDs rejected
    outright) plus size and depth caps, with the caps re-clamped to the
    hard maxima.

    Raises:
        SchematronEngineError: The submission violates the guard; the
            engine refuses to run (an input we refuse is an engine error,
            not a rule failure — D8/D9).
    """
    effective_max_bytes = clamp(
        max_bytes,
        HARD_MAX_INPUT_BYTES,
        default=HARD_MAX_INPUT_BYTES,
    )
    effective_max_depth = clamp(
        max_depth,
        HARD_MAX_INPUT_DEPTH,
        default=HARD_MAX_INPUT_DEPTH,
    )

    size = submission_path.stat().st_size
    if size > effective_max_bytes:
        msg = (
            f"XML submission is too large ({size:,} bytes > "
            f"{effective_max_bytes:,} bytes)."
        )
        raise SchematronEngineError(msg)

    try:
        root = SafeET.fromstring(submission_path.read_bytes(), forbid_dtd=True)
    except (EntitiesForbidden, ExternalReferenceForbidden, DTDForbidden) as exc:
        msg = (
            "XML submission contains forbidden constructs (entities, "
            "external references, or DTD declarations)."
        )
        raise SchematronEngineError(msg) from exc
    except SafeET.ParseError as exc:
        msg = f"Submission is not well-formed XML: {exc}"
        raise SchematronEngineError(msg) from exc

    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        if depth > effective_max_depth:
            msg = (
                f"XML submission nests deeper than the maximum "
                f"({effective_max_depth} levels)."
            )
            raise SchematronEngineError(msg)
        stack.extend((child, depth + 1) for child in element)


def run_transform(
    xslt_path: Path,
    xml_path: Path,
    output_path: Path,
    *,
    timeout_seconds: int,
) -> str:
    """Run the pack XSLT over the submission via the Saxon worker subprocess.

    The subprocess boundary is what makes the timeout REAL: SaxonC executes
    native code that in-process signals cannot interrupt, but
    ``subprocess.run(timeout=…)`` kills the worker unconditionally.

    Returns:
        The SVRL report text the worker wrote.

    Raises:
        SchematronTransformTimeout: Wall-clock budget exceeded (→ D9
            ``engine_status="timeout"``).
        SchematronEngineError: The worker failed to compile/transform
            (→ D9 ``engine_status="error"``).
    """
    effective_timeout = clamp(
        timeout_seconds,
        HARD_MAX_XSLT_TIMEOUT_SECONDS,
        default=HARD_MAX_XSLT_TIMEOUT_SECONDS,
    )

    command = [
        sys.executable,
        "-m",
        "validator_backends.schematron.saxon_worker",
        str(xslt_path),
        str(xml_path),
        str(output_path),
    ]
    logger.info(
        "Running Saxon worker (timeout=%ss): %s",
        effective_timeout,
        " ".join(command),
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SchematronTransformTimeout(effective_timeout) from exc

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip()[-_STDERR_TAIL_CHARS:]
        msg = f"Saxon transform failed: {stderr_tail or 'no error detail'}"
        raise SchematronEngineError(msg)

    if not output_path.exists():
        msg = "Saxon worker exited cleanly but produced no SVRL output."
        raise SchematronEngineError(msg)

    return output_path.read_text(encoding="utf-8")


def saxon_engine_version() -> str:
    """Name + version of the engine that ran, for D5 provenance."""
    try:
        from saxonche import PySaxonProcessor

        with PySaxonProcessor(license=False) as processor:
            # e.g. "SaxonC-HE 12.9 from Saxonica"
            return str(processor.version).split(" from ")[0]
    except Exception:
        logger.warning("Could not determine Saxon version", exc_info=True)
        return "SaxonC-HE (unknown version)"
