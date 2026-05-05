# Verifying a validator backend release

This document is for **operators** pulling validator backend
images and confirming their provenance. The maintainer's release
recipe (signing keys, GAR mirror setup, etc.) is internal
documentation; this file covers what you need to know as a
downstream consumer.

## Where the images live

Validator backend container images are published to **GitHub
Container Registry (GHCR)** at:

```
ghcr.io/danielmcquillen/validibot-validator-backend-<validator>:<tag>
```

GHCR is free for public images, has no rate limit on anonymous
pulls, and authenticates via standard Docker tooling. No GCP, AWS,
or third-party registry credentials required.

Available backends today:

- `ghcr.io/danielmcquillen/validibot-validator-backend-energyplus`
- `ghcr.io/danielmcquillen/validibot-validator-backend-fmu`

Each release publishes both `:vX.Y.Z` (immutable, recommended for
production) and `:latest` (mutable convenience pointer for
development).

## How image versions are set

The Docker containers do not read `VALIDATOR_VERSION` or
`VALIDATOR_BACKEND_VERSION` at runtime. The version is **image metadata
only** — baked into the OCI labels at build time from the Dockerfile's
`ARG VALIDATOR_BACKEND_VERSION` default:

- `org.opencontainers.image.version` ← `ARG VALIDATOR_BACKEND_VERSION`
  (the Dockerfile default)
- `org.opencontainers.image.revision` ← the commit SHA used for the
  build (passed in by the recipe)
- `io.validibot.validator-backend.slug` identifies which backend the
  image implements (`energyplus`, `fmu`, …)

There's no Python-side `BACKEND_IMAGE_VERSION` constant and no separate
version file. The Dockerfile is the single source of truth.

### Wrapper version vs bundled-library version

`VALIDATOR_BACKEND_VERSION` is **OUR backend wrapper's version** — the
container image, the entrypoint, the envelope handling. It is
*intentionally decoupled* from the upstream library version that the
wrapper bundles (e.g. EnergyPlus 25.2.0 inside the EnergyPlus backend
container).

A fresh release ships these values:

| Backend | Wrapper version | Bundled library |
|---|---|---|
| EnergyPlus | `0.1.0` (Dockerfile default) | EnergyPlus 25.2.0 (downloaded in the Dockerfile) |
| FMU | `0.1.0` (Dockerfile default) | fmpy (pinned in `requirements.txt`) |

Bumping the wrapper version does NOT imply bumping the bundled library,
and vice versa. They iterate independently.

### Bumping a backend's version

Edit the Dockerfile's `ARG` default — that's the only change required:

```dockerfile
# validator_backends/fmu/Dockerfile
ARG VALIDATOR_BACKEND_VERSION="0.1.1"   # was "0.1.0"
```

The next `just build fmu` (or `just build-push fmu`) stamps the new
version onto the image. Different backends can have different version
labels in the same repo release; they're independent files.

### Manual builds

If you build manually instead of using the just recipes, pass the
build-arg only when overriding the Dockerfile default — release-engineering
edge cases like RC builds:

```bash
# Use the Dockerfile default (the canonical version):
docker buildx build \
  --platform linux/amd64 \
  --load \
  -f validator_backends/fmu/Dockerfile \
  --build-arg VALIDATOR_BACKEND_REVISION="$(git rev-parse --short HEAD)" \
  --build-arg VALIDATOR_BACKEND_SLUG=fmu \
  -t validibot-validator-backend-fmu:0.1.0 \
  .

# Stamp an RC version instead:
docker buildx build \
  --platform linux/amd64 \
  --load \
  -f validator_backends/fmu/Dockerfile \
  --build-arg VALIDATOR_BACKEND_VERSION=0.1.1-rc1 \
  --build-arg VALIDATOR_BACKEND_REVISION="$(git rev-parse --short HEAD)" \
  --build-arg VALIDATOR_BACKEND_SLUG=fmu \
  -t validibot-validator-backend-fmu:0.1.1-rc1 \
  .
```

The image digest remains the exact trust root for a run; the version
label is for inventory and support readability.

## Two layers of provenance

Each release ships:

1. **A signed git tag** on the `validibot-validator-backends`
   repo, verifiable via `git verify-tag` against the repo's
   `.allowed_signers`.
2. **A sigstore build-provenance attestation** on the image
   digest, verifiable via `gh attestation verify`. This is what
   the runtime
   `VALIDATOR_BACKEND_IMAGE_POLICY=signed-digest` setting
   consumes when enabled.

The two layers stack: the signed git tag gates the CI run that
produces the image attestation, so a verified attestation
implicitly verifies the source commit.

## Verifying a release before deploy

