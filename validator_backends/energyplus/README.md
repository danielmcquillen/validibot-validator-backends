# EnergyPlus™ Validator Container

Container for running [EnergyPlus™](https://energyplus.net/) simulations as part of validation workflows. Can be deployed as a Cloud Run Job, Kubernetes Job, or run locally via Docker.

## Overview

This container:
1. Downloads `input.json` (EnergyPlusInputEnvelope) from storage
2. Downloads the IDF/epJSON model and, for full runs, an EPW weather file
3. Creates a private normalized model copy and runs selected modelling-review checks
4. Runs a full EnergyPlus simulation or conversion-only preflight
5. Extracts review evidence and available SQL metrics
6. Creates and uploads `output.json` plus run artifacts
7. POSTs a callback to Validibot (cloud mode) or exits (local mode)

## Container Interface

### Environment Variables

- `VALIDIBOT_INPUT_URI` (required): Storage URI to input.json (e.g., `gs://bucket/org_id/run_id/input.json` or `file:///app/storage/runs/run_id/input.json`)
- `VALIDIBOT_OUTPUT_URI` (optional): Where to write output.json (derived from input if not set)
- `VALIDIBOT_RUN_ID` (optional): Validation run ID for logging
- `GOOGLE_CLOUD_PROJECT`: GCP project ID (auto-set by Cloud Run when deployed to GCP)

### Input Envelope Structure

```json
{
  "schema_version": "validibot.input.v1",
  "run_id": "abc-123",
  "validator": {
    "id": "validator-uuid",
    "type": "ENERGYPLUS",
    "version": "24.2.0"
  },
  "input_files": [
    {
      "name": "model.idf",
      "mime_type": "application/vnd.energyplus.idf",
      "role": "primary-model",
      "port_key": "primary_model",
      "uri": "gs://bucket/models/model.idf"
    }
  ],
  "resource_files": [
    {
      "id": "weather-resource-uuid",
      "type": "energyplus_weather",
      "port_key": "weather_file",
      "uri": "gs://bucket/weather/USA_CA_SF.epw"
    }
  ],
  "inputs": {
    "timestep_per_hour": 4,
    "invocation_mode": "cli",
    "idf_checks": ["hvac-sizing", "schedule-coverage"],
    "run_simulation": true,
    "review_profile": "standard"
  },
  "context": {
    "callback_url": "https://validibot.example.com/api/v1/validation-callbacks/",
    "execution_bundle_uri": "gs://bucket/org_id/run_id/",
    "timeout_seconds": 3600
  }
}
```

### Output Envelope Structure

```json
{
  "schema_version": "validibot.output.v1",
  "run_id": "abc-123",
  "validator": {
    "id": "validator-uuid",
    "type": "ENERGYPLUS",
    "version": "24.2.0"
  },
  "status": "success",
  "timing": {
    "started_at": "2025-12-04T10:00:00Z",
    "finished_at": "2025-12-04T10:05:30Z"
  },
  "messages": [],
  "metrics": [],
  "artifacts": [
    {
      "name": "simulation.sql",
      "type": "simulation-db",
      "mime_type": "application/x-sqlite3",
      "uri": "gs://bucket/org_id/run_id/outputs/eplusout.sql",
      "size_bytes": 524288
    }
  ],
  "outputs": {
    "outputs": {
      "eplusout_sql": "/tmp/run/eplusout.sql",
      "eplusout_err": "/tmp/run/eplusout.err"
    },
    "metrics": {
      "site_electricity_kwh": 1234.5,
      "site_natural_gas_kwh": 250.0,
      "site_district_cooling_kwh": 0.0,
      "site_district_heating_kwh": 100.0,
      "site_other_fuels_kwh": 0.0,
      "site_eui_kwh_m2": 18.7,
      "simulated_conditioned_area_m2": 84.7
    },
    "logs": {
      "stdout_tail": "...",
      "stderr_tail": "...",
      "err_tail": "..."
    },
    "energyplus_returncode": 0,
    "execution_seconds": 330.5,
    "invocation_mode": "cli",
    "energyplus_binary_version": "25.2.0",
    "energyplus_binary_build": "cf7368216c",
    "idd_version": "25.2.0",
    "idd_build": "cf7368216c",
    "idd_path": "/opt/energyplus/Energy+.idd",
    "idf_version": "25.2",
    "version_match": true,
    "completed_successfully": true,
    "warning_count": 0,
    "severe_count": 0,
    "fatal_count": 0,
    "review_issue_count": 0,
    "has_sql_output": true,
    "has_err_output": true,
    "has_csv_output": true,
    "has_eso_output": true
  }
}
```

`run_simulation=false` selects conversion-only preflight and does not require
weather or SQL-backed metrics. The backend always passes the bundled
`Energy+.idd` explicitly and compares the model, binary, and IDD at major/minor
precision. EnergyPlus owns IDF/IDD validation in both modes. Validibot merges
native diagnostics from `eplusout.err`, stdout, and stderr into findings and
adds an explicit failure finding if a nonzero EnergyPlus exit has no diagnostic.

For every full simulation, the private normalized IDF or epJSON copy also
enforces `Output:SQLite` with `SimpleAndTabular` and unconverted SI units. It
preserves author-selected summary reports while ensuring that
`AnnualBuildingUtilityPerformanceSummary` and
`DemandEndUseComponentsSummary` are requested. EnergyPlus has no CLI switch
for SQLite output, so this normalization guarantees the tabular contract used
by post-processing without modifying the submitted model.

The legacy `duplicate-names` review-check value remains accepted as a no-op so
saved workflows continue to run. Duplicate-name findings now come only from
EnergyPlus's IDD-aware native validation.

Modeled site EUI sums every GJ-valued `Total End Uses` fuel column before
dividing by the reported simulation area. This includes electricity, natural
gas, district energy, and uncommon fuels such as propane or fuel oil. It is not
a measured or weather-normalized CBPS WNEUI/EUIt value.

## Building and Deploying

Use the justfile commands from the repository root:

```bash
# Build container locally
just build energyplus

# Configure your container registry (one-time setup)
cp justfile.local.example justfile.local
# Edit justfile.local with your registry details

# Build and push to your container registry
just build-push energyplus

# Deploy to Cloud Run Jobs (GCP)
just deploy energyplus prod

# View logs
just logs energyplus
```

### Execute Job (for testing)

```bash
# Replace with your region and bucket
gcloud run jobs execute validibot-validator-backend-energyplus \
  --region <your-region> \
  --update-env-vars VALIDIBOT_INPUT_URI=gs://<your-bucket>/test/input.json
```

## Local Development

### Install Dependencies

```bash
uv sync
```

### Run Tests

```bash
just test-validator energyplus
```

## EnergyPlus™ Version

This container uses EnergyPlus™ 25.2.0. To update:

1. Modify `Dockerfile` to install different version
2. Update `validator.version` in Django database
3. Rebuild and redeploy container

---

EnergyPlus™ is a trademark of the U.S. Department of Energy. EnergyPlus is distributed under a BSD-3-Clause license by the National Renewable Energy Laboratory (NREL). Validibot is not affiliated with, endorsed by, or sponsored by DOE or NREL.
