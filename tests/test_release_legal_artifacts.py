"""Tests for reproducible dependency and legal evidence in released images.

Released backends process customer files outside the Django process, so their
dependency inventory must be independently reviewable. These tests keep the
human-authored direct pins aligned with ``uv.lock``, require committed
application SBOMs, and ensure the release license policy accepts the actual
Python environment used by the test job.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.backend_artifacts import check, render_artifacts
from scripts.backend_inventory import validated_backends
from scripts.generate_legal_artifacts import installed_records, load_policy


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "legal" / "license-policy.toml"


def test_generated_locks_and_application_sboms_match_uv_lock():
    """A tagged release must not publish stale transitives or SBOM evidence."""
    backends = validated_backends()

    check(backends)

    for backend in backends:
        artifacts = render_artifacts(backend)
        requirements_path = REPO_ROOT / backend.values["requirements_lock"]
        sbom_path = REPO_ROOT / backend.values["application_sbom"]
        assert requirements_path in artifacts
        assert sbom_path in artifacts
        sbom = json.loads(artifacts[sbom_path])
        assert sbom["bomFormat"] == "CycloneDX"
        assert sbom["specVersion"] == "1.5"
        assert sbom["metadata"]["component"]["name"] == backend.values["image_name"]


def test_installed_python_environment_passes_reviewed_license_policy():
    """Unknown or newly disallowed runtime licenses must stop release CI."""
    policy = load_policy(POLICY_PATH)

    records = installed_records(policy)

    assert records
    assert all(record.license_expression in policy.allowed_expressions for record in records)


def test_every_backend_dockerfile_embeds_legal_artifacts_and_minimal_source():
    """No release image may omit notices or absorb unrelated backend files."""
    expected_base = (
        "python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
    )
    for backend in validated_backends():
        dockerfile = (REPO_ROOT / backend.values["dockerfile"]).read_text(encoding="utf-8")

        assert f"FROM {expected_base}" in dockerfile
        assert backend.values["requirements_lock"] in dockerfile
        assert backend.values["application_sbom"] in dockerfile
        assert "LICENSE NOTICE /app/legal/" in dockerfile
        assert "generate_legal_artifacts.py" in dockerfile
        assert (
            "COPY --chown=validibot:validibot validator_backends /app/validator_backends"
        ) not in dockerfile
