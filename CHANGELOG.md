# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
