# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed (BREAKING)

- **Repository renamed** from `validibot-validators` to
  `validibot-validator-backends`. The GitHub URL, package name, and the
  internal Python module are all renamed in lockstep. GitHub auto-redirects
  the old clone URL, but explicit updates are recommended.
- **Python package renamed** in `pyproject.toml` from `validibot-validators` to
  `validibot-validator-backends`. Anything pulling this in via `pip install` or
  declaring it as a dependency must update the distribution name.
- **Internal module renamed** from `validators/` to `validator_backends/`.
  - All Python imports of `from validators.X import …` must become
    `from validator_backends.X import …`.
  - Any monkeypatch / `setattr` strings naming `"validators.fmu.runner..."`
    must update to `"validator_backends.fmu.runner..."`.
  - Pytest test paths, ruff/mypy targets, and coverage source all moved.
- **Docker image names renamed** from `validibot-validator-{validator}` to
  `validibot-validator-backend-{validator}`. Examples:
  - `validibot-validator-energyplus` → `validibot-validator-backend-energyplus`
  - `validibot-validator-fmu` → `validibot-validator-backend-fmu`
  - `IMAGE_NAME` constants in each `__metadata__.py` moved in lockstep.
- **Cloud Run job names renamed** to match the new image-name prefix. Existing
  jobs (`validibot-validator-energyplus`, etc.) are NOT updated by
  `just deploy` — running it after this change creates new jobs alongside the
  old ones. Old jobs need manual `gcloud run jobs delete` after the new ones
  are confirmed working.
- **Dockerfile build paths updated.** Build context still the repo root, but
  `-f` paths now reference `validator_backends/{name}/Dockerfile` instead of
  `validators/{name}/Dockerfile`. The internal `COPY validators` directives
  also moved to `COPY validator_backends`.

### Notes for downstream consumers

Sibling Validibot repos that referenced this project — `validibot/`,
`validibot-cloud/`, `validibot-cli/`, `validibot-project/`,
`validibot-marketing/`, `validibot-shared/` — were updated in coordinated
commits. Self-hosted operators using `validibot-validator-*` image names in
their own dispatch code or orchestration must rename to
`validibot-validator-backend-*`.

## [0.5.0] - 2026-04-18

### Added

- **Platform-agnostic callback authentication** — new `validators/core/callback_auth.py`
  module introduces a `CallbackAuth` abstract base class and three concrete
  backends:
  - `GCPCallbackAuth` — fetches a Google-signed OIDC identity token from the
    GCE metadata server on every callback. Used when `DEPLOYMENT_TARGET=gcp`.
  - `SharedSecretCallbackAuth` — sends `Authorization: Worker-Key <secret>`
    using `WORKER_API_KEY`. Used for Docker Compose and local dev.
  - `NullCallbackAuth` — no auth headers (for tests / trusted local runs).

  Backend selection is factory-driven off the `DEPLOYMENT_TARGET` environment
  variable, matching the Django side (`validibot/core/api/task_auth.py`). See
  [ADR-2026-04-18](https://github.com/mcquilleninteractive/validibot-project/blob/main/docs/adr/completed/2026-04-18-worker-endpoint-auth-platform-agnostic.md).

- **New environment variables** (validator Cloud Run Jobs):
  - `DEPLOYMENT_TARGET` — required; `just deploy` now passes `gcp` automatically.
  - `TASK_OIDC_AUDIENCE` — optional override. When unset, `GCPCallbackAuth`
    derives the audience from the callback URL's origin (scheme + host, NO
    path), which matches Cloud Tasks/Scheduler's audience semantics and is
    what Django strict-verifies.
  - `WORKER_API_KEY` — still honoured on `docker_compose`.

- **24 new auth backend tests** in `validators/core/tests/test_callback_auth.py`
  covering fail-closed behaviour, metadata-server error paths, audience
  derivation, header-caching semantics, and factory selection. 7 client tests
  rewritten to exercise the new auth contract.

### Changed

