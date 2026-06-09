"""
Defense-in-depth tests for the EnergyPlus model safety scan.

WHY THIS SUITE MATTERS:
The EnergyPlus backend runs a native binary against an arbitrary,
user-supplied IDF/epJSON model. Several first-class IDF object types
(``PythonPlugin``, ``ExternalInterface`` and its FMU import/export variants,
``EnergyManagementSystem:Program``/``Subroutine``, and ``Schedule:File`` with
an absolute/remote path) are effectively code or external-resource loaders.
The sandboxed container is the primary security boundary, but
``runner._scan_model_for_unsafe_objects`` adds a cheap, fail-closed second
layer that rejects such models before the binary is ever invoked. These tests
lock in that behaviour so a future refactor cannot silently let a
code-loading model reach the simulator.
"""

from __future__ import annotations

import pytest

from validator_backends.energyplus import runner


# A minimal IDF fragment whose only notable feature is a PythonPlugin object —
# the canonical "load and run arbitrary Python during simulation" capability.
_PYTHON_PLUGIN_IDF = """
  Building,
    Test Building,            !- Name
    0,                        !- North Axis {deg}
    City,                     !- Terrain
    0.04,                     !- Loads Convergence Tolerance Value
    0.4,                      !- Temperature Convergence Tolerance Value {deltaC}
    FullExterior,            !- Solar Distribution
    25;                       !- Maximum Number of Warmup Days

  PythonPlugin:Instance,
    MyAttacker,               !- Name
    Yes,                      !- Run During Warmup
    attacker_module,          !- Python Module Name
    AttackerClass;            !- Plugin Class Name
"""


def test_python_plugin_model_is_rejected(tmp_path) -> None:
    """A model containing PythonPlugin must be rejected before EnergyPlus runs.

    WHY: PythonPlugin lets a model author execute arbitrary Python inside the
    simulation process. The defense-in-depth scan must fail closed on it (the
    module default is ``ALLOW_UNSAFE_IDF_OBJECTS = False``), raising a
    ``ValueError`` subclass so the caller treats it as bad input rather than a
    retryable runtime error. This is the core regression guard for the fix.
    """
    model_file = tmp_path / "model.idf"
    model_file.write_text(_PYTHON_PLUGIN_IDF, encoding="utf-8")

    # Defensive: confirm the escape hatch is off by default so this test is
    # exercising the real production behaviour.
    assert runner.ALLOW_UNSAFE_IDF_OBJECTS is False

    with pytest.raises(runner.UnsafeModelObjectError) as exc_info:
        runner._scan_model_for_unsafe_objects(model_file)  # type: ignore[attr-defined]

    # The error must name the offending object type so an operator can act.
    assert "PythonPlugin" in str(exc_info.value)
    # It must remain a ValueError for existing input-error handling paths.
    assert isinstance(exc_info.value, ValueError)


def test_benign_model_passes_scan(tmp_path) -> None:
    """An ordinary model with no high-risk objects must pass the scan.

    WHY: The scan is deliberately conservative but must not block legitimate
    simulations. A plain IDF (including a relative-path ``Schedule:File``,
    which resolves inside the sandboxed work dir) should not be flagged, so
    we confirm the false-positive boundary holds for normal input.
    """
    benign_idf = (
        "  Building, Test, 0, City, 0.04, 0.4, FullExterior, 25;\n"
        "  Schedule:File,\n"
        "    OccSchedule,            !- Name\n"
        "    Any Number,             !- Schedule Type Limits Name\n"
        "    occupancy.csv,          !- File Name (relative, in work dir)\n"
        "    2,                      !- Column Number\n"
        "    1;                      !- Rows to Skip at Top\n"
    )
    model_file = tmp_path / "benign.idf"
    model_file.write_text(benign_idf, encoding="utf-8")

    # Should not raise.
    runner._scan_model_for_unsafe_objects(model_file)  # type: ignore[attr-defined]
