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

    The input envelope commits to the only permitted output URI. Self-hosted
    Docker may repeat that URI via ``VALIDIBOT_OUTPUT_URI``, but may not
    redirect the attempt to another location.

    Args:
        input_envelope: Input envelope with context.expected_output_uri

    Returns:
        URI where output.json should be uploaded

    Raises:
        AttributeError: If envelope doesn't have expected structure
        ValueError: If an environment override conflicts with the contract
    """
    # Check for explicit output URI (self-hosted Docker)
    expected_output_uri = str(input_envelope.context.expected_output_uri)
    output_uri = os.getenv("VALIDIBOT_OUTPUT_URI")
    if output_uri and output_uri != expected_output_uri:
        msg = (
            "VALIDIBOT_OUTPUT_URI conflicts with the input envelope: "
            f"expected {expected_output_uri!r}, got {output_uri!r}"
        )
        raise ValueError(msg)

    output_uri = output_uri or expected_output_uri

    logger.info("Output envelope will be uploaded to %s", output_uri)

    return output_uri
