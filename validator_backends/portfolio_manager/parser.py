"""Bounded carrier readers and canonical Portfolio Manager normalization."""

from __future__ import annotations

import io
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

import xlrd
from defusedxml import ElementTree as DefusedElementTree
from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel

from validibot_shared.portfolio_manager import PortfolioManagerPropertyResult


if TYPE_CHECKING:
    from collections.abc import Sequence

_MAX_ROWS = 20_000
_MAX_COLUMNS = 500
_MAX_XML_ELEMENTS = 200_000
_MAX_XML_DEPTH = 200
_MAX_CELL_TEXT = 4_096


class PortfolioManagerParseError(ValueError):
    """A report carrier is safe to read but not a recognized report."""


def _key(value: object) -> str:
    """Normalize report headings and XML names without guessing their meaning."""
    text = str(value or "").casefold().replace("²", "2")
    return re.sub(r"[^a-z0-9]+", "", text)


_ALIASES: dict[str, set[str]] = {
    "property_id": {
        "portfoliomanagerpropertyid",
        "propertyid",
        "propertynumber",
    },
    "property_name": {"propertyname", "name"},
    "parent_property_id": {"parentpropertyid", "parentid"},
    "reporting_period_start": {
        "reportingperiodstartdate",
        "reportingperiodstartingdate",
        "periodstartdate",
        "startdate",
    },
    "reporting_period_end": {
        "reportingperiodenddate",
        "reportingperiodendingdate",
        "yearending",
        "periodenddate",
        "enddate",
    },
    "property_type": {
        "primarypropertytype",
        "propertytype",
        "primaryfunction",
    },
    "gross_floor_area_ft2": {
        "propertygfaselfreportedft2",
        "propertygrossfloorareaselfreportedft2",
        "grossfloorareaft2",
        "grossfloorarea",
        "propertygfa",
    },
    "site_eui_kbtu_ft2_yr": {
        "siteeuikbtuft2",
        "siteeuikbtuft2yr",
        "siteeui",
    },
    "weather_normalized_site_eui_kbtu_ft2_yr": {
        "weathernormalizedsiteeuikbtuft2",
        "weathernormalizedsiteeuikbtuft2yr",
        "weathernormalizedsiteeui",
        "wneui",
    },
    "source_eui_kbtu_ft2_yr": {
        "sourceeuikbtuft2",
        "sourceeuikbtuft2yr",
        "sourceeui",
    },
    "national_median_site_eui_kbtu_ft2_yr": {
        "nationalmediansiteeuikbtuft2",
        "nationalmediansiteeuikbtuft2yr",
        "nationalmediansiteeui",
    },
    "site_energy_use_kbtu": {
        "siteenergyusekbtu",
        "siteenergykbtu",
        "siteenergyuse",
    },
    "weather_normalized_site_energy_use_kbtu": {
        "weathernormalizedsiteenergyusekbtu",
        "weathernormalizedsiteenergykbtu",
        "weathernormalizedsiteenergyuse",
    },
    "weather_normalized_site_electricity_kwh": {
        "weathernormalizedsiteelectricitykwh",
        "weathernormalizedsiteelectricity",
    },
    "weather_normalized_site_electricity_intensity_kwh_ft2": {
        "weathernormalizedsiteelectricityintensitykwhft2",
        "weathernormalizedsiteelectricityintensity",
    },
    "weather_normalized_site_natural_gas_therms": {
        "weathernormalizedsitenaturalgasusetherms",
        "weathernormalizedsitenaturalgastherms",
        "weathernormalizedsitenaturalgasuse",
    },
    "weather_normalized_site_natural_gas_intensity_therms_ft2": {
        "weathernormalizedsitenaturalgasintensitythermsft2",
        "weathernormalizedsitenaturalgasintensity",
    },
    "onsite_renewable_electricity_generated_kwh": {
        "electricityusegeneratedfromonsiterenewablesystemskwh",
        "electricitygeneratedfromonsiterenewablesystemskwh",
    },
    "onsite_renewable_electricity_exported_kwh": {
        "electricityusegeneratedfromonsiterenewablesystemsandexportedkwh",
        "electricitygeneratedfromonsiterenewablesystemsandexportedkwh",
    },
    "electricity_grid_and_onsite_renewable_kbtu": {
        "electricityusegridpurchaseandgeneratedfromonsiterenewablesystemskbtu",
    },
    "electricity_grid_purchase_kbtu": {
        "electricityusegridpurchasekbtu",
    },
    "onsite_renewable_electricity_used_onsite_kbtu": {
        "electricityusegeneratedfromonsiterenewablesystemsandusedonsitekbtu",
    },
    "natural_gas_use_kbtu": {
        "naturalgasusekbtu",
    },
    "percent_electricity_from_onsite_renewables": {
        "percentoftotalelectricitygeneratedfromonsiterenewablesystems",
        "percentelectricitygeneratedfromonsiterenewablesystems",
    },
    "energy_star_score": {"energystarscore", "score"},
    "heating_degree_days": {"heatingdegreedays", "hdd"},
    "cooling_degree_days": {"coolingdegreedays", "cdd"},
    "weather_station_id": {"weatherstationid"},
    "weather_station_name": {"weatherstationname"},
    "washington_standard_id": {
        "stateofwashingtoncleanbuildingsstandard",
        "washingtoncleanbuildingsstandardid",
    },
}
_ALIAS_TO_FIELD = {alias: field for field, aliases in _ALIASES.items() for alias in aliases}
_DECIMAL_FIELDS = {
    "gross_floor_area_ft2",
    "site_eui_kbtu_ft2_yr",
    "weather_normalized_site_eui_kbtu_ft2_yr",
    "source_eui_kbtu_ft2_yr",
    "national_median_site_eui_kbtu_ft2_yr",
    "site_energy_use_kbtu",
    "weather_normalized_site_energy_use_kbtu",
    "weather_normalized_site_electricity_kwh",
    "weather_normalized_site_electricity_intensity_kwh_ft2",
    "weather_normalized_site_natural_gas_therms",
    "weather_normalized_site_natural_gas_intensity_therms_ft2",
    "onsite_renewable_electricity_generated_kwh",
    "onsite_renewable_electricity_exported_kwh",
    "electricity_grid_and_onsite_renewable_kbtu",
    "electricity_grid_purchase_kbtu",
    "onsite_renewable_electricity_used_onsite_kbtu",
    "natural_gas_use_kbtu",
    "percent_electricity_from_onsite_renewables",
    "energy_star_score",
    "heating_degree_days",
    "cooling_degree_days",
}
_DATE_FIELDS = {"reporting_period_start", "reporting_period_end"}
_MISSING = {"", "n/a", "na", "not available", "not applicable", "--", "none", "null"}


