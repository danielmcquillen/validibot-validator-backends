"""
Integration-style FMU test using the Feedthrough.fmu fixture.

This exercises the runner against a real FMU (no network, local file copy). It
monkeypatches download_file to copy the fixture into the working directory,
builds a typed FMUInputEnvelope, and asserts the output echoes the input for
the known feedthrough variable.

Two FMU fixtures are available:
- Feedthrough.fmu: FMI 2.0, x86_64 only (darwin64)
- Feedthrough_fmi3_arm64.fmu: FMI 3.0, includes aarch64-darwin for Apple Silicon
"""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

import pytest

from validator_backends.fmu import runner
from validibot_shared.fmu.envelopes import FMUInputEnvelope, FMUInputs, FMUOutputs
from validibot_shared.validations.envelopes import (
    ExecutionContext,
    InputFileItem,
    SupportedMimeType,
)


def _is_apple_silicon() -> bool:
    """Check if running on Apple Silicon (ARM64 macOS)."""
    return platform.system().lower() == "darwin" and platform.machine().lower() in (
        "arm64",
        "aarch64",
    )


@pytest.mark.integration
def test_feedthrough_fmu_echoes_input_x86(monkeypatch, tmp_path) -> None:
    """Run FMI 2.0 Feedthrough.fmu (x86_64) and assert Int32_output matches Int32_input."""
    if _is_apple_silicon():
        pytest.skip("Feedthrough.fmu (FMI 2.0) is x86_64-only; skip on Apple Silicon.")

    fixture = Path(__file__).parent / "assets" / "Feedthrough.fmu"
    assert fixture.exists(), "Feedthrough.fmu fixture missing"

    def _fake_download(uri: str, dest: Path) -> None:
        shutil.copy(Path(uri), dest)

    monkeypatch.setattr("validator_backends.fmu.runner.download_file", _fake_download)

    envelope = FMUInputEnvelope(
        run_id="test-run",
        validator={"id": "1", "type": "FMU", "version": "1"},
        org={"id": "1", "name": "Test Org"},
        workflow={"id": "1", "step_id": "1", "step_name": "Feedthrough"},
        input_files=[
            InputFileItem(
                name="model.fmu",
                mime_type=SupportedMimeType.FMU,
                role="fmu",
                uri=str(fixture),
            )
        ],
        inputs=FMUInputs(
            input_values={"Int32_input": 5},
            output_variables=["Int32_output"],
        ),
        context=ExecutionContext(
            callback_url="http://example.com",
            execution_bundle_uri=str(tmp_path),
        ),
    )

    outputs, _ = runner.run_fmu_simulation(envelope)
    assert isinstance(outputs, FMUOutputs)
    assert outputs.output_values["Int32_output"] == pytest.approx(5)


@pytest.mark.integration
def test_feedthrough_fmu_echoes_input_arm64(monkeypatch, tmp_path) -> None:
    """Run FMI 3.0 Feedthrough.fmu (ARM64) and assert Int32_output matches Int32_input.

    This test uses the Reference FMUs from https://github.com/modelica/Reference-FMUs
    which include native aarch64-darwin binaries for Apple Silicon.
    """
    if not _is_apple_silicon():
        pytest.skip("FMI 3.0 ARM64 test only runs on Apple Silicon.")

    fixture = Path(__file__).parent / "assets" / "Feedthrough_fmi3_arm64.fmu"
    assert fixture.exists(), "Feedthrough_fmi3_arm64.fmu fixture missing"

    def _fake_download(uri: str, dest: Path) -> None:
        shutil.copy(Path(uri), dest)

    monkeypatch.setattr("validator_backends.fmu.runner.download_file", _fake_download)

    envelope = FMUInputEnvelope(
        run_id="test-run",
        validator={"id": "1", "type": "FMU", "version": "1"},
        org={"id": "1", "name": "Test Org"},
        workflow={"id": "1", "step_id": "1", "step_name": "Feedthrough"},
        input_files=[
            InputFileItem(
                name="model.fmu",
                mime_type=SupportedMimeType.FMU,
                role="fmu",
                uri=str(fixture),
            )
        ],
        inputs=FMUInputs(
            input_values={"Int32_input": 5},
            output_variables=["Int32_output"],
        ),
        context=ExecutionContext(
            callback_url="http://example.com",
            execution_bundle_uri=str(tmp_path),
        ),
    )

    outputs, _ = runner.run_fmu_simulation(envelope)
    assert isinstance(outputs, FMUOutputs)
    assert outputs.output_values["Int32_output"] == pytest.approx(5)
