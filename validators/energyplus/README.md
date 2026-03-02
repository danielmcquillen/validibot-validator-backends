# EnergyPlus™ Validator Container

Container for running [EnergyPlus™](https://energyplus.net/) simulations as part of validation workflows. Can be deployed as a Cloud Run Job, Kubernetes Job, or run locally via Docker.

## Overview

This container:
1. Downloads `input.json` (EnergyPlusInputEnvelope) from storage
2. Downloads IDF/epJSON model and EPW weather files from storage
3. Runs EnergyPlus simulation
4. Extracts metrics from SQL database
5. Creates `output.json` (EnergyPlusOutputEnvelope) with results
6. Uploads output.json to storage
7. POSTs callback to the Validibot platform (cloud mode) or exits (local mode)

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
      "uri": "gs://bucket/models/model.idf"
    },
    {
      "name": "weather.epw",
      "mime_type": "application/vnd.energyplus.epw",
      "role": "weather",
      "uri": "gs://bucket/weather/USA_CA_SF.epw"
    }
  ],
  "inputs": {
    "timestep_per_hour": 4,
    "output_variables": ["Zone Mean Air Temperature"],
    "invocation_mode": "cli"
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
  "messages": [
    {
      "severity": "INFO",
      "text": "Simulation completed successfully"
    }
  ],
  "metrics": [
    {
      "name": "electricity_kwh",
      "value": 1234.5,
      "unit": "kWh",
      "category": "energy"
    },
    {
      "name": "energy_use_intensity_kwh_m2",
      "value": 18.7,
      "unit": "kWh/m²",
      "category": "energy"
    }
  ],
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
      "electricity_kwh": 1234.5,
      "energy_use_intensity_kwh_m2": 18.7
    },
    "logs": {
      "stdout_tail": "...",
      "stderr_tail": "...",
      "err_tail": "..."
    },
    "energyplus_returncode": 0,
    "execution_seconds": 330.5,
    "invocation_mode": "cli"
  }
}
```

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
gcloud run jobs execute validibot-validator-energyplus \
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
