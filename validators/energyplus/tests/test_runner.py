"""
Unit tests for EnergyPlus container helpers.

Covers artifact type inference, GCS URI rewriting, tabular metric extraction,
and output variable extraction (window envelope metrics).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from validators.energyplus import runner
from validators.energyplus.main import (
    _infer_artifact_type,
    _rewrite_output_paths,
)
from validibot_shared.energyplus.envelopes import EnergyPlusOutputs
from validibot_shared.energyplus.models import (
    EnergyPlusSimulationLogs,
    EnergyPlusSimulationMetrics,
    EnergyPlusSimulationOutputs,
)
from validibot_shared.validations.envelopes import ValidationArtifact


# Joules → kWh for assertions
J_TO_KWH = 1.0 / 3_600_000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_tabular_tables(cur: sqlite3.Cursor) -> None:
    """Create the TabularData tables used by _extract_metrics.

    EnergyPlus 25.x stores tabular data in ``TabularData`` with integer
    foreign keys and provides a ``TabularDataWithStrings`` convenience view
    that resolves them to human-readable names.  Column names in the view
    do NOT include units (e.g. ``"Electricity"`` not ``"Electricity [kWh]"``).
    Energy values are stored in GJ.  A separate ``Units`` column indicates
    the unit type (``"GJ"`` for energy, ``"m2"`` for area, ``"m3"`` for
    water, etc.).
    """
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS TabularData (
            ReportName TEXT, TableName TEXT, RowName TEXT, ColumnName TEXT, Value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS TabularDataWithStrings (
            ReportName TEXT, TableName TEXT, RowName TEXT, ColumnName TEXT, Value TEXT, Units TEXT
        )
        """
    )


def _create_report_data_tables(cur: sqlite3.Cursor) -> None:
    """Create ReportDataDictionary and ReportData tables matching E+ schema."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ReportDataDictionary (
            ReportDataDictionaryIndex INTEGER PRIMARY KEY,
            IsMeter INTEGER,
            Type TEXT,
            IndexGroup TEXT,
            TimestepType TEXT,
            KeyValue TEXT,
            Name TEXT,
            ReportingFrequency TEXT,
            ScheduleName TEXT,
            Units TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ReportData (
            ReportDataIndex INTEGER PRIMARY KEY,
            TimeIndex INTEGER,
            ReportDataDictionaryIndex INTEGER,
            Value REAL
        )
        """
    )


def _make_sql_db(tmp_path: Path, *, with_report_data: bool = False) -> Path:
    """Create a minimal eplusout.sql with tabular data and optionally report data tables."""
    sql_path = tmp_path / "eplusout.sql"
    conn = sqlite3.connect(sql_path)
    cur = conn.cursor()
    _create_tabular_tables(cur)

    # TabularData uses old-style column names (not used by current code,
    # kept for schema compatibility).
    tabular_rows = [
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Total End Uses",
            "Electricity",
            "0.36",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Total End Uses",
            "Natural Gas",
            "0.18",
        ),
        ("Entire Facility", "Building Area", "Total Building Area", "Area", "25"),
    ]
    cur.executemany("INSERT INTO TabularData VALUES (?, ?, ?, ?, ?)", tabular_rows)

    # TabularDataWithStrings matches the EnergyPlus 25.x view schema.
    # Energy values are in GJ:  0.36 GJ ≈ 100 kWh,  0.18 GJ ≈ 50 kWh.
    tdws_rows = [
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Total End Uses",
            "Electricity",
            "0.36",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Total End Uses",
            "Natural Gas",
            "0.18",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Total End Uses",
            "Water",
            "0.00",
            "m3",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Heating",
            "Electricity",
            "0.10",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Heating",
            "Natural Gas",
            "0.08",
            "GJ",
        ),
        ("AnnualBuildingUtilityPerformanceSummary", "End Uses", "Heating", "Water", "0.00", "m3"),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Cooling",
            "Electricity",
            "0.15",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Cooling",
            "Natural Gas",
            "0.00",
            "GJ",
        ),
        ("AnnualBuildingUtilityPerformanceSummary", "End Uses", "Cooling", "Water", "0.00", "m3"),
        ("Entire Facility", "Building Area", "Total Building Area", "Area", "25", "m2"),
    ]
    cur.executemany("INSERT INTO TabularDataWithStrings VALUES (?, ?, ?, ?, ?, ?)", tdws_rows)

    if with_report_data:
        _create_report_data_tables(cur)

    conn.commit()
    conn.close()
    return sql_path


# ---------------------------------------------------------------------------
# Artifact type inference
# ---------------------------------------------------------------------------


def test_infer_artifact_type() -> None:
    """Artifact typing should detect common EnergyPlus outputs."""
    assert _infer_artifact_type("eplusout.sql") == "simulation-db"
    assert _infer_artifact_type("results.csv") == "timeseries-csv"
    assert _infer_artifact_type("eplusout.err") == "err-log"
    assert _infer_artifact_type("other.bin") == "file"


