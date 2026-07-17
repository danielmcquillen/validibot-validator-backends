"""
SHACL Validator Metadata.

Describes the SHACL validator container for the launcher. Unlike EnergyPlus/FMU,
SHACL is light on CPU/memory — the container exists for *isolation*, not compute.
It parses untrusted RDF and runs author-supplied SPARQL (SHACL-AF constraints and
SPARQL-ASK assertions), which must never run next to the worker's credentials.
"""

from __future__ import annotations


# Validator identification
VALIDATOR_TYPE = "SHACL"
VALIDATOR_NAME = "SHACL RDF Graph Validator"
VALIDATOR_DESCRIPTION = (
    "Validates RDF graphs against SHACL shapes using pyshacl, and evaluates "
    "author-defined SPARQL-ASK assertions, in an isolated container."
)

# Container image naming (used by validibot to construct the image / job name).
# Full image name: {VALIDATOR_IMAGE_REGISTRY}/{IMAGE_NAME}:{tag}
IMAGE_NAME = "validibot-validator-backend-shacl"

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
    "text/turtle",
    "application/rdf+xml",
    "application/ld+json",
    "application/n-triples",
    "application/n-quads",
]

# No auxiliary resource files — shapes/ontology travel inline in the envelope.
REQUIRED_AUXILIARY_FILES = []

# Resource requirements. SHACL is memory-light relative to simulation backends;
# the triple/timeout caps in the envelope are the real DoS backstop. The 1-hour
# task timeout is a coarse upper bound — the per-run pyshacl/SPARQL timeouts fire
# long before that.
RESOURCE_REQUIREMENTS = {
    "memory_limit": "2Gi",
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
