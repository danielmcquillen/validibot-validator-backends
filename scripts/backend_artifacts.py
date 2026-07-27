#!/usr/bin/env python3
"""Generate reproducible runtime locks and application SBOMs for backend images.

The repository's ``uv.lock`` is the dependency authority. Each backend keeps a
small, human-reviewable direct-requirements file, while this helper exports the
complete transitive environment (including hashes) and a deterministic
CycloneDX application SBOM for the selected backend. Release CI runs ``check``
so stale generated artifacts cannot be published.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any

from scripts.backend_inventory import Backend


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
GENERATED_HEADER = "# Generated from uv.lock by scripts/backend_artifacts.py. Do not edit.\n"
EXACT_REQUIREMENT_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;#]+)$"
)
SBOM_NAMESPACE = uuid.UUID("9d4184e5-f802-4651-9ab8-43c6e402ee33")


class ArtifactError(ValueError):
    """A source requirement or generated backend artifact is invalid."""


def _normalized_name(value: str) -> str:
    """Return the PEP 503-normalized spelling used for comparisons."""
    return re.sub(r"[-_.]+", "-", value).lower()


def _exact_requirements(values: list[str], *, source: str) -> dict[str, str]:
    """Parse exact requirement strings without accepting ranges or URLs."""
    requirements: dict[str, str] = {}
    for value in values:
        match = EXACT_REQUIREMENT_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ArtifactError(f"{source} must use exact name==version pins: {value}")
        name = _normalized_name(match.group("name"))
        requirements[name] = match.group("version")
    return requirements


def expected_direct_requirements(backend: Backend) -> dict[str, str]:
    """Return root plus backend-extra pins declared in ``pyproject.toml``."""
    document = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    project = document["project"]
    values = list(project["dependencies"])
    optional = project.get("optional-dependencies", {})
    extra = backend.slug.replace("_", "-")
    values.extend(optional.get(extra, optional.get(backend.slug, [])))
    return _exact_requirements(values, source=f"pyproject.toml[{backend.slug}]")


def declared_direct_requirements(backend: Backend) -> dict[str, str]:
    """Return uncommented exact pins from a backend requirements source."""
    path = REPO_ROOT / str(backend.values["requirements"])
    values = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return _exact_requirements(values, source=str(path.relative_to(REPO_ROOT)))


def validate_direct_requirements(backend: Backend) -> None:
    """Fail when an image's declared roots differ from its locked project extra."""
    expected = expected_direct_requirements(backend)
    declared = declared_direct_requirements(backend)
    if declared != expected:
        missing = sorted(set(expected.items()) - set(declared.items()))
        unexpected = sorted(set(declared.items()) - set(expected.items()))
        raise ArtifactError(
            f"{backend.slug} direct requirements differ from pyproject.toml; "
            f"missing={missing}, unexpected={unexpected}"
        )


def _run_uv(arguments: list[str]) -> str:
    """Run a frozen uv export and return its UTF-8 output."""
    process = subprocess.run(
        ["uv", *arguments],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        raise ArtifactError(process.stderr.strip() or "uv export failed")
    return process.stdout


def render_requirements_lock(backend: Backend) -> str:
    """Render a complete hash-pinned requirements file from ``uv.lock``."""
    extra = backend.slug.replace("_", "-")
    exported = _run_uv(
        [
            "export",
            "--frozen",
            "--no-dev",
            "--extra",
            extra,
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
        ]
    )
    return GENERATED_HEADER + exported.lstrip()


def _replace_dependency_ref(value: Any, *, old: str, new: str) -> None:
    """Replace a CycloneDX root reference in nested dependency records."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "ref" and child == old:
                value[key] = new
            else:
                _replace_dependency_ref(child, old=old, new=new)
    elif isinstance(value, list):
        for child in value:
            _replace_dependency_ref(child, old=old, new=new)


def render_application_sbom(backend: Backend) -> str:
    """Render a deterministic CycloneDX 1.5 application-dependency SBOM."""
    extra = backend.slug.replace("_", "-")
    exported = _run_uv(
        [
            "--preview-features",
            "sbom-export",
            "export",
            "--frozen",
            "--no-dev",
            "--extra",
            extra,
            "--no-emit-project",
            "--format",
            "cyclonedx1.5",
        ]
    )
    document = json.loads(exported)
    metadata = document.setdefault("metadata", {})
    metadata.pop("timestamp", None)
    old_ref = str(metadata.get("component", {}).get("bom-ref", ""))
    new_ref = f"{backend.values['image_name']}@{backend.values['release_version']}"
    metadata["component"] = {
        "type": "application",
        "bom-ref": new_ref,
        "name": str(backend.values["image_name"]),
        "version": backend.release_version,
        "properties": [
            {
                "name": "io.validibot.validator-backend.slug",
                "value": backend.slug,
            },
            {
                "name": "io.validibot.shared-contract.version",
                "value": str(backend.values["shared_contract"]),
            },
        ],
    }
    _replace_dependency_ref(document.get("dependencies", []), old=old_ref, new=new_ref)
    identity = json.dumps(
        {
            "component": metadata["component"],
            "components": document.get("components", []),
            "dependencies": document.get("dependencies", []),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    document["serialNumber"] = f"urn:uuid:{uuid.uuid5(SBOM_NAMESPACE, identity)}"
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_artifacts(backend: Backend) -> dict[Path, str]:
    """Return every generated file and its expected content for one backend."""
    validate_direct_requirements(backend)
    return {
        REPO_ROOT / str(backend.values["requirements_lock"]): render_requirements_lock(backend),
        REPO_ROOT / str(backend.values["application_sbom"]): render_application_sbom(backend),
    }


def generate(backends: tuple[Backend, ...]) -> None:
    """Write generated artifacts for the selected backends."""
    for backend in backends:
        for path, content in render_artifacts(backend).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(path.relative_to(REPO_ROOT))


def check(backends: tuple[Backend, ...]) -> None:
    """Compare committed artifacts with fresh exports without rewriting them."""
    stale: list[str] = []
    for backend in backends:
        for path, expected in render_artifacts(backend).items():
            actual = path.read_text(encoding="utf-8") if path.exists() else None
            if actual != expected:
                stale.append(str(path.relative_to(REPO_ROOT)))
    if stale:
        raise ArtifactError(
            "generated backend artifacts are stale or missing: "
            + ", ".join(stale)
            + "; run scripts/backend_artifacts.py generate"
        )


def _selected_backends(slug: str | None) -> tuple[Backend, ...]:
    """Return all inventory records or one requested backend."""
    inventory = tomllib.loads((REPO_ROOT / "backends.toml").read_text(encoding="utf-8"))
    backends = tuple(Backend(values=dict(values)) for values in inventory["backend"])
    if slug is None:
        return backends
    selected = tuple(backend for backend in backends if backend.slug == slug)
    if not selected:
        raise ArtifactError(f"unknown backend: {slug}")
    return selected


def main(argv: list[str] | None = None) -> int:
    """Generate or verify artifacts for one backend or the entire inventory."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--backend")
    args = parser.parse_args(argv)
    try:
        backends = _selected_backends(args.backend)
        if args.command == "generate":
            generate(backends)
        else:
            check(backends)
            print("Generated backend artifacts are current.")
    except ArtifactError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
