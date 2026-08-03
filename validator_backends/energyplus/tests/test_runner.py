"""
Unit tests for EnergyPlus container helpers.

Covers artifact type inference, GCS URI rewriting, tabular metric extraction,
and output variable extraction (window envelope metrics).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from validator_backends.energyplus import runner
from validator_backends.energyplus.main import (
    _infer_artifact_type,
    _rewrite_output_paths,
)
from validibot_shared.energyplus.envelopes import EnergyPlusInputs, EnergyPlusOutputs
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
            ReportName TEXT, TableName TEXT, RowName TEXT, ColumnName TEXT,
            Value TEXT, Units TEXT,
            ReportForString TEXT DEFAULT 'Entire Facility'
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
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "Building Area",
            "Net Conditioned Building Area",
            "Area",
            "25",
        ),
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
            "District Cooling",
            "0.036",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Total End Uses",
            "District Heating Water",
            "0.072",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Total End Uses",
            "Propane",
            "0.018",
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
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Interior Lighting",
            "Electricity",
            "0.036",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Fans",
            "Electricity",
            "0.018",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Pumps",
            "Electricity",
            "0.0108",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "End Uses",
            "Water Systems",
            "Natural Gas",
            "0.0072",
            "GJ",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "Comfort and Setpoint Not Met Summary",
            "Time Setpoint Not Met During Occupied Heating",
            "Facility",
            "12",
            "hr",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "Comfort and Setpoint Not Met Summary",
            "Time Setpoint Not Met During Occupied Cooling",
            "Facility",
            "8",
            "hr",
        ),
        (
            "DemandEndUseComponentsSummary",
            "End Uses",
            "Total End Uses",
            "Electricity",
            "5000",
            "W",
        ),
        (
            "AnnualBuildingUtilityPerformanceSummary",
            "Building Area",
            "Net Conditioned Building Area",
            "Area",
            "25",
            "m2",
        ),
    ]
    cur.executemany(
        """
        INSERT INTO TabularDataWithStrings (
            ReportName, TableName, RowName, ColumnName, Value, Units
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        tdws_rows,
    )

    if with_report_data:
        _create_report_data_tables(cur)

    conn.commit()
    conn.close()
    return sql_path


# ---------------------------------------------------------------------------
# Artifact type inference
# ---------------------------------------------------------------------------


