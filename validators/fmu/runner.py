"""
FMU simulation runner for Cloud Run Jobs.

Resolves the FMU from GCS, runs a short simulation with fmpy, and returns
catalog-keyed outputs suitable for the Django callback pipeline.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fmpy import read_model_description, simulate_fmu

from validators.core.gcs_client import download_file
from validibot_shared.fmu.envelopes import FMUOutputs


if TYPE_CHECKING:
    from validibot_shared.fmu.envelopes import FMUInputEnvelope

logger = logging.getLogger(__name__)


def run_fmu_simulation(input_envelope: FMUInputEnvelope) -> tuple[FMUOutputs, Path]:
    """
    Execute an FMU using fmpy and return FMUOutputs plus the working directory.

    Raises:
        ValueError: If input_envelope is missing required fields
        RuntimeError: If simulation fails
    """
    # Validate required fields
    if input_envelope is None:
        raise ValueError("input_envelope is required")
    if not input_envelope.run_id:
        raise ValueError("input_envelope.run_id is required")
    if not input_envelope.input_files:
        raise ValueError("input_envelope.input_files is required")
    if input_envelope.inputs is None:
        raise ValueError("input_envelope.inputs is required")
    if input_envelope.inputs.simulation is None:
        raise ValueError("input_envelope.inputs.simulation is required")

    start_time = time.time()
    work_dir = Path("/tmp/fmu_run") / input_envelope.run_id
    work_dir.mkdir(parents=True, exist_ok=True)

    fmu_path = _download_fmu(input_envelope, work_dir)
    if not fmu_path.exists():
        raise ValueError(f"FMU file not found at {fmu_path}")

    md = _read_model_description(fmu_path)

    sim_cfg = input_envelope.inputs.simulation
    requested_outputs = input_envelope.inputs.output_variables or []
    if not requested_outputs:
        requested_outputs = _discover_output_variables(md)

    start_values = dict(input_envelope.inputs.input_values or {})
    log_messages: list[str] = []

    try:
        # Note: fmpy 3.x requires start_values to be a dict (not None) because it
        # calls start_values.copy(). Pass empty dict if no start values provided.
        # output=None is fine (tells fmpy to use default outputs from model).
        result = simulate_fmu(
            filename=str(fmu_path),
            start_time=sim_cfg.start_time,
            stop_time=sim_cfg.stop_time,
            step_size=sim_cfg.step_size,
            output=requested_outputs if requested_outputs else None,
            start_values=start_values,  # Always pass dict, never None
            logger=log_messages.append,
        )

        sim_time_reached = _resolve_sim_time(result, sim_cfg.stop_time)
        output_values = _collect_output_values(
            result=result,
            outputs=requested_outputs,
            fallback_inputs=start_values,
        )
        execution_seconds = time.time() - start_time

        outputs = FMUOutputs(
            output_values=output_values,
            fmu_guid=md.get("guid"),
            fmi_version=md.get("fmi_version"),
            model_name=md.get("model_name"),
            execution_seconds=execution_seconds,
            simulation_time_reached=sim_time_reached,
            fmu_log="\n".join(log_messages) if log_messages else None,
        )
        return outputs, work_dir
    except Exception as exc:
        logger.exception("FMU simulation failed: %s", exc)
        execution_seconds = time.time() - start_time
        outputs = FMUOutputs(
            output_values=start_values,
            fmu_guid=md.get("guid"),
            fmi_version=md.get("fmi_version"),
            model_name=md.get("model_name"),
            execution_seconds=execution_seconds,
            simulation_time_reached=sim_cfg.start_time,
            fmu_log="\n".join(log_messages) if log_messages else str(exc),
        )
        raise RuntimeError(f"FMU simulation failed: {exc}") from exc


def _download_fmu(input_envelope, work_dir: Path) -> Path:
    """Download the FMU referenced in the input envelope to the working directory."""
    fmu_uri = None
    for file_item in input_envelope.input_files:
        if file_item.role == "fmu":
            fmu_uri = file_item.uri
            break
    if not fmu_uri:
        raise ValueError("No FMU URI found in input_files")

    target = work_dir / "model.fmu"
    download_file(fmu_uri, target)
    return target


def _discover_output_variables(fmu_path: Path) -> list[str]:
    """Backward-compat shim - kept for callers using the old signature."""
    md = _read_model_description(fmu_path)
    return _extract_output_variables(md)


def _resolve_sim_time(result, default_stop: float) -> float:
    """Extract the last simulation time if present."""
    try:
        if hasattr(result, "dtype") and "time" in result.dtype.names:
            return float(result["time"][-1])
    except Exception:
        logger.debug("Could not resolve simulation time from result.", exc_info=True)
    return default_stop


def _collect_output_values(
    *,
    result,
    outputs: list[str],
    fallback_inputs: dict[str, object],
) -> dict[str, object]:
    """
    Collect the final values for each requested output, falling back to inputs when absent.
    """
    values: dict[str, object] = {}
    for name in outputs:
        if hasattr(result, "dtype") and name in result.dtype.names:
            try:
                values[name] = result[name][-1].item()
                continue
            except Exception:
                logger.debug("Failed to extract output %s from result", name)
        if name in fallback_inputs:
            values[name] = fallback_inputs[name]
    return values


def _read_model_description(fmu_path: Path) -> dict:
    """Parse modelDescription.xml and return metadata plus variables."""
    try:
        md = read_model_description(str(fmu_path))
        return {
            "guid": getattr(md, "guid", None),
            "model_name": getattr(md, "modelName", None),
            "fmi_version": getattr(md, "fmiVersion", None),
            "variables": getattr(md, "modelVariables", []),
        }
    except Exception:
        logger.exception("Failed to read modelDescription.xml")
        return {"guid": None, "model_name": None, "fmi_version": None, "variables": []}


def _extract_output_variables(md: dict) -> list[str]:
    """Extract output variable names from parsed model description."""
    try:
        variables = md.get("variables", [])
        return [
            getattr(var, "name", "")
            for var in variables
            if getattr(var, "causality", "").lower() == "output"
        ]
    except Exception:
        logger.exception("Failed to extract output variable names")
        return []
