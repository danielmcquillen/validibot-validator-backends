"""
EnergyPlus simulation runner.

Handles downloading input files, running EnergyPlus, and extracting results.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from validator_backends.core.gcs_client import download_verified_file
from validator_backends.core.scratch import attempt_scratch_base
from validator_backends.core.storage_client import create_attempt_work_dir
from validibot_shared.energyplus.envelopes import EnergyPlusOutputs
from validibot_shared.energyplus.models import (
    STDOUT_TAIL_CHARS,
    EnergyPlusSimulationLogs,
    EnergyPlusSimulationMetrics,
    EnergyPlusSimulationOutputs,
)


if TYPE_CHECKING:
    from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope

logger = logging.getLogger(__name__)

GJ_TO_KWH = 1_000_000_000.0 / 3_600_000.0


@dataclass(frozen=True)
class EnergyPlusInstallationEvidence:
    """Identity of the EnergyPlus executable and bundled IDD used for a run."""

    binary_path: Path
    binary_version: str | None
    binary_build: str | None
    idd_path: Path
    idd_version: str | None
    idd_build: str | None


# ---------------------------------------------------------------------------
# Defense-in-depth: pre-run inspection of user-supplied model files
# ---------------------------------------------------------------------------
#
# WHY: EnergyPlus runs a native binary against an arbitrary, user-supplied
# IDF/epJSON model. Several first-class IDF object types are effectively code
# or external-resource loaders — they let a model author load shared
# libraries, reach out to the network/filesystem, or run interpreted code
# during the simulation:
#
#   * PythonPlugin:*                                  → loads & runs Python
#   * ExternalInterface (+ FMU import/export)         → loads FMU shared libs,
#                                                        opens sockets (BCVTB)
#   * EnergyManagementSystem:Program/Subroutine       → runs Erl runtime code
#   * Schedule:File pointing at an absolute/remote path → reads arbitrary files
#
# The sandboxed container is and remains the PRIMARY security boundary; this
# scan is defense-in-depth only. We reject by default so a malicious or
# accidentally-dangerous model cannot reach these capabilities even if the
# container's isolation is weakened or misconfigured. Operators with a
# legitimate need (e.g. trusted internal FMU co-simulation) can opt out via
# the ALLOW_UNSAFE_IDF_OBJECTS module constant.

# Off-by-default escape hatch. Set to True only in a deployment that fully
# trusts its model authors AND accepts that the container is the sole boundary.
ALLOW_UNSAFE_IDF_OBJECTS = False

# High-risk IDF/epJSON object-type tokens (matched case-insensitively as plain
# substrings against the raw model text). These map to capabilities that load
# code, load native libraries, open sockets, or read arbitrary files. The
# longer/more-specific tokens are intentionally listed so the reported reason
# is precise; the short prefixes catch the remaining variants.
UNSAFE_IDF_OBJECT_TOKENS = (
    "PythonPlugin",
    "ExternalInterface:FunctionalMockupUnitImport",
    "ExternalInterface:FunctionalMockupUnitExport",
    "ExternalInterface",
    "EnergyManagementSystem:Program",
    "EnergyManagementSystem:Subroutine",
)

# Schedule:File is only dangerous when it references an absolute or remote
# path (a relative path resolves inside the sandboxed work_dir). We flag it
# separately so an ordinary, in-work-dir Schedule:File is not rejected.
_SCHEDULE_FILE_TOKEN = "Schedule:File"
_REMOTE_PATH_PREFIXES = ("http://", "https://", "ftp://", "file://", "\\\\")

# Number of leading bytes of the model file to inspect. The header region of
# an IDF/epJSON is where object definitions live; capping the read keeps the
# scan cheap and bounded for very large generated models.
MODEL_SCAN_MAX_BYTES = 5_000_000


class UnsafeModelObjectError(ValueError):
    """Raised when a model file contains a high-risk IDF/epJSON object type.

    WHY: Signals that the defense-in-depth pre-run scan rejected the model
    before EnergyPlus was invoked. Subclasses ``ValueError`` so existing
    callers that treat input-validation failures as ``ValueError`` continue
    to handle it as a (non-retryable) bad-input condition.
    """


def _detect_unsafe_model_objects(model_text: str) -> str | None:
    """Return a human-readable reason if the model text uses an unsafe object.

    The scan is a conservative, case-insensitive substring match over the raw
    model text. It is intentionally coarse: it does not parse the IDF/epJSON
    grammar, so it errs toward flagging (false positives are acceptable;
    silently running dangerous objects is not).

    Args:
        model_text: Raw text of the IDF or epJSON model file.

    Returns:
        A short reason string describing the first unsafe object found, or
        ``None`` if no high-risk object types were detected.
    """
    lowered = model_text.lower()

    for token in UNSAFE_IDF_OBJECT_TOKENS:
        if token.lower() in lowered:
            return f"model references high-risk object type {token!r}"

    # Schedule:File is only flagged when its file-name field points outside the
    # work_dir, i.e. at an absolute path or a remote URL. A relative path
    # resolves inside the sandboxed work_dir and is fine. We extract each
    # Schedule:File *object body* (the text from the object type up to its
    # terminating ';') and inspect its comma-separated fields, so that an
    # absolute path in some unrelated object does not trigger a false positive.
    if _SCHEDULE_FILE_TOKEN.lower() in lowered:
        for body in _iter_object_bodies(model_text, _SCHEDULE_FILE_TOKEN):
            reason = _classify_schedule_file_path(body)
            if reason is not None:
                return reason

    return None


def _iter_object_bodies(model_text: str, object_type: str):
    """Yield the raw text of each IDF/epJSON object of ``object_type``.

    An IDF object starts with its type token and ends at the next ``;`` field
    terminator. This is a deliberately loose extractor (it also works on the
    flattened text of an epJSON file, where the field values still appear as
    substrings) used only to bound the region we inspect for unsafe paths.

    Args:
        model_text: Raw model text.
        object_type: The object type token to search for (e.g. ``Schedule:File``).

    Yields:
        The substring from each occurrence of ``object_type`` up to (and
        including) the next semicolon, or to end-of-text if none follows.
    """
    lowered = model_text.lower()
    needle = object_type.lower()
    start = lowered.find(needle)
    while start != -1:
        end = model_text.find(";", start)
        if end == -1:
            yield model_text[start:]
            return
        yield model_text[start : end + 1]
        start = lowered.find(needle, end + 1)


def _classify_schedule_file_path(object_body: str) -> str | None:
    """Return a reason if a Schedule:File object body uses an unsafe path.

    Each comma/semicolon-separated field of the object body is examined for a
    remote URL, an absolute POSIX path, or a Windows drive path. IDF comments
    run from ``!`` to end-of-line, so we strip them per physical line *before*
    splitting into fields — otherwise a comment on one line would swallow the
    field value on the next, hiding an absolute path and causing a dangerous
    false negative.

    Args:
        object_body: The raw text of one ``Schedule:File`` object.

    Returns:
        A reason string if an absolute/remote path is referenced, else ``None``.
    """
    # Strip end-of-line IDF comments line by line, then re-join.
    decommented = "\n".join(line.split("!", 1)[0] for line in object_body.splitlines())

    for raw_field in re.split(r"[,;]", decommented):
        field = raw_field.strip()
        if not field:
            continue
        lowered_field = field.lower()
        if any(prefix in lowered_field for prefix in _REMOTE_PATH_PREFIXES):
            return f"{_SCHEDULE_FILE_TOKEN} references a remote path"
        if field.startswith("/"):
            return f"{_SCHEDULE_FILE_TOKEN} references an absolute path"
        if re.match(r"[A-Za-z]:[\\/]", field):
            return f"{_SCHEDULE_FILE_TOKEN} references an absolute path"
    return None


def _scan_model_for_unsafe_objects(model_file: Path) -> None:
    """Reject a model file that contains high-risk IDF/epJSON object types.

    This is a defense-in-depth check that runs BEFORE the EnergyPlus binary is
    invoked. The sandboxed container remains the primary security boundary;
    this scan adds a cheap second layer so a model that loads code, loads
    native libraries, opens sockets, or reads arbitrary files cannot reach
    those capabilities by default.

    Set the module constant ``ALLOW_UNSAFE_IDF_OBJECTS = True`` to bypass the
    check in a deployment that fully trusts its model authors.

    Args:
        model_file: Path to the downloaded IDF or epJSON model file.

    Raises:
        UnsafeModelObjectError: If an unsafe object type is detected and
            ``ALLOW_UNSAFE_IDF_OBJECTS`` is False.
    """
    if ALLOW_UNSAFE_IDF_OBJECTS:
        logger.warning(
            "ALLOW_UNSAFE_IDF_OBJECTS is enabled; skipping model safety scan for %s",
            model_file,
        )
        return

    try:
        model_text = model_file.read_text(encoding="utf-8", errors="replace")[
            :MODEL_SCAN_MAX_BYTES
        ]
    except OSError as exc:
        # If we cannot read the model to inspect it, fail closed rather than
        # running an uninspected file through the native binary.
        raise UnsafeModelObjectError(
            f"Could not read model file for safety inspection: {model_file}",
        ) from exc

    reason = _detect_unsafe_model_objects(model_text)
    if reason is not None:
        logger.warning("Rejecting EnergyPlus model %s: %s", model_file, reason)
        raise UnsafeModelObjectError(
            "EnergyPlus model rejected by defense-in-depth safety scan: "
            f"{reason}. These object types can load code, load native "
            "libraries, open network sockets, or read arbitrary files. Set "
            "ALLOW_UNSAFE_IDF_OBJECTS=True only in a fully trusted deployment.",
        )


# ---------------------------------------------------------------------------
# Review-readiness evidence and private working-copy normalization
# ---------------------------------------------------------------------------


def _parse_energyplus_version(text: str) -> tuple[str | None, str | None]:
    """Parse the numeric version and optional build from ``--version`` text."""

    match = re.search(
        r"(?:EnergyPlus(?:\s*,)?\s*(?:Version\s*)?)?"
        r"(?P<version>\d+\.\d+(?:\.\d+)?)"
        r"(?:-(?P<build>[A-Za-z0-9._]+))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    return match.group("version"), match.group("build")


def _parse_idd_metadata(idd_path: Path) -> tuple[str | None, str | None]:
    """Read ``!IDD_Version`` and ``!IDD_BUILD`` from the exact IDD file."""

    try:
        header = idd_path.read_text(encoding="utf-8", errors="replace")[:64_000]
    except OSError:
        logger.warning("Could not read EnergyPlus IDD metadata from %s", idd_path)
        return None, None

    version_match = re.search(
        r"^\s*!IDD_Version\s+([^\s!]+)",
        header,
        re.IGNORECASE | re.MULTILINE,
    )
    build_match = re.search(
        r"^\s*!IDD_BUILD\s+([^\s!]+)",
        header,
        re.IGNORECASE | re.MULTILINE,
    )
    return (
        version_match.group(1).strip() if version_match else None,
        build_match.group(1).strip() if build_match else None,
    )


def _collect_installation_evidence() -> EnergyPlusInstallationEvidence:
    """Locate the executable/IDD and collect the evidence used by this run."""

    binary = shutil.which("energyplus")
    if not binary:
        raise FileNotFoundError("EnergyPlus executable was not found on PATH")
    binary_path = Path(binary).resolve()
    idd_path = binary_path.parent / "Energy+.idd"
    if not idd_path.is_file():
        raise FileNotFoundError(
            f"Bundled EnergyPlus IDD was not found beside the executable: {idd_path}",
        )

    version_result = subprocess.run(
        [str(binary_path), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    version_text = "\n".join(
        part for part in (version_result.stdout, version_result.stderr) if part
    )
    binary_version, binary_build = _parse_energyplus_version(version_text)
    idd_version, idd_build = _parse_idd_metadata(idd_path)
    return EnergyPlusInstallationEvidence(
        binary_path=binary_path,
        binary_version=binary_version,
        binary_build=binary_build,
        idd_path=idd_path,
        idd_version=idd_version,
        idd_build=idd_build,
    )


def _parse_model_version(model_file: Path) -> str | None:
    """Extract the EnergyPlus Version object from IDF or epJSON."""

    try:
        if model_file.suffix.lower() in {".epjson", ".json"}:
            payload = json.loads(model_file.read_text(encoding="utf-8"))
            versions = payload.get("Version") if isinstance(payload, dict) else None
            if isinstance(versions, dict):
                for value in versions.values():
                    if not isinstance(value, dict):
                        continue
                    identifier = value.get("version_identifier")
                    if identifier is not None and str(identifier).strip():
                        return str(identifier).strip()
            return None

        content = model_file.read_text(encoding="utf-8", errors="replace")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None

    decommented = "\n".join(line.split("!", 1)[0] for line in content.splitlines())
    match = re.search(
        r"(?:^|;)\s*Version\s*,\s*([^,;\s]+)\s*;",
        decommented,
        re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1).strip() if match else None


def _major_minor(version: str | None) -> tuple[int, int] | None:
    """Normalize a version string to the major/minor pair used by IDFs."""

    if not version:
        return None
    match = re.search(r"(\d+)\.(\d+)", version)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _versions_match(
    idf_version: str | None,
    binary_version: str | None,
    idd_version: str | None,
) -> bool | None:
    """Compare the model, binary, and IDD at EnergyPlus major/minor precision."""

    model_pair = _major_minor(idf_version)
    binary_pair = _major_minor(binary_version)
    idd_pair = _major_minor(idd_version)
    if model_pair is None or binary_pair is None or idd_pair is None:
        return None
    return model_pair == binary_pair == idd_pair


def _prepare_model_working_copy(
    model_file: Path,
    work_dir: Path,
    timestep_per_hour: int,
) -> Path:
    """Create and normalize a private model copy without changing the source."""

    suffix = model_file.suffix.lower()
    normalized_suffix = ".epjson" if suffix in {".epjson", ".json"} else ".idf"
    working_model = work_dir / f"validibot-normalized-model{normalized_suffix}"
    shutil.copy2(model_file, working_model)

    if normalized_suffix == ".epjson":
        payload = json.loads(working_model.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("EnergyPlus epJSON model must contain a JSON object")
        timestep_objects = payload.get("Timestep")
        if not isinstance(timestep_objects, dict) or not timestep_objects:
            timestep_objects = {"Timestep 1": {}}
            payload["Timestep"] = timestep_objects
        first = next(iter(timestep_objects))
        if not isinstance(timestep_objects[first], dict):
            timestep_objects[first] = {}
        timestep_objects[first]["number_of_timesteps_per_hour"] = timestep_per_hour
        working_model.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return working_model

    content = working_model.read_text(encoding="utf-8", errors="replace")
    timestep_pattern = re.compile(
        r"(?:^|(?<=;))\s*Timestep\s*,.*?;",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    replacement = f"\nTimestep,\n  {timestep_per_hour};"
    if timestep_pattern.search(content):
        content = timestep_pattern.sub(replacement, content, count=1)
    else:
        content = content.rstrip() + replacement + "\n"
    working_model.write_text(content, encoding="utf-8")
    return working_model


def _idf_objects(model_text: str) -> list[tuple[str, list[str]]]:
    """Return loose IDF object-type/field tuples for bounded review checks."""

    decommented = "\n".join(line.split("!", 1)[0] for line in model_text.splitlines())
    objects: list[tuple[str, list[str]]] = []
    for body in decommented.split(";"):
        parts = [part.strip() for part in body.split(",")]
        if not parts or not parts[0]:
            continue
        objects.append((parts[0], parts[1:]))
    return objects


def _review_message(
    *,
    severity: str,
    code: str,
    text: str,
    category: str,
) -> dict[str, Any]:
    """Build the internal message shape later converted to ValidationMessage."""

    return {
        "severity": severity,
        "code": code,
        "text": text,
        "category": category,
        "tags": ["energyplus-review", category],
    }


def _check_duplicate_names(
    idf_objects: list[tuple[str, list[str]]] | None,
    epjson: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Find duplicate object names using EnergyPlus case-insensitive identity."""

    duplicates: list[str] = []
    if idf_objects is not None:
        seen: set[tuple[str, str]] = set()
        for object_type, fields in idf_objects:
            if not fields or not fields[0]:
                continue
            key = (object_type.casefold(), fields[0].casefold())
            if key in seen:
                duplicates.append(f"{object_type}={fields[0]}")
            seen.add(key)
    elif epjson is not None:
        for object_type, instances in epjson.items():
            if not isinstance(instances, dict):
                continue
            seen_names: set[str] = set()
            for name in instances:
                normalized = str(name).casefold()
                if normalized in seen_names:
                    duplicates.append(f"{object_type}={name}")
                seen_names.add(normalized)

    if not duplicates:
        return []
    preview = ", ".join(duplicates[:5])
    suffix = "" if len(duplicates) <= 5 else f" (+{len(duplicates) - 5} more)"
    return [
        _review_message(
            severity="error",
            code="ENERGYPLUS_REVIEW_DUPLICATE_OBJECT_NAME",
            text=f"Duplicate EnergyPlus object names detected: {preview}{suffix}",
            category="duplicate-object-names",
        ),
    ]


