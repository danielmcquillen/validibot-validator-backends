# =============================================================================
# Validibot Validators Justfile
# =============================================================================
#
# Build, test, and deploy validator containers for Cloud Run Jobs.
#
# USAGE:
#   just                        # List all available commands
#   just build energyplus       # Build a specific validator locally
#   just test                   # Run all tests
#   just deploy energyplus dev  # Deploy to dev stage
#
# SETUP:
#   Before using build-push or deploy commands, create a .env file:
#     cp .env.example .env
#     # Edit .env with your GCP project and region
#
# DEPLOYMENT:
#   This justfile handles the full deployment lifecycle. You can also use
#   the main validibot justfile (../validibot/justfile) which has equivalent
#   commands. Both work - use whichever is more convenient:
#
#     From validibot-validator-backends/:  just deploy energyplus dev
#     From validibot/:      just validator-deploy energyplus dev
#
# =============================================================================

set shell := ["bash", "-cu"]

# Load .env file if present (optional — vars can also be set via environment)
set dotenv-load

# =============================================================================
# Configuration
# =============================================================================

# GCP settings - loaded from .env, environment variables, or command line:
#   cp .env.example .env        # then edit with your values
#   export VALIDIBOT_GCP_PROJECT=my-project
#   just --set gcp_project "my-project" deploy energyplus dev
gcp_project := env("VALIDIBOT_GCP_PROJECT", "")
gcp_region := env("VALIDIBOT_GCP_REGION", "us-central1")

# Artifact Registry path (constructed from GCP settings)
ar_host := gcp_region + "-docker.pkg.dev"
ar_repo := ar_host + "/" + gcp_project + "/validibot"

# Git SHA for tagging
git_sha := `git rev-parse --short HEAD 2>/dev/null || echo "dev"`

# Available validators
validators := "energyplus fmu"

# =============================================================================
# Default - List Commands
# =============================================================================

@default:
    just --list

# =============================================================================
# Development
# =============================================================================

# Run all tests
test *args:
    uv run --extra dev --extra fmu pytest {{args}}

# Run tests for a specific validator
test-validator validator:
    uv run --extra dev --extra fmu pytest validator_backends/{{validator}}/tests

# Lint all code
lint:
    uv run ruff check .

# Lint and fix
lint-fix:
    uv run ruff check . --fix

# Format code
format:
    uv run ruff format .

# Type check
typecheck:
    uv run mypy .

# Run all checks (lint + test)
check: lint test

# =============================================================================
# Docker Build
# =============================================================================

# Build a validator container locally (for testing only)
# Build context is the repo root (validibot-validator-backends/), not the validator subdirectory
# Builds for linux/amd64 since Cloud Run requires that architecture
build validator:
    @echo "Building {{validator}} container..."
    docker buildx build \
        --platform linux/amd64 \
        --load \
        -f validator_backends/{{validator}}/Dockerfile \
        -t validibot-validator-backend-{{validator}}:latest \
        -t validibot-validator-backend-{{validator}}:{{git_sha}} \
        .
    @echo "✓ Built validibot-validator-backend-{{validator}}:{{git_sha}}"

# Build all validator containers
build-all:
    #!/usr/bin/env bash
    set -euo pipefail
    for v in {{validators}}; do
        just build "$v"
    done
    echo "✓ All validators built"

# =============================================================================
# Docker Push (to Artifact Registry)
# =============================================================================

# Build and push a validator to Artifact Registry in one step
# Uses buildx with --push to avoid platform manifest issues on Apple Silicon
# Requires VALIDIBOT_GCP_PROJECT environment variable to be set
build-push validator:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -z "{{gcp_project}}" ]]; then
        echo "Error: Container registry not configured."
        echo ""
        echo "Set environment variables before running:"
        echo "  export VALIDIBOT_GCP_PROJECT=your-project-id"
        echo "  export VALIDIBOT_GCP_REGION=us-central1  # optional, defaults to us-central1"
        echo ""
        echo "Or pass directly:"
        echo "  just --set gcp_project your-project-id build-push {{validator}}"
        exit 1
    fi
    echo "Building and pushing {{validator}} container..."
    docker buildx build \
        --platform linux/amd64 \
        --push \
        -f validator_backends/{{validator}}/Dockerfile \
        -t {{ar_repo}}/validibot-validator-backend-{{validator}}:latest \
        -t {{ar_repo}}/validibot-validator-backend-{{validator}}:{{git_sha}} \
        .
    echo "✓ Built and pushed {{ar_repo}}/validibot-validator-backend-{{validator}}:{{git_sha}}"

