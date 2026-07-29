# Cloud Run Job image for the SHACL validator.
#
# Build context must be the repo root (not validator_backends/shacl/):
#   docker build -f validator_backends/shacl/Dockerfile -t ... .
#
# This container exists for ISOLATION, not compute. It parses untrusted RDF and
# runs author-supplied SPARQL, which must never run next to the Validibot
# worker's database credentials, identity, or network. Deploy it with a
# locked-down service account (no DB, no secrets) and egress blocked.

FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

# Backend wrapper version — decoupled from the RDF library pins in
# requirements.txt. Every build reads the value from backends.toml.
ARG VALIDATOR_BACKEND_VERSION
ARG VALIDATOR_BACKEND_REVISION="unknown"
ARG VALIDATOR_BACKEND_SOURCE="https://github.com/mcquilleninteractive/validibot-validator-backends"
ARG VALIDATOR_BACKEND_SLUG="shacl"

LABEL org.opencontainers.image.title="Validibot SHACL validator backend" \
      org.opencontainers.image.version="${VALIDATOR_BACKEND_VERSION}" \
      org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}" \
      org.opencontainers.image.source="${VALIDATOR_BACKEND_SOURCE}" \
      io.validibot.validator-backend.slug="${VALIDATOR_BACKEND_SLUG}"

WORKDIR /app

# Fixed PYTHONPATH so "from validator_backends.core.x" imports resolve. The base
# image doesn't define PYTHONPATH, so we set a fixed value rather than appending.
ENV PYTHONPATH=/app

# Copy requirements first (for layer caching).
COPY validator_backends/shacl/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes --no-deps \
        -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

RUN mkdir -p /app/legal
COPY LICENSE NOTICE /app/legal/
COPY validator_backends/shacl/legal/APPLICATION-SBOM.cdx.json \
    /app/legal/APPLICATION-SBOM.cdx.json
COPY legal/license-policy.toml /tmp/license-policy.toml
COPY scripts/generate_legal_artifacts.py /tmp/generate_legal_artifacts.py
RUN python /tmp/generate_legal_artifacts.py \
        --policy /tmp/license-policy.toml \
        --output-dir /app/legal \
        --component-name "validibot-validator-backend-shacl" \
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
RUN mkdir -p /app/validator_backends/core /app/validator_backends/shacl \
    && chown -R validibot:validibot /app/validator_backends
COPY --chown=validibot:validibot validator_backends/__init__.py \
    /app/validator_backends/__init__.py
COPY --chown=validibot:validibot validator_backends/core/*.py \
    /app/validator_backends/core/
COPY --chown=validibot:validibot validator_backends/shacl/*.py \
    /app/validator_backends/shacl/

USER validibot

# Run as a module so the subprocess workers (python -m validator_backends.shacl.*)
# resolve correctly.
CMD ["python", "-m", "validator_backends.core.entrypoint", "--backend-module", "validator_backends.shacl.main"]
