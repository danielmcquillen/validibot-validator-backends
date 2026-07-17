"""
Schematron Validator Metadata.

Describes the Schematron validator container for the launcher. Like SHACL,
Schematron is light on CPU/memory — the container exists for *isolation*,
not compute. It compiles author-uploaded Schematron into XSLT (SchXslt2)
and executes it (Saxon) over untrusted submitted XML, which must never run
next to the worker's credentials.
"""

from __future__ import annotations


# Validator identification
VALIDATOR_TYPE = "SCHEMATRON"
VALIDATOR_NAME = "Schematron Validator"
VALIDATOR_DESCRIPTION = (
    "Compiles and runs author-uploaded Schematron rules (e.g. EN 16931 / "
    "Peppol BIS Billing 3.0 .sch files) against XML submissions using "
    "SaxonC-HE, in an isolated container."
)

# Container image naming (used by validibot to construct the image / job name).
# Full image name: {VALIDATOR_IMAGE_REGISTRY}/{IMAGE_NAME}:{tag}
IMAGE_NAME = "validibot-validator-backend-schematron"

# Environment variables the container expects.
ENV_VARS = {
    "VALIDIBOT_INPUT_URI": {
        "required": True,
        "description": "Storage URI to input envelope (gs:// or file://)",
    },
    "VALIDIBOT_OUTPUT_URI": {
        "required": False,
        "description": (
            "Storage URI for output envelope; if set, it must exactly match "
            "the input envelope contract"
        ),
    },
    "VALIDIBOT_RUN_ID": {
        "required": False,
        "description": "Validation run ID for logging and tracing",
    },
}

# Supported input MIME types (from validibot_shared.validations.envelopes).
SUPPORTED_INPUT_TYPES = [
    "application/xml",
    "text/xml",
]

# The author's Schematron rules arrive per run INLINE in the input envelope
# (inputs.schematron_text); this container compiles them with the vendored
# SchXslt2 transpiler (schxslt2/). No rules are ever baked into the image
# (ADR D4b).
REQUIRED_AUXILIARY_FILES = []

# Resource requirements. Schematron is memory-light; the D8 caps in the
# envelope (input size/depth, XSLT wall-clock, findings) are the real DoS
# backstop, re-clamped in engine.py. The 1-hour task timeout is a coarse
# upper bound — the per-run XSLT timeout fires long before that.
RESOURCE_REQUIREMENTS = {
    "memory_limit": "1Gi",
    "cpu_limit": "1.0",
    "timeout_seconds": 3600,
}

# Supported storage backends.
SUPPORTED_STORAGE_BACKENDS = ["gs://", "file://"]


def get_metadata() -> dict:
    """Return all metadata as a dictionary (for startup logging / introspection)."""
    return {
        "validator_type": VALIDATOR_TYPE,
        "validator_name": VALIDATOR_NAME,
        "validator_description": VALIDATOR_DESCRIPTION,
        "image_name": IMAGE_NAME,
        "env_vars": ENV_VARS,
        "supported_input_types": SUPPORTED_INPUT_TYPES,
        "required_auxiliary_files": REQUIRED_AUXILIARY_FILES,
        "resource_requirements": RESOURCE_REQUIREMENTS,
        "supported_storage_backends": SUPPORTED_STORAGE_BACKENDS,
    }
