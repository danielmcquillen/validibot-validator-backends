# Multi-stage build for EnergyPlus™ validator container
#
# EnergyPlus™ is a trademark of the U.S. Department of Energy.
# EnergyPlus is distributed under a BSD-3-Clause license by NREL.
# The EnergyPlus license is preserved at /opt/energyplus/ in the built image.
# See: https://github.com/NREL/EnergyPlus/blob/develop/LICENSE.txt
#
# Build context must be validibot-validators/ (not validators/energyplus/)
# Build with: docker build -f validators/energyplus/Dockerfile -t ... .

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

# Add /app to PYTHONPATH so imports like "from validators.core.x" work
ENV PYTHONPATH="/app:${PYTHONPATH}"

# Copy requirements first (for layer caching)
COPY validators/energyplus/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user for container execution
RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid 1000 --no-create-home validibot

# Copy the entire validators package (includes core, energyplus, and __init__.py)
# --chown ensures files are readable by the non-root user (UID 1000) when the
# Docker runner launches the container with user=1000:1000 and read_only=True.
COPY --chown=validibot:validibot validators /app/validators

# Create working directory for EnergyPlus runs (writable by validibot user)
# Note: when run with read_only=True + tmpfs on /tmp, this directory is
# recreated on the tmpfs mount, so ownership here is mainly for non-tmpfs usage.
RUN mkdir -p /tmp/energyplus_run && chown validibot:validibot /tmp/energyplus_run

USER validibot

# Run as a module so relative imports work correctly
CMD ["python", "-m", "validators.energyplus.main"]
