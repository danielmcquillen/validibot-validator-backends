# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.15.0] - 2026-07-20

### Added

- Add one shared bounded HTTP Service runtime for EnergyPlus, FMU, SHACL, and
  Schematron while preserving their existing one-shot Cloud Run Job mode.
- Run every Service request in a fresh child process and scratch directory,
  with concurrency one, immutable revision/image checks, hard deadlines, and
  bounded secret-redacted child logs.
- Re-authorize an attempt against the worker immediately before domain compute,
  then replay verified immutable output without repeating domain work.
- Declare the versioned Service runtime contract and supported execution shapes
  in the authoritative backend inventory.

### Security

- Keep per-attempt GCS bearer credentials only in the request child environment
  and remove its scratch/process state after every delivery.
- Reject deployment, revision, image, task, prefix, expiry, and absolute-deadline
  mismatches before starting validator compute.

## [0.14.0] - 2026-07-19

### Added

- Consume explicit Google Credential Access Boundary tokens for Cloud Run GCS
  operations and reject any URI outside the injected execution-attempt prefix.
- Renew expired tokens through the worker using the parsed input envelope's
  attempt callback nonce, while keeping bearer values out of logs and reprs.

### Security

- Fail closed when Django marks an attempt capability as required but token
  delivery is absent or incomplete; never fall back silently to ambient ADC.
- Upgrade every backend image to `validibot-shared==0.18.0` so released images
  include the current URI-free execution-evidence contract.

## [0.13.0] - 2026-07-17

### Changed (BREAKING)

- Upgrade every validator image to `validibot-shared==0.17.0` and the
  `validibot.attempt.v2` execution contract.
- Echo the per-attempt callback nonce on every EnergyPlus, FMU, SHACL, and
  Schematron success and failure callback path.
- Refresh Google Cloud Storage, Google Auth, Sentry, Ruff, mypy, and SaxonC;
  align production image requirements with the root dependency contract.
- Update the pinned CodeQL SARIF upload action to v4.37.1.

### Security

- Require both the callback ID and raw attempt nonce before issuing an HTTP
  callback, while keeping the nonce out of logs and persisted result envelopes.
- Pin every callback callsite with a cross-backend invariant test so a future
  validator path cannot silently omit attempt authentication.

## [0.12.0] - 2026-07-17

### Changed

- Publish local result envelopes, artifacts, and directory manifests through
  atomic create-only materialization rather than replacement.
- Publish every GCS output with `ifGenerationMatch=0` and translate an
  existing-object precondition failure into a typed storage conflict.
- Key EnergyPlus and FMU scratch directories by a safe hash of the execution
  attempt ID and create them exclusively rather than reusing run-level state.
- Treat output-identity conflicts as attempt failures even where an ordinary
  artifact availability error may still produce a result without artifacts.

### Security

- Reject stale verified-input destinations before execution and reject all
  same-byte output replays. Retries must use a new execution-attempt identity;
  existing files or objects are never accepted as implicit idempotency proof.

## [0.11.0] - 2026-07-17

### Changed (BREAKING)

- Upgrade every validator image to `validibot-shared==0.16.0` and require
  exact size, SHA-256, and immutable storage version on every input, resource,
  and produced artifact.
- Route EnergyPlus, FMU, SHACL, and Schematron file materialization through one
  bounded streaming verifier before any parser, native binary, or model code
  sees the bytes.
- Pin GCS reads to the envelope's exact object generation and use the declared
  SHA-256 as the immutable version identity for read-only local attempt files.
- Upload helpers now return exact output size, digest, and storage version so
  output envelopes satisfy the same file-identity contract.

### Security

- Reject missing/stale GCS generations, provider size mismatches, over- and
  under-sized streams, digest mismatches, and local-version mismatches.
- Materialize into a sibling temporary file and atomically rename only after
  verification, preserving any previously committed destination on failure.

## [0.10.0] - 2026-07-17

### Changed (BREAKING)

- Upgrade every validator image to the required `validibot-shared==0.15.0`
  strict execution-attempt envelope contract.
- Bind every success and failure output to the input envelope's step run,
  execution attempt, contract version, canonical SHA-256, and exact output URI.
- Reject `VALIDIBOT_OUTPUT_URI` overrides that conflict with the output URI
  committed to by the input envelope.

### Added

- Add one shared output-identity helper used by EnergyPlus, FMU, SHACL, and
  Schematron so their success and failure paths cannot drift.
- Pin the cross-repository canonical input fixture digest in backend tests.

## [0.9.4] - 2026-07-15

### Changed

- Dependency alignment only: bump `validibot-shared` to `0.14.0` so all
  repos track the evidence-manifest artifact-lineage schema release. Validator
  backend envelope behavior is unchanged.