def _check_hvac_sizing(
    idf_objects: list[tuple[str, list[str]]] | None,
    epjson: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Warn when SimulationControl explicitly disables both sizing passes."""

    zone_sizing: Any = None
    system_sizing: Any = None
    if idf_objects is not None:
        for object_type, fields in idf_objects:
            if object_type.casefold() == "simulationcontrol":
                zone_sizing = fields[0] if len(fields) > 0 else None
                system_sizing = fields[1] if len(fields) > 1 else None
                break
    elif epjson is not None:
        controls = epjson.get("SimulationControl")
        if isinstance(controls, dict) and controls:
            control = next(iter(controls.values()))
            if isinstance(control, dict):
                zone_sizing = control.get("do_zone_sizing_calculation")
                system_sizing = control.get("do_system_sizing_calculation")

    def is_disabled(value: Any) -> bool:
        return value is False or str(value).strip().casefold() in {"no", "false", "0"}

    if not (is_disabled(zone_sizing) and is_disabled(system_sizing)):
        return []
    return [
        _review_message(
            severity="warning",
            code="ENERGYPLUS_REVIEW_HVAC_SIZING_DISABLED",
            text=(
                "SimulationControl disables both zone and system sizing; "
                "review autosized HVAC fields before relying on results."
            ),
            category="sizing-autosizing",
        ),
    ]


def _check_schedule_coverage(
    idf_objects: list[tuple[str, list[str]]] | None,
    epjson: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Check that each Schedule:Week:Daily names all seven day schedules."""

    incomplete: list[str] = []
    if idf_objects is not None:
        for object_type, fields in idf_objects:
            if object_type.casefold() != "schedule:week:daily":
                continue
            name = fields[0] if fields else "<unnamed>"
            day_fields = fields[1:8]
            if len(day_fields) < 7 or any(not field.strip() for field in day_fields):
                incomplete.append(name)
    elif epjson is not None:
        schedules = epjson.get("Schedule:Week:Daily")
        day_keys = (
            "sunday_schedule_day_name",
            "monday_schedule_day_name",
            "tuesday_schedule_day_name",
            "wednesday_schedule_day_name",
            "thursday_schedule_day_name",
            "friday_schedule_day_name",
            "saturday_schedule_day_name",
        )
        if isinstance(schedules, dict):
            for name, schedule in schedules.items():
                if not isinstance(schedule, dict) or any(
                    not str(schedule.get(key, "")).strip() for key in day_keys
                ):
                    incomplete.append(str(name))

    if not incomplete:
        return []
    return [
        _review_message(
            severity="warning",
            code="ENERGYPLUS_REVIEW_SCHEDULE_COVERAGE",
            text=(
                "Schedule:Week:Daily objects do not cover every weekday: "
                + ", ".join(incomplete[:10])
            ),
            category="schedule-coverage",
        ),
    ]


def run_idf_checks(model_file: Path, checks: list[str]) -> list[dict[str, Any]]:
    """Run selected Validibot checks against the normalized model copy."""

    if not checks:
        return []
    try:
        if model_file.suffix.lower() == ".epjson":
            loaded = json.loads(model_file.read_text(encoding="utf-8"))
            epjson = loaded if isinstance(loaded, dict) else None
            idf_objects = None
        else:
            epjson = None
            idf_objects = _idf_objects(
                model_file.read_text(encoding="utf-8", errors="replace"),
            )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return [
            _review_message(
                severity="error",
                code="ENERGYPLUS_REVIEW_CHECK_PARSE_FAILED",
                text=f"Validibot could not parse the model for preflight checks: {exc}",
                category="preflight-check-failure",
            ),
        ]

    check_functions = {
        "duplicate-names": _check_duplicate_names,
        "hvac-sizing": _check_hvac_sizing,
        "schedule-coverage": _check_schedule_coverage,
    }
    messages: list[dict[str, Any]] = []
    for check in checks:
        check_function = check_functions.get(check)
        if check_function is None:
            messages.append(
                _review_message(
                    severity="warning",
                    code="ENERGYPLUS_REVIEW_CHECK_UNSUPPORTED",
                    text=f"Unsupported EnergyPlus review check: {check}",
                    category="unsupported-review-check",
                ),
            )
            continue
        messages.extend(check_function(idf_objects, epjson))
    return messages


def run_energyplus_simulation(
    input_envelope: EnergyPlusInputEnvelope,
) -> tuple[EnergyPlusOutputs, Path, list[dict]]:
    """
    Run EnergyPlus simulation based on input envelope.

    Args:
        input_envelope: Typed input envelope with files and configuration

    Returns:
        Tuple of:
        - EnergyPlus outputs including returncode, metrics, logs, and file paths
        - Working directory containing artifacts to upload
        - List of parsed error/warning messages from .err file

    Raises:
        ValueError: If required input files are missing
        RuntimeError: If EnergyPlus execution fails critically
    """
    start_time = time.time()

    # The envelope's attempt identity, rather than its parent run, owns scratch
    # state. Exclusive creation makes stale in-container state a hard conflict.
    work_dir = create_attempt_work_dir(
        attempt_scratch_base("energyplus_run"),
        str(input_envelope.context.execution_attempt_id),
    )

    logger.info("Working directory: %s", work_dir)

    installation = _collect_installation_evidence()

    # Download input files from GCS
    logger.info("Downloading input files...")
    model_file, weather_file = _download_input_files(input_envelope, work_dir)

    # The submitted artifact remains untouched. All Validibot normalization and
    # review checks operate on a private copy that is retained as run evidence.
    working_model = _prepare_model_working_copy(
        model_file,
        work_dir,
        input_envelope.inputs.timestep_per_hour,
    )
    idf_version = _parse_model_version(working_model)
    version_match = _versions_match(
        idf_version,
        installation.binary_version,
        installation.idd_version,
    )
    review_messages = run_idf_checks(
        working_model,
        list(input_envelope.inputs.idf_checks),
    )

    # Run EnergyPlus
    logger.info(
        "Running EnergyPlus %s...",
        "simulation" if input_envelope.inputs.run_simulation else "preflight",
    )
    returncode, stdout, stderr = _run_energyplus(
        model_file=working_model,
        weather_file=weather_file,
        work_dir=work_dir,
        config=input_envelope.inputs,
        installation=installation,
    )

    execution_seconds = time.time() - start_time

    # Collect output file paths
    err_path = work_dir / "eplusout.err" if (work_dir / "eplusout.err").exists() else None
    outputs = EnergyPlusSimulationOutputs(
        eplusout_sql=work_dir / "eplusout.sql" if (work_dir / "eplusout.sql").exists() else None,
        eplusout_err=err_path,
        eplusout_csv=work_dir / "eplusout.csv" if (work_dir / "eplusout.csv").exists() else None,
        eplusout_eso=work_dir / "eplusout.eso" if (work_dir / "eplusout.eso").exists() else None,
    )

    # Extract metrics from SQL database
    logger.info("Extracting metrics...")
    metrics = _extract_metrics(outputs.eplusout_sql)

    # Collect logs
    logs = EnergyPlusSimulationLogs(
        stdout_tail=stdout[-STDOUT_TAIL_CHARS:] if stdout else None,
        stderr_tail=stderr[-STDOUT_TAIL_CHARS:] if stderr else None,
        err_tail=_read_err_tail(err_path),
    )

    # Parse error messages from .err file
    logger.info("Parsing error messages...")
    parsed_messages = parse_err_file(err_path)
    warning_count, severe_count, fatal_count = _count_err_issues(err_path)
    parsed_messages = [*review_messages, *parsed_messages]

    profile_messages = _profile_required_output_messages(
        review_profile=input_envelope.inputs.review_profile,
        run_simulation=input_envelope.inputs.run_simulation,
        version_match=version_match,
        has_sql_output=outputs.eplusout_sql is not None,
        metrics=metrics,
    )
    parsed_messages.extend(profile_messages)
    review_issue_count = sum(
        1 for message in parsed_messages if "energyplus-review" in message.get("tags", [])
    )
    completed_successfully = returncode == 0 and fatal_count == 0
    if parsed_messages:
        logger.info("Found %d error/warning messages in .err file", len(parsed_messages))

    logger.info(
        "Simulation complete (returncode=%d, duration=%.2fs)",
        returncode,
        execution_seconds,
    )

    return (
        EnergyPlusOutputs(
            outputs=outputs,
            metrics=metrics,
            logs=logs,
            energyplus_returncode=returncode,
            execution_seconds=execution_seconds,
            invocation_mode=input_envelope.inputs.invocation_mode,
            energyplus_binary_version=installation.binary_version,
            energyplus_binary_build=installation.binary_build,
            idd_version=installation.idd_version,
            idd_build=installation.idd_build,
            idd_path=str(installation.idd_path),
            idf_version=idf_version,
            version_match=version_match,
            completed_successfully=completed_successfully,
            warning_count=warning_count,
            severe_count=severe_count,
            fatal_count=fatal_count,
            review_issue_count=review_issue_count,
            has_sql_output=outputs.eplusout_sql is not None,
            has_err_output=outputs.eplusout_err is not None,
            has_csv_output=outputs.eplusout_csv is not None,
            has_eso_output=outputs.eplusout_eso is not None,
        ),
        work_dir,
        parsed_messages,
    )


def _download_input_files(
    input_envelope: EnergyPlusInputEnvelope,
    work_dir: Path,
) -> tuple[Path, Path | None]:
    """
    Download input files and resource files to working directory.

    Input files (submission data) come from input_envelope.input_files.
    Resource files (weather, etc.) come from input_envelope.resource_files.

    Args:
        input_envelope: Input envelope with file URIs
        work_dir: Local working directory

    Returns:
        Tuple of (primary model file, optional weather file)

    Raises:
        ValueError: If primary model file or weather file is missing
    """
    model_file = None
    weather_file = None

    # Download input files (submission data)
    for file_item in input_envelope.input_files:
        logger.info("Downloading input file: %s (role=%s)", file_item.name, file_item.role)

        destination = work_dir / file_item.name
        download_verified_file(file_item, destination)

        # Track primary model file
        if file_item.role == "primary-model":
            model_file = destination
        # Legacy: also check input_files for weather (backwards compatibility)
        if file_item.role == "weather":
            weather_file = destination

    # Download resource files (weather, libraries, etc.)
    resource_files = getattr(input_envelope, "resource_files", []) or []
    for resource in resource_files:
        logger.info(
            "Downloading resource file: %s (type=%s)",
            resource.id,
            resource.type,
        )

        destination = work_dir / resource.name

        try:
            download_verified_file(resource, destination)
        except ValueError as exc:
            if resource.type == "energyplus_weather":
                raise ValueError(
                    f"Weather file missing or unreadable at {resource.uri}",
                ) from exc
            raise

        # Track weather file from resource_files
        if resource.type == "energyplus_weather":
            weather_file = destination

    if model_file is None:
        raise ValueError("No primary-model file found in input_files")

    if weather_file is None and input_envelope.inputs.run_simulation:
        raise ValueError(
            "No weather file found. Provide weather via resource_files "
            "(type='energyplus_weather') or input_files (role='weather')."
        )

    return model_file, weather_file


def _run_energyplus(
    model_file: Path,
    weather_file: Path | None,
    work_dir: Path,
    config,
    installation: EnergyPlusInstallationEvidence,
) -> tuple[int, str, str]:
    """
    Execute EnergyPlus simulation.

    Args:
        model_file: Path to IDF or epJSON model file
        weather_file: Path to the EPW weather file for full simulations
        work_dir: Working directory for simulation
        config: EnergyPlusInputs configuration

    Returns:
        Tuple of (returncode, stdout, stderr)

    Raises:
        UnsafeModelObjectError: If the defense-in-depth scan rejects the model
            because it contains a high-risk IDF/epJSON object type.
    """
    # Defense-in-depth: inspect the user-supplied model before invoking the
    # native binary. The container is still the primary boundary; this is a
    # cheap second layer that fails closed on code/library/network/file-loading
    # object types unless ALLOW_UNSAFE_IDF_OBJECTS is explicitly enabled.
    _scan_model_for_unsafe_objects(model_file)

    # Build EnergyPlus command
    cmd = [
        str(installation.binary_path),
        "--idd",
        str(installation.idd_path),
        "--output-directory",
        str(work_dir),
    ]

    if config.run_simulation:
        if weather_file is None:
            raise ValueError("A weather file is required for full EnergyPlus simulation")
        cmd.extend(["--weather", str(weather_file)])
    else:
        cmd.append("--convert-only")

    cmd.append(str(model_file))

    logger.info("Executing: %s", " ".join(cmd))

    # Run EnergyPlus
    result = subprocess.run(
        cmd,
        check=False,
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour timeout
    )

    logger.info("EnergyPlus returncode: %d", result.returncode)

    return result.returncode, result.stdout, result.stderr


def _extract_metrics(sql_path: Path | None) -> EnergyPlusSimulationMetrics:
    """
    Extract metrics from EnergyPlus SQL output database.

    Args:
        sql_path: Path to eplusout.sql file (or None if not generated)

    Returns:
        Extracted metrics (may be empty if SQL is missing)
    """
    if sql_path is None or not sql_path.exists():
        logger.warning("No SQL database found, cannot extract metrics")
        return EnergyPlusSimulationMetrics()

    try:
        with sqlite3.connect(sql_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            def fetch_tabular_metric(
                report: str,
                table: str,
                row: str,
                column: str,
                *,
                report_for: str = "Entire Facility",
                units: str | None = None,
            ) -> float | None:
                """Fetch a single value from TabularDataWithStrings.

                EnergyPlus 25.x stores tabular data with integer foreign keys
                in ``TabularData`` and provides a convenience view
                ``TabularDataWithStrings`` that resolves them.  Column names
                in the view do NOT include units (e.g. ``"Electricity"``
                rather than ``"Electricity [kWh]"``).  Energy values are in
                GJ; callers must convert to kWh when needed.
                """
                query = """
                    SELECT Value AS metric_value
                    FROM TabularDataWithStrings
                    WHERE ReportName = ?
                      AND ReportForString = ?
                      AND TableName = ?
                      AND RowName = ?
                      AND ColumnName = ?
                """
                params: list[str] = [report, report_for, table, row, column]
                if units is not None:
                    query += " AND Units = ?"
                    params.append(units)
                query += " LIMIT 1"
                result = cursor.execute(query, params).fetchone()
                if not result:
                    return None
                try:
                    return float(result["metric_value"])
                except (TypeError, ValueError):
                    return None

            def fetch_first(
                candidates: list[tuple[str, str, str, str]],
            ) -> float | None:
                """Return the first available metric across EnergyPlus table aliases."""

                for candidate in candidates:
                    value = fetch_tabular_metric(*candidate)
                    if value is not None:
                        return value
                return None

            def sum_end_use_row(row_name: str) -> float | None:
                """Sum all energy fuel columns for an End Uses row (in GJ).

                The End Uses table has separate columns per fuel type
                (Electricity, Natural Gas, District Cooling, District
                Heating Water, etc.).  For a total we sum all energy
                columns, excluding Water (m3).
                """
                result = cursor.execute(
                    """
                    SELECT SUM(CAST(Value AS REAL)) AS total_gj
                    FROM TabularDataWithStrings
                    WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary'
                      AND ReportForString = 'Entire Facility'
                      AND TableName = 'End Uses'
                      AND RowName = ?
                      AND Units = 'GJ'
                    """,
                    (row_name,),
                ).fetchone()
                if not result or result["total_gj"] is None:
                    return None
                return float(result["total_gj"])

            def total_fuel_gj(column_names: tuple[str, ...]) -> float | None:
                """Sum named fuel columns without treating absence as zero."""

                placeholders = ", ".join("?" for _ in column_names)
                result = cursor.execute(
                    f"""
                    SELECT SUM(CAST(Value AS REAL)) AS total_gj
                    FROM TabularDataWithStrings
                    WHERE ReportName = 'AnnualBuildingUtilityPerformanceSummary'
                      AND ReportForString = 'Entire Facility'
                      AND TableName = 'End Uses'
                      AND RowName = 'Total End Uses'
                      AND Units = 'GJ'
                      AND ColumnName IN ({placeholders})
                    """,
                    column_names,
                ).fetchone()
                if not result or result["total_gj"] is None:
                    return None
                return float(result["total_gj"])

            # Total site energy by fuel type (GJ → kWh).
            elec_gj = fetch_tabular_metric(
                "AnnualBuildingUtilityPerformanceSummary",
                "End Uses",
                "Total End Uses",
                "Electricity",
                units="GJ",
            )
            gas_gj = fetch_tabular_metric(
                "AnnualBuildingUtilityPerformanceSummary",
                "End Uses",
                "Total End Uses",
                "Natural Gas",
                units="GJ",
            )
            district_cooling_gj = total_fuel_gj(("District Cooling",))

            # Newer versions split district heating water/steam. Older versions
            # expose one combined District Heating column; never add both forms.
            district_heating_gj = total_fuel_gj(
                ("District Heating Water", "District Heating Steam"),
            )
            if district_heating_gj is None:
                district_heating_gj = total_fuel_gj(("District Heating",))

            building_area = fetch_tabular_metric(
                "AnnualBuildingUtilityPerformanceSummary",
                "Building Area",
                "Net Conditioned Building Area",
                "Area",
                units="m2",
            )

            # EUI uses every GJ-valued site fuel. Water is excluded by units.
            # The explicit aggregate keeps total-site derivations identical to
            # the EUI numerator when uncommon fuels appear in a model.
            total_site_gj = sum_end_use_row("Total End Uses")
            named_site_gj = sum(
                value
                for value in (
                    elec_gj,
                    gas_gj,
                    district_cooling_gj,
                    district_heating_gj,
                )
                if value is not None
            )
            other_fuels_gj = (
                max(total_site_gj - named_site_gj, 0.0) if total_site_gj is not None else None
            )
            total_site_kwh = total_site_gj * GJ_TO_KWH if total_site_gj is not None else None
            eui = (
                total_site_kwh / building_area
                if total_site_kwh is not None and building_area is not None and building_area > 0
                else None
            )

            end_use_gj = {
                "heating_energy_kwh": sum_end_use_row("Heating"),
                "cooling_energy_kwh": sum_end_use_row("Cooling"),
                "interior_lighting_kwh": sum_end_use_row("Interior Lighting"),
                "fans_energy_kwh": sum_end_use_row("Fans"),
                "pumps_energy_kwh": sum_end_use_row("Pumps"),
                "water_systems_kwh": sum_end_use_row("Water Systems"),
            }

            unmet_heating_hours = fetch_first(
                [
                    (
                        "AnnualBuildingUtilityPerformanceSummary",
                        "Comfort and Setpoint Not Met Summary",
                        "Time Setpoint Not Met During Occupied Heating",
                        "Facility",
                    ),
                    (
                        "SystemSummary",
                        "Time Setpoint Not Met",
                        "Facility",
                        "During Occupied Heating",
                    ),
                ],
            )
            unmet_cooling_hours = fetch_first(
                [
                    (
                        "AnnualBuildingUtilityPerformanceSummary",
                        "Comfort and Setpoint Not Met Summary",
                        "Time Setpoint Not Met During Occupied Cooling",
                        "Facility",
                    ),
                    (
                        "SystemSummary",
                        "Time Setpoint Not Met",
                        "Facility",
                        "During Occupied Cooling",
                    ),
                ],
            )
            peak_electric_demand_w = fetch_tabular_metric(
                "DemandEndUseComponentsSummary",
                "End Uses",
                "Total End Uses",
                "Electricity",
                units="W",
            )

            # Window envelope metrics from output variables (not tabular data).
            # These require Output:Variable objects in the IDF; if absent the
            # ReportDataDictionary won't have matching rows and we get None.
            window_heat_gain = _fetch_output_variable_sum(
                cursor,
                "Surface Window Heat Gain Energy",
            )
            window_heat_loss = _fetch_output_variable_sum(
                cursor,
                "Surface Window Heat Loss Energy",
            )
            window_transmitted_solar = _fetch_output_variable_sum(
                cursor,
                "Surface Window Transmitted Solar Radiation Energy",
            )

            _log_sql_errors(cursor)

    except Exception as exc:  # pragma: no cover - defensive, metrics best-effort
        logger.warning("Failed to extract metrics from SQL: %s", exc)
        return EnergyPlusSimulationMetrics()

    return EnergyPlusSimulationMetrics(
        site_electricity_kwh=(elec_gj * GJ_TO_KWH if elec_gj is not None else None),
        site_natural_gas_kwh=(gas_gj * GJ_TO_KWH if gas_gj is not None else None),
        site_district_cooling_kwh=(
            district_cooling_gj * GJ_TO_KWH if district_cooling_gj is not None else None
        ),
        site_district_heating_kwh=(
            district_heating_gj * GJ_TO_KWH if district_heating_gj is not None else None
        ),
        site_other_fuels_kwh=(other_fuels_gj * GJ_TO_KWH if other_fuels_gj is not None else None),
        site_eui_kwh_m2=eui,
        simulated_conditioned_area_m2=building_area,
        unmet_heating_hours=unmet_heating_hours,
        unmet_cooling_hours=unmet_cooling_hours,
        peak_electric_demand_w=peak_electric_demand_w,
        window_heat_gain_kwh=window_heat_gain,
        window_heat_loss_kwh=window_heat_loss,
        window_transmitted_solar_kwh=window_transmitted_solar,
        **{
            key: value * GJ_TO_KWH if value is not None else None
            for key, value in end_use_gj.items()
        },
    )


# Preferred reporting frequency order for output variable summation.
# "Run Period" gives a single annual total per key and avoids summing
# thousands of timestep rows.  We pick exactly one frequency to avoid
# double-counting when an IDF requests the same variable at multiple
# frequencies (each gets a separate ReportDataDictionaryIndex).
_FREQUENCY_PREFERENCE = [
    "Run Period",
    "Monthly",
    "Daily",
    "Hourly",
    "Zone Timestep",
    "HVAC System Timestep",
]

# Joules → kWh conversion factor
_J_TO_KWH = 1.0 / 3_600_000.0


def _fetch_output_variable_sum(
    cursor: sqlite3.Cursor,
    variable_name: str,
) -> float | None:
    """
    Sum an EnergyPlus output variable across all key values (surfaces) for
    the annual run period, converting Joules to kWh.

    EnergyPlus stores output variable data in two tables:
    - ReportDataDictionary: maps variable name + key + frequency → index
    - ReportData: stores timestep values keyed by that index

    Each reporting frequency requested in the IDF creates a *separate*
    dictionary entry.  To avoid double-counting we pick exactly one
    frequency, preferring "Run Period" (already annual) and falling back
    to finer granularities.

    Returns None if the variable was not requested in the IDF (no matching
    dictionary entries).
    """
    # Check whether the ReportDataDictionary table exists at all
    table_check = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ReportDataDictionary'",
    ).fetchone()
    if not table_check:
        return None

    # Find all frequencies available for this variable
    freq_rows = cursor.execute(
        """
        SELECT DISTINCT ReportingFrequency
        FROM ReportDataDictionary
        WHERE Name = ? AND IsMeter = 0
        """,
        (variable_name,),
    ).fetchall()

    if not freq_rows:
        return None

    available = {r[0] for r in freq_rows}

    # Pick exactly one frequency (best available)
    chosen = None
    for pref in _FREQUENCY_PREFERENCE:
        if pref in available:
            chosen = pref
            break
    if chosen is None:
        # Unknown frequency string — pick first available as last resort
        chosen = next(iter(available))

    # Sum values across all key values (surfaces) for the chosen frequency
    result = cursor.execute(
        """
        SELECT SUM(rd.Value)
        FROM ReportData rd
        JOIN ReportDataDictionary rdd
          ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
        WHERE rdd.Name = ?
          AND rdd.ReportingFrequency = ?
          AND rdd.IsMeter = 0
        """,
        (variable_name, chosen),
    ).fetchone()

    if result is None or result[0] is None:
        return None

    total_joules = float(result[0])
    return total_joules * _J_TO_KWH


def _log_sql_errors(cursor: sqlite3.Cursor) -> None:
    """Log non-info errors from the EnergyPlus SQL Errors table if present."""
    try:
        cols = cursor.execute("PRAGMA table_info('Errors')").fetchall()
        if not cols:
            return
        rows = cursor.execute("SELECT * FROM Errors").fetchall()
        if not rows:
            return
        columns = [c[1] if isinstance(c, tuple) else c[0] for c in cols]
        for row in rows:
            data = {columns[i]: row[i] for i in range(len(columns))}
            severity = str(
                data.get("Severity") or data.get("ErrorType") or data.get("Level") or ""
            ).lower()
            if severity == "info":
                continue
            message = str(
                data.get("Message") or data.get("ErrorMessage") or data.get("Text") or row
            )
            context = data.get("Context") or data.get("Context1") or data.get("Context2")
            logger.warning(
                "EnergyPlus SQL error [%s]: %s%s",
                severity or "unknown",
                message,
                f" (context: {context})" if context else "",
            )
    except Exception:  # pragma: no cover - best effort
        logger.debug("Could not log SQL errors", exc_info=True)


def _read_err_tail(err_path: Path | None, max_lines: int = 200) -> str | None:
    """
    Read tail of eplusout.err file.

    Args:
        err_path: Path to eplusout.err file (or None if not generated)
        max_lines: Maximum number of lines to read from end of file

    Returns:
        Tail of err file, or None if file doesn't exist
    """
    if err_path is None or not err_path.exists():
        return None

    try:
        with err_path.open("r") as f:
            lines = f.readlines()
            tail_lines = lines[-max_lines:] if len(lines) > max_lines else lines
            return "".join(tail_lines)
    except Exception as e:
        logger.warning("Failed to read err file: %s", e)
        return None


_ERR_CLASSIFIERS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "version-idd-mismatch",
        "ENERGYPLUS_REVIEW_VERSION_IDD_MISMATCH",
        re.compile(r"(?:version|idd).*(?:mismatch|different|incorrect)", re.IGNORECASE),
    ),
    (
        "duplicate-object-names",
        "ENERGYPLUS_REVIEW_DUPLICATE_OBJECT_NAME",
        re.compile(r"(?:duplicate.*name|name.*already exists)", re.IGNORECASE),
    ),
    (
        "invalid-object-reference",
        "ENERGYPLUS_REVIEW_INVALID_OBJECT_REFERENCE",
        re.compile(
            r"(?:object reference|referenced object|did not find|not found|invalid reference)",
            re.IGNORECASE,
        ),
    ),
    (
        "schedule-coverage",
        "ENERGYPLUS_REVIEW_SCHEDULE_COVERAGE",
        re.compile(r"schedule.*(?:coverage|missing|invalid|not found|day)", re.IGNORECASE),
    ),
    (
        "sizing-autosizing",
        "ENERGYPLUS_REVIEW_SIZING_AUTOSIZING",
        re.compile(r"(?:autosiz|sizing)", re.IGNORECASE),
    ),
    (
        "weather-design-day",
        "ENERGYPLUS_REVIEW_WEATHER_DESIGN_DAY",
        re.compile(r"(?:weather|design[ -]?day|epw)", re.IGNORECASE),
    ),
    (
        "unmet-hours-comfort",
        "ENERGYPLUS_REVIEW_UNMET_HOURS_COMFORT",
        re.compile(r"(?:setpoint not met|unmet|not comfortable|comfort)", re.IGNORECASE),
    ),
    (
        "output-report-configuration",
        "ENERGYPLUS_REVIEW_OUTPUT_REPORT_CONFIGURATION",
        re.compile(r"(?:output:|report.*(?:missing|not found)|requested output)", re.IGNORECASE),
    ),
    (
        "deprecated-object-field",
        "ENERGYPLUS_REVIEW_DEPRECATED_OBJECT_FIELD",
        re.compile(r"(?:deprecated|obsolete)", re.IGNORECASE),
    ),
    (
        "convergence-runtime",
        "ENERGYPLUS_REVIEW_CONVERGENCE_RUNTIME",
        re.compile(r"(?:convergence|did not converge|iteration limit)", re.IGNORECASE),
    ),
)


