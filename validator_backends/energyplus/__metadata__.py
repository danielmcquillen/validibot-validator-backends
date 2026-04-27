"""
EnergyPlus Validator Metadata.

This module defines metadata about the EnergyPlus validator container,
used by the container launcher to configure execution.

The metadata is exposed at runtime so the orchestrator can query
container capabilities without running a full validation.
"""

from __future__ import annotations


# Validator identification
VALIDATOR_TYPE = "ENERGYPLUS"
VALIDATOR_NAME = "EnergyPlus Simulation Validator"
VALIDATOR_DESCRIPTION = (
    "Runs EnergyPlus building energy simulations and extracts "
    "performance metrics (energy use, temperatures, etc.)"
)

# Container image naming (used by validibot to construct image name)
# Full image name: {VALIDATOR_IMAGE_REGISTRY}/{IMAGE_NAME}:{tag}
IMAGE_NAME = "validibot-validator-backend-energyplus"

# Environment variables
# These are the environment variables the container expects
ENV_VARS = {
    "VALIDIBOT_INPUT_URI": {
        "required": True,
        "description": "Storage URI to input envelope (gs:// or file://)",
    },
    "VALIDIBOT_OUTPUT_URI": {
        "required": False,
        "description": "Storage URI for output envelope (optional, derived from input if not set)",
    },
    "VALIDIBOT_RUN_ID": {
        "required": False,
        "description": "Validation run ID for logging and tracing",
    },
}

# Supported input file types (MIME types from validibot_shared.validations.envelopes)
SUPPORTED_INPUT_TYPES = [
    "application/vnd.energyplus.idf",  # IDF text format
    "application/vnd.energyplus.epjson",  # epJSON format
]

# Required auxiliary files
REQUIRED_AUXILIARY_FILES = [
    {
        "role": "weather",
        "mime_type": "application/vnd.energyplus.epw",
        "description": "EnergyPlus weather file (.epw)",
    },
]

# Resource requirements (defaults, can be overridden by orchestrator)
RESOURCE_REQUIREMENTS = {
    "memory_limit": "4Gi",  # 4GB RAM
    "cpu_limit": "2.0",  # 2 CPU cores
    "timeout_seconds": 3600,  # 1 hour max
}

# Supported storage backends
SUPPORTED_STORAGE_BACKENDS = ["gs://", "file://"]


def get_metadata() -> dict:
    """
    Return all metadata as a dictionary.

    This can be called at container startup to log capabilities
    or by external tools to query validator features.
    """
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