# ---------------------------------------------------------------------------
# GCS URI rewriting
# ---------------------------------------------------------------------------


def test_rewrite_output_paths_prefers_gcs_uris() -> None:
    """Output paths should be rewritten to uploaded GCS URIs when available."""
    artifacts = [
        ValidationArtifact(
            name="eplusout.sql",
            type="simulation-db",
            mime_type="application/x-sqlite3",
            uri="gs://bucket/run/outputs/eplusout.sql",
            size_bytes=123,
        ),
        ValidationArtifact(
            name="eplusout.err",
            type="err-log",
            mime_type="text/plain",
            uri="gs://bucket/run/outputs/eplusout.err",
            size_bytes=42,
        ),
    ]

    outputs = EnergyPlusOutputs(
        outputs=EnergyPlusSimulationOutputs(
            eplusout_sql=Path("/tmp/eplusout.sql"),
            eplusout_err=Path("/tmp/eplusout.err"),
        ),
        metrics=EnergyPlusSimulationMetrics(),
        logs=EnergyPlusSimulationLogs(),
        energyplus_returncode=0,
        execution_seconds=1.0,
        invocation_mode="cli",
    )

    rewritten = _rewrite_output_paths(outputs, artifacts)

    assert str(rewritten.outputs.eplusout_sql) == "gs://bucket/run/outputs/eplusout.sql"
    assert str(rewritten.outputs.eplusout_err) == "gs://bucket/run/outputs/eplusout.err"


# ---------------------------------------------------------------------------
# Tabular metric extraction
# ---------------------------------------------------------------------------


def test_extract_metrics_reads_tabular_data(tmp_path) -> None:
    """_extract_metrics should pull electricity, gas, and area from SQL tables.

    Test data uses GJ values (the native unit in EnergyPlus 25.x SQL):
    - 0.36 GJ electricity ≈ 100 kWh
    - 0.18 GJ natural gas ≈ 50 kWh
    - 25 m² floor area
    - EUI = 100 / 25 = 4 kWh/m²
    - Heating: 0.10 + 0.08 = 0.18 GJ ≈ 50 kWh
    - Cooling: 0.15 GJ ≈ 41.67 kWh
    """
    sql_path = _make_sql_db(tmp_path, with_report_data=True)
    metrics = runner._extract_metrics(sql_path)  # type: ignore[attr-defined]
    assert metrics.site_electricity_kwh == pytest.approx(100, rel=1e-3)
    assert metrics.site_natural_gas_kwh == pytest.approx(50, rel=1e-3)
    assert metrics.site_eui_kwh_m2 == pytest.approx(4, rel=1e-3)
    assert metrics.heating_energy_kwh == pytest.approx(50, rel=1e-3)
    assert metrics.cooling_energy_kwh == pytest.approx(41.67, rel=1e-2)


# ---------------------------------------------------------------------------
# Output variable extraction (window envelope metrics)
# ---------------------------------------------------------------------------