def parse_report_bytes(
    content: bytes,
    *,
    filename: str,
) -> list[PortfolioManagerPropertyResult]:
    """Parse one supported report into exactly its canonical property records."""
    suffix = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if suffix == "xlsx":
        rows = _xlsx_rows(content)
        return _records_from_rows(rows, filename=filename, carrier="xlsx")
    if suffix == "xls":
        prefix = content.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if prefix.startswith((b"<?xml", b"<workbook", b"<ss:workbook")):
            rows = _spreadsheetml_rows(content)
        elif prefix.startswith((b"<!doctype html", b"<html", b"<table")):
            rows = _html_table_rows(content)
        elif content.startswith(b"PK\x03\x04"):
            rows = _xlsx_rows(content)
        else:
            rows = _xls_rows(content)
        return _records_from_rows(rows, filename=filename, carrier="xls")
    if suffix == "xml":
        return _xml_records(content, filename=filename)
    raise PortfolioManagerParseError(
        f"Unsupported report extension for {filename!r}; expected .xls, .xlsx, or .xml"
    )


def _xlsx_rows(content: bytes) -> list[list[Any]]:
    """Read OOXML in formula-visible, read-only mode."""
    try:
        workbook = load_workbook(
            io.BytesIO(content),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
    except Exception as exc:
        raise PortfolioManagerParseError(f"Could not read OOXML workbook: {exc}") from exc
    try:
        matrices: list[list[Any]] = []
        for sheet in workbook.worksheets:
            if sheet.max_row > _MAX_ROWS or sheet.max_column > _MAX_COLUMNS:
                raise PortfolioManagerParseError("Workbook exceeds row or column limits")
            for row in sheet.iter_rows(values_only=True):
                values = list(row)
                if any(isinstance(value, str) and value.startswith("=") for value in values):
                    raise PortfolioManagerParseError(
                        "Formula cells are not accepted in Portfolio Manager exports"
                    )
                matrices.append(values)
        return matrices
    finally:
        workbook.close()


def _xls_rows(content: bytes) -> list[list[Any]]:
    """Read a legacy BIFF workbook from bounded in-memory bytes."""
    try:
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
    except Exception as exc:
        raise PortfolioManagerParseError(f"Could not read legacy XLS workbook: {exc}") from exc
    rows: list[list[Any]] = []
    try:
        for sheet in workbook.sheets():
            if sheet.nrows > _MAX_ROWS or sheet.ncols > _MAX_COLUMNS:
                raise PortfolioManagerParseError("Workbook exceeds row or column limits")
            for row_index in range(sheet.nrows):
                values: list[Any] = []
                for cell in sheet.row(row_index):
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        value = datetime(*xlrd.xldate_as_tuple(value, workbook.datemode))
                    values.append(value)
                rows.append(values)
    finally:
        workbook.release_resources()
    return rows


def _safe_xml_root(content: bytes):
    """Parse XML with entity expansion disabled and explicit shape limits."""
    try:
        root = DefusedElementTree.fromstring(content)
    except Exception as exc:
        raise PortfolioManagerParseError(f"Could not parse Portfolio Manager XML: {exc}") from exc
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > _MAX_XML_ELEMENTS:
            raise PortfolioManagerParseError("XML exceeds the element limit")
        if depth > _MAX_XML_DEPTH:
            raise PortfolioManagerParseError("XML exceeds the nesting-depth limit")
        stack.extend((child, depth + 1) for child in list(element))
    return root


def _spreadsheetml_rows(content: bytes) -> list[list[Any]]:
    """Read the XML Spreadsheet carrier sometimes served with an .xls suffix."""
    root = _safe_xml_root(content)
    rows: list[list[Any]] = []
    for row in root.iter():
        if _local_name(row.tag).casefold() != "row":
            continue
        values: list[Any] = []
        for cell in list(row):
            if _local_name(cell.tag).casefold() != "cell":
                continue
            data = next(
                (child for child in cell.iter() if _local_name(child.tag).casefold() == "data"),
                None,
            )
            values.append(data.text if data is not None else "")
        rows.append(values)
    if not rows:
        raise PortfolioManagerParseError("SpreadsheetML workbook contains no rows")
    return rows


class _BoundedHTMLTableParser(HTMLParser):
    """Extract the first report-like HTML table without executing anything."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        """Begin bounded row and cell buffers."""
        del attrs
        normalized = tag.casefold()
        if normalized == "tr":
            self._row = []
        elif normalized in {"td", "th"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        """Collect text only while inside a cell."""
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Commit complete cells and rows while enforcing parser limits."""
        normalized = tag.casefold()
        if normalized in {"td", "th"} and self._cell_parts is not None:
            value = " ".join(self._cell_parts).strip()
            if len(value) > _MAX_CELL_TEXT:
                raise PortfolioManagerParseError("HTML workbook cell is too large")
            if self._row is not None:
                self._row.append(value)
                if len(self._row) > _MAX_COLUMNS:
                    raise PortfolioManagerParseError(
                        "HTML workbook exceeds the column limit"
                    )
            self._cell_parts = None
        elif normalized == "tr" and self._row is not None:
            self.rows.append(self._row)
            if len(self.rows) > _MAX_ROWS:
                raise PortfolioManagerParseError("HTML workbook exceeds the row limit")
            self._row = None


def _html_table_rows(content: bytes) -> list[list[Any]]:
    """Read legacy HTML-table workbooks sometimes downloaded with an XLS suffix."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("windows-1252")
        except UnicodeDecodeError as exc:
            raise PortfolioManagerParseError(
                "HTML workbook uses an unsupported character encoding"
            ) from exc
    parser = _BoundedHTMLTableParser()
    try:
        parser.feed(text)
        parser.close()
    except (PortfolioManagerParseError, ValueError) as exc:
        raise PortfolioManagerParseError(f"Could not read HTML workbook: {exc}") from exc
    if not parser.rows:
        raise PortfolioManagerParseError("HTML workbook contains no table rows")
    return parser.rows


def _xml_records(
    content: bytes,
    *,
    filename: str,
) -> list[PortfolioManagerPropertyResult]:
    """Normalize a Portfolio Manager XML response or XML custom report."""
    root = _safe_xml_root(content)
    if _local_name(root.tag).casefold() == "workbook":
        return _records_from_rows(
            _spreadsheetml_rows(content),
            filename=filename,
            carrier="xml",
        )

    tabular_records = []
    for element in root.iter():
        if _key(_local_name(element.tag)) not in {"row", "record"}:
            continue
        mapping = _mapping_from_xml_row(element)
        if mapping.get("property_id"):
            tabular_records.append(
                _record_from_mapping(
                    mapping,
                    filename=filename,
                    carrier="xml",
                )
            )
    if tabular_records:
        return tabular_records

    candidates = [
        element
        for element in root.iter()
        if _key(_local_name(element.tag)) in {"property", "propertymetrics"}
        and _flatten_xml(element).get("property_id")
    ]
    if not candidates:
        flattened = _flatten_xml(root)
        if flattened.get("property_id"):
            candidates = [root]
    if not candidates:
        raise PortfolioManagerParseError(
            "XML is not a recognized Portfolio Manager property report"
        )
    return [
        _record_from_mapping(
            _flatten_xml(element),
            filename=filename,
            carrier="xml",
        )
        for element in candidates
    ]


def _mapping_from_xml_row(element) -> dict[str, Any]:
    """Read generic row/column XML emitted by custom-report download paths."""
    result: dict[str, Any] = {}
    alert_states: dict[str, str] = {}
    custom_ids: dict[str, str] = {}
    for node in element.iter():
        if node is element or list(node):
            continue
        heading = next(
            (
                str(attribute_value)
                for attribute_name, attribute_value in node.attrib.items()
                if _key(_local_name(attribute_name))
                in {"name", "metric", "metricname", "heading", "label"}
            ),
            _local_name(node.tag),
        )
        value = (node.text or "").strip()
        field = _ALIAS_TO_FIELD.get(_key(heading))
        if field:
            result[field] = value
        elif _is_alert_heading(heading):
            alert_states[heading] = _alert_state(value)
        elif _is_identity_heading(heading) and value:
            custom_ids[heading] = value
    result["alert_states"] = alert_states
    result["custom_ids"] = custom_ids
    return result


def _flatten_xml(element) -> dict[str, Any]:
    """Flatten recognized metric leaves and named IDs below one property node."""
    result: dict[str, Any] = {}
    custom_ids: dict[str, str] = {}
    for node in element.iter():
        children = list(node)
        local = _local_name(node.tag)
        normalized = _key(local)
        value = (node.text or "").strip()
        if children and normalized not in {"standardid", "customid"}:
            continue
        field = _ALIAS_TO_FIELD.get(normalized)
        if field and value:
            result[field] = value
        if normalized in {"standardid", "customid"}:
            name = next(
                (
                    str(attribute_value)
                    for attribute_name, attribute_value in node.attrib.items()
                    if _key(_local_name(attribute_name)) in {"name", "type"}
                ),
                "",
            )
            id_value = value or next(
                (
                    (child.text or "").strip()
                    for child in children
                    if _key(_local_name(child.tag)) in {"id", "value"}
                ),
                "",
            )
            if name and id_value:
                custom_ids[name] = id_value
                if _key(name) == "stateofwashingtoncleanbuildingsstandard":
                    result["washington_standard_id"] = id_value
    result["custom_ids"] = custom_ids
    return result


def _records_from_rows(
    rows: Sequence[Sequence[Any]],
    *,
    filename: str,
    carrier: str,
) -> list[PortfolioManagerPropertyResult]:
    """Discover a metric header and normalize every nonblank data row below it."""
    header_index = -1
    mapped_headers: list[str | None] = []
    for index, row in enumerate(rows[:100]):
        candidates = [_ALIAS_TO_FIELD.get(_key(value)) for value in row]
        known = sum(candidate is not None for candidate in candidates)
        if "property_id" in candidates and known >= 2:
            header_index = index
            mapped_headers = candidates
            break
    if header_index < 0:
        raise PortfolioManagerParseError(
            "Workbook does not contain a recognized Portfolio Manager report header"
        )

    records: list[PortfolioManagerPropertyResult] = []
    for row in rows[header_index + 1 :]:
        if not any(_text(value) for value in row):
            continue
        mapping: dict[str, Any] = {}
        alert_states: dict[str, str] = {}
        for column, field in enumerate(mapped_headers):
            if column >= len(row):
                continue
            raw = row[column]
            header_text = str(rows[header_index][column] or "")
            if field:
                mapping[field] = raw
            elif _is_alert_heading(header_text):
                alert_states[header_text.strip()] = _alert_state(raw)
            elif _is_identity_heading(header_text) and _text(raw):
                mapping.setdefault("custom_ids", {})[header_text.strip()] = _text(raw)
        mapping["alert_states"] = alert_states
        if not _text(mapping.get("property_id")):
            continue
        if _key(mapping["property_id"]) in _ALIASES["property_id"]:
            continue
        records.append(_record_from_mapping(mapping, filename=filename, carrier=carrier))
    if not records:
        raise PortfolioManagerParseError("Portfolio Manager report contains no property row")
    return records


def _record_from_mapping(
    mapping: dict[str, Any],
    *,
    filename: str,
    carrier: str,
) -> PortfolioManagerPropertyResult:
    """Coerce one carrier mapping into the canonical property model."""
    values: dict[str, Any] = {
        "member_name": filename,
        "carrier": carrier,
        "property_id": _text(mapping.get("property_id")),
        "property_name": _text(mapping.get("property_name")),
        "parent_property_id": _text(mapping.get("parent_property_id")),
        "property_type": _text(mapping.get("property_type")),
        "weather_station_id": _text(mapping.get("weather_station_id")),
        "weather_station_name": _text(mapping.get("weather_station_name")),
        "washington_standard_id": _text(mapping.get("washington_standard_id")),
        "custom_ids": {
            _text(name): _text(value)
            for name, value in (mapping.get("custom_ids") or {}).items()
            if _text(name) and _text(value)
        },
        "alert_states": mapping.get("alert_states") or {},
    }
    metric_states: dict[str, str] = {}
    for field in _DECIMAL_FIELDS:
        raw = mapping.get(field)
        parsed = _decimal(raw)
        values[field] = parsed
        if field not in mapping:
            metric_states[field] = "absent"
        elif parsed is not None:
            metric_states[field] = "value"
        elif _text(raw).casefold() in _MISSING:
            metric_states[field] = "not_available"
        else:
            metric_states[field] = "invalid"
    values["metric_states"] = metric_states
    for field in _DATE_FIELDS:
        raw = mapping.get(field)
        parsed = _date(raw)
        values[field] = parsed
        if field not in mapping:
            metric_states[field] = "absent"
        elif parsed is not None:
            metric_states[field] = "value"
        elif _text(raw).casefold() in _MISSING:
            metric_states[field] = "not_available"
        else:
            metric_states[field] = "invalid"
    if not values["property_id"]:
        raise PortfolioManagerParseError("A property row is missing Portfolio Manager Property ID")
    return PortfolioManagerPropertyResult.model_validate(values)


def _decimal(value: Any) -> Decimal | None:
    """Parse Portfolio Manager numeric display values without binary floats."""
    text = _text(value)
    if text.casefold() in _MISSING:
        return None
    cleaned = (
        text.replace(",", "")
        .replace("$", "")
        .replace("%", "")
        .replace("\N{MINUS SIGN}", "-")
        .strip()
    )
    match = re.match(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)", cleaned)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _date(value: Any) -> date | None:
    """Parse native spreadsheet dates and common Portfolio Manager displays."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and value > 0:
        try:
            converted = from_excel(value)
            return converted.date() if isinstance(converted, datetime) else converted
        except (TypeError, ValueError, OverflowError):
            pass
    text = _text(value)
    if text.casefold() in _MISSING:
        return None
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%Y"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=UTC)
            if pattern == "%Y":
                return date(parsed.year, 12, 31)
            return parsed.date()
        except ValueError:
            continue
    return None


def _alert_state(value: Any) -> str:
    """Normalize report alert cells without treating absence as a clean result."""
    text = _text(value).casefold()
    if not text:
        return "not_verifiable"
    if text in {"n/a", "na", "not available", "not applicable", "--"}:
        return "not_verifiable"
    if text in {"no", "none", "false", "0", "ok", "pass", "passed"}:
        return "clean"
    return "alert"


def _is_identity_heading(value: object) -> bool:
    """Recognize identity columns without treating every unknown metric as an ID."""
    normalized = _key(value)
    return normalized.startswith("customid") or normalized.startswith("standardid")


def _is_alert_heading(value: object) -> bool:
    """Recognize EPA Alert Metrics and the estimated-energy evidence field."""
    normalized = _key(value)
    return "alert" in normalized or (
        "estimated" in normalized and "energy" in normalized
    )


def _text(value: Any) -> str:
    """Return trimmed display text while preserving opaque identifiers."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _local_name(tag: str) -> str:
    """Strip Clark-notation or prefixed XML namespaces."""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]
