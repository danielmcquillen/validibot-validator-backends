"""Gross floor area semantics for Portfolio Manager reports.

Portfolio Manager exports three related floor-area columns that are easy to
conflate but mean different things:

* **Property GFA - Self-Reported (ft²)** — the buildings' gross floor area,
  which by EPA's definition *excludes* parking, outside bays, and docks.
* **Property Floor Area (Buildings and Parking) (ft²)** — the same area plus
  the parking area.
* **Property Floor Area (Parking) (ft²)** — the parking area on its own.

Only the first matches EPA's gross floor area definition, and it is the basis
Washington's Clean Buildings Performance Standard uses when it computes EUIt
from Form B floor area. The canonical ``gross_floor_area_ft2`` field therefore
means "parking-excluded", and this suite pins that meaning down.

Why it matters beyond definitional tidiness: the reported Site EUI and WNEUI
are computed by Portfolio Manager on its own parking-excluded denominator, so
these columns never distort an intensity. They distort the places where floor
area is used as a *quantity* — the GFA weight in ``_weighted_metric``,
``total_gross_floor_area_ft2``, and ``_floor_area_compliance_percent`` in the
runner. A parking garage silently inflates a property's weight in portfolio
aggregates and overstates how much regulated floor area a program covers.

The suite also guards determinism. Before these fields were separated, all
three columns aliased onto one canonical field, so which value survived
depended on the order headings or XML metrics happened to appear in — and in
the workbook carriers, a report carrying two of them tripped the
duplicate-mapping guard in ``_find_report_header`` and failed to parse at all.
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from validator_backends.portfolio_manager.parser import parse_report_bytes


SELF_REPORTED = "Property GFA - Self-Reported (ft²)"
BUILDINGS_AND_PARKING = "Property Floor Area (Buildings and Parking) (ft²)"
PARKING = "Property Floor Area (Parking) (ft²)"


def _record(columns: dict[str, object]):
    """Parse one workbook property row built from the given extra columns.

    Tests vary only the floor-area columns, so the identity and reporting
    fields every report needs are supplied here to keep each case readable.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    headings = ["Portfolio Manager Property ID", "Property Name", *columns]
    sheet.append(headings)
    sheet.append(["00123", "Library", *columns.values()])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return parse_report_bytes(buffer.getvalue(), filename="report.xlsx")[0]


def _property_metrics_xml(metrics: dict[str, object]) -> bytes:
    """Build report-result XML in Portfolio Manager's metric-name/value shape.

    The XML carriers walk metric elements in document order, which is exactly
    where an order-dependent alias would show itself.
    """
    elements = "".join(
        f'<metric name="{name}" dataType="numeric" uom="ft²"><value>{value}</value></metric>'
        for name, value in metrics.items()
    )
    return (
        '<?xml version="1.0"?>'
        "<reportData><informationAndMetrics>"
        '<propertyMetrics propertyId="8300001" month="12" year="2025">'
        '<metric name="portfolioManagerPropertyId"><value>8300001</value></metric>'
        f"{elements}"
        "</propertyMetrics>"
        "</informationAndMetrics></reportData>"
    ).encode()


# ── Precedence when a template carries more than one floor-area column ──
#
# EPA report templates are author-configurable, so a single export can easily
# carry both the self-reported and the buildings-and-parking column. The
# parking-excluded column must win every time, from either column order.


def test_self_reported_gfa_wins_when_both_columns_are_present() -> None:
    """A template offering both columns must resolve to EPA's GFA definition.

    This also covers a parsing regression: because both headings previously
    aliased onto ``gross_floor_area_ft2``, the duplicate-mapping guard in
    ``_find_report_header`` rejected the whole workbook. A real two-column
    export could not be validated at all.
    """
    record = _record(
        {
            SELF_REPORTED: 90_000,
            BUILDINGS_AND_PARKING: 100_000,
        }
    )

    assert str(record.gross_floor_area_ft2) == "90000"
    assert record.gross_floor_area_basis == "self_reported"
    assert str(record.gross_floor_area_buildings_and_parking_ft2) == "100000"


def test_self_reported_gfa_wins_regardless_of_column_order() -> None:
    """Resolution must follow an explicit rule, not header iteration order.

    Reversing the two columns is the whole point of the test: the previous
    last-writer-wins behavior made the resolved value a property of how the
    author happened to arrange the report template.
    """
    record = _record(
        {
            BUILDINGS_AND_PARKING: 100_000,
            SELF_REPORTED: 90_000,
        }
    )

    assert str(record.gross_floor_area_ft2) == "90000"
    assert record.gross_floor_area_basis == "self_reported"


def test_xml_metric_order_does_not_decide_the_gross_floor_area() -> None:
    """The XML carriers must agree with the workbook carriers on precedence.

    Report-result XML lists metrics in document order and the parser walks
    them in that order, so this is the carrier where an order-dependent alias
    was most likely to produce a different answer for the same report.
    """
    record = parse_report_bytes(
        _property_metrics_xml(
            {
                "propertyFloorAreaBuildingsAndParking": 100_000,
                "propGrossFloorArea": 90_000,
                "propertyFloorAreaParking": 10_000,
            }
        ),
        filename="report.xml",
    )[0]

    assert str(record.gross_floor_area_ft2) == "90000"
    assert record.gross_floor_area_basis == "self_reported"
    assert str(record.parking_floor_area_ft2) == "10000"


# ── A single column: what each one can and cannot prove ──
#
# Most real templates carry exactly one floor-area column. The value is used
# either way, but the recorded basis says which definition backs it so a
# downstream consumer is never misled about what the number means.