- **BREAKING (GCP only)**: OIDC audience is now origin-only (`https://host`),
  previously the full callback URL. Cloud Tasks and Cloud Scheduler have
  always signed tokens with the origin, so Django's strict verification would
  reject any validator still sending a full-URL audience. Anyone pinned to
  0.4.x running on GCP must upgrade in lockstep with the Django worker
  deploy that ships the matching `CloudTasksOIDCAuthentication` class.

- `just deploy` now passes `DEPLOYMENT_TARGET=gcp` to the Cloud Run Job env.
  Existing deployments will continue to work because the factory defaults to
  the GCP backend when `DEPLOYMENT_TARGET` is unset AND a Google-signed
  metadata server is reachable, but redeploying is strongly recommended so
  the env var is set explicitly.

### Security

- Fail-closed design throughout: empty allowlist, missing audience, missing
  `google-auth`, and metadata-server errors all return an empty header set
  so the callback fails authentication at the worker rather than silently
  bypassing it.

## [0.4.2] - 2026-03-25

### Fixed

- Pinned the validator runtime and development dependencies to exact versions for reproducible builds.
- Updated the validator containers to require `validibot-shared==0.4.2` and aligned the shared `pydantic` pin used during locking.

## [0.4.1] - 2026-03-20

### Fixed

- `just test` and `just test-validator` now run with the FMU extra so the
  default test workflow works on a clean checkout.
- Validators now require `validibot-shared>=0.4.1` and use the sibling
  checkout as the local uv source during coordinated development.
- Updated the README setup commands to install the FMU test dependencies.

## [0.4.0] - 2026-03-20

### Changed

- **FMU runner**: Clarified that the runner consumes and returns native
  FMU variable names exactly as specified in the envelope. The core
  Django app maps these to `SignalDefinition` rows on ingestion.
- **FMU README**: Updated to reflect native variable name contract
  (previously said "catalog-keyed inputs").

## [0.3.2] - 2026-03-10

### Added

- **Window envelope metric extraction** — the EnergyPlus validator now extracts
  `window_heat_gain_kwh`, `window_heat_loss_kwh`, and `window_transmitted_solar_kwh`
  from the `ReportData`/`ReportDataDictionary` tables in `eplusout.sql`. These
  correspond to the `Surface Window Heat Gain Energy`, `Surface Window Heat Loss
  Energy`, and `Surface Window Transmitted Solar Radiation Energy` output variables.
  Values are summed across all surfaces, converted from J to kWh, and returned as
  `None` when the corresponding `Output:Variable` objects are not present in the IDF.
  Uses frequency-aware extraction (preferring "Run Period" data) to avoid
  double-counting when an IDF requests the same variable at multiple frequencies.
  Requires validibot-shared >= 0.3.1.

## [0.3.1] - 2026-03-09

### Fixed

- **Container permission error when run as non-root user** — Dockerfiles now
  create the `validibot` user (UID 1000) before copying application files and
  use `COPY --chown=validibot:validibot` to ensure the code is readable.
  Previously, files were copied as root with mode 600, causing
  `PermissionError` when the core platform's Docker runner launched containers
  with `user=1000:1000` and `read_only=True` (security hardening added in
  validibot v0.x, Feb 2026).

## [0.3.0] - 2026-02-25

### Changed

- **BREAKING**: Renamed `validators/fmi/` directory to `validators/fmu/`
  - All class/function references: `FMI*` -> `FMU*` (e.g., `run_fmi_simulation()` -> `run_fmu_simulation()`)
  - Docker image: `validibot-validator-fmi` -> `validibot-validator-fmu`
  - Validator type: `"FMI"` -> `"FMU"`
  - Updated imports to use `validibot_shared.fmu` (requires validibot-shared >= 0.3.0)
  - Justfile, pyproject.toml, and test paths updated accordingly

## [0.2.1] - 2026-02-16

### Added

- Pre-commit hooks with TruffleHog secret scanning, detect-private-key, and Ruff linting
- Dependabot configuration for Python dependency updates
- Hardened .gitignore to exclude key material and credential files
- CI workflow with linting, tests, and pip-audit dependency auditing
