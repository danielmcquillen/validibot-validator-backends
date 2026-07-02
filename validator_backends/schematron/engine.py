"""Schematron engine primitives: guards, compile-and-run, hard caps.

Container-side half of the ADR-2026-07-01 D8 posture. Django already
validated the uploaded rules at authoring time and pre-guarded the
submission at dispatch, but this container trusts nothing it receives
(defence in depth):

- :func:`guard_submission` — the defusedxml posture (no DTD, no entities,
  no external references) plus size/depth caps, re-applied here.
- :func:`run_schematron` — the SchXslt2 transpile of the author's ``.sch``
  plus the Saxon run, all inside a **subprocess** with a hard wall-clock
  timeout (native code cannot be interrupted in-process). A source that
  fails to COMPILE maps to ``rules_invalid`` — an authoring problem,
  reported distinctly from generic engine errors (D9).

The D8 limits from the envelope are re-clamped to the hard maxima below —
Django clamps before shipping, but a hand-crafted envelope must not be able
to widen the safety net.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import TYPE_CHECKING

from defusedxml import ElementTree as SafeET
from defusedxml.common import DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden

from validator_backends.schematron.saxon_worker import (
    EXIT_COMPILE_ERROR,
    SCHXSLT2_DIR,
    TRANSPILER_STYLESHEET,
)


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
    ``SchematronOutputs.engine_error_code`` (e.g. ``rules_invalid``) so
    Django can map it to its reserved ``schematron.*`` finding codes.
    """

    def __init__(self, message: str, *, error_code: str = "") -> None:
        super().__init__(message)
        self.error_code = error_code


class SchematronTransformTimeout(SchematronEngineError):
    """The compile-and-run exceeded its wall-clock budget (D8/D9)."""

    def __init__(self, timeout_seconds: int) -> None:
        super().__init__(
            f"Schematron transform exceeded {timeout_seconds}s wall-clock "
            f"limit and was terminated.",
        )
        self.timeout_seconds = timeout_seconds


def transpiler_available() -> bool:
    """Whether the vendored SchXslt2 transpiler is present in this build.

    ``transpile.xsl`` is engine tooling vendored into the image (see
    ``schxslt2/README.md``); a build without it cannot compile any
    Schematron, so callers fail fast with a clear message instead of a
    confusing Saxon error.
    """
    return (SCHXSLT2_DIR / TRANSPILER_STYLESHEET).is_file()


def clamp(value: int, hard_max: int, *, default: int) -> int:
    """Clamp an envelope-supplied limit to its hard maximum."""
    if value <= 0:
        return default
    return min(value, hard_max)


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
        msg = f"XML submission is too large ({size:,} bytes > {effective_max_bytes:,} bytes)."
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
            msg = f"XML submission nests deeper than the maximum ({effective_max_depth} levels)."
            raise SchematronEngineError(msg)
        stack.extend((child, depth + 1) for child in element)


def detect_query_binding(sch_path: Path) -> str:
    """Read the ``queryBinding`` declared by the .sch root, for provenance.

    Normalised to the ``xslt1``/``xslt2`` tokens the contract uses; the ISO
    default (no attribute, or the legacy ``xslt`` spelling) is ``xslt1``.
    Detection failures return ``""`` — provenance must never kill a run.
    """
    try:
        root = SafeET.parse(str(sch_path), forbid_dtd=True).getroot()
    except Exception:
        logger.warning("Could not detect queryBinding", exc_info=True)
        return ""
    binding = (root.get("queryBinding") or "").strip().lower()
    if binding in ("", "xslt"):
        return "xslt1"
    return binding


def run_schematron(
    sch_path: Path,
    xml_path: Path,
    output_path: Path,
    *,
    timeout_seconds: int,
) -> str:
    """Compile the author's .sch and run it via the Saxon worker subprocess.

    The subprocess boundary is what makes the timeout REAL: SaxonC executes
    native code that in-process signals cannot interrupt, but
    ``subprocess.run(timeout=…)`` kills the worker unconditionally. The
    budget covers compile AND run — a pathological source can be slow in
    either phase.

    Returns:
        The SVRL report text the worker wrote.

    Raises:
        SchematronTransformTimeout: Wall-clock budget exceeded (→ D9
            ``engine_status="timeout"``).
        SchematronEngineError: With ``error_code="rules_invalid"`` when the
            .sch failed to compile (an authoring problem), or with no code
            for other engine failures.
    """
    if not transpiler_available():
        msg = (
            "The SchXslt2 transpiler is not vendored into this build "
            "(see schxslt2/README.md) — cannot compile Schematron rules."
        )
        raise SchematronEngineError(msg)

    effective_timeout = clamp(
        timeout_seconds,
        HARD_MAX_XSLT_TIMEOUT_SECONDS,
        default=HARD_MAX_XSLT_TIMEOUT_SECONDS,
    )

    command = [
        sys.executable,
        "-m",
        "validator_backends.schematron.saxon_worker",
        str(sch_path),
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

    if completed.returncode == EXIT_COMPILE_ERROR:
        stderr_tail = (completed.stderr or "").strip()[-_STDERR_TAIL_CHARS:]
        msg = f"The Schematron rules failed to compile: {stderr_tail or 'no error detail'}"
        raise SchematronEngineError(msg, error_code="rules_invalid")

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip()[-_STDERR_TAIL_CHARS:]
        msg = f"Saxon transform failed: {stderr_tail or 'no error detail'}"
        raise SchematronEngineError(msg)

    if not output_path.exists():
        msg = "Saxon worker exited cleanly but produced no SVRL output."
        raise SchematronEngineError(msg)

    return output_path.read_text(encoding="utf-8")


def schxslt2_version() -> str:
    """The vendored transpiler's version, from its release VERSION file."""
    try:
        return (SCHXSLT2_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def engine_version() -> str:
    """Identity of the toolchain that ran, for D5 provenance.

    Both halves matter for reproducibility: SchXslt2 decides how the
    ``.sch`` compiles, Saxon decides how the compiled XSLT executes —
    e.g. ``"SchXslt2 1.11.1 + SaxonC-HE 12.9"``.
    """
    try:
        from saxonche import PySaxonProcessor

        with PySaxonProcessor(license=False) as processor:
            # e.g. "SaxonC-HE 12.9 from Saxonica"
            saxon = str(processor.version).split(" from ")[0]
    except Exception:
        logger.warning("Could not determine Saxon version", exc_info=True)
        saxon = "SaxonC-HE (unknown version)"
    return f"SchXslt2 {schxslt2_version()} + {saxon}"