## [0.9.3] - 2026-07-14

### Changed

- Align validator container requirements with `pyproject.toml`: refresh
  `google-cloud-storage`, `google-auth`, FMU's `fmpy`, the SHACL RDF stack, and
  the Docker-cache-busting `validibot-shared==0.13.0` pin used by every backend.

## [0.9.2] - 2026-07-10

### Changed

- Publish shacl and schematron images (were missing from the matrix)

## [0.9.1] - 2026-07-10

### Changed


- Dependency refresh across the validators: `pyshacl` 0.31.0 → 0.40.0,
  `owlrl` 7.1.4 → 7.6.2, `rdflib` 7.6.0, `lxml` 6.1.1, `fmpy` 0.3.29 → 0.3.30,
  plus `google-cloud-storage`, `google-auth`, `ruff`, `mypy`, and the pinned
  GitHub Actions used in CI and the release workflow.

### Fixed

- **CI dependency resolution.** Bumped the CI-pinned `uv` from 0.5.16 to
  0.11.19 so the job-level `UV_NO_SOURCES=1` is actually honored. The old
  `uv` predated that environment variable and silently ignored it, so
  `uv sync` tried to resolve the local `../validibot-shared` path source
  (absent on the runner) instead of the published `validibot-shared` from
  the index, failing every CI run.
- **FMU integration tests** now `pytest.skip` cleanly when the gitignored
  `*.fmu` fixtures are absent (as in CI) instead of failing on a missing
  asset. They stay real integration tests where the fixtures exist locally.

### Notes

- **`saxonche` remains pinned at 12.9.0.** SaxonC-HE 13 tightens
  `allowedProtocols` enforcement so the vendored SchXslt2 transpiler can no
  longer be read under the in-process security lockdown (ADR-2026-07-01 D8b),
  which breaks the Schematron transform. Upgrading requires reworking
  `saxon_worker.py` to load its trusted stylesheets as strings rather than by
  file path; deferred to a future release.

## [0.9.0] - 2026-07-09

### Added

- New **Schematron validator backend** (`validator_backends/schematron/`,
  image `validibot-validator-backend-schematron`) per ADR-2026-07-01: the
  author's Schematron rules arrive **inline** in the input envelope
  (`inputs.schematron_text`); the container compiles them with the vendored
  **SchXslt2 1.11.1** transpiler (`schxslt2/transpile.xsl`, MIT — the
  maintained successor to the retired ISO skeleton and archived SchXslt 1;
  see `schxslt2/README.md` for provenance) and runs the resulting XSLT 3.0
  stylesheet over the XML submission using SaxonC-HE 12.9 (MPL-2.0; both
  license notices copied to `/app/THIRD_PARTY_NOTICES/`). The submission is
  re-guarded with the defusedxml posture; compile-and-run executes in a
  subprocess with a hard wall-clock timeout; failures map to the D9
  taxonomy — including `rules_invalid` for uploaded rules that fail to
  compile (either the transpile or the compile of the generated
  stylesheet) — instead of fabricated findings. Verified against the real
  OpenPeppol BIS Billing 3.0 schematron, embedded `xsl:function` helpers
  included. The `engine` provenance field reports both toolchain halves,
  e.g. `"SchXslt2 1.11.1 + SaxonC-HE 12.9"`.
- `schematron` optional-dependency extra (saxonche + defusedxml) wired into
  the `just test` recipes.

### Changed

- `validibot-shared` bumped to 0.12.0 (inline Schematron rules contract +
  the canonical SVRL parser).
- Clarified that `SchematronInputs.max_memory_mb` is advisory per run:
  Schematron memory is bounded at the container level by the Cloud Run Job's
  `--memory` allocation, not enforced per run (documented in
  `schematron/runner.py`). Timeout protection remains per-run (the worker
  subprocess wall-clock).

### Fixed

- `schematron` added to the justfile `validators` list, so `build-all` /
  `build-push-all` / `deploy-all` and the release CI matrix build and ship the
  Schematron backend image instead of silently skipping it.

### Security

- Schematron's Saxon worker denies URI-retrieval protocols
  (`allowedProtocols=""`) instead of allowing `file://`, so author-uploaded
  rules cannot use `doc()`, `document()`, `unparsed-text()`, or `collection()`
  to read arbitrary container-local files — or reach out over `http://`
  (SSRF) — and copy the result into SVRL output.