def test_parking_excluded_column_alone_reports_the_self_reported_basis() -> None:
    """The ordinary case must stay ordinary — one column, no derivation."""
    record = _record({SELF_REPORTED: 90_000})

    assert str(record.gross_floor_area_ft2) == "90000"
    assert record.gross_floor_area_basis == "self_reported"
    assert record.gross_floor_area_buildings_and_parking_ft2 is None
    assert record.parking_floor_area_ft2 is None


def test_parking_inclusive_column_alone_is_used_but_labelled_as_such() -> None:
    """A parking-inclusive-only report must remain usable, not silently wrong.

    The custom-report template used by the project's own fixtures carries only
    this column, and floor area gates ``benchmark_ready``. Refusing to resolve
    a GFA would make that whole report shape unvalidatable, so the value is
    used — but the basis records that it may include parking, which is what a
    consumer needs in order to judge an aggregate built from it.
    """
    record = _record({BUILDINGS_AND_PARKING: 100_000})

    assert str(record.gross_floor_area_ft2) == "100000"
    assert record.gross_floor_area_basis == "buildings_and_parking"
    assert record.parking_floor_area_ft2 is None


def test_absent_floor_area_columns_leave_the_basis_absent() -> None:
    """A report with no floor area must not acquire one, or a misleading basis."""
    record = _record({"Site EUI (kBtu/ft²)": 42.5})

    assert record.gross_floor_area_ft2 is None
    assert record.gross_floor_area_basis == "absent"
    assert record.metric_states["gross_floor_area_ft2"] == "absent"


# ── Correcting a parking-inclusive value with the parking column ──
#
# EPA defines buildings-and-parking as the buildings' GFA plus the parking
# area, so subtracting the parking column recovers the parking-excluded area
# exactly. This is the case that actually changes a number, so the basis makes
# the derivation visible rather than implicit.


def test_nonzero_parking_area_is_subtracted_from_the_inclusive_column() -> None:
    """A property with a garage must not carry its parking area as GFA.

    This is the reported defect in its clearest form: 100,000 ft² of buildings
    and parking with a 25,000 ft² garage is a 75,000 ft² building. Treating it
    as 100,000 overstates the regulated floor area by a third and inflates the
    property's weight in every GFA-weighted portfolio aggregate.
    """
    record = _record(
        {
            BUILDINGS_AND_PARKING: 100_000,
            PARKING: 25_000,
        }
    )

    assert str(record.gross_floor_area_ft2) == "75000"
    assert record.gross_floor_area_basis == "buildings_and_parking_less_parking"
    assert str(record.gross_floor_area_buildings_and_parking_ft2) == "100000"
    assert str(record.parking_floor_area_ft2) == "25000"
    assert record.metric_states["gross_floor_area_ft2"] == "value"


def test_zero_parking_area_still_records_a_derived_basis() -> None:
    """A reported zero is evidence of no parking, not an absent column.

    A property that explicitly reports zero parking has *proven* that its
    buildings-and-parking figure equals its GFA, which is a stronger claim
    than the same number with no parking column at all. The basis has to
    distinguish the two.
    """
    record = _record(
        {
            BUILDINGS_AND_PARKING: 100_000,
            PARKING: 0,
        }
    )

    assert str(record.gross_floor_area_ft2) == "100000"
    assert record.gross_floor_area_basis == "buildings_and_parking_less_parking"
    assert str(record.parking_floor_area_ft2) == "0"


def test_parking_area_is_captured_even_when_it_is_not_needed() -> None:
    """Parking area is an output in its own right, not just a correction term.

    Washington's floor area conventions turn on what is excluded, so a
    reviewer needs to see the parking area even when the report already
    supplies a parking-excluded GFA that makes subtracting it unnecessary.
    """
    record = _record(
        {
            SELF_REPORTED: 90_000,
            BUILDINGS_AND_PARKING: 115_000,
            PARKING: 25_000,
        }
    )

    assert str(record.gross_floor_area_ft2) == "90000"
    assert record.gross_floor_area_basis == "self_reported"
    assert str(record.parking_floor_area_ft2) == "25000"


def test_parking_larger_than_the_inclusive_column_is_not_derived() -> None:
    """Contradictory columns must degrade honestly, never produce a negative area.

    Parking cannot exceed buildings-and-parking in a coherent report, so the
    two columns disagree and no subtraction can be trusted. Falling back to
    the unchanged inclusive value with a ``buildings_and_parking`` basis keeps
    the record readable and flags the value as parking-inclusive; deriving
    would yield a negative area that the envelope's ``ge=0`` bound rejects,
    failing the whole report over one bad cell.
    """
    record = _record(
        {
            BUILDINGS_AND_PARKING: 20_000,
            PARKING: 25_000,
        }
    )

    assert str(record.gross_floor_area_ft2) == "20000"
    assert record.gross_floor_area_basis == "buildings_and_parking"
    assert str(record.parking_floor_area_ft2) == "25000"


def test_xml_derives_gross_floor_area_from_the_parking_column() -> None:
    """Carrier choice must not change the resolved value, per the ADR contract.

    The ADR requires every accepted carrier to normalize into the same
    canonical model, so the derivation has to hold for the XML path too and
    not just the workbook path where it is easiest to test.
    """
    record = parse_report_bytes(
        _property_metrics_xml(
            {
                "propertyFloorAreaBuildingsAndParking": 100_000,
                "propertyFloorAreaParking": 25_000,
            }
        ),
        filename="report.xml",
    )[0]

    assert str(record.gross_floor_area_ft2) == "75000"
    assert record.gross_floor_area_basis == "buildings_and_parking_less_parking"
