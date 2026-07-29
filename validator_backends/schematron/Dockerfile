# Cloud Run Job image for the Schematron validator.
#
# Build context must be the repo root (not validator_backends/schematron/):
#   docker build -f validator_backends/schematron/Dockerfile -t ... .
#
# This container exists for ISOLATION, not compute. It compiles
# author-supplied Schematron into XSLT (SchXslt2) and executes it
# (SaxonC-HE) over untrusted submitted XML, which must never run next to
# the Validibot worker's database credentials, identity, or network.
# Deploy it with a locked-down service account (no DB, no secrets) and
# egress blocked — that network boundary is the enforcement of last resort
# behind the in-process Saxon lockdown (ADR-2026-07-01 D8b).
#
# Rules are NEVER baked into this image: the author's Schematron source
# arrives per run inline in the input envelope, and this container compiles
# it with the vendored SchXslt2 transpiler (schxslt2/) before running it
# (D4b).

FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

# Backend wrapper version — decoupled from the Saxon pin in requirements.txt.
# Every build reads the value from backends.toml.
ARG VALIDATOR_BACKEND_VERSION
ARG VALIDATOR_BACKEND_REVISION="unknown"
ARG VALIDATOR_BACKEND_SOURCE="https://github.com/mcquilleninteractive/validibot-validator-backends"
ARG VALIDATOR_BACKEND_SLUG="schematron"

LABEL org.opencontainers.image.title="Validibot Schematron validator backend" \
      org.opencontainers.image.version="${VALIDATOR_BACKEND_VERSION}" \
      org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}" \
      org.opencontainers.image.source="${VALIDATOR_BACKEND_SOURCE}" \
      io.validibot.validator-backend.slug="${VALIDATOR_BACKEND_SLUG}"

WORKDIR /app

# Fixed PYTHONPATH so "from validator_backends.core.x" imports resolve. The base
# image doesn't define PYTHONPATH, so we set a fixed value rather than appending.
ENV PYTHONPATH=/app

# Copy requirements first (for layer caching).
COPY validator_backends/schematron/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes --no-deps \
        -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

# SaxonC-HE's wheel does not currently publish a license file in dist-info, so
# keep an explicit MPL/source notice alongside the generated package report.
RUN mkdir -p /app/legal/components
COPY LICENSE NOTICE /app/legal/
COPY legal/SAXONC-HE-NOTICE.md /app/legal/components/SAXONC-HE-NOTICE.md
COPY validator_backends/schematron/legal/APPLICATION-SBOM.cdx.json \
    /app/legal/APPLICATION-SBOM.cdx.json
COPY legal/license-policy.toml /tmp/license-policy.toml
COPY scripts/generate_legal_artifacts.py /tmp/generate_legal_artifacts.py
RUN python /tmp/generate_legal_artifacts.py \
        --policy /tmp/license-policy.toml \
        --output-dir /app/legal \
        --component-name "validibot-validator-backend-schematron" \
        --component-version "${VALIDATOR_BACKEND_VERSION}" \
    && rm /tmp/license-policy.toml /tmp/generate_legal_artifacts.py

# Non-root user for container execution (Cloud Run + the Docker runner launch
# with user=1000:1000 and read_only=True).
RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid 1000 --no-create-home validibot

# Copy only the package marker, shared runtime, and this backend. The root
# .dockerignore excludes tests and caches inside these directories. Keeping
# unrelated backends out of the image prevents their fixtures and optional
# runtime material from becoming part of this release artifact.
RUN mkdir -p /app/validator_backends/core \
        /app/validator_backends/schematron/schxslt2 \
    && chown -R validibot:validibot /app/validator_backends
COPY --chown=validibot:validibot validator_backends/__init__.py \
    /app/validator_backends/__init__.py
COPY --chown=validibot:validibot validator_backends/core/*.py \
    /app/validator_backends/core/
COPY --chown=validibot:validibot validator_backends/schematron/*.py \
    /app/validator_backends/schematron/
COPY --chown=validibot:validibot validator_backends/schematron/schxslt2 \
    /app/validator_backends/schematron/schxslt2

# SchXslt2 (MIT, vendored at validator_backends/schematron/schxslt2/) —
# copy its LICENSE beside the other legal artifacts for discoverability.
RUN mkdir -p /app/legal/components/schxslt2 \
    && cp /app/validator_backends/schematron/schxslt2/LICENSE \
        /app/legal/components/schxslt2/LICENSE

# Build-time guard: an image without the vendored SchXslt2 transpiler
# (schxslt2/transpile.xsl — see its README) cannot compile any Schematron,
# so fail the BUILD loudly rather than every run quietly.
RUN python -c "from validator_backends.schematron.engine import transpiler_available; \
    assert transpiler_available(), 'schxslt2/transpile.xsl missing — see schxslt2/README.md'"

USER validibot

# Run as a module so the Saxon worker subprocess
# (python -m validator_backends.schematron.saxon_worker) resolves correctly.
CMD ["python", "-m", "validator_backends.core.entrypoint", "--backend-module", "validator_backends.schematron.main"]
