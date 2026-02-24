# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **BREAKING**: Renamed `validators/fmi/` directory to `validators/fmu/`
  - All class/function references: `FMI*` -> `FMU*` (e.g., `run_fmi_simulation()` -> `run_fmu_simulation()`)
  - Docker image: `validibot-validator-fmi` -> `validibot-validator-fmu`
  - Validator type: `"FMI"` -> `"FMU"`
  - Updated imports to use `validibot_shared.fmu` (requires validibot-shared >= 0.3.0)
  - Justfile, pyproject.toml, and test paths updated accordingly

### Added

- Pre-commit hooks with TruffleHog secret scanning, detect-private-key, and Ruff linting
- Dependabot configuration for Python dependency updates
- Hardened .gitignore to exclude key material and credential files
- CI workflow with linting, tests, and pip-audit dependency auditing
