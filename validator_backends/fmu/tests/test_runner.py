"""Unit tests for FMU runner helpers (pure functions only)."""

from __future__ import annotations

import numpy as np

from validator_backends.fmu import runner


def test_collect_output_values_prefers_result_fields() -> None:
    """Result arrays should populate outputs with final values."""
    # Build a structured array similar to fmpy output
    dtype = [("time", "f4"), ("y", "f4")]
    data = np.array([(0.0, 1.0), (1.0, 2.5)], dtype=dtype)

    values = runner._collect_output_values(  # type: ignore[attr-defined]
        result=data,
        outputs=["y", "missing"],
        fallback_inputs={"y": 0, "missing": 9},
    )

    assert values["y"] == 2.5
    assert values["missing"] == 9


def test_resolve_sim_time_uses_last_time_column() -> None:
    """Simulation time resolution should read the last time entry when present."""
    dtype = [("time", "f4"), ("y", "f4")]
    data = np.array([(0.0, 1.0), (1.5, 3.0)], dtype=dtype)
    assert runner._resolve_sim_time(data, default_stop=10.0) == 1.5  # type: ignore[attr-defined]


def test_discover_output_variables_reads_causality(tmp_path) -> None:
    """Outputs are discovered by causality=output in modelDescription.xml."""
    xml = """
    <fmiModelDescription>
      <ModelVariables>
        <ScalarVariable name="a" causality="parameter"><Real /></ScalarVariable>
        <ScalarVariable name="b" causality="output"><Real /></ScalarVariable>
        <ScalarVariable name="c" causality="input"><Real /></ScalarVariable>
      </ModelVariables>
    </fmiModelDescription>
    """
    fmu_path = tmp_path / "modelDescription.xml"
    fmu_path.write_text(xml)

    metadata = {
        "variables": [
            type("V", (), {"name": "a", "causality": "parameter"})(),
            type("V", (), {"name": "b", "causality": "output"})(),
            type("V", (), {"name": "c", "causality": "input"})(),
        ]
    }
    outputs = runner._extract_output_variables(metadata)  # type: ignore[attr-defined]
    assert outputs == ["b"]
