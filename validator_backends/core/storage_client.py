"""
Storage client utilities for validator containers.

Provides helpers for downloading input envelopes and uploading output envelopes
to various storage backends. Supports:

- gs:// - Google Cloud Storage (for production Cloud Run Jobs)
- file:// - Local filesystem (for self-hosted Docker deployments)

This module abstracts storage operations so validators work identically
whether running on GCP Cloud Run or self-hosted Docker.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from pydantic import BaseModel


logger = logging.getLogger(__name__)


# =============================================================================
# URI Parsing
# =============================================================================


def parse_uri(uri: str) -> tuple[str, str]:
    """
    Parse a storage URI into scheme and path.

    Args:
        uri: Storage URI like 'gs://bucket/path' or 'file:///path/to/file'

    Returns:
        Tuple of (scheme, path). For gs://, path includes bucket.
        For file://, path is the absolute filesystem path.

    Raises:
        ValueError: If URI scheme is not supported

    Examples:
        >>> parse_uri("gs://my-bucket/path/to/file.json")
        ('gs', 'my-bucket/path/to/file.json')
        >>> parse_uri("file:///app/storage/data.json")
        ('file', '/app/storage/data.json')
    """
    if uri.startswith("gs://"):
        return "gs", uri[5:]  # Remove 'gs://'
    if uri.startswith("file://"):
        return "file", uri[7:]  # Remove 'file://'

    raise ValueError(
        f"Unsupported URI scheme: {uri}. "
        "Supported schemes: gs:// (Google Cloud Storage), file:// (local filesystem)"
    )


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """
    Parse a GCS URI into bucket and blob path.

    Args:
        uri: GCS URI like 'gs://bucket-name/path/to/file.json'

    Returns:
        Tuple of (bucket_name, blob_path)

    Raises:
        ValueError: If URI is not a valid GCS URI
    """
    if not uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI (must start with gs://): {uri}")

    uri_without_scheme = uri[5:]  # Remove 'gs://'
    parts = uri_without_scheme.split("/", 1)

    expected_parts = 2
    if len(parts) != expected_parts:
        raise ValueError(f"Invalid GCS URI (missing path): {uri}")

    bucket_name, blob_path = parts
    return bucket_name, blob_path


# =============================================================================
# Envelope Operations
# =============================================================================


def download_envelope[T: BaseModel](uri: str, envelope_class: type[T]) -> T:
    """
    Download and deserialize a Pydantic envelope from storage.

    Supports both gs:// (GCS) and file:// (local filesystem) URIs.

    Args:
        uri: Storage URI to the envelope JSON file
        envelope_class: Pydantic model class to deserialize to

    Returns:
        Deserialized envelope instance

    Raises:
        ValueError: If URI is invalid or file doesn't exist
        ValidationError: If JSON doesn't match envelope schema
    """
    logger.info("Downloading envelope from %s", uri)

    scheme, path = parse_uri(uri)

    if scheme == "gs":
        json_content = _download_gcs_text(uri)
    elif scheme == "file":
        json_content = _read_local_file(path)
    else:
        raise ValueError(f"Unsupported URI scheme: {scheme}")

    envelope = envelope_class.model_validate_json(json_content)

    logger.info(
        "Successfully loaded %s envelope (run_id=%s)",
        envelope_class.__name__,
        getattr(envelope, "run_id", "unknown"),
    )

    return envelope


def upload_envelope(envelope: BaseModel, uri: str) -> None:
    """
    Serialize and upload a Pydantic envelope to storage.

    Supports both gs:// (GCS) and file:// (local filesystem) URIs.

    Args:
        envelope: Pydantic model instance to upload
        uri: Storage URI where the envelope should be uploaded

    Raises:
        ValueError: If URI is invalid
    """
    logger.info("Uploading %s to %s", envelope.__class__.__name__, uri)

    # Serialize to JSON
    json_content = envelope.model_dump_json(indent=2, exclude_none=True)

    scheme, path = parse_uri(uri)

    if scheme == "gs":
        _upload_gcs_text(uri, json_content)
    elif scheme == "file":
        _write_local_file(path, json_content)
    else:
        raise ValueError(f"Unsupported URI scheme: {scheme}")

    logger.info("Successfully uploaded envelope to %s", uri)


# =============================================================================
# File Operations
# =============================================================================


def download_file(uri: str, destination: Path) -> None:
    """
    Download a file from storage to local filesystem.

    Supports both gs:// (GCS) and file:// (local filesystem) URIs.

    Args:
        uri: Storage URI to the file
        destination: Local path where file should be saved

    Raises:
        ValueError: If URI is invalid or file doesn't exist
    """
    logger.info("Downloading file from %s to %s", uri, destination)

    scheme, path = parse_uri(uri)

    # Ensure parent directory exists
    destination.parent.mkdir(parents=True, exist_ok=True)

    if scheme == "gs":
        _download_gcs_file(uri, destination)
    elif scheme == "file":
        _copy_local_file(Path(path), destination)
    else:
        raise ValueError(f"Unsupported URI scheme: {scheme}")

    logger.info(
        "Successfully downloaded file to %s (%d bytes)",
        destination,
        destination.stat().st_size,
    )


def upload_file(source: Path, uri: str, content_type: str | None = None) -> None:
    """
    Upload a file from local filesystem to storage.

    Supports both gs:// (GCS) and file:// (local filesystem) URIs.

    Args:
        source: Local path to the file
        uri: Storage URI where file should be uploaded
        content_type: Optional MIME type for the file (used for GCS only)

    Raises:
        ValueError: If URI is invalid or source file doesn't exist
    """
    if not source.exists():
        raise ValueError(f"Source file does not exist: {source}")

    logger.info("Uploading file from %s to %s", source, uri)

    scheme, path = parse_uri(uri)

    if scheme == "gs":
        _upload_gcs_file(source, uri, content_type)
    elif scheme == "file":
        _copy_local_file(source, Path(path))
    else:
        raise ValueError(f"Unsupported URI scheme: {scheme}")

    logger.info("Successfully uploaded file to %s (%d bytes)", uri, source.stat().st_size)


def upload_directory(
    source_dir: Path, base_uri: str, manifest_path: str = "manifest.json"
) -> dict:
    """
    Upload an entire directory to storage and create a manifest.

    Supports both gs:// (GCS) and file:// (local filesystem) URIs.

    Args:
        source_dir: Local directory to upload
        base_uri: Storage URI prefix (e.g., 'gs://bucket/path/' or 'file:///app/storage/')
        manifest_path: Relative path for manifest file within base_uri

    Returns:
        Manifest dict with file listings

    Raises:
        ValueError: If source_dir doesn't exist or base_uri is invalid
    """
    if not source_dir.exists():
        raise ValueError(f"Source directory does not exist: {source_dir}")

    logger.info("Uploading directory %s to %s", source_dir, base_uri)

    # Ensure base_uri ends with /
    if not base_uri.endswith("/"):
        base_uri += "/"

    files_uploaded = []

    # Upload all files in directory
    for file_path in source_dir.rglob("*"):
        if file_path.is_file():
            # Calculate relative path from source_dir
            rel_path = file_path.relative_to(source_dir)
            file_uri = f"{base_uri}{rel_path.as_posix()}"

            # Upload file
            upload_file(file_path, file_uri)

            files_uploaded.append(
                {
                    "name": rel_path.as_posix(),
                    "uri": file_uri,
                    "size_bytes": file_path.stat().st_size,
                }
            )

    # Create manifest
    manifest = {
        "format": "directory",
        "base_uri": base_uri,
        "files": files_uploaded,
        "total_files": len(files_uploaded),
        "total_bytes": sum(f["size_bytes"] for f in files_uploaded),
    }

    # Upload manifest
    manifest_uri = f"{base_uri}{manifest_path}"
    scheme, path = parse_uri(manifest_uri)

    manifest_json = json.dumps(manifest, indent=2)
    if scheme == "gs":
        _upload_gcs_text(manifest_uri, manifest_json)
    elif scheme == "file":
        _write_local_file(path, manifest_json)

    logger.info("Uploaded %d files, manifest at %s", len(files_uploaded), manifest_uri)

    manifest["manifest_uri"] = manifest_uri
    return manifest


# =============================================================================
# Local Filesystem Helpers
# =============================================================================


def _read_local_file(path: str) -> str:
    """Read text content from a local file."""
    file_path = Path(path)
    if not file_path.exists():
        raise ValueError(f"File not found: {path}")
    return file_path.read_text(encoding="utf-8")


def _write_local_file(path: str, content: str) -> None:
    """Write text content to a local file."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def _copy_local_file(source: Path, destination: Path) -> None:
    """Copy a file from source to destination on local filesystem."""
    if not source.exists():
        raise ValueError(f"Source file not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


# =============================================================================
# Google Cloud Storage Helpers
# =============================================================================


def _get_gcs_client():
    """Get or create a GCS client (lazy import to avoid requiring GCS in local mode)."""
    from google.cloud import storage

    return storage.Client()


def _download_gcs_text(uri: str) -> str:
    """Download text content from GCS."""
    bucket_name, blob_path = parse_gcs_uri(uri)
    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    if not blob.exists():
        raise ValueError(f"File not found at {uri}")

    return blob.download_as_text()


def _upload_gcs_text(uri: str, content: str) -> None:
    """Upload text content to GCS."""
    bucket_name, blob_path = parse_gcs_uri(uri)
    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(content, content_type="application/json")


def _download_gcs_file(uri: str, destination: Path) -> None:
    """Download a file from GCS to local filesystem."""
    bucket_name, blob_path = parse_gcs_uri(uri)
    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    if not blob.exists():
        raise ValueError(f"File not found at {uri}")

    blob.download_to_filename(str(destination))


def _upload_gcs_file(source: Path, uri: str, content_type: str | None = None) -> None:
    """Upload a file to GCS."""
    bucket_name, blob_path = parse_gcs_uri(uri)
    client = _get_gcs_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(str(source), content_type=content_type)