def _classify_err_message(message: dict[str, Any]) -> dict[str, Any]:
    """Attach a stable reviewer code/tag when an EnergyPlus message is known."""

    text = str(message.get("text", ""))
    for category, code, pattern in _ERR_CLASSIFIERS:
        if pattern.search(text):
            message["code"] = code
            message["category"] = category
            message["tags"] = ["energyplus-review", category]
            return message
    message["tags"] = ["energyplus"]
    return message


def _count_err_issues(err_path: Path | None) -> tuple[int, int, int]:
    """Count warnings, severe errors, and fatal markers without UI deduping.

    EnergyPlus's terminal summary includes recurring warning/severe counts that
    may exceed the number of individually rendered markers. Prefer that summary
    when present and retain marker counting as the failure/preflight fallback.
    """

    if err_path is None or not err_path.exists():
        return 0, 0, 0
    try:
        content = err_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0, 0
    patterns = (
        re.compile(r"^\s*\*\*\s*Warning\s*\*\*", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^\s*\*\*\s*Severe\s*\*\*", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^\s*\*\*\s*Fatal\s*\*\*", re.IGNORECASE | re.MULTILINE),
    )
    marker_counts = tuple(len(pattern.findall(content)) for pattern in patterns)
    summaries = re.findall(
        r"EnergyPlus\s+(?:Completed Successfully|Terminated).*?"
        r"(\d+)\s+Warning(?:s)?;\s*(\d+)\s+Severe Errors",
        content,
        re.IGNORECASE,
    )
    if not summaries:
        return marker_counts  # type: ignore[return-value]
    warning_count, severe_count = summaries[-1]
    return int(warning_count), int(severe_count), marker_counts[2]


