"""Tests for the authoritative validator backend inventory.

The supply-chain ADR makes ``backends.toml`` the source of backend membership.
These tests deliberately compare the manifest with the current handwritten
release and developer entry points so drift is caught before a release omits a
supported backend or publishes one with the wrong shared contract.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "backends.toml"


def _manifest() -> dict:
    """Load the committed backend inventory."""
    return tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _backends() -> list[dict]:
    """Return manifest backend records in declared order."""
    return list(_manifest()["backend"])


def _release_slugs() -> list[str]:
    """Extract the release matrix from the GitHub Actions workflow."""
    release_yml = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8",
    )
    match = re.search(r"validator:\s*\[([^\]]+)\]", release_yml)
    assert match, "release workflow must declare a validator matrix"
    return [slug.strip() for slug in match.group(1).split(",")]


def _justfile_slugs() -> list[str]:
    """Extract the developer/release backend list from the justfile."""
    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    match = re.search(r'^validators := "([^"]+)"$', justfile, flags=re.MULTILINE)
    assert match, "justfile must declare validators"
    return match.group(1).split()


def test_manifest_schema_and_paths_are_valid():
    """Every manifest entry must point at the files release tooling needs."""
    manifest = _manifest()
    assert manifest["schema_version"] == 1

    slugs = [backend["slug"] for backend in _backends()]
    assert len(slugs) == len(set(slugs))

    for backend in _backends():
        slug = backend["slug"]
        assert (REPO_ROOT / "validator_backends" / slug).is_dir()
        for key in ("dockerfile", "requirements", "test_path", "version_source"):
            assert (REPO_ROOT / backend[key]).exists(), f"{slug}.{key} is missing"
        assert backend["image_name"] == f"validibot-validator-backend-{slug}"
        assert backend["platforms"] == ["linux/amd64"]


def test_manifest_matches_release_and_developer_matrices():
    """Release and justfile membership must stay in sync with the manifest."""
    release_slugs = [
        backend["slug"] for backend in _backends() if backend.get("release") is True
    ]

    assert _release_slugs() == release_slugs
    assert _justfile_slugs() == release_slugs


def test_shared_contract_matches_backend_requirements():
    """Each backend's requirement pin must match its manifest contract version."""
    for backend in _backends():
        requirements = (REPO_ROOT / backend["requirements"]).read_text(
            encoding="utf-8",
        )
        expected_pin = f"validibot-shared=={backend['shared_contract']}"
        assert expected_pin in requirements


def test_dockerignore_excludes_local_build_noise_and_secrets():
    """The root build context must exclude local state and credential material."""
    ignored = set(
        (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines(),
    )

    for required in {
        ".git",
        ".venv",
        ".env",
        ".env.*",
        ".envs",
        ".pytest_cache",
        ".ruff_cache",
        "*.pem",
        "*.key",
        "dist",
        "build",
    }:
        assert required in ignored
