# Cloud Run Job image for the SHACL validator.
#
# Build context must be the repo root (not validator_backends/shacl/):
#   docker build -f validator_backends/shacl/Dockerfile -t ... .
#
# This container exists for ISOLATION, not compute. It parses untrusted RDF and
# runs author-supplied SPARQL, which must never run next to the Validibot
# worker's database credentials, identity, or network. Deploy it with a
# locked-down service account (no DB, no secrets) and egress blocked.

FROM python:3.13-slim

# Backend wrapper version — bump when wrapper code / image layout / output
# semantics change. Decoupled from the RDF library pins in requirements.txt.
ARG VALIDATOR_BACKEND_VERSION="0.15.0"
ARG VALIDATOR_BACKEND_REVISION="unknown"
ARG VALIDATOR_BACKEND_SOURCE="https://github.com/danielmcquillen/validibot-validator-backends"
ARG VALIDATOR_BACKEND_SLUG="shacl"

LABEL org.opencontainers.image.title="Validibot SHACL validator backend" \
      org.opencontainers.image.version="${VALIDATOR_BACKEND_VERSION}" \
      org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}" \
      org.opencontainers.image.source="${VALIDATOR_BACKEND_SOURCE}" \
      io.validibot.validator-backend.slug="${VALIDATOR_BACKEND_SLUG}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Fixed PYTHONPATH so "from validator_backends.core.x" imports resolve. The base
# image doesn't define PYTHONPATH, so we set a fixed value rather than appending.
ENV PYTHONPATH=/app

# Copy requirements first (for layer caching).
COPY validator_backends/shacl/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root user for container execution (Cloud Run + the Docker runner launch
# with user=1000:1000 and read_only=True).
RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid 1000 --no-create-home validibot

# Copy the entire validator_backends package (includes core, shacl, __init__.py).
COPY --chown=validibot:validibot validator_backends /app/validator_backends

USER validibot

# Run as a module so the subprocess workers (python -m validator_backends.shacl.*)
# resolve correctly.
CMD ["python", "-m", "validator_backends.core.entrypoint", "--backend-module", "validator_backends.shacl.main"]