- New `engine.guard_rules()` re-applies the hardened-XML posture — defusedxml's
  no-DTD / no-entities / no-external-references stance, the ISO Schematron
  root-element check, and the size/depth caps — to the author's `.sch`
  **before anything parses it** (provenance detection *and* Saxon's compile),
  raising `rules_invalid` on a violation. The runner already re-guarded the
  submitted XML (D8a); it now guards the rules too (D8b), so a hostile rules
  document that reaches the container by any path (a forged envelope, an
  import/admin-created ruleset, a future non-form authoring surface) fails
  deterministically at this pre-guard rather than relying on Saxon's incidental
  rejection of a DTD.

## [0.7.1] - 2026-06-06

### Changed

- SHACL backend pySHACL timeout defaults raised to match the producer side:
  `DEFAULT_PYSHACL_TIMEOUT_SECONDS` 30 → 300 and
  `HARD_MAX_PYSHACL_TIMEOUT_SECONDS` 120 → 1800 (`shacl/engine.py`). These now
  mirror Django's `_DEFAULT_PYSHACL_TIMEOUT` / `_HARD_MAX_PYSHACL_TIMEOUT`
  (`validations/.../shacl/launch.py`) and the `validibot-shared`
  `SHACLInputs.pyshacl_timeout_seconds` envelope default. The previous 30s/120s
  caps were a backstop sized before real building models were tested; a graph
  near the 1M-triple cap can legitimately take minutes, so the old caps caused
  spurious timeouts. The 1800s hard cap stays below the container's 3600s outer
  timeout so pySHACL fails cleanly with a useful message.
- Local `just build` now builds for the host architecture by default (with
  `--load` for local testing) instead of forcing `linux/amd64`. On Apple
  Silicon an amd64 image runs under QEMU ~14× slower, pushing heavy validators
  past their wall-clock budgets and causing spurious local "timeouts".
  Production images (`build-push` / CI) remain pinned to `linux/amd64`; set
  `VALIDATOR_BUILD_PLATFORM` to force a platform for parity testing.

## [0.7.0] - 2026-06-03

### Added

- SHACL validator backend — isolated-container SHACL/RDF validation
  (`validator_backends/shacl/`), so untrusted RDF parsing and author-supplied
  SPARQL (SHACL-AF constraints + SPARQL-ASK assertions) execute in the
  container backend rather than in-process in the Django worker. Consumes the
  `validibot-shared` `SHACLInputEnvelope` contract. (Backfilled entry: 0.7.0
  was tagged and released without a changelog section.)

## [0.6.0] - 2026-05-03

### Added

- Release workflow `.github/workflows/release.yml`.
  On a signed tag push, the workflow verifies
  the tag signature and builds each backend image (energyplus +
  fmu) in parallel. Each image is published with full
  supply-chain provenance:
  - **GitHub Container Registry (default, free, public)** —
    pushed to `ghcr.io/<owner>/validibot-validator-backend-<validator>:vX.Y.Z`.
    Anyone running self-hosted Validibot can pull official
    images without third-party registry credentials.
  - **Google Artifact Registry (optional secondary mirror)** —
    when the operator configures `GCP_PROJECT_ID`,
    `GCP_REGION`, `GCP_GAR_REPOSITORY`,
    `GCP_WORKLOAD_IDENTITY_PROVIDER`, and
    `GCP_SERVICE_ACCOUNT_EMAIL`, the workflow mirrors the same
    image digest to GAR via `crane copy`. Used by Validibot
    cloud's deploy pipeline for latency / IAM reasons.
  - `actions/attest-build-provenance` writes a sigstore-signed
    attestation linking the image digest to this CI run.
  - `docker/build-push-action` with `provenance: mode=max` +
    `sbom: true` embeds a SPDX SBOM into the OCI image manifest
    via BuildKit's attestation API.
  - Per-backend SPDX SBOM uploaded to the GitHub release page.
  Maintainer recipe documented in
  `validibot-project/docs/operations/releasing/validibot-validator-backends.md`;
  operator-facing verification recipe in `RELEASING.md`.
- OpenSSF Scorecard workflow `.github/workflows/scorecard.yml`.
  Runs weekly + on push to `main`; publishes results to the public
  Scorecard dashboard. README badge added.
- `RELEASING.md` documenting the maintainer release recipe (sign
  tag → push → CI verifies + publishes) and the operator
  verification recipe (`gh attestation verify oci://<image>@<digest>`).

### Changed

- Bump `validibot-shared` 0.6.0 → 0.7.0.
  Added an optional ``StepValidatorRecord.validator_backend_image_digest``
  field on the producer-side evidence manifest schema. The validator
  backends don't use the new field (they only touch
  ``validibot_shared.{validations,fmu,energyplus}.envelopes``), but
  this repo keeps validibot-shared in lockstep with the validibot
  producer so cross-repo tooling sees a single version. Container
  images must be rebuilt to bake in the new wheel.

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
  variable, matching the Django side (`validibot/core/api/task_auth.py`).

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
  Django app maps these to `StepIODefinition` rows on ingestion.
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
