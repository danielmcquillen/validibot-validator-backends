# Isolated low-compute backend for untrusted Portfolio Manager XLS/XLSX/XML/ZIP.
FROM python:3.13-slim

ARG VALIDATOR_BACKEND_VERSION="0.16.0"
ARG VALIDATOR_BACKEND_REVISION="unknown"
ARG VALIDATOR_BACKEND_SOURCE="https://github.com/danielmcquillen/validibot-validator-backends"
ARG VALIDATOR_BACKEND_SLUG="portfolio_manager"

LABEL org.opencontainers.image.title="Validibot Portfolio Manager validator backend" \
      org.opencontainers.image.version="${VALIDATOR_BACKEND_VERSION}" \
      org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}" \
      org.opencontainers.image.source="${VALIDATOR_BACKEND_SOURCE}" \
      io.validibot.validator-backend.slug="${VALIDATOR_BACKEND_SLUG}"

WORKDIR /app
ENV PYTHONPATH=/app

COPY validator_backends/portfolio_manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid 1000 --no-create-home validibot

COPY --chown=validibot:validibot validator_backends /app/validator_backends

USER validibot
CMD ["python", "-m", "validator_backends.core.entrypoint", "--backend-module", "validator_backends.portfolio_manager.main"]
