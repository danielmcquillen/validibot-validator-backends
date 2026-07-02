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

FROM python:3.13-slim

# Backend wrapper version — bump when wrapper code / image layout / output
# semantics change. Decoupled from the Saxon pin in requirements.txt.
ARG VALIDATOR_BACKEND_VERSION="0.1.0"
ARG VALIDATOR_BACKEND_REVISION="unknown"
ARG VALIDATOR_BACKEND_SOURCE="https://github.com/danielmcquillen/validibot-validator-backends"
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
COPY validator_backends/schematron/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# License compliance (ADR-2026-07-01 D4): SaxonC-HE is MPL-2.0; distributing
# it in this (AGPL-aggregate) image requires retaining Saxonica's LICENSE /
# NOTICES. pip already keeps them in the wheel's dist-info; this step copies
# them to a discoverable top-level path so image scanners and humans find
# them without spelunking site-packages.
RUN mkdir -p /app/THIRD_PARTY_NOTICES/saxonche \
    && cp -r /usr/local/lib/python3.13/site-packages/saxonche-*.dist-info/licenses/. \
        /app/THIRD_PARTY_NOTICES/saxonche/ 2>/dev/null \
    || cp /usr/local/lib/python3.13/site-packages/saxonche-*.dist-info/LICENSE* \
        /app/THIRD_PARTY_NOTICES/saxonche/ 2>/dev/null \
    || echo "WARNING: saxonche license files not found in dist-info" \
        > /app/THIRD_PARTY_NOTICES/saxonche/MISSING.txt

# Non-root user for container execution (Cloud Run + the Docker runner launch
# with user=1000:1000 and read_only=True).
RUN groupadd --gid 1000 validibot \
    && useradd --uid 1000 --gid 1000 --no-create-home validibot

# Copy the entire validator_backends package (includes core, schematron, __init__.py).
COPY --chown=validibot:validibot validator_backends /app/validator_backends

# SchXslt2 (MIT, vendored at validator_backends/schematron/schxslt2/) —
# copy its LICENSE beside the Saxon notices for the same discoverability.
RUN mkdir -p /app/THIRD_PARTY_NOTICES/schxslt2 \
    && cp /app/validator_backends/schematron/schxslt2/LICENSE \
        /app/THIRD_PARTY_NOTICES/schxslt2/LICENSE

# Build-time guard: an image without the vendored SchXslt2 transpiler
# (schxslt2/transpile.xsl — see its README) cannot compile any Schematron,
# so fail the BUILD loudly rather than every run quietly.
RUN python -c "from validator_backends.schematron.engine import transpiler_available; \
    assert transpiler_available(), 'schxslt2/transpile.xsl missing — see schxslt2/README.md'"

USER validibot

# Run as a module so the Saxon worker subprocess
# (python -m validator_backends.schematron.saxon_worker) resolves correctly.
CMD ["python", "-m", "validator_backends.schematron.main"]