def _profile_required_output_messages(
    *,
    review_profile: str,
    run_simulation: bool,
    version_match: bool | None,
    has_sql_output: bool,
    metrics: EnergyPlusSimulationMetrics,
) -> list[dict[str, Any]]:
    """Report missing/mismatched evidence with profile-specific severity."""

    strict = review_profile == "leed_review"
    messages: list[dict[str, Any]] = []
    severity = "error" if strict else "warning"

    if version_match is not True:
        messages.append(
            _review_message(
                severity=severity,
                code=(
                    "ENERGYPLUS_REVIEW_VERSION_MISMATCH"
                    if version_match is False
                    else "ENERGYPLUS_REVIEW_VERSION_EVIDENCE_MISSING"
                ),
                text=(
                    "The model Version does not match the EnergyPlus binary and IDD."
                    if version_match is False
                    else "Complete model, EnergyPlus binary, and IDD version evidence is unavailable."
                ),
                category="version-idd-mismatch",
            ),
        )

    if not run_simulation:
        return messages

    if not has_sql_output:
        messages.append(
            _review_message(
                severity=severity,
                code="ENERGYPLUS_REVIEW_SQL_OUTPUT_MISSING",
                text="The simulation did not produce eplusout.sql; metrics are unavailable.",
                category="output-report-configuration",
            ),
        )

    if metrics.simulated_conditioned_area_m2 is None:
        messages.append(
            _review_message(
                severity=severity,
                code="ENERGYPLUS_REVIEW_FLOOR_AREA_MISSING",
                text="EnergyPlus did not report conditioned floor area for EUI review.",
                category="missing-required-output",
            ),
        )

    if strict:
        strict_metrics = {
            "site_eui_kwh_m2": metrics.site_eui_kwh_m2,
            "unmet_heating_hours": metrics.unmet_heating_hours,
            "unmet_cooling_hours": metrics.unmet_cooling_hours,
        }
        for field_name, value in strict_metrics.items():
            if value is not None:
                continue
            messages.append(
                _review_message(
                    severity="error",
                    code="ENERGYPLUS_LEED_REQUIRED_OUTPUT_MISSING",
                    text=f"LEED review profile requires output {field_name!r}.",
                    category="missing-required-output",
                ),
            )
    return messages


