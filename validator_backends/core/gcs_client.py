"""
GCS client utilities for validator containers.

DEPRECATED: This module is kept for backwards compatibility.
New code should import from validator_backends.core.storage_client instead,
which supports both gs:// (GCS) and file:// (local filesystem) URIs.

All functions are re-exported from storage_client.py.
"""

from __future__ import annotations

# Re-export all functions from storage_client for backwards compatibility
from validator_backends.core.storage_client import (
    download_envelope,
    download_file,
    parse_gcs_uri,
    upload_directory,
    upload_envelope,
    upload_file,
)


__all__ = [
    "download_envelope",
    "download_file",
    "parse_gcs_uri",
    "upload_directory",
    "upload_envelope",
    "upload_file",
]
