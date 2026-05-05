# Multi-stage build for EnergyPlus™ validator container
#
# EnergyPlus™ is a trademark of the U.S. Department of Energy.
# EnergyPlus is distributed under a BSD-3-Clause license by NREL.
# The EnergyPlus license is preserved at /opt/energyplus/ in the built image.
# See: https://github.com/NREL/EnergyPlus/blob/develop/LICENSE.txt
#
# Build context must be the repo root (not validator_backends/energyplus/)
# Build with: docker build -f validator_backends/energyplus/Dockerfile -t ... .

# =============================================================================
# Stage 1: Build EnergyPlus
# =============================================================================
FROM ubuntu:22.04 AS energyplus-builder

# Install EnergyPlus build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    wget \
    git \
    && rm -rf /var/lib/apt/lists/*

# Download and install EnergyPlus 25.2.0 (latest as of Dec 2024)
# Update this URL for different EnergyPlus versions
# Check releases at: https://github.com/NREL/EnergyPlus/releases
RUN wget -q https://github.com/NREL/EnergyPlus/releases/download/v25.2.0/EnergyPlus-25.2.0-cf7368216c-Linux-Ubuntu22.04-x86_64.tar.gz \
    && tar -xzf EnergyPlus-25.2.0-cf7368216c-Linux-Ubuntu22.04-x86_64.tar.gz \
    && mv EnergyPlus-25.2.0-cf7368216c-Linux-Ubuntu22.04-x86_64 /opt/energyplus \
    && rm EnergyPlus-25.2.0-cf7368216c-Linux-Ubuntu22.04-x86_64.tar.gz

# =============================================================================
# Stage 2: Runtime container
# =============================================================================
FROM python:3.13-slim

# Backend wrapper version. This is OUR backend's version — bump it when the
# wrapper code, image layout, or output semantics change. It is INTENTIONALLY
# decoupled from the bundled EnergyPlus binary's upstream version (currently
# 25.2.0, downloaded above) — the upstream library can stay pinned while we
# iterate on the wrapper, and vice versa.
#
# Build-arg override is supported for release-engineering edge cases:
#   docker build --build-arg VALIDATOR_BACKEND_VERSION=0.1.1-rc1 ...
ARG VALIDATOR_BACKEND_VERSION="0.1.0"
ARG VALIDATOR_BACKEND_REVISION="unknown"
ARG VALIDATOR_BACKEND_SOURCE="https://github.com/danielmcquillen/validibot-validator-backends"
ARG VALIDATOR_BACKEND_SLUG="energyplus"

LABEL org.opencontainers.image.title="Validibot EnergyPlus validator backend" \
      org.opencontainers.image.version="${VALIDATOR_BACKEND_VERSION}" \
      org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}" \
      org.opencontainers.image.source="${VALIDATOR_BACKEND_SOURCE}" \
      io.validibot.validator-backend.slug="${VALIDATOR_BACKEND_SLUG}"

# Install runtime dependencies
# - libgomp1: OpenMP runtime for parallel processing
# - libx11-6, libxext6: X11 libs required by EnergyPlus binary
# - git: needed for pip install from GitHub
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libx11-6 \
    libxext6 \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy EnergyPlus from builder stage (includes LICENSE.txt for BSD-3-Clause compliance)
COPY --from=energyplus-builder /opt/energyplus /opt/energyplus

# Add EnergyPlus to PATH
ENV PATH="/opt/energyplus:${PATH}"

# Set working directory
WORKDIR /app

# Add /app to PYTHONPATH so imports like "from validator_backends.core.x" work.
# We set PYTHONPATH=/app rather than /app:${PYTHONPATH} because the base
# image doesn't define PYTHONPATH; referencing the undefined var triggers
# BuildKit's UndefinedVar lint warning. The validator container needs
# nothing else on PYTHONPATH, so a fixed value is correct.
ENV PYTHONPATH=/app

# Copy requirements first (for layer caching)
COPY validator_backends/energyplus/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user for container execution
RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid 1000 --no-create-home validibot

# Copy the entire validator_backends package (includes core, energyplus, and __init__.py)
# --chown ensures files are readable by the non-root user (UID 1000) when the
# Docker runner launches the container with user=1000:1000 and read_only=True.
COPY --chown=validibot:validibot validator_backends /app/validator_backends

# Create working directory for EnergyPlus runs (writable by validibot user)
# Note: when run with read_only=True + tmpfs on /tmp, this directory is
# recreated on the tmpfs mount, so ownership here is mainly for non-tmpfs usage.
RUN mkdir -p /tmp/energyplus_run && chown validibot:validibot /tmp/energyplus_run

USER validibot

# Run as a module so relative imports work correctly
CMD ["python", "-m", "validator_backends.energyplus.main"]
