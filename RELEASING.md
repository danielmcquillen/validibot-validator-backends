# Releasing validator backend images

This document describes how validator backend maintainers cut a
release and how operators verify the resulting Docker images. Trust
ADR Phase 5 Session D shipped signed-tag enforcement plus per-image
SBOMs and OIDC build provenance — this file is the operator-facing
doc that explains both the release recipe and the verification
recipe.

## TL;DR

```bash
# Maintainer (release):
git tag -s v1.2.3 -m "Release notes go here"
git push origin v1.2.3
# CI workflow .github/workflows/release.yml then:
#   1. verifies the tag is signed
#   2. builds each backend image (linux/amd64)
#   3. pushes to Google Artifact Registry
#   4. attaches a sigstore build-provenance attestation to the
#      image digest
#   5. extracts the BuildKit-attached SBOM and uploads it to the
#      GitHub release page

# Operator (verify before deploy):
git fetch --tags
git verify-tag v1.2.3
gh attestation verify oci://<image>@sha256:<digest> \
    --owner danielmcquillen
```

## Why this repo's provenance story differs from validibot/'s

`validibot/` (the Django app) ships as source via `git clone`, so
operators verify integrity via `git verify-tag` on the cloned
checkout. `validibot-validator-backends` (this repo) ships as
**Docker images**, so the verification target is the *image
digest*, not the *git commit*. The two layers stack:

- **Layer 1 (git tag)**: signed by the maintainer, verifies that a
  particular commit hash was approved for release.
- **Layer 2 (sigstore image attestation)**: signed by GitHub
  Actions OIDC, verifies that the *image bytes* were produced by
  *this exact CI run* on *that exact commit*.

An operator running with `VALIDATOR_BACKEND_IMAGE_POLICY=signed-digest`
checks Layer 2 at deploy time. Layer 1 is what gates the CI run that
produces Layer 2 in the first place.

## How the maintainer signs

Identical setup to `validibot/RELEASING.md` — see that file for the
SSH and GPG configuration. Once your git is configured to sign tags
by default (`tag.gpgsign true`), every release tag is signed
automatically.

## Cutting a release

1. Update `CHANGELOG.md` under `[Unreleased]` and bump the version
   in `pyproject.toml` (the validator backends repo's own version,
   not validibot-shared's).
2. Commit and push on a PR. After merge:
   ```bash
   git checkout main
   git pull
   ```
3. Sign and push the tag:
   ```bash
   git tag -s vX.Y.Z -m "vX.Y.Z release notes"
   git push origin vX.Y.Z
   ```
4. CI runs `.github/workflows/release.yml`:
   - Verifies the tag is signed.
   - For each validator (energyplus, fmu in parallel):
     - Builds the image with `docker buildx build`.
     - Pushes to GAR with both `vX.Y.Z` and `latest` tags.
     - `actions/attest-build-provenance` attaches a sigstore
       attestation linking the image digest to this CI run.
     - Extracts the BuildKit-attached SBOM and attaches it to the
       GitHub release page.

### Required CI configuration

The release workflow expects the following on the GitHub repo:

| Kind | Name | Purpose |
|---|---|---|
| Variable | `GCP_PROJECT_ID` | GAR project |
| Variable | `GCP_REGION` | GAR region (e.g. `us-west1`) |
| Variable | `GCP_GAR_REPOSITORY` | GAR repo name within the project |
| Secret | `GCP_WORKLOAD_IDENTITY_PROVIDER` | WIF provider path |
| Secret | `GCP_SERVICE_ACCOUNT_EMAIL` | SA with `roles/artifactregistry.writer` |

Workload Identity Federation lets GitHub Actions auth to GCP without
long-lived credentials. Set up via:
<https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines>

## How an operator verifies a deployed image

After pulling an image, an operator can confirm both layers of
provenance:

```bash
# Resolve the digest of the version tag — operators should always
# pin by digest in production, not by tag.
DIGEST=$(crane digest \
    "us-west1-docker.pkg.dev/proj/repo/validibot-validator-backend-energyplus:v1.2.3")
echo "Image digest: $DIGEST"

# Verify the sigstore build-provenance attestation. This confirms
# the image bytes were produced by Validibot's GitHub Actions on
# the expected commit.
gh attestation verify \
    "oci://us-west1-docker.pkg.dev/proj/repo/validibot-validator-backend-energyplus@$DIGEST" \
    --owner danielmcquillen
```

The `gh attestation verify` command exits 0 only when:

- A sigstore attestation exists for the digest.
- The attestation was signed via OIDC by `danielmcquillen/validibot-validator-backends`'s
  GitHub Actions identity.
- The attestation chain validates against the sigstore root.

This is the *runtime* gate — independent of `VALIDATOR_BACKEND_IMAGE_POLICY`,
which a Validibot deployment uses to refuse to *launch* an image
that doesn't satisfy the policy.

## What's in a release

Every signed-tag release publishes (for each backend):

1. **A signed git tag** — verifiable signature linking commit hash
   to maintainer.
2. **Two Docker image tags** in GAR: `vX.Y.Z` (immutable) and
   `latest` (mutable convenience pointer).
3. **A BuildKit-attached SBOM** in the OCI image manifest itself
   — pull with `docker buildx imagetools inspect <ref> --format '{{ json .SBOM }}'`.
4. **A sigstore build-provenance attestation** — verifiable with
   `gh attestation verify oci://<ref>@<digest>`.
5. **A standalone SBOM artifact** attached to the GitHub release
   page — for tools that prefer fetching SBOMs from a release
   page rather than the registry.

## Related docs

- `validibot/RELEASING.md` — sibling recipe for the Django app.
- `validibot-shared/CHANGELOG.md` — the wheel-publishing flow uses
  PyPI trusted publishing (OIDC) rather than git tag signatures.
- `validibot-project/docs/adr/2026-04-27-trust-boundary-hardening-and-evidence-first-validation.md`,
  Phase 5 Session D — the architectural decision behind this
  release process.
- `validibot/docs/architecture/validator-backend-trust-tiers.md` —
  how the trust tier and image policy interact at deploy time.