def parse_err_file(err_path: Path | None) -> list[dict]:
    """
    Parse EnergyPlus .err file and extract error/warning messages.

    EnergyPlus .err files contain lines like:
        ** Warning ** Some warning message
        ** Severe  ** Some severe error message
        **  Fatal  ** Some fatal error message

    Multi-line messages are continued on subsequent lines until the next
    marker or summary section.

    Args:
        err_path: Path to eplusout.err file (or None if not generated)

    Returns:
        List of dicts with keys: severity, text, code (optional)
    """
    if err_path is None or not err_path.exists():
        return []

    messages: list[dict] = []

    try:
        content = err_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("Failed to read err file for parsing: %s", e)
        return []

    # Pattern to match EnergyPlus error markers
    # Examples:
    #   ** Warning ** message text
    #   ** Severe  ** message text
    #   **  Fatal  ** message text

    # Split into lines and process
    lines = content.split("\n")
    current_message: dict[str, Any] | None = None
    seen_messages: set[str] = set()  # Dedupe messages

    def save_current() -> None:
        nonlocal current_message
        if current_message and current_message["text"] not in seen_messages:
            seen_messages.add(current_message["text"])
            messages.append(_classify_err_message(current_message))
        current_message = None

    for line in lines:
        # Check for error markers
        warning_match = re.match(r"\s*\*\*\s*Warning\s*\*\*\s*(.*)", line, re.IGNORECASE)
        severe_match = re.match(r"\s*\*\*\s*Severe\s*\*\*\s*(.*)", line, re.IGNORECASE)
        fatal_match = re.match(r"\s*\*\*\s*Fatal\s*\*\*\s*(.*)", line, re.IGNORECASE)

        # Skip summary lines (start with many asterisks)
        if line.strip().startswith("*************"):
            save_current()
            continue

        # Skip "...Summary of Errors" section
        if "Summary of Errors" in line or "Reference severe error" in line:
            save_current()
            continue

        if fatal_match:
            save_current()
            current_message = {
                "severity": "error",  # Fatal maps to error
                "text": fatal_match.group(1).strip(),
                "code": "ENERGYPLUS_FATAL",
                "kind": "fatal",
            }
        elif severe_match:
            save_current()
            current_message = {
                "severity": "error",  # Severe maps to error
                "text": severe_match.group(1).strip(),
                "code": "ENERGYPLUS_SEVERE",
                "kind": "severe",
            }
        elif warning_match:
            save_current()
            current_message = {
                "severity": "warning",
                "text": warning_match.group(1).strip(),
                "code": "ENERGYPLUS_WARNING",
                "kind": "warning",
            }
        elif current_message and line.strip():
            # Continuation of previous message (multi-line errors)
            # Only append if it looks like content, not a separator
            stripped = line.strip()
            if not stripped.startswith("~"):
                current_message["text"] += " " + stripped

    # Don't forget the last message
    save_current()

    return messages
