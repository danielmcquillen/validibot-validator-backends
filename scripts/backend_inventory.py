#!/usr/bin/env python3
"""Validate and query the authoritative validator backend inventory.

``backends.toml`` is the only file that lists release-enabled backends, their
build inputs, and the released version offered to an installation. This helper
interprets that file for local builds and GitHub Actions; it never supplies a
second backend list or a fallback version.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY_PATH = REPO_ROOT / "backends.toml"
SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>"
    r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
BACKEND_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
PROVIDER_SLUG_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
RELEASE_TAG_PATTERN = re.compile(
    rf"^(?P<backend>[a-z][a-z0-9_]*)-v(?P<version>{SEMVER_PATTERN.pattern[1:-1]})$"
)


class InventoryError(ValueError):
    """The backend inventory or a requested release is invalid."""


@dataclass(frozen=True, slots=True)
class Backend:
    """One validated backend record read directly from ``backends.toml``."""

    values: dict[str, Any]

    @property
    def slug(self) -> str:
        """Return the application and source-code backend identifier."""
        return str(self.values["slug"])

    @property
    def release_version(self) -> str:
        """Return the released version offered by this repository checkout."""
        return str(self.values["release_version"])


def load_inventory(path: Path = DEFAULT_INVENTORY_PATH) -> dict[str, Any]:
    """Load an inventory file without adding defaults or inferred values."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InventoryError(f"Could not read backend inventory {path}: {exc}") from exc


def _required_string(record: dict[str, Any], key: str, *, backend: str) -> str:
    """Return one required non-empty string from a backend record."""
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"{backend}.{key} must be a non-empty string")
    return value


def _require_path(
    record: dict[str, Any],
    key: str,
    *,
    backend: str,
    repo_root: Path,
) -> None:
    """Require one inventory path to exist inside the backend repository."""
    relative = Path(_required_string(record, key, backend=backend))
    if relative.is_absolute() or ".." in relative.parts:
        raise InventoryError(f"{backend}.{key} must stay inside the repository")
    if not (repo_root / relative).exists():
        raise InventoryError(f"{backend}.{key} does not exist: {relative}")


