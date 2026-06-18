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
validators := "energyplus fmu shacl"

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
    uv run --extra dev --extra fmu --extra shacl pytest {{args}}

# Run tests for a specific validator
test-validator validator:
    uv run --extra dev --extra fmu --extra shacl pytest validator_backends/{{validator}}/tests

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
#
# Builds for the HOST architecture by default. This recipe `--load`s the image
# into the local Docker for local testing (e.g. the `just local-cloud` stack) —
# it does NOT push to Cloud Run, so it should be native: on Apple Silicon an
# amd64 image runs under QEMU emulation ~14x slower, which pushes heavy
# validators (e.g. SHACL on a real ASHRAE 223P model: ~15s native vs ~205s
# emulated) past their wall-clock budgets and makes them spuriously "time out"
# locally. Cloud Run images come from `build-push` (still pinned to amd64) or CI,
# so production is unaffected. To force a specific platform for parity testing,
# set VALIDATOR_BUILD_PLATFORM=linux/amd64 (or any buildx platform).
build validator:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Building {{validator}} container${VALIDATOR_BUILD_PLATFORM:+ for ${VALIDATOR_BUILD_PLATFORM}}..."
    # The backend image version comes from the Dockerfile's
    # ``ARG VALIDATOR_BACKEND_VERSION`` default — that's the single source of
    # truth. Release engineering can override via the env var
    # (``VALIDATOR_BACKEND_VERSION=0.1.1-rc1 just build energyplus``); the
    # ``${VAR:+--build-arg ...}`` form passes the build-arg only when the
    # env var is set and otherwise lets the Dockerfile default win.
    docker buildx build \
        ${VALIDATOR_BUILD_PLATFORM:+--platform "${VALIDATOR_BUILD_PLATFORM}"} \
        --load \
        -f validator_backends/{{validator}}/Dockerfile \
        ${VALIDATOR_BACKEND_VERSION:+--build-arg VALIDATOR_BACKEND_VERSION="${VALIDATOR_BACKEND_VERSION}"} \
        --build-arg VALIDATOR_BACKEND_REVISION="{{git_sha}}" \
        --build-arg VALIDATOR_BACKEND_SLUG="{{validator}}" \
        -t validibot-validator-backend-{{validator}}:latest \
        -t validibot-validator-backend-{{validator}}:{{git_sha}} \
        .
    echo "✓ Built validibot-validator-backend-{{validator}}:{{git_sha}}"

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
    # See ``build`` recipe for the version-handling rationale: Dockerfile
    # default is canonical; env-var override is opt-in for release
    # engineering.
    docker buildx build \
        --platform linux/amd64 \
        --push \
        -f validator_backends/{{validator}}/Dockerfile \
        ${VALIDATOR_BACKEND_VERSION:+--build-arg VALIDATOR_BACKEND_VERSION="${VALIDATOR_BACKEND_VERSION}"} \
        --build-arg VALIDATOR_BACKEND_REVISION="{{git_sha}}" \
        --build-arg VALIDATOR_BACKEND_SLUG="{{validator}}" \
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

    # Compute stage-specific names.
    #
    # Two DISTINCT service accounts, matching the main repo's
    # `just validator-deploy` (validibot/just/gcp/mod.just):
    #   * RUNTIME_SA — the dedicated, least-privilege identity the validator
    #     container RUNS AS (--service-account). It has GCS access on the run
    #     bundle and run.invoker on the worker (for callbacks), but NOT
    #     secrets / Cloud SQL / Cloud Tasks / KMS. A compromised validator is
    #     thus contained.
    #   * INVOKER_SA — the main web/worker identity allowed to TRIGGER the job
    #     (granted validibot_job_runner below). The Django worker runs as this
    #     SA when it calls the Jobs API to launch a validation.
    #
    # Previously both were the broad `validibot-cloudrun-*` SA, so the container
    # ran with the full app identity (secrets/DB/tasks/KMS). Because this recipe
    # and the main repo's recipe deploy the SAME job name, whichever ran last
    # set the job's runtime identity — so the standalone path could silently
    # widen it. Using RUNTIME_SA here keeps both deploy paths in agreement on
    # least privilege. (Both SAs are created by `just gcp init-stage` in the
    # main repo; run that first.)
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-validator-backend-{{validator}}"
        RUNTIME_SA="validibot-validator-prod@{{gcp_project}}.iam.gserviceaccount.com"
        INVOKER_SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
    else
        JOB_NAME="validibot-validator-backend-{{validator}}-{{stage}}"
        RUNTIME_SA="validibot-validator-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        INVOKER_SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
    fi

    echo "Deploying $JOB_NAME to {{stage}}..."
    gcloud run jobs deploy "$JOB_NAME" \
        --image {{ar_repo}}/validibot-validator-backend-{{validator}}:{{git_sha}} \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --service-account "$RUNTIME_SA" \
        --memory 4Gi \
        --cpu 2 \
        --max-retries 0 \
        --task-timeout 3600 \
        --set-env-vars "PYTHONUNBUFFERED=1,VALIDIBOT_STAGE={{stage}},DEPLOYMENT_TARGET=gcp" \
        --labels "validator={{validator}},revision={{git_sha}},stage={{stage}}"
    echo "✓ $JOB_NAME deployed (runs as $RUNTIME_SA)"

    # Grant the MAIN web/worker SA permission to run this job with overrides.
    # Uses custom role with run.jobs.run + run.jobs.runWithOverrides (for the
    # VALIDIBOT_INPUT_URI env override). This is the INVOKER, NOT the runtime
    # identity set above.
    echo "Granting job runner permission to $INVOKER_SA on $JOB_NAME..."
    gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --member="serviceAccount:$INVOKER_SA" \
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