```bash
# Pull the image. For production, prefer pinning by digest rather
# than tag — operators running with VALIDATOR_BACKEND_IMAGE_POLICY=digest
# require this anyway.
docker pull ghcr.io/danielmcquillen/validibot-validator-backend-energyplus:v0.6.0

# Resolve the digest:
DIGEST=$(crane digest \
  ghcr.io/danielmcquillen/validibot-validator-backend-energyplus:v0.6.0)
echo "Image digest: $DIGEST"

# Verify the sigstore attestation against the digest. This
# confirms the image was built by Validibot's GitHub Actions on
# the expected commit, signed via OIDC.
gh attestation verify \
  "oci://ghcr.io/danielmcquillen/validibot-validator-backend-energyplus@$DIGEST" \
  --owner danielmcquillen
# Expected output: "Verification succeeded!"
```

`gh attestation verify` exits 0 only when:

- A sigstore attestation exists for the digest.
- The attestation was signed via OIDC by
  `danielmcquillen/validibot-validator-backends`'s GitHub Actions
  identity.
- The attestation chain validates against the sigstore root.

This is the *runtime* gate — independent of
`VALIDATOR_BACKEND_IMAGE_POLICY`, which a Validibot deployment
uses to refuse to *launch* an image that doesn't satisfy the
policy.

## Pulling images for production-grade environments

### From GHCR directly (recommended for self-hosted)

```yaml
# docker-compose.yml or deployment manifest
services:
  validator-energyplus:
    image: ghcr.io/danielmcquillen/validibot-validator-backend-energyplus@sha256:abc123...
```

Pin by digest in production. The version-tag form is convenient
during development; production deployments should reference the
specific digest the deploy was tested against.

### Mirroring to a private registry (AWS ECR, Harbor, etc.)

If your deployment infrastructure prefers pulling from a
registry inside your network or cloud (latency, IAM, egress
billing), mirror the digest to your registry:

```bash
# Install crane (preserves digest across registries):
brew install crane   # or download from go-containerregistry releases

crane copy \
  ghcr.io/danielmcquillen/validibot-validator-backend-energyplus:v0.6.0 \
  111122223333.dkr.ecr.us-west-2.amazonaws.com/validibot-validator-backend-energyplus:v0.6.0
```

The image digest is preserved across the copy, so
`gh attestation verify oci://your-registry@<digest>` continues to
work — the attestation is bound to the bytes, not the registry
name.

### Air-gapped deployments

```bash
# On an internet-connected transit host:
docker pull ghcr.io/danielmcquillen/validibot-validator-backend-energyplus:v0.6.0
gh attestation verify \
  "oci://ghcr.io/danielmcquillen/validibot-validator-backend-energyplus:v0.6.0" \
  --owner danielmcquillen
docker save -o energyplus-v0.6.0.tar \
  ghcr.io/danielmcquillen/validibot-validator-backend-energyplus:v0.6.0

# Transfer the tarball through your air-gap process. On the
# air-gapped host:
docker load -i energyplus-v0.6.0.tar
```

Verification happens at the network boundary (the transit host),
since `gh attestation verify` requires internet access to query
the sigstore transparency log.

## What's in a release

For each backend, every signed-tag release publishes:

1. **A signed git tag** (`vX.Y.Z`) verifiable via
   `git verify-tag`.
2. **Two image tags on GHCR**: `vX.Y.Z` (immutable) and `latest`
   (mutable).
3. **A sigstore build-provenance attestation** on the image
   digest, queryable via `gh attestation verify`.
4. **A SLSA in-toto provenance attestation** embedded in the OCI
   image manifest itself, queryable via
   `docker buildx imagetools inspect <ref> --format '{{ json .Provenance }}'`.
5. **A SPDX SBOM** embedded in the OCI image manifest, queryable
   via `docker buildx imagetools inspect <ref> --format '{{ json .SBOM }}'`.
6. **A standalone SBOM artifact** attached to the GitHub release
   page (`validibot-validator-backend-<validator>.spdx.json`) for
   tools that prefer fetching SBOMs from a release page rather
   than from the registry.

## Checking image integrity in CI

For operators integrating Validibot into their own CI pipelines,
add an attestation-verify step before deploy:

```yaml
- name: Verify validator backend image
  run: |
    DIGEST=$(crane digest \
      ghcr.io/danielmcquillen/validibot-validator-backend-energyplus:v0.6.0)
    gh attestation verify \
      "oci://ghcr.io/danielmcquillen/validibot-validator-backend-energyplus@$DIGEST" \
      --owner danielmcquillen
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

This step exits non-zero if Validibot's CI didn't sign this exact
digest, blocking the deploy.

## Related repositories

- **`validibot`** — the Django application that orchestrates
  validator backend launches. See that repo's `RELEASING.md` for
  the source-clone verification recipe.
- **`validibot-shared`** — Pydantic models on PyPI. Verify via
  PyPI's OIDC attestation UI or the `pypi-attestations` CLI.

## ADR reference

The full architectural rationale for this release model lives in
[ADR-2026-04-27 §Phase 5 Session D](https://github.com/danielmcquillen/validibot/blob/main/docs/adr/2026-04-27-trust-boundary-hardening-and-evidence-first-validation.md)
in the `validibot-project` repository.
