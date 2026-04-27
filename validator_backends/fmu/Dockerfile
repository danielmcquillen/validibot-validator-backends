# Cloud Run Job image for FMU validator
#
# Build context must be the repo root (not validator_backends/fmu/)
# Build with: docker build -f validator_backends/fmu/Dockerfile -t ... .

FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Add /app to PYTHONPATH so imports like "from validator_backends.core.x" work
ENV PYTHONPATH="/app:${PYTHONPATH}"

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