def _shared_requirement_version(path: Path) -> str:
    """Read the exact ``validibot-shared`` requirement from one image file."""
    match = re.search(
        r"^validibot-shared==(?P<version>[^\s#]+)$",
        path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise InventoryError(f"{path} must pin validibot-shared exactly")
    return match.group("version")


def validate_inventory(
    document: dict[str, Any],
    *,
    repo_root: Path = REPO_ROOT,
) -> tuple[Backend, ...]:
    """Validate schema 2 and every build/release fact used by automation."""
    if document.get("schema_version") != 2:
        raise InventoryError("backends.toml schema_version must be 2")
    raw_backends = document.get("backend")
    if not isinstance(raw_backends, list) or not raw_backends:
        raise InventoryError("backends.toml must contain at least one [[backend]]")

    backends: list[Backend] = []
    slugs: set[str] = set()
    provider_slugs: set[str] = set()
    for raw_record in raw_backends:
        if not isinstance(raw_record, dict):
            raise InventoryError("every [[backend]] entry must be a table")
        slug = _required_string(raw_record, "slug", backend="<unknown>")
        if not BACKEND_SLUG_PATTERN.fullmatch(slug):
            raise InventoryError(f"{slug}.slug is not a normalized backend slug")
        if slug in slugs:
            raise InventoryError(f"duplicate backend slug: {slug}")
        slugs.add(slug)

        release_enabled = raw_record.get("release") is True
        if release_enabled:
            version = _required_string(raw_record, "release_version", backend=slug)
            if not SEMVER_PATTERN.fullmatch(version):
                raise InventoryError(
                    f"{slug}.release_version is not semantic versioning: {version}"
                )
            provider_slug = _required_string(
                raw_record,
                "provider_resource_slug",
                backend=slug,
            )
            if not PROVIDER_SLUG_PATTERN.fullmatch(provider_slug):
                raise InventoryError(f"{slug}.provider_resource_slug is not Cloud Run compatible")
            if provider_slug in provider_slugs:
                raise InventoryError(f"duplicate provider_resource_slug: {provider_slug}")
            provider_slugs.add(provider_slug)

        if "version_source" in raw_record:
            raise InventoryError(f"{slug}.version_source was removed by schema 2")
        for key in (
            "dockerfile",
            "requirements",
            "requirements_lock",
            "application_sbom",
            "test_path",
        ):
            _require_path(raw_record, key, backend=slug, repo_root=repo_root)

        image_name = _required_string(raw_record, "image_name", backend=slug)
        expected_image_name = f"validibot-validator-backend-{slug.replace('_', '-')}"
        if image_name != expected_image_name:
            raise InventoryError(
                f"{slug}.image_name must be {expected_image_name!r}, got {image_name!r}"
            )

        shared_contract = _required_string(
            raw_record,
            "shared_contract",
            backend=slug,
        )
        requirement_version = _shared_requirement_version(
            repo_root / str(raw_record["requirements"])
        )
        if shared_contract != requirement_version:
            raise InventoryError(
                f"{slug}.shared_contract {shared_contract!r} differs from "
                f"{raw_record['requirements']} ({requirement_version!r})"
            )

        dockerfile = (repo_root / str(raw_record["dockerfile"])).read_text(encoding="utf-8")
        declarations = re.findall(
            r"^ARG[ \t]+VALIDATOR_BACKEND_VERSION(?P<default>[ \t]*=.*)?$",
            dockerfile,
            flags=re.MULTILINE,
        )
        if declarations != [""]:
            raise InventoryError(
                f"{slug} Dockerfile must declare exactly "
                "ARG VALIDATOR_BACKEND_VERSION with no default"
            )
        backends.append(Backend(values=dict(raw_record)))
    return tuple(backends)


def validated_backends(path: Path = DEFAULT_INVENTORY_PATH) -> tuple[Backend, ...]:
    """Load and validate the inventory located at ``path``."""
    return validate_inventory(load_inventory(path), repo_root=path.resolve().parent)


def backend_by_slug(
    slug: str,
    *,
    path: Path = DEFAULT_INVENTORY_PATH,
) -> Backend:
    """Return one release-enabled backend or fail with a concrete message."""
    for backend in validated_backends(path):
        if backend.slug == slug:
            if backend.values.get("release") is not True:
                raise InventoryError(f"backend {slug!r} is not release-enabled")
            return backend
    raise InventoryError(f"backend {slug!r} is not listed in {path}")


def parse_release_tag(
    tag: str,
    *,
    path: Path = DEFAULT_INVENTORY_PATH,
) -> Backend:
    """Validate a backend-specific tag against the exact inventory version."""
    match = RELEASE_TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise InventoryError(
            "release tag must be {backend}-v{semantic-version}, for example energyplus-v0.17.0"
        )
    backend = backend_by_slug(match.group("backend"), path=path)
    tag_version = match.group("version")
    if tag_version != backend.release_version:
        raise InventoryError(
            f"tag {tag!r} says {tag_version}, but backends.toml offers "
            f"{backend.slug} {backend.release_version}"
        )
    return backend


def release_tags(
    path: Path = DEFAULT_INVENTORY_PATH,
) -> tuple[str, ...]:
    """Return current tags for every release-enabled backend in inventory order."""
    return tuple(
        f"{backend.slug}-v{backend.release_version}"
        for backend in validated_backends(path)
        if backend.values.get("release") is True
    )


def normalized_provider_version(version: str) -> str:
    """Encode a semantic version without losing pre-release/build boundaries."""
    match = SEMVER_PATTERN.fullmatch(version)
    if match is None:
        raise InventoryError(f"not a semantic version: {version}")
    normalized = f"{match.group('major')}-{match.group('minor')}-{match.group('patch')}"
    if match.group("prerelease"):
        normalized += "-pre-" + match.group("prerelease").replace(".", "-")
    if match.group("build"):
        normalized += "-build-" + match.group("build").replace(".", "-")
    return normalized


def provider_resource_name(
    *,
    kind: str,
    backend: Backend,
    stage: str,
) -> str:
    """Build and validate one bounded release-specific Cloud Run resource name."""
    prefix_by_kind = {"service": "vb-vs", "job": "vb-vj"}
    if kind not in prefix_by_kind:
        raise InventoryError("resource kind must be 'service' or 'job'")
    if stage not in {"dev", "staging", "prod"}:
        raise InventoryError("stage must be dev, staging, or prod")
    if stage == "dev":
        candidate = f"{prefix_by_kind[kind]}-{backend.values['provider_resource_slug']}-dev"
    else:
        candidate = (
            f"{prefix_by_kind[kind]}-"
            f"{backend.values['provider_resource_slug']}-v"
            f"{normalized_provider_version(backend.release_version)}"
        )
        if stage == "staging":
            candidate += "-stg"
    if len(candidate) > 63 or not PROVIDER_SLUG_PATTERN.fullmatch(candidate):
        raise InventoryError(
            f"{kind} resource name is not a valid bounded Cloud Run name: {candidate}"
        )
    return candidate


def release_output(backend: Backend, *, tag: str) -> dict[str, str]:
    """Return the exact selected record fields used by the release workflow."""
    values = backend.values
    return {
        "backend": backend.slug,
        "version": backend.release_version,
        "source_tag": tag,
        "dockerfile": str(values["dockerfile"]),
        "requirements": str(values["requirements"]),
        "requirements_lock": str(values["requirements_lock"]),
        "application_sbom": str(values["application_sbom"]),
        "test_path": str(values["test_path"]),
        "image_name": str(values["image_name"]),
        "shared_contract": str(values["shared_contract"]),
        "platform": str(values["platforms"][0]),
        "sbom": f"{values['image_name']}.spdx.json",
        "release_record": (f"{values['image_name']}-v{backend.release_version}.json"),
    }


def _parser() -> argparse.ArgumentParser:
    """Create the small command surface consumed by builds and CI."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inventory",
        type=Path,
        default=DEFAULT_INVENTORY_PATH,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")

    field_parser = subparsers.add_parser("field")
    field_parser.add_argument("backend")
    field_parser.add_argument("field")

    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("tag")
    release_parser.add_argument("--github-output", type=Path)

    subparsers.add_parser("release-tags")

    name_parser = subparsers.add_parser("provider-name")
    name_parser.add_argument("kind", choices=("service", "job"))
    name_parser.add_argument("backend")
    name_parser.add_argument("stage", choices=("dev", "staging", "prod"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one inventory query and print only non-secret deterministic data."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            backends = validated_backends(args.inventory)
            print(
                json.dumps(
                    [backend.values for backend in backends],
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "field":
            backend = backend_by_slug(args.backend, path=args.inventory)
            if args.field not in backend.values:
                raise InventoryError(
                    f"{backend.slug}.{args.field} is not present in backends.toml"
                )
            value = backend.values[args.field]
            if isinstance(value, (dict, list)):
                print(json.dumps(value, sort_keys=True))
            else:
                print(value)
            return 0
        if args.command == "release":
            backend = parse_release_tag(args.tag, path=args.inventory)
            output = release_output(backend, tag=args.tag)
            if args.github_output:
                with args.github_output.open("a", encoding="utf-8") as output_file:
                    for key, value in output.items():
                        output_file.write(f"{key}={value}\n")
            print(json.dumps(output, sort_keys=True))
            return 0
        if args.command == "release-tags":
            for tag in release_tags(args.inventory):
                print(tag)
            return 0
        if args.command == "provider-name":
            backend = backend_by_slug(args.backend, path=args.inventory)
            print(
                provider_resource_name(
                    kind=args.kind,
                    backend=backend,
                    stage=args.stage,
                )
            )
            return 0
    except InventoryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