def test_infer_artifact_type() -> None:
    """Artifact typing should reserve each declared role for its named output."""
    assert _infer_artifact_type("eplusout.sql") == "simulation-db"
    assert _infer_artifact_type("results.csv") == "timeseries-csv"
    assert _infer_artifact_type("eplusout.err") == "err-log"
    assert _infer_artifact_type("sqlite.err") == "file"
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
            sha256="1" * 64,
            storage_version="1700000000000000",
        ),
        ValidationArtifact(
            name="eplusout.err",
            type="err-log",
            mime_type="text/plain",
            uri="gs://bucket/run/outputs/eplusout.err",
            size_bytes=42,
            sha256="2" * 64,
            storage_version="1700000000000001",
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
    """Metric extraction should populate the complete declared SQL contract.

    Test data uses GJ values (the native unit in EnergyPlus 25.x SQL):
    - 0.36 GJ electricity ≈ 100 kWh
    - 0.18 GJ natural gas ≈ 50 kWh
    - 25 m² floor area
    - District cooling/heating: 10 + 20 kWh
    - Other fuels: 5 kWh propane
    - Mixed-fuel EUI = (100 + 50 + 10 + 20 + 5) / 25 = 7.4 kWh/m²
    - Heating: 0.10 + 0.08 = 0.18 GJ ≈ 50 kWh
    - Cooling: 0.15 GJ ≈ 41.67 kWh

    A conflicting non-facility row proves the extractor filters the real
    ``ReportForString`` column instead of mistaking it for ``ReportName``.
    """
    sql_path = _make_sql_db(tmp_path, with_report_data=True)
    with sqlite3.connect(sql_path) as conn:
        conn.execute(
            """
            INSERT INTO TabularDataWithStrings (
                ReportName, TableName, RowName, ColumnName, Value, Units,
                ReportForString
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "AnnualBuildingUtilityPerformanceSummary",
                "End Uses",
                "Total End Uses",
                "Electricity",
                "999",
                "GJ",
                "Other Context",
            ),
        )
    metrics = runner._extract_metrics(sql_path)  # type: ignore[attr-defined]
    assert metrics.site_electricity_kwh == pytest.approx(100, rel=1e-3)
    assert metrics.site_natural_gas_kwh == pytest.approx(50, rel=1e-3)
    assert metrics.site_district_cooling_kwh == pytest.approx(10, rel=1e-3)
    assert metrics.site_district_heating_kwh == pytest.approx(20, rel=1e-3)
    assert metrics.site_other_fuels_kwh == pytest.approx(5, rel=1e-3)
    assert metrics.site_eui_kwh_m2 == pytest.approx(7.4, rel=1e-3)
    assert metrics.heating_energy_kwh == pytest.approx(50, rel=1e-3)
    assert metrics.cooling_energy_kwh == pytest.approx(41.67, rel=1e-2)
    assert metrics.interior_lighting_kwh == pytest.approx(10)
    assert metrics.fans_energy_kwh == pytest.approx(5)
    assert metrics.pumps_energy_kwh == pytest.approx(3)
    assert metrics.water_systems_kwh == pytest.approx(2)
    assert metrics.unmet_heating_hours == pytest.approx(12)
    assert metrics.unmet_cooling_hours == pytest.approx(8)
    assert metrics.peak_electric_demand_w == pytest.approx(5000)
    assert metrics.simulated_conditioned_area_m2 == pytest.approx(25)


def test_extract_metrics_returns_explicit_none_when_sql_is_missing(tmp_path) -> None:
    """Unavailable declared metrics should remain null instead of sentinels."""
    metrics = runner._extract_metrics(tmp_path / "missing.sql")  # type: ignore[attr-defined]

    assert all(value is None for value in metrics.model_dump().values())


# ---------------------------------------------------------------------------
# Review-readiness version evidence and command construction
# ---------------------------------------------------------------------------


def _installation(tmp_path: Path) -> runner.EnergyPlusInstallationEvidence:
    """Build deterministic installation evidence for command unit tests."""
    binary = tmp_path / "energyplus"
    idd = tmp_path / "Energy+.idd"
    binary.touch()
    idd.write_text("!IDD_Version 25.2.0\n!IDD_BUILD build123\n", encoding="utf-8")
    return runner.EnergyPlusInstallationEvidence(
        binary_path=binary,
        binary_version="25.2.0",
        binary_build="build123",
        idd_path=idd,
        idd_version="25.2.0",
        idd_build="build123",
    )


def test_version_and_build_parsers_normalize_review_evidence(tmp_path) -> None:
    """Binary, IDD, IDF, and epJSON version evidence should parse consistently."""
    assert runner._parse_energyplus_version(  # type: ignore[attr-defined]
        "EnergyPlus, Version 25.2.0-cf7368216c",
    ) == ("25.2.0", "cf7368216c")

    idd = tmp_path / "Energy+.idd"
    idd.write_text("!IDD_Version 25.2.0\n!IDD_BUILD cf7368216c\n", encoding="utf-8")
    assert runner._parse_idd_metadata(idd) == ("25.2.0", "cf7368216c")  # type: ignore[attr-defined]

    idf = tmp_path / "model.idf"
    idf.write_text("Version, 25.2;\n", encoding="utf-8")
    epjson = tmp_path / "model.epjson"
    epjson.write_text(
        '{"Version": {"Version 1": {"version_identifier": "25.2"}}}',
        encoding="utf-8",
    )
    assert runner._parse_model_version(idf) == "25.2"  # type: ignore[attr-defined]
    assert runner._parse_model_version(epjson) == "25.2"  # type: ignore[attr-defined]
    assert runner._versions_match("25.2", "25.2.0", "25.2.0") is True  # type: ignore[attr-defined]
    assert runner._versions_match("24.2", "25.2.0", "25.2.0") is False  # type: ignore[attr-defined]


def test_full_simulation_command_uses_explicit_idd_and_weather(
    tmp_path,
    monkeypatch,
) -> None:
    """Full runs should pin the exact IDD and require the selected EPW."""
    model = tmp_path / "model.idf"
    weather = tmp_path / "weather.epw"
    model.write_text("Version, 25.2;", encoding="utf-8")
    weather.touch()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner._run_energyplus(  # type: ignore[attr-defined]
        model,
        weather,
        tmp_path,
        EnergyPlusInputs(run_simulation=True),
        _installation(tmp_path),
    )

    command = captured["command"]
    assert "--idd" in command
    assert "--weather" in command
    assert "--convert-only" not in command


def test_preflight_command_uses_conversion_only_without_weather(
    tmp_path,
    monkeypatch,
) -> None:
    """Preflight should avoid weather and SQL requirements while still parsing."""
    model = tmp_path / "model.idf"
    model.write_text("Version, 25.2;", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner._run_energyplus(  # type: ignore[attr-defined]
        model,
        None,
        tmp_path,
        EnergyPlusInputs(run_simulation=False),
        _installation(tmp_path),
    )

    command = captured["command"]
    assert "--idd" in command
    assert "--convert-only" in command
    assert "--weather" not in command


# ---------------------------------------------------------------------------
# Working-copy normalization and Validibot preflight checks
# ---------------------------------------------------------------------------


def test_timestep_normalization_never_mutates_submitted_idf(tmp_path) -> None:
    """The runtime timestep override must only edit the retained working copy."""
    source = tmp_path / "source.idf"
    original = "Version, 25.2;\nTimestep, 4;\n"
    source.write_text(original, encoding="utf-8")

    normalized = runner._prepare_model_working_copy(source, tmp_path, 12)  # type: ignore[attr-defined]

    assert source.read_text(encoding="utf-8") == original
    assert "Timestep,\n  12;" in normalized.read_text(encoding="utf-8")


def test_timestep_normalization_updates_epjson_working_copy(tmp_path) -> None:
    """epJSON models should receive the same private timestep override as IDF."""
    source = tmp_path / "source.epjson"
    source.write_text(
        '{"Version":{"v":{"version_identifier":"25.2"}},'
        '"Timestep":{"Timestep 1":{"number_of_timesteps_per_hour":4}}}',
        encoding="utf-8",
    )

    normalized = runner._prepare_model_working_copy(source, tmp_path, 6)  # type: ignore[attr-defined]
    assert '"number_of_timesteps_per_hour": 6' in normalized.read_text(
        encoding="utf-8",
    )
    assert '"number_of_timesteps_per_hour":4' in source.read_text(encoding="utf-8")


def test_selected_idf_checks_emit_stable_review_codes(tmp_path) -> None:
    """Duplicate, sizing, and schedule findings should be assertion-friendly."""
    model = tmp_path / "review.idf"
    model.write_text(
        """
        Version, 25.2;
        SimulationControl, No, No;
        Schedule:Day:Constant, Workday, Any Number, 1;
        Schedule:Day:Constant, workday, Any Number, 0;
        Schedule:Week:Daily, Broken Week, Workday, Workday;
        """,
        encoding="utf-8",
    )

    messages = runner.run_idf_checks(
        model,
        ["duplicate-names", "hvac-sizing", "schedule-coverage"],
    )

    assert {message["code"] for message in messages} == {
        "ENERGYPLUS_REVIEW_DUPLICATE_OBJECT_NAME",
        "ENERGYPLUS_REVIEW_HVAC_SIZING_DISABLED",
        "ENERGYPLUS_REVIEW_SCHEDULE_COVERAGE",
    }
    assert all("energyplus-review" in message["tags"] for message in messages)


def test_err_parser_counts_and_classifies_reviewer_issues(tmp_path) -> None:
    """The .err parser should preserve severity counts and stable categories."""
    err = tmp_path / "eplusout.err"
    err.write_text(
        """
        ** Warning ** Schedule coverage is invalid for WEEK
        ** Severe  ** Referenced object named COIL-1 was not found
        **  Fatal  ** Simulation terminated because HVAC did not converge
        """,
        encoding="utf-8",
    )

    messages = runner.parse_err_file(err)

    assert runner._count_err_issues(err) == (1, 1, 1)  # type: ignore[attr-defined]
    assert {message["code"] for message in messages} == {
        "ENERGYPLUS_REVIEW_SCHEDULE_COVERAGE",
        "ENERGYPLUS_REVIEW_INVALID_OBJECT_REFERENCE",
        "ENERGYPLUS_REVIEW_CONVERGENCE_RUNTIME",
    }
    assert all("energyplus-review" in message["tags"] for message in messages)


def test_err_counts_prefer_energyplus_terminal_summary(tmp_path) -> None:
    """Recurring issues included in EnergyPlus's summary must not be undercounted."""
    err = tmp_path / "eplusout.err"
    err.write_text(
        """
        ** Warning ** One rendered warning
        ** Severe  ** One rendered severe error
        **  Fatal  ** Simulation terminated
        ************* EnergyPlus Terminated--Fatal Error Detected. 7 Warnings; 3 Severe Errors; Elapsed Time=00:01
        """,
        encoding="utf-8",
    )

    assert runner._count_err_issues(err) == (7, 3, 1)  # type: ignore[attr-defined]


def test_leed_profile_promotes_missing_review_evidence_to_errors() -> None:
    """LEED readiness should require evidence without hard-coding thresholds."""
    standard = runner._profile_required_output_messages(  # type: ignore[attr-defined]
        review_profile="standard",
        run_simulation=True,
        version_match=False,
        has_sql_output=False,
        metrics=EnergyPlusSimulationMetrics(),
    )
    leed = runner._profile_required_output_messages(  # type: ignore[attr-defined]
        review_profile="leed_review",
        run_simulation=True,
        version_match=False,
        has_sql_output=False,
        metrics=EnergyPlusSimulationMetrics(),
    )

    assert standard
    assert all(message["severity"] == "warning" for message in standard)
    assert leed
    assert all(message["severity"] == "error" for message in leed)
    assert (
        sum(message["code"] == "ENERGYPLUS_LEED_REQUIRED_OUTPUT_MISSING" for message in leed) == 3
    )


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
