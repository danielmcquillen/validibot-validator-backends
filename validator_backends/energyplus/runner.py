"""
EnergyPlus simulation runner.

Handles downloading input files, running EnergyPlus, and extracting results.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from validator_backends.core.gcs_client import download_file
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

    # Create working directory
    work_dir = Path("/tmp/energyplus_run") / input_envelope.run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Working directory: %s", work_dir)

    # Download input files from GCS
    logger.info("Downloading input files...")
    model_file, weather_file = _download_input_files(input_envelope, work_dir)

    # Run EnergyPlus
    logger.info("Running EnergyPlus simulation...")
    returncode, stdout, stderr = _run_energyplus(
        model_file=model_file,
        weather_file=weather_file,
        work_dir=work_dir,
        config=input_envelope.inputs,
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
        ),
        work_dir,
        parsed_messages,
    )


def _download_input_files(
    input_envelope: EnergyPlusInputEnvelope,
    work_dir: Path,
) -> tuple[Path, Path]:
    """
    Download input files and resource files to working directory.

    Input files (submission data) come from input_envelope.input_files.
    Resource files (weather, etc.) come from input_envelope.resource_files.

    Args:
        input_envelope: Input envelope with file URIs
        work_dir: Local working directory

    Returns:
        Tuple of (primary model file, weather file)

    Raises:
        ValueError: If primary model file or weather file is missing
    """
    model_file = None
    weather_file = None

    # Download input files (submission data)
    for file_item in input_envelope.input_files:
        logger.info("Downloading input file: %s (role=%s)", file_item.name, file_item.role)

        destination = work_dir / file_item.name
        download_file(file_item.uri, destination)

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

        # Determine filename from URI
        uri_path = resource.uri.split("/")[-1]
        destination = work_dir / uri_path

        try:
            download_file(resource.uri, destination)
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

    if weather_file is None:
        raise ValueError(
            "No weather file found. Provide weather via resource_files "
            "(type='energyplus_weather') or input_files (role='weather')."
        )

    return model_file, weather_file


def _run_energyplus(
    model_file: Path,
    weather_file: Path,
    work_dir: Path,
    config,
) -> tuple[int, str, str]:
    """
    Execute EnergyPlus simulation.

    Args:
        model_file: Path to IDF or epJSON model file
        weather_file: Path to the EPW weather file
        work_dir: Working directory for simulation
        config: EnergyPlusInputs configuration

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    # Build EnergyPlus command
    cmd = [
        "energyplus",
        "--output-directory",
        str(work_dir),
    ]

    cmd.extend(["--weather", str(weather_file)])

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

    electricity = -1.0
    gas = -1.0
    eui = -1.0
    building_area = -1.0
    heating_gj = -1.0
    cooling_gj = -1.0
    _GJ_TO_KWH = 277.778

    try:
        with sqlite3.connect(sql_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            def fetch_tabular_metric(
                report: str,
                table: str,
                row: str,
                column: str,
            ) -> float:
                """Fetch a single value from TabularDataWithStrings.

                EnergyPlus 25.x stores tabular data with integer foreign keys
                in ``TabularData`` and provides a convenience view
                ``TabularDataWithStrings`` that resolves them.  Column names
                in the view do NOT include units (e.g. ``"Electricity"``
                rather than ``"Electricity [kWh]"``).  Energy values are in
                GJ; callers must convert to kWh when needed.
                """
                result = cursor.execute(
                    """
                    SELECT Value AS metric_value
                    FROM TabularDataWithStrings
                    WHERE ReportName = ?
                      AND TableName = ?
                      AND RowName = ?
                      AND ColumnName = ?
                    LIMIT 1
                    """,
                    (report, table, row, column),
                ).fetchone()
                if not result:
                    return -1.0
                try:
                    return float(result["metric_value"])
                except Exception:
                    return -1.0

            def _sum_end_use_row(row_name: str) -> float:
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
                      AND TableName = 'End Uses'
                      AND RowName = ?
                      AND Units = 'GJ'
                    """,
                    (row_name,),
                ).fetchone()
                if not result or result["total_gj"] is None:
                    return -1.0
                return float(result["total_gj"])

            # Total site energy by fuel type (GJ → kWh).
            elec_gj = fetch_tabular_metric(
                "AnnualBuildingUtilityPerformanceSummary",
                "End Uses",
                "Total End Uses",
                "Electricity",
            )
            gas_gj = fetch_tabular_metric(
                "AnnualBuildingUtilityPerformanceSummary",
                "End Uses",
                "Total End Uses",
                "Natural Gas",
            )
            if elec_gj >= 0:
                electricity = elec_gj * _GJ_TO_KWH
            if gas_gj >= 0:
                gas = gas_gj * _GJ_TO_KWH

            building_area = fetch_tabular_metric(
                "Entire Facility",
                "Building Area",
                "Total Building Area",
                "Area",
            )

            if electricity >= 0 and building_area > 0:
                eui = electricity / building_area

            # End-use breakdown — sum ALL fuel columns per category so
            # IdealLoadsAirSystem district heating/cooling is included.
            heating_gj = _sum_end_use_row("Heating")
            cooling_gj = _sum_end_use_row("Cooling")

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
        heating_gj = -1.0
        cooling_gj = -1.0
        window_heat_gain = None
        window_heat_loss = None
        window_transmitted_solar = None

    metrics = EnergyPlusSimulationMetrics()
    if electricity >= 0:
        metrics.site_electricity_kwh = electricity
    if gas >= 0:
        metrics.site_natural_gas_kwh = gas
    if eui >= 0:
        metrics.site_eui_kwh_m2 = eui
    if heating_gj >= 0:
        metrics.heating_energy_kwh = heating_gj * _GJ_TO_KWH
    if cooling_gj >= 0:
        metrics.cooling_energy_kwh = cooling_gj * _GJ_TO_KWH
    if window_heat_gain is not None:
        metrics.window_heat_gain_kwh = window_heat_gain
    if window_heat_loss is not None:
        metrics.window_heat_loss_kwh = window_heat_loss
    if window_transmitted_solar is not None:
        metrics.window_transmitted_solar_kwh = window_transmitted_solar
    return metrics


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
    import re

    # Split into lines and process
    lines = content.split("\n")
    current_message: dict | None = None
    seen_messages: set[str] = set()  # Dedupe messages

    for line in lines:
        # Check for error markers
        warning_match = re.match(r"\s*\*\*\s*Warning\s*\*\*\s*(.*)", line, re.IGNORECASE)
        severe_match = re.match(r"\s*\*\*\s*Severe\s*\*\*\s*(.*)", line, re.IGNORECASE)
        fatal_match = re.match(r"\s*\*\*\s*Fatal\s*\*\*\s*(.*)", line, re.IGNORECASE)

        # Skip summary lines (start with many asterisks)
        if line.strip().startswith("*************"):
            # Save current message if any
            if current_message and current_message["text"] not in seen_messages:
                seen_messages.add(current_message["text"])
                messages.append(current_message)
            current_message = None
            continue

        # Skip "...Summary of Errors" section
        if "Summary of Errors" in line or "Reference severe error" in line:
            if current_message and current_message["text"] not in seen_messages:
                seen_messages.add(current_message["text"])
                messages.append(current_message)
            current_message = None
            continue

        if fatal_match:
            # Save previous message
            if current_message and current_message["text"] not in seen_messages:
                seen_messages.add(current_message["text"])
                messages.append(current_message)
            current_message = {
                "severity": "error",  # Fatal maps to error
                "text": fatal_match.group(1).strip(),
                "code": "ENERGYPLUS_FATAL",
            }
        elif severe_match:
            if current_message and current_message["text"] not in seen_messages:
                seen_messages.add(current_message["text"])
                messages.append(current_message)
            current_message = {
                "severity": "error",  # Severe maps to error
                "text": severe_match.group(1).strip(),
                "code": "ENERGYPLUS_SEVERE",
            }
        elif warning_match:
            if current_message and current_message["text"] not in seen_messages:
                seen_messages.add(current_message["text"])
                messages.append(current_message)
            current_message = {
                "severity": "warning",
                "text": warning_match.group(1).strip(),
                "code": "ENERGYPLUS_WARNING",
            }
        elif current_message and line.strip():
            # Continuation of previous message (multi-line errors)
            # Only append if it looks like content, not a separator
            stripped = line.strip()
            if not stripped.startswith("~"):
                current_message["text"] += " " + stripped

    # Don't forget the last message
    if current_message and current_message["text"] not in seen_messages:
        messages.append(current_message)

    return messages
