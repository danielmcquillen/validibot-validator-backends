"""Compatibility tests for anonymized public Portfolio Manager reports.

Synthetic unit fixtures prove individual coercion rules, but they cannot expose
carrier metadata or workbook-shape differences. This suite exercises genuine
BIFF, OOXML, multi-sheet, full-property, and report-result XML structures while
asserting that all carriers converge on the shared property result contract.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from validator_backends.portfolio_manager.parser import parse_report_bytes


ASSETS = Path(__file__).with_name("assets")


def _parse_asset(name: str):
    """Parse one immutable compatibility fixture under its real extension."""
    path = ASSETS / name
    return parse_report_bytes(path.read_bytes(), filename=path.name)


@pytest.mark.parametrize(
    "extension,expected_carrier",
    [("xls", "xls"), ("xlsx", "xlsx")],
)
def test_flat_custom_report_parses_genuine_excel_carriers(
    extension: str,
    expected_carrier: str,
) -> None:
    """Legacy BIFF and OOXML must both preserve IDs, dates, and energy metrics."""
    records = _parse_asset(f"portfolio-manager-custom-report-anonymized.{extension}")

    assert [record.property_id for record in records] == [
        "7100001",
        "7100002",
        "7100003",
    ]
    assert all(record.carrier == expected_carrier for record in records)
    assert records[0].property_name == "Anonymized Building 1"
    assert records[0].parent_property_id == ""
    assert records[0].reporting_period_end == date(2025, 12, 31)
    assert str(records[0].gross_floor_area_ft2) == "100000"
    assert str(records[0].site_eui_kbtu_ft2_yr) == "41.2"
    assert str(records[0].weather_normalized_site_eui_kbtu_ft2_yr) == "39.5"
    assert str(records[0].source_eui_kbtu_ft2_yr) == "91.4"
    assert str(records[0].energy_star_score) == "82"
    assert records[0].custom_ids["Custom Property ID 1 - ID"] == "VB-7100001-6"


def test_flat_excel_carriers_produce_equivalent_canonical_records() -> None:
    """Changing only the Excel container must not change downstream CEL facts."""
    legacy = _parse_asset("portfolio-manager-custom-report-anonymized.xls")
    ooxml = _parse_asset("portfolio-manager-custom-report-anonymized.xlsx")

    assert [record.model_dump(exclude={"carrier", "member_name"}) for record in legacy] == [
        record.model_dump(exclude={"carrier", "member_name"}) for record in ooxml
    ]


def test_data_request_ignores_monthly_usage_as_property_rows() -> None:
    """The property metrics sheet is authoritative; monthly rows are not buildings."""
    records = _parse_asset("portfolio-manager-data-request-anonymized.xlsx")

    assert len(records) == 4
    assert [record.property_id for record in records] == [
        "8100001",
        "8100002",
        "8100003",
        "8100004",
    ]
    assert [str(record.site_eui_kbtu_ft2_yr) for record in records] == [
        "41.2",
        "51.2",
        "61.2",
        "71.2",
    ]
    assert all(record.reporting_period_end == date(2025, 12, 31) for record in records)


def test_full_property_export_joins_property_identity_sheet() -> None:
    """A one-property multi-sheet export exposes its primary facts and custom IDs."""
    records = _parse_asset("portfolio-manager-full-property-anonymized.xlsx")

    assert len(records) == 1
    record = records[0]
    assert record.property_id == "8200001"
    assert record.property_name == "Anonymized Building 1"
    assert record.property_type == "Office"
    assert str(record.gross_floor_area_ft2) == "100000"
    assert record.washington_standard_id == "WA-VB-8200001"
    assert record.custom_ids["State of Washington Clean Buildings Standard"] == "WA-VB-8200001"
    assert record.site_eui_kbtu_ft2_yr is None
    assert record.metric_states["site_eui_kbtu_ft2_yr"] == "absent"


def test_report_data_xml_maps_named_metrics_and_ignores_monthly_duplicates() -> None:
    """Official-style metric elements normalize once per information record."""
    records = _parse_asset("portfolio-manager-custom-report-anonymized.xml")

    assert len(records) == 3
    assert [record.property_id for record in records] == [
        "8300001",
        "8300002",
        "8300003",
    ]
    first = records[0]
    assert first.reporting_period_end == date(2025, 12, 31)
    assert str(first.gross_floor_area_ft2) == "100000"
    assert str(first.site_eui_kbtu_ft2_yr) == "41.2"
    assert str(first.weather_normalized_site_eui_kbtu_ft2_yr) == "39.5"
    assert str(first.source_eui_kbtu_ft2_yr) == "91.4"
    assert str(first.national_median_site_eui_kbtu_ft2_yr) == "61.0"
    assert str(first.energy_star_score) == "82"
    assert first.custom_ids["Validibot Fixture ID"] == "VB-8300001"
    assert first.alert_states["alertEnergyMeterGap"] == "clean"
    assert first.alert_states["alertEnergyMeterNoAssociation"] == "clean"
    assert first.alert_states["estimatedValuesEnergy"] == "clean"
    assert first.alert_states["estimatedDataFlagElectricityGridPurchase"] == "clean"


@pytest.mark.parametrize(
    "name,expected_id",
    [
        ("portfolio-manager-custom-report-single-anonymized.xls", "7100001"),
        ("portfolio-manager-custom-report-single-anonymized.xlsx", "7100001"),
        ("portfolio-manager-custom-report-single-anonymized.xml", "8300001"),
    ],
)
def test_single_property_derivatives_remain_valid_report_members(
    name: str,
    expected_id: str,
) -> None:
    """ZIP workflows need authentic one-property members for every carrier."""
    records = _parse_asset(name)

    assert len(records) == 1
    assert records[0].property_id == expected_id