# =============================================================================
# Release
# =============================================================================
#
# Cuts a signed-tag release. CI then verifies the signature, builds
# each backend image with full supply-chain provenance (sigstore
# attestation + SBOM), pushes to GHCR, and (when configured) mirrors
# to GAR.
#
# Operator verification (after pull): see RELEASING.md.

# Release a new version: signs the tag, pushes, CI builds + publishes.
# Usage: just release 0.6.0
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate version format.
    if [[ ! "{{VERSION}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "✗ Version must be in format X.Y.Z (e.g., 0.6.0). Got: {{VERSION}}"
        exit 1
    fi

    # Refuse if working tree is dirty.
    if [[ -n $(git status --porcelain) ]]; then
        echo "✗ Working tree has uncommitted changes. Commit or stash first."
        git status --short
        exit 1
    fi

    # Refuse if not on main.
    BRANCH=$(git branch --show-current)
    if [[ "$BRANCH" != "main" ]]; then
        echo "✗ Not on main branch (currently on '$BRANCH')."
        echo "  Releases are cut from main only. Switch with: git checkout main"
        exit 1
    fi

    # Refuse if tag already exists locally or remotely.
    TAG="v{{VERSION}}"
    if git rev-parse "$TAG" >/dev/null 2>&1; then
        echo "✗ Tag $TAG already exists locally."
        exit 1
    fi
    if git ls-remote --tags origin "refs/tags/$TAG" | grep -q "$TAG"; then
        echo "✗ Tag $TAG already exists on origin."
        exit 1
    fi

    # Confirm we're up-to-date with origin.
    git fetch origin main
    if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
        echo "✗ Local main is not in sync with origin/main."
        echo "  Run: git pull"
        exit 1
    fi

    # Verify pyproject.toml version matches the requested release.
    TOML_VERSION=$(grep '^version = ' pyproject.toml | head -1 | sed 's/version = "\(.*\)"/\1/')
    if [[ "$TOML_VERSION" != "{{VERSION}}" ]]; then
        echo "✗ pyproject.toml version ($TOML_VERSION) doesn't match {{VERSION}}."
        echo "  Bump the version in pyproject.toml first, commit, and push."
        exit 1
    fi

    # Verify the cross-repo dependency on validibot-shared is at the
    # latest published version. This catches the "I forgot to bump
    # validibot-shared in this repo after publishing a new shared
    # release" failure mode — easy to miss, hard to debug after the
    # release is out (image bytes are wrong, validators may misbehave).
    #
    # Override with VALIDIBOT_RELEASE_ALLOW_STALE_SHARED=1 for
    # emergencies (e.g. PyPI is down, or you intentionally want to
    # pin to an older shared release).
    if [[ "${VALIDIBOT_RELEASE_ALLOW_STALE_SHARED:-0}" != "1" ]]; then
        SHARED_PINNED=$(grep -E '"validibot-shared==' pyproject.toml | head -1 | sed -E 's/.*"validibot-shared==([^"]+)".*/\1/')
        if [[ -z "$SHARED_PINNED" ]]; then
            echo "⚠ Could not detect validibot-shared pin in pyproject.toml; skipping freshness check."
        else
            SHARED_LATEST=$(curl -s --max-time 10 https://pypi.org/pypi/validibot-shared/json 2>/dev/null | jq -r '.info.version' 2>/dev/null)
            if [[ -z "$SHARED_LATEST" ]] || [[ "$SHARED_LATEST" == "null" ]]; then
                echo "⚠ Could not query PyPI for latest validibot-shared. Currently pinned: $SHARED_PINNED."
                echo "  Press Enter to continue anyway, Ctrl+C to abort..."
                read -r
            elif [[ "$SHARED_PINNED" != "$SHARED_LATEST" ]]; then
                echo "✗ validibot-shared is pinned to $SHARED_PINNED but latest on PyPI is $SHARED_LATEST."
                echo ""
                echo "  Update pyproject.toml so the line reads:"
                echo "      \"validibot-shared==$SHARED_LATEST\","
                echo ""
                echo "  Then commit + push, and re-run: just release {{VERSION}}"
                echo ""
                echo "  Override (emergencies only): VALIDIBOT_RELEASE_ALLOW_STALE_SHARED=1 just release {{VERSION}}"
                exit 1
            else
                echo "✓ validibot-shared is at latest ($SHARED_LATEST)"
            fi
        fi
    fi

    echo ""
    echo "About to sign and push tag $TAG."
    echo "CI will then build + push images for: {{validators}}"
    echo "Press Enter to continue, Ctrl+C to abort..."
    read -r

    # Sign the tag. Requires `git config --global tag.gpgsign true`
    # and a signing key configured. The CI workflow at
    # .github/workflows/release.yml verifies the signature and
    # publishes the release artefacts.
    git tag -s "$TAG" -m "$TAG"
    git push origin "$TAG"

    echo ""
    echo "✓ Pushed $TAG"
    echo "  CI will:"
    echo "    1. Verify the tag signature"
    echo "    2. Build each backend image (in parallel)"
    echo "    3. Push to GHCR with sigstore attestation"
    echo "    4. (Optional) Mirror to GAR if GCP_PROJECT_ID is configured"
    echo "    5. Attach SBOM to GitHub release"
    echo "  Monitor: gh run watch"