class TestFetchOutputVariableSum:
    """
    Tests for _fetch_output_variable_sum which extracts annual totals of
    EnergyPlus output variables from the ReportDataDictionary/ReportData
    tables and converts J → kWh.
    """

    def test_returns_none_when_no_report_data_table(self, tmp_path) -> None:
        """If ReportDataDictionary doesn't exist, returns None gracefully."""
        sql_path = _make_sql_db(tmp_path, with_report_data=False)
        conn = sqlite3.connect(sql_path)
        result = runner._fetch_output_variable_sum(  # type: ignore[attr-defined]
            conn.cursor(),
            "Surface Window Heat Gain Energy",
        )
        assert result is None

    def test_returns_none_when_variable_not_in_idf(self, tmp_path) -> None:
        """If the variable was never requested in the IDF, returns None."""
        sql_path = _make_sql_db(tmp_path, with_report_data=True)
        conn = sqlite3.connect(sql_path)
        result = runner._fetch_output_variable_sum(  # type: ignore[attr-defined]
            conn.cursor(),
            "Surface Window Heat Gain Energy",
        )
        assert result is None

    def test_sums_run_period_across_surfaces(self, tmp_path) -> None:
        """
        With Run Period frequency, sums values across multiple surfaces
        and converts J → kWh.

        Two surfaces each reporting 3,600,000 J at Run Period frequency
        should yield 2.0 kWh total.
        """
        sql_path = _make_sql_db(tmp_path, with_report_data=True)
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        # Two surfaces, each with one Run Period value of 3,600,000 J (= 1 kWh)
        cur.execute(
            "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                0,
                "Sum",
                "Zone",
                "Zone Timestep",
                "Window1",
                "Surface Window Heat Gain Energy",
                "Run Period",
                "",
                "J",
            ),
        )
        cur.execute(
            "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                0,
                "Sum",
                "Zone",
                "Zone Timestep",
                "Window2",
                "Surface Window Heat Gain Energy",
                "Run Period",
                "",
                "J",
            ),
        )
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (1, 1, 1, 3_600_000.0))
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (2, 1, 2, 3_600_000.0))
        conn.commit()

        result = runner._fetch_output_variable_sum(  # type: ignore[attr-defined]
            conn.cursor(),
            "Surface Window Heat Gain Energy",
        )
        assert result == pytest.approx(2.0)

    def test_sums_hourly_when_no_run_period(self, tmp_path) -> None:
        """
        Falls back to Hourly frequency when Run Period is not available.

        One surface with two hourly values of 1,800,000 J each should
        yield 1.0 kWh total.
        """
        sql_path = _make_sql_db(tmp_path, with_report_data=True)
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        cur.execute(
            "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                0,
                "Sum",
                "Zone",
                "Zone Timestep",
                "Window1",
                "Surface Window Heat Loss Energy",
                "Hourly",
                "",
                "J",
            ),
        )
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (1, 1, 1, 1_800_000.0))
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (2, 2, 1, 1_800_000.0))
        conn.commit()

        result = runner._fetch_output_variable_sum(  # type: ignore[attr-defined]
            conn.cursor(),
            "Surface Window Heat Loss Energy",
        )
        assert result == pytest.approx(1.0)

    def test_prefers_run_period_over_hourly(self, tmp_path) -> None:
        """
        When both Run Period and Hourly data exist for the same variable,
        uses only Run Period to avoid double-counting.

        This scenario happens when an IDF requests the same Output:Variable
        at multiple reporting frequencies.
        """
        sql_path = _make_sql_db(tmp_path, with_report_data=True)
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        # Run Period entry: 7,200,000 J = 2 kWh (the correct annual total)
        cur.execute(
            "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                0,
                "Sum",
                "Zone",
                "Zone Timestep",
                "Window1",
                "Surface Window Transmitted Solar Radiation Energy",
                "Run Period",
                "",
                "J",
            ),
        )
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (1, 1, 1, 7_200_000.0))

        # Hourly entries: same total spread across hours (would double-count)
        cur.execute(
            "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                0,
                "Sum",
                "Zone",
                "Zone Timestep",
                "Window1",
                "Surface Window Transmitted Solar Radiation Energy",
                "Hourly",
                "",
                "J",
            ),
        )
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (2, 2, 2, 3_600_000.0))
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (3, 3, 2, 3_600_000.0))
        conn.commit()

        result = runner._fetch_output_variable_sum(  # type: ignore[attr-defined]
            conn.cursor(),
            "Surface Window Transmitted Solar Radiation Energy",
        )
        # Should use Run Period (2 kWh), not Hourly (also 2 kWh by coincidence
        # in this test, but in reality hourly sum would differ from run period)
        assert result == pytest.approx(2.0)

    def test_ignores_meter_entries(self, tmp_path) -> None:
        """
        Meter entries (IsMeter=1) for a matching variable name should be
        ignored — only output variable entries (IsMeter=0) are used.
        """
        sql_path = _make_sql_db(tmp_path, with_report_data=True)
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        # Meter entry (IsMeter=1) — should be ignored
        cur.execute(
            "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                1,
                "Sum",
                "Facility",
                "Zone Timestep",
                "",
                "Surface Window Heat Gain Energy",
                "Run Period",
                "",
                "J",
            ),
        )
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (1, 1, 1, 999_999.0))
        conn.commit()

        result = runner._fetch_output_variable_sum(  # type: ignore[attr-defined]
            conn.cursor(),
            "Surface Window Heat Gain Energy",
        )
        assert result is None

    def test_extract_metrics_includes_window_metrics(self, tmp_path) -> None:
        """
        End-to-end: _extract_metrics populates window_heat_gain_kwh when
        the output variable data is present in the SQL database.
        """
        sql_path = _make_sql_db(tmp_path, with_report_data=True)
        conn = sqlite3.connect(sql_path)
        cur = conn.cursor()

        # Add window heat gain data (one surface, Run Period)
        cur.execute(
            "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                0,
                "Sum",
                "Zone",
                "Zone Timestep",
                "Window1",
                "Surface Window Heat Gain Energy",
                "Run Period",
                "",
                "J",
            ),
        )
        cur.execute("INSERT INTO ReportData VALUES (?, ?, ?, ?)", (1, 1, 1, 36_000_000.0))
        conn.commit()
        conn.close()

        metrics = runner._extract_metrics(sql_path)  # type: ignore[attr-defined]
        assert metrics.window_heat_gain_kwh == pytest.approx(10.0)
        # Other window metrics should be None (not in DB)
        assert metrics.window_heat_loss_kwh is None
        assert metrics.window_transmitted_solar_kwh is None
