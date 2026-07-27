"""XML shape and safety tests for Portfolio Manager report results.

Portfolio Manager XML appears as simple property responses, row/column reports,
and named-metric `reportData` documents. This suite focuses on namespaced
report-result behavior, missing-value evidence, custom identities, malformed
documents, and bounded XML parsing.
"""

from __future__ import annotations

import pytest

from validator_backends.portfolio_manager import parser
from validator_backends.portfolio_manager.parser import (
    PortfolioManagerParseError,
    parse_report_bytes,
)


def test_namespaced_report_data_maps_metric_names() -> None:
    """Default XML namespaces must not hide property IDs or energy metrics."""
    content = b"""<?xml version="1.0"?>
    <reportData xmlns="urn:energystar:test">
      <informationAndMetrics>
        <propertyMetrics propertyId="00901" month="12" year="2025">
          <metric name="portfolioManagerPropertyId"><value>00901</value></metric>
          <metric name="propertyName"><value>Library</value></metric>
          <metric name="siteIntensity"><value>44.2</value></metric>
        </propertyMetrics>
      </informationAndMetrics>
    </reportData>"""

    records = parse_report_bytes(content, filename="report.xml")

    assert records[0].property_id == "00901"
    assert records[0].property_name == "Library"
    assert str(records[0].site_eui_kbtu_ft2_yr) == "44.2"
    assert records[0].reporting_period_end.isoformat() == "2025-12-31"


def test_nil_metric_is_not_available_rather_than_absent() -> None:
    """A returned nil metric is evidence distinct from an omitted report column."""
    content = b"""<?xml version="1.0"?>
    <reportData xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <informationAndMetrics>
        <propertyMetrics propertyId="902" month="12" year="2025">
          <metric name="siteIntensityWN"><value xsi:nil="true"/></metric>
          <metric name="periodEndingDate"><value xsi:nil="true"/></metric>
        </propertyMetrics>
      </informationAndMetrics>
    </reportData>"""

    record = parse_report_bytes(content, filename="report.xml")[0]

    assert record.weather_normalized_site_eui_kbtu_ft2_yr is None
    assert record.reporting_period_end.isoformat() == "2025-12-31"
    assert record.metric_states["weather_normalized_site_eui_kbtu_ft2_yr"] == "not_available"


def test_custom_property_id_name_and_value_are_paired() -> None:
    """Numbered XML custom-ID metrics must become one author-selectable identity."""
    content = b"""<?xml version="1.0"?>
    <reportData>
      <informationAndMetrics>
        <propertyMetrics propertyId="903" month="12" year="2025">
          <metric name="customPropertyId1Id"><value>CITY-903</value></metric>
          <metric name="customPropertyId1Name"><value>City Building ID</value></metric>
        </propertyMetrics>
      </informationAndMetrics>
    </reportData>"""

    record = parse_report_bytes(content, filename="report.xml")[0]

    assert record.custom_ids == {"City Building ID": "CITY-903"}


def test_malformed_xml_is_a_validation_error() -> None:
    """Broken XML must fail deterministically instead of leaking parser exceptions."""
    with pytest.raises(PortfolioManagerParseError, match="Could not parse"):
        parse_report_bytes(b"<reportData>", filename="report.xml")


def test_entity_expansion_is_rejected() -> None:
    """External or expanding entities cannot be used to read files or exhaust memory."""
    content = b"""<?xml version="1.0"?>
    <!DOCTYPE reportData [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <reportData><property><propertyId>&xxe;</propertyId></property></reportData>"""

    with pytest.raises(PortfolioManagerParseError, match="Could not parse"):
        parse_report_bytes(content, filename="report.xml")


def test_xml_processing_instruction_is_rejected() -> None:
    """Stylesheet or other execution-adjacent instructions are outside the contract."""
    content = b"""<?xml version="1.0"?>
    <?xml-stylesheet href="https://example.invalid/report.xsl"?>
    <property><propertyId>901</propertyId><siteEUI>40</siteEUI></property>"""

    with pytest.raises(PortfolioManagerParseError, match="processing instructions"):
        parse_report_bytes(content, filename="report.xml")


def test_xml_external_schema_location_is_rejected() -> None:
    """A report cannot ask the backend to resolve an external XML schema."""
    content = b"""<property
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:schemaLocation="urn:test https://example.invalid/report.xsd">
      <propertyId>901</propertyId><siteEUI>40</siteEUI>
    </property>"""

    with pytest.raises(PortfolioManagerParseError, match="schema locations"):
        parse_report_bytes(content, filename="report.xml")


def test_xml_oversized_text_is_rejected() -> None:
    """One pathological source value cannot bypass the shared text bound."""
    content = (
        b"<property><propertyId>901</propertyId><propertyName>"
        + b"x" * (parser._MAX_CELL_TEXT + 1)
        + b"</propertyName><siteEUI>40</siteEUI></property>"
    )

    with pytest.raises(PortfolioManagerParseError, match="value-length"):
        parse_report_bytes(content, filename="report.xml")


def test_report_data_without_property_identity_is_rejected() -> None:
    """Metrics without a property ID cannot be reconciled or safely aggregated."""
    content = b"""<?xml version="1.0"?>
    <reportData>
      <informationAndMetrics>
        <propertyMetrics month="12" year="2025">
          <metric name="siteIntensity"><value>40</value></metric>
        </propertyMetrics>
      </informationAndMetrics>
    </reportData>"""

    with pytest.raises(PortfolioManagerParseError, match="not a recognized"):
        parse_report_bytes(content, filename="report.xml")


def test_xml_element_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured element bounds must apply before report-shape traversal."""
    monkeypatch.setattr(parser, "_MAX_XML_ELEMENTS", 2)
    content = b"<response><property><propertyId>1</propertyId></property></response>"

    with pytest.raises(PortfolioManagerParseError, match="element limit"):
        parse_report_bytes(content, filename="report.xml")


def test_xml_depth_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deeply nested XML must stop at the parser boundary."""
    monkeypatch.setattr(parser, "_MAX_XML_DEPTH", 3)
    content = b"<response><one><two><propertyId>1</propertyId></two></one></response>"

    with pytest.raises(PortfolioManagerParseError, match="nesting-depth"):
        parse_report_bytes(content, filename="report.xml")