# Build and push all validators
build-push-all:
    #!/usr/bin/env bash
    set -euo pipefail
    for v in {{validators}}; do
        just build-push "$v"
    done
    echo "✓ All validators built and pushed"

# =============================================================================
# Cloud Run Jobs Deployment
# =============================================================================

# Deploy a validator as a Cloud Run Job to a specific stage
# Usage: just deploy energyplus dev | just deploy fmu prod
deploy validator stage: (build-push validator)
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-validator-backend-{{validator}}"
        SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
    else
        JOB_NAME="validibot-validator-backend-{{validator}}-{{stage}}"
        SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
    fi

    echo "Deploying $JOB_NAME to {{stage}}..."
    gcloud run jobs deploy "$JOB_NAME" \
        --image {{ar_repo}}/validibot-validator-backend-{{validator}}:{{git_sha}} \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --service-account "$SA" \
        --memory 4Gi \
        --cpu 2 \
        --max-retries 0 \
        --task-timeout 3600 \
        --set-env-vars "PYTHONUNBUFFERED=1,VALIDATOR_VERSION={{git_sha}},VALIDIBOT_STAGE={{stage}},DEPLOYMENT_TARGET=gcp" \
        --labels "validator={{validator}},version={{git_sha}},stage={{stage}}"
    echo "✓ $JOB_NAME deployed"

    # Grant the service account permission to run this job with overrides
    # Uses custom role with run.jobs.run + run.jobs.runWithOverrides (for VALIDIBOT_INPUT_URI env)
    echo "Granting job runner permission to $SA on $JOB_NAME..."
    gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --member="serviceAccount:$SA" \
        --role="projects/{{gcp_project}}/roles/validibot_job_runner"
    echo "✓ IAM binding added"

# Deploy all validators to a stage
# Usage: just deploy-all dev | just deploy-all prod
deploy-all stage:
    #!/usr/bin/env bash
    set -euo pipefail
    for v in {{validators}}; do
        just deploy "$v" {{stage}}
    done
    echo "✓ All validators deployed to {{stage}}"

# =============================================================================
# Cloud Run Jobs Management
# =============================================================================

# List all validator jobs
list-jobs:
    gcloud run jobs list \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --filter "name~validibot-validator"

# Show job details
describe-job validator stage="prod":
    #!/usr/bin/env bash
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-validator-backend-{{validator}}"
    else
        JOB_NAME="validibot-validator-backend-{{validator}}-{{stage}}"
    fi
    gcloud run jobs describe "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}}

# View recent job logs
logs validator stage="prod" lines="100":
    #!/usr/bin/env bash
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-validator-backend-{{validator}}"
    else
        JOB_NAME="validibot-validator-backend-{{validator}}-{{stage}}"
    fi
    gcloud logging read \
        "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$JOB_NAME\"" \
        --project {{gcp_project}} \
        --limit {{lines}} \
        --format "table(timestamp,textPayload)"

# Delete a validator job
delete-job validator stage="prod":
    #!/usr/bin/env bash
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-validator-backend-{{validator}}"
    else
        JOB_NAME="validibot-validator-backend-{{validator}}-{{stage}}"
    fi
    echo "Deleting Cloud Run Job $JOB_NAME..."
    gcloud run jobs delete "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --quiet
    echo "✓ Deleted $JOB_NAME"

# =============================================================================
# Local Development Helpers
# =============================================================================

# Run a validator container locally (for testing)
run-local validator input_uri:
    docker run --rm \
        -e VALIDIBOT_INPUT_URI={{input_uri}} \
        -e GOOGLE_APPLICATION_CREDENTIALS=/tmp/keys/adc.json \
        -v "$HOME/.config/gcloud/application_default_credentials.json:/tmp/keys/adc.json:ro" \
        validibot-validator-backend-{{validator}}:latest

# Shell into a validator container (for debugging)
shell validator:
    docker run --rm -it \
        --entrypoint /bin/bash \
        validibot-validator-backend-{{validator}}:latest

# =============================================================================
# CI/CD Helpers
# =============================================================================

# Build, test, and deploy (for CI)
ci-deploy validator stage:
    just lint
    just test-validator {{validator}}
    just deploy {{validator}} {{stage}}

# Verify all validators are deployable (dry run)
verify-all:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Verifying all validators..."
    just lint
    just test
    for v in {{validators}}; do
        just build "$v"
    done
    echo "✓ All validators verified"
