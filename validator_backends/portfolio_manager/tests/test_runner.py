"""Collection, EBL, target, and aggregate tests for Portfolio Manager.

The collection is one domain document rather than generic bulk submissions.
These tests prove that every safe member is processed, EBL reconciliation
remains observational, per-building targets override defaults, unsafe archive
members fail validation, and parent/child overlap suppresses aggregates without
silently double-counting floor area.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import TYPE_CHECKING

from openpyxl import Workbook

from validator_backends.portfolio_manager.runner import (
    property_results_artifact_json,
    run_portfolio_manager_validation,
)
from validibot_shared.portfolio_manager import (
    PortfolioManagerInputs,
    build_portfolio_manager_input_envelope,
)
from validibot_shared.validations.envelopes import (
    ATTEMPT_CONTRACT_VERSION,
    ExecutionContext,
    ResourceFileItem,
    ValidationStatus,
    ValidatorType,
)


if TYPE_CHECKING:
    from pathlib import Path


def _xlsx_bytes(
    *,
    property_id: str,
    standard_id: str,
    wneui: float,
    parent_id: str = "",
) -> bytes:
    """Create one deterministic one-property report member."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "Portfolio Manager Property ID",
            "Parent Property ID",
            "Property Name",
            "Reporting Period Starting Date",
            "Reporting Period Ending Date",
            "Property GFA - Self-Reported (ft²)",
            "Site EUI (kBtu/ft²)",
            "Weather Normalized Site EUI (kBtu/ft²)",
            "State of Washington Clean Buildings Standard",
        ]
    )
    sheet.append(
        [
            property_id,
            parent_id,
            f"Property {property_id}",
            "2025-01-01",
            "2025-12-31",
            100_000,
            wneui + 2,
            wneui,
            standard_id,
        ]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _write(path: Path, content: bytes) -> tuple[int, str, str]:
    """Write immutable local input bytes and return their envelope identity."""
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    return len(content), digest, f"sha256:{digest}"


def _envelope(
    tmp_path: Path,
    *,
    submission_name: str,
    submission_bytes: bytes,
    inputs: PortfolioManagerInputs,
    ebl: dict | None = None,
):
    """Build an exact local-storage envelope for a runner test."""
    submission_path = tmp_path / submission_name
    size, digest, version = _write(submission_path, submission_bytes)
    context = ExecutionContext(
        execution_attempt_id=f"attempt-{submission_name}",
        step_run_id=f"step-run-{submission_name}",
        attempt_contract_version=ATTEMPT_CONTRACT_VERSION,
        expected_output_uri=(tmp_path / f"{submission_name}.output.json").as_uri(),
        execution_bundle_uri=(tmp_path / "bundle").as_uri(),
        skip_callback=True,
    )
    resource = None
    if ebl is not None:
        ebl_path = tmp_path / "ebl.json"
        ebl_bytes = json.dumps(ebl).encode()
        ebl_size, ebl_digest, ebl_version = _write(ebl_path, ebl_bytes)
        resource = ResourceFileItem(
            id="ebl-1",
            name="ebl.json",
            type="portfolio_manager_ebl_v1",
            port_key="expected_buildings_list",
            uri=ebl_path.as_uri(),
            size_bytes=ebl_size,
            sha256=ebl_digest,
            storage_version=ebl_version,
        )
    validator = type(
        "Validator",
        (),
        {
            "id": "validator-1",
            "validation_type": ValidatorType.PORTFOLIO_MANAGER,
            "version": "1.0.0",
        },
    )()
    return build_portfolio_manager_input_envelope(
        run_id="run-1",
        validator=validator,
        org_id="org-1",
        org_name="Organization",
        workflow_id="workflow-1",
        step_id="step-1",
        step_name="Portfolio Manager",
        submission_name=submission_name,
        submission_uri=submission_path.as_uri(),
        submission_size_bytes=size,
        submission_sha256=digest,
        submission_storage_version=version,
        inputs=inputs,
        context=context,
        expected_buildings_list=resource,
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    """Create a ZIP collection without touching an untrusted extraction path."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_ebl_target_overrides_default_and_reconciliation_is_observational(
    tmp_path: Path,
) -> None:
    """Missing roster rows remain CEL-visible facts while matched EUIt wins precedence."""
    archive = _zip_bytes(
        {
            "one.xlsx": _xlsx_bytes(
                property_id="101",
                standard_id="WA-101",
                wneui=39,
            )
        }
    )
    ebl = {
        "schema_version": "1.0",
        "id_field": {
            "kind": "standard_id",
            "name": "State of Washington Clean Buildings Standard",
        },
        "euit_unit": "kBtu/ft2/year",
        "buildings": [
            {"id_value": "WA-101", "euit": "40"},
            {"id_value": "WA-MISSING", "euit": "50"},
        ],
    }
    envelope = _envelope(
        tmp_path,
        submission_name="reports.zip",
        submission_bytes=archive,
        inputs=PortfolioManagerInputs(
            submission_structure="zip_collection",
            default_euit_kbtu_ft2_yr="60",
            compare_to_euit=True,
        ),
        ebl=ebl,
    )

    result = run_portfolio_manager_validation(envelope)

    assert result.status == ValidationStatus.SUCCESS
    assert result.outputs.missing_expected_building_count == 1
    assert result.outputs.property_results[0].resolved_euit_source == "ebl"
    assert str(result.outputs.property_results[0].resolved_euit_kbtu_ft2_yr) == "40"
    assert result.outputs.property_results[0].meets_euit is True
    assert str(result.outputs.property_results[0].euit_percent_difference) == "2.500"
    assert result.outputs.property_results[0].near_euit is False


def test_zip_processes_valid_members_even_when_an_unsafe_member_fails(
    tmp_path: Path,
) -> None:
    """One unsafe path fails integrity but does not hide facts from safe members."""
    archive = _zip_bytes(
        {
            "safe.xlsx": _xlsx_bytes(
                property_id="201",
                standard_id="WA-201",
                wneui=45,
            ),
            "../escape.xml": b"<property><propertyId>bad</propertyId></property>",
        }
    )
    envelope = _envelope(
        tmp_path,
        submission_name="reports.zip",
        submission_bytes=archive,
        inputs=PortfolioManagerInputs(submission_structure="zip_collection"),
    )

    result = run_portfolio_manager_validation(envelope)

    assert result.status == ValidationStatus.FAILED_VALIDATION
    assert result.outputs.property_count == 1
    assert result.outputs.invalid_file_count == 1
    assert any(
        finding.code == "portfolio_manager.archive.unsafe_member"
        for finding in result.outputs.findings
    )


def test_parent_child_overlap_blocks_aggregates_without_becoming_an_error(
    tmp_path: Path,
) -> None:
    """Campus overlap is a policy fact, but totals must never double-count it."""
    archive = _zip_bytes(
        {
            "parent.xlsx": _xlsx_bytes(
                property_id="300",
                standard_id="WA-300",
                wneui=40,
            ),
            "child.xlsx": _xlsx_bytes(
                property_id="301",
                parent_id="300",
                standard_id="WA-301",
                wneui=50,
            ),
        }
    )
    envelope = _envelope(
        tmp_path,
        submission_name="reports.zip",
        submission_bytes=archive,
        inputs=PortfolioManagerInputs(submission_structure="zip_collection"),
    )

    result = run_portfolio_manager_validation(envelope)

    assert result.status == ValidationStatus.SUCCESS
    assert result.outputs.parent_child_overlap_count == 1
    assert result.outputs.aggregate_metrics_available is False
    assert result.outputs.total_gross_floor_area_ft2 is None
    assert result.outputs.weighted_weather_normalized_site_eui_kbtu_ft2_yr is None


def test_property_results_artifact_is_carrier_neutral_and_versioned(
    tmp_path: Path,
) -> None:
    """Downstream consumers receive stable JSON rather than workbook-specific cells."""
    report = _xlsx_bytes(property_id="401", standard_id="WA-401", wneui=38)
    envelope = _envelope(
        tmp_path,
        submission_name="report.xlsx",
        submission_bytes=report,
        inputs=PortfolioManagerInputs(default_euit_kbtu_ft2_yr="40"),
    )
    result = run_portfolio_manager_validation(envelope)

    payload = json.loads(property_results_artifact_json(result.outputs))

    assert payload["schema_version"] == "validibot.portfolio_manager.property_results.v1"
    assert payload["summary"]["property_count"] == 1
    assert payload["summary"]["target_met_property_count"] == 1
    assert payload["properties"][0]["property_id"] == "401"
    assert payload["properties"][0]["carrier"] == "xlsx"
    assert payload["findings"] == []


def test_logical_submission_name_never_becomes_a_local_path(tmp_path: Path) -> None:
    """A trusted envelope bug must not turn report metadata into path traversal."""
    report = _xlsx_bytes(property_id="402", standard_id="WA-402", wneui=38)
    envelope = _envelope(
        tmp_path,
        submission_name="report.xlsx",
        submission_bytes=report,
        inputs=PortfolioManagerInputs(),
    )
    envelope.input_files[0].name = "../../report.xlsx"

    result = run_portfolio_manager_validation(envelope)

    assert result.status == ValidationStatus.SUCCESS
    assert result.outputs.property_results[0].property_id == "402"


def test_near_target_is_an_advisory_band_only_for_above_target_properties(
    tmp_path: Path,
) -> None:
    """A near miss never changes the exact WNEUI-at-or-below-EUIt result."""
    report = _xlsx_bytes(property_id="501", standard_id="WA-501", wneui=42)
    envelope = _envelope(
        tmp_path,
        submission_name="report.xlsx",
        submission_bytes=report,
        inputs=PortfolioManagerInputs(
            default_euit_kbtu_ft2_yr="40",
            near_target_percent="5",
        ),
    )

    result = run_portfolio_manager_validation(envelope)
    record = result.outputs.property_results[0]

    assert record.meets_euit is False
    assert record.near_euit is True
    assert record.euit_percent_difference == -5


def test_enabled_alert_check_is_not_verifiable_when_column_is_absent(
    tmp_path: Path,
) -> None:
    """Missing Alert Metric evidence is an error, never an implicit clean result."""
    report = _xlsx_bytes(property_id="601", standard_id="WA-601", wneui=40)
    envelope = _envelope(
        tmp_path,
        submission_name="report.xlsx",
        submission_bytes=report,
        inputs=PortfolioManagerInputs(meter_gap_policy="warning"),
    )

    result = run_portfolio_manager_validation(envelope)

    assert result.status == ValidationStatus.FAILED_VALIDATION
    assert any(
        finding.code == "portfolio_manager.check_not_verifiable"
        for finding in result.outputs.findings
    )


def test_hidden_archive_metadata_is_ignored_and_empty_collection_fails(
    tmp_path: Path,
) -> None:
    """Finder metadata is not a report, but it cannot make an empty ZIP valid."""
    archive = _zip_bytes(
        {
            "__MACOSX/._report.xlsx": b"metadata",
            ".DS_Store": b"metadata",
        }
    )
    envelope = _envelope(
        tmp_path,
        submission_name="reports.zip",
        submission_bytes=archive,
        inputs=PortfolioManagerInputs(submission_structure="zip_collection"),
    )

    result = run_portfolio_manager_validation(envelope)

    assert result.status == ValidationStatus.FAILED_VALIDATION
    assert result.outputs.file_count == 0
    assert any(
        finding.code == "portfolio_manager.archive.empty"
        for finding in result.outputs.findings
    )
