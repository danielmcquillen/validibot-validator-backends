"""
Envelope loader utilities for validator containers.

Provides helpers for loading input envelopes from environment variables or
command-line arguments. Supports multiple deployment modes:

- Cloud Run Jobs: Uses VALIDIBOT_INPUT_URI environment variable
- Self-hosted Docker: Uses VALIDIBOT_INPUT_URI environment variable
- Manual testing: Accepts URI as first command-line argument

The loader checks for URIs in this order:
1. VALIDIBOT_INPUT_URI environment variable
2. First command-line argument (for manual testing)
"""

from __future__ import annotations

import logging
import os
import sys

from pydantic import BaseModel

from validator_backends.core.storage_client import download_envelope


logger = logging.getLogger(__name__)


def load_input_envelope[T: BaseModel](envelope_class: type[T]) -> T:
    """
    Load input envelope from environment variable or command-line argument.

    Checks for input URI in this order:
    1. VALIDIBOT_INPUT_URI environment variable
    2. First command-line argument (manual testing)

    Supports both gs:// (GCS) and file:// (local filesystem) URIs.

    Args:
        envelope_class: Pydantic model class to deserialize to

    Returns:
        Deserialized envelope instance

    Raises:
        ValueError: If no input URI is provided
        ValidationError: If JSON doesn't match envelope schema
    """
    # Check VALIDIBOT_INPUT_URI (standardized for all deployment targets)
    input_uri = os.getenv("VALIDIBOT_INPUT_URI")

    # Fall back to command-line argument (manual testing)
    if not input_uri and len(sys.argv) > 1:
        input_uri = sys.argv[1]

    if not input_uri:
        raise ValueError(
            "No input URI provided. Set VALIDIBOT_INPUT_URI "
            "environment variable, or pass URI as first argument."
        )

    logger.info("Loading input envelope from %s", input_uri)

    return download_envelope(input_uri, envelope_class)


def get_output_uri(input_envelope: BaseModel) -> str:
    """
    Get the output.json URI from input envelope's execution context.

    For self-hosted Docker deployments, the output URI can also be specified
    via VALIDIBOT_OUTPUT_URI environment variable.

    Args:
        input_envelope: Input envelope with context.execution_bundle_uri

    Returns:
        URI where output.json should be uploaded

    Raises:
        AttributeError: If envelope doesn't have expected structure
    """
    # Check for explicit output URI (self-hosted Docker)
    output_uri = os.getenv("VALIDIBOT_OUTPUT_URI")
    if output_uri:
        logger.info("Using output URI from environment: %s", output_uri)
        return output_uri

    # Fall back to deriving from execution bundle
    execution_bundle_uri = input_envelope.context.execution_bundle_uri

    # Ensure it ends with /
    if not execution_bundle_uri.endswith("/"):
        execution_bundle_uri += "/"

    output_uri = f"{execution_bundle_uri}output.json"

    logger.info("Output envelope will be uploaded to %s", output_uri)

    return output_uri
