"""Carrier-normalization tests for Portfolio Manager reports.

The backend advertises XLSX, XML, and legacy XLS carriers but exposes one
carrier-neutral property model. These tests use small deterministic fixtures to
prove metric aliases, opaque IDs, decimal values, and formula rejection before
the higher-level collection policy runs.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from validator_backends.portfolio_manager.parser import (
    PortfolioManagerParseError,
    parse_report_bytes,
)


def _xlsx_bytes(*, formula: bool = False, property_id: str = "00123") -> bytes:
    """Create a minimal Portfolio Manager-like custom report workbook."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["ENERGY STAR Portfolio Manager Custom Report"])
    sheet.append(
        [
            "Portfolio Manager Property ID",
            "Property Name",
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
            "Library",
            "2025-12-31",
            100_000,
            42.5,
            "=40+1" if formula else 39.5,
            "WA-0001",
        ]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_xlsx_custom_report_normalizes_energy_metrics() -> None:
    """Common EPA headings become stable typed fields used by CEL and EBL logic."""
    records = parse_report_bytes(_xlsx_bytes(), filename="report.xlsx")

    assert len(records) == 1
    assert records[0].property_id == "00123"
    assert str(records[0].weather_normalized_site_eui_kbtu_ft2_yr) == "39.5"
    assert records[0].washington_standard_id == "WA-0001"


def test_xlsx_formula_cells_are_rejected() -> None:
    """Submitted formulas cannot make validation depend on a spreadsheet engine cache."""
    with pytest.raises(PortfolioManagerParseError, match="Formula"):
        parse_report_bytes(_xlsx_bytes(formula=True), filename="report.xlsx")


def test_portfolio_manager_xml_normalizes_property_elements() -> None:
    """API-style XML and spreadsheet reports converge on the same property model."""
    content = b"""<?xml version="1.0"?>
    <response>
      <property>
        <propertyId>987</propertyId>
        <propertyName>City Hall</propertyName>
        <yearEnding>2025-12-31</yearEnding>
        <grossFloorArea>75000</grossFloorArea>
        <siteEUI>51.2</siteEUI>
        <weatherNormalizedSiteEUI>48.1</weatherNormalizedSiteEUI>
        <standardId name="State of Washington Clean Buildings Standard">
          <id>WA-987</id>
        </standardId>
      </property>
    </response>
    """

    records = parse_report_bytes(content, filename="property.xml")

    assert records[0].property_id == "987"
    assert str(records[0].site_eui_kbtu_ft2_yr) == "51.2"
    assert records[0].washington_standard_id == "WA-987"


def test_legacy_html_xls_normalizes_the_same_report_contract() -> None:
    """HTML-table downloads carrying an XLS suffix remain non-executing input."""
    content = b"""<!doctype html><html><body><table>
      <tr><th>Portfolio Manager Property ID</th>
          <th>Site EUI (kBtu/ft2)</th></tr>
      <tr><td>00042</td><td>41.7</td></tr>
    </table></body></html>"""

    records = parse_report_bytes(content, filename="report.xls")

    assert records[0].property_id == "00042"
    assert str(records[0].site_eui_kbtu_ft2_yr) == "41.7"


def test_generic_row_column_xml_is_supported() -> None:
    """Custom-report XML row/column carriers map labels instead of guessing tags."""
    content = b"""<?xml version="1.0"?>
    <report>
      <row>
        <column name="Portfolio Manager Property ID">701</column>
        <column name="Weather Normalized Site EUI (kBtu/ft2)">37.2</column>
      </row>
    </report>"""

    records = parse_report_bytes(content, filename="report.xml")

    assert records[0].property_id == "701"
    assert str(records[0].weather_normalized_site_eui_kbtu_ft2_yr) == "37.2"


def test_unknown_metric_columns_do_not_become_custom_ids() -> None:
    """An arbitrary metric heading must not be misrepresented as identity evidence."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "Portfolio Manager Property ID",
            "Site EUI (kBtu/ft2)",
            "Unrelated Metric",
            "Custom ID 1",
        ]
    )
    sheet.append(["9", "30", "not-an-id", "CITY-9"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()

    record = parse_report_bytes(buffer.getvalue(), filename="report.xlsx")[0]

    assert "Unrelated Metric" not in record.custom_ids
    assert record.custom_ids["Custom ID 1"] == "CITY-9"
