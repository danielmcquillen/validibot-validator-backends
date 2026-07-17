"""Helpers for uploading single report artifacts from backend output text."""

from __future__ import annotations

import tempfile
from pathlib import Path

from validator_backends.core.storage_client import upload_file
from validibot_shared.validations.envelopes import ValidationArtifact


def upload_text_report_artifact(
    *,
    content: str,
    execution_bundle_uri: str,
    filename: str,
    artifact_type: str,
    mime_type: str,
) -> ValidationArtifact | None:
    """Upload report text as one backend ``ValidationArtifact``.

    Backends often produce a canonical report as text inside their typed output
    model. This helper materializes those bytes into storage so Django can
    index the report through the normal produced-artifact path.
    """

    if not content:
        return None

    base_uri = execution_bundle_uri.rstrip("/")
    artifact_uri = f"{base_uri}/outputs/{filename}"

    with tempfile.TemporaryDirectory(prefix="validibot-report-") as tmp:
        report_path = Path(tmp) / filename
        report_path.write_text(content, encoding="utf-8")
        stored = upload_file(report_path, artifact_uri, content_type=mime_type)

    return ValidationArtifact(
        name=filename,
        type=artifact_type,
        mime_type=mime_type,
        uri=artifact_uri,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        storage_version=stored.storage_version,
    )
