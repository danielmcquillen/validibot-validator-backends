"""Strict request contract for private Cloud Run validator Services.

The request contains one durable attempt identity plus one transient,
attempt-scoped GCS capability.  Unknown fields are rejected so adding a new
piece of authority requires an explicit runtime and dispatcher change.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import UUID4, AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, SecretStr


class AttemptGCSCapability(BaseModel):
    """Short-lived bearer authority limited to one attempt storage prefix."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    access_token: Annotated[SecretStr, Field(min_length=1)]
    expires_at: AwareDatetime
    allowed_prefix: Annotated[str, Field(pattern=r"^gs://[^/]+/.+/$", max_length=2048)]
    project_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
    refresh_url: HttpUrl


class ServiceExecutionRequest(BaseModel):
    """One authenticated provider-task delivery for a pinned deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1]
    attempt_id: UUID4
    deployment_id: UUID4
    deployment_revision: Annotated[str, Field(min_length=1, max_length=128)]
    provider_resource_name: Annotated[str, Field(min_length=1, max_length=512)]
    provider_task_name: Annotated[str, Field(min_length=1, max_length=1024)]
    service_name: Annotated[
        str,
        Field(pattern=r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$"),
    ]
    service_revision: Annotated[str, Field(min_length=1, max_length=128)]
    backend_image_digest: Annotated[
        str,
        Field(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]
    input_uri: Annotated[str, Field(pattern=r"^gs://[^/]+/.+\.json$", max_length=2048)]
    timeout_at: AwareDatetime
    domain_timeout_seconds: Annotated[int, Field(ge=1, le=1500)]
    gcs_capability: AttemptGCSCapability
