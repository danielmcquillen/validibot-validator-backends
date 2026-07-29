# Cloud Run Job image for FMU validator
#
# Build context must be the repo root (not validator_backends/fmu/)
# Build with: docker build -f validator_backends/fmu/Dockerfile -t ... .

FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

# Backend wrapper version. This is OUR backend's version — bump it when the
# wrapper code, image layout, or output semantics change. It is INTENTIONALLY
# decoupled from upstream FMU library versions (which are pinned in
# requirements.txt) — the wrapper can iterate independently, and vice versa.
#
# Every build passes the release_version read from backends.toml.
ARG VALIDATOR_BACKEND_VERSION
ARG VALIDATOR_BACKEND_REVISION="unknown"
ARG VALIDATOR_BACKEND_SOURCE="https://github.com/mcquilleninteractive/validibot-validator-backends"
ARG VALIDATOR_BACKEND_SLUG="fmu"

LABEL org.opencontainers.image.title="Validibot FMU validator backend" \
      org.opencontainers.image.version="${VALIDATOR_BACKEND_VERSION}" \
      org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}" \
      org.opencontainers.image.source="${VALIDATOR_BACKEND_SOURCE}" \
      io.validibot.validator-backend.slug="${VALIDATOR_BACKEND_SLUG}"

WORKDIR /app

# Add /app to PYTHONPATH so imports like "from validator_backends.core.x" work.
# We set PYTHONPATH=/app rather than /app:${PYTHONPATH} because the base
# image doesn't define PYTHONPATH; referencing the undefined var triggers
# BuildKit's UndefinedVar lint warning. The validator container needs
# nothing else on PYTHONPATH, so a fixed value is correct.
ENV PYTHONPATH=/app

# Copy requirements first (for layer caching)
COPY validator_backends/fmu/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes --no-deps \
        -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

RUN mkdir -p /app/legal
COPY LICENSE NOTICE /app/legal/
COPY validator_backends/fmu/legal/APPLICATION-SBOM.cdx.json \
    /app/legal/APPLICATION-SBOM.cdx.json
COPY legal/license-policy.toml /tmp/license-policy.toml
COPY scripts/generate_legal_artifacts.py /tmp/generate_legal_artifacts.py
RUN python /tmp/generate_legal_artifacts.py \
        --policy /tmp/license-policy.toml \
        --output-dir /app/legal \
        --component-name "validibot-validator-backend-fmu" \
        --component-version "${VALIDATOR_BACKEND_VERSION}" \
    && rm /tmp/license-policy.toml /tmp/generate_legal_artifacts.py

# Create non-root user for container execution
RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid 1000 --no-create-home validibot

# Copy only the package marker, shared runtime, and this backend. The root
# .dockerignore excludes tests and caches inside these directories. Keeping
# unrelated backends out of the image prevents their fixtures and optional
# runtime material from becoming part of this release artifact.
RUN mkdir -p /app/validator_backends/core /app/validator_backends/fmu \
    && chown -R validibot:validibot /app/validator_backends
COPY --chown=validibot:validibot validator_backends/__init__.py \
    /app/validator_backends/__init__.py
COPY --chown=validibot:validibot validator_backends/core/*.py \
    /app/validator_backends/core/
COPY --chown=validibot:validibot validator_backends/fmu/*.py \
    /app/validator_backends/fmu/

USER validibot

# Run as a module so relative imports work correctly
CMD ["python", "-m", "validator_backends.core.entrypoint", "--backend-module", "validator_backends.fmu.main"]
