# Cloud Run Job image for FMU validator
#
# Build context must be the repo root (not validator_backends/fmu/)
# Build with: docker build -f validator_backends/fmu/Dockerfile -t ... .

FROM python:3.13-slim

# Backend wrapper version. This is OUR backend's version — bump it when the
# wrapper code, image layout, or output semantics change. It is INTENTIONALLY
# decoupled from upstream FMU library versions (which are pinned in
# requirements.txt) — the wrapper can iterate independently, and vice versa.
#
# Build-arg override is supported for release-engineering edge cases:
#   docker build --build-arg VALIDATOR_BACKEND_VERSION=0.1.1-rc1 ...
ARG VALIDATOR_BACKEND_VERSION="0.1.0"
ARG VALIDATOR_BACKEND_REVISION="unknown"
ARG VALIDATOR_BACKEND_SOURCE="https://github.com/danielmcquillen/validibot-validator-backends"
ARG VALIDATOR_BACKEND_SLUG="fmu"

LABEL org.opencontainers.image.title="Validibot FMU validator backend" \
      org.opencontainers.image.version="${VALIDATOR_BACKEND_VERSION}" \
      org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}" \
      org.opencontainers.image.source="${VALIDATOR_BACKEND_SOURCE}" \
      io.validibot.validator-backend.slug="${VALIDATOR_BACKEND_SLUG}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Add /app to PYTHONPATH so imports like "from validator_backends.core.x" work.
# We set PYTHONPATH=/app rather than /app:${PYTHONPATH} because the base
# image doesn't define PYTHONPATH; referencing the undefined var triggers
# BuildKit's UndefinedVar lint warning. The validator container needs
# nothing else on PYTHONPATH, so a fixed value is correct.
ENV PYTHONPATH=/app

# Copy requirements first (for layer caching)
COPY validator_backends/fmu/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user for container execution
RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid 1000 --no-create-home validibot

# Copy the entire validator_backends package (includes core, fmu, and __init__.py)
# --chown ensures files are readable by the non-root user (UID 1000) when the
# Docker runner launches the container with user=1000:1000 and read_only=True.
COPY --chown=validibot:validibot validator_backends /app/validator_backends

USER validibot

# Run as a module so relative imports work correctly
CMD ["python", "-m", "validator_backends.fmu.main"]
