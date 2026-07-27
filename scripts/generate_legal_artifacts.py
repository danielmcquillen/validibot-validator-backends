#!/usr/bin/env python3
"""Enforce image license policy and generate discoverable license artifacts.

The script inspects the distributions actually installed in an image, rather
than assuming that direct requirements describe the runtime. It rejects
unknown or unapproved licenses, writes a concise third-party report, and copies
wheel-provided license/notice files to one discoverable directory. The
lock-derived CycloneDX application SBOM is generated separately because an
image cannot contain a digest-accurate SBOM of itself without recursion.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = SCRIPT_ROOT / "legal" / "license-policy.toml"
NORMALIZED_NAME_PATTERN = re.compile(r"[-_.]+")
LICENSE_FILE_PATTERN = re.compile(
    r"(^|/)(license|licence|copying|notice)([._-].*)?$",
    flags=re.IGNORECASE,
)
LICENSE_ALIASES = {
    "Apache 2.0": "Apache-2.0",
    "Apache-2.0": "Apache-2.0",
    "BSD-3-Clause": "BSD-3-Clause",
    "BSD 3-Clause License": "BSD-3-Clause",
    "3-Clause BSD License": "BSD-3-Clause",
    "MIT": "MIT",
    "MPL-2.0": "MPL-2.0",
    "PSFL": "PSF-2.0",
}
CLASSIFIER_LICENSES = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}


class LicensePolicyError(ValueError):
    """An installed distribution has unknown or disallowed license metadata."""


@dataclass(frozen=True, slots=True)
class Policy:
    """Reviewed license expressions and package-specific metadata overrides."""

    allowed_expressions: frozenset[str]
    overrides: dict[str, str]
    source_urls: dict[str, str]


@dataclass(frozen=True, slots=True)
class DistributionRecord:
    """License-relevant metadata for one installed Python distribution."""

    name: str
    normalized_name: str
    version: str
    license_expression: str
    source_url: str
    distribution: importlib.metadata.Distribution


def normalized_name(value: str) -> str:
    """Return a stable PEP 503-style package name."""
    return NORMALIZED_NAME_PATTERN.sub("-", value).lower()


def load_policy(path: Path) -> Policy:
    """Load the small reviewed license-policy document."""
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise LicensePolicyError("license policy schema_version must be 1")
    return Policy(
        allowed_expressions=frozenset(document["allowed_expressions"]),
        overrides={
            normalized_name(name): expression
            for name, expression in document.get("overrides", {}).items()
        },
        source_urls={
            normalized_name(name): url for name, url in document.get("source_urls", {}).items()
        },
    )


def _project_url(distribution: importlib.metadata.Distribution) -> str:
    """Return the best published source/homepage URL in package metadata."""
    project_urls = distribution.metadata.get_all("Project-URL", [])
    preferred = ("source", "repository", "homepage", "documentation")
    parsed: dict[str, str] = {}
    for value in project_urls:
        label, separator, url = value.partition(",")
        if separator:
            parsed[label.strip().lower()] = url.strip()
    for label in preferred:
        if parsed.get(label):
            return parsed[label]
    return distribution.metadata.get("Home-page", "").strip()


def resolve_license(
    distribution: importlib.metadata.Distribution,
    policy: Policy,
) -> str:
    """Resolve one SPDX expression or fail on ambiguous package metadata."""
    name = distribution.metadata.get("Name", "")
    package = normalized_name(name)
    if package in policy.overrides:
        expression = policy.overrides[package]
    else:
        expression = distribution.metadata.get("License-Expression", "").strip()
        raw_license = distribution.metadata.get("License", "").strip()
        if not expression and raw_license:
            first_line = raw_license.splitlines()[0].strip()
            expression = LICENSE_ALIASES.get(first_line, "")
        if not expression:
            for classifier in distribution.metadata.get_all("Classifier", []):
                expression = CLASSIFIER_LICENSES.get(classifier, "")
                if expression:
                    break
    if not expression:
        raise LicensePolicyError(
            f"{name}=={distribution.version} has no reviewed license expression"
        )
    if expression not in policy.allowed_expressions:
        raise LicensePolicyError(
            f"{name}=={distribution.version} uses disallowed license {expression!r}"
        )
    return expression


def installed_records(policy: Policy) -> tuple[DistributionRecord, ...]:
    """Inspect and validate every installed Python distribution."""
    records: list[DistributionRecord] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "").strip()
        if not name:
            raise LicensePolicyError("installed distribution is missing its name")
        package = normalized_name(name)
        records.append(
            DistributionRecord(
                name=name,
                normalized_name=package,
                version=distribution.version,
                license_expression=resolve_license(distribution, policy),
                source_url=policy.source_urls.get(package) or _project_url(distribution),
                distribution=distribution,
            )
        )
    return tuple(sorted(records, key=lambda record: (record.normalized_name, record.version)))


def _copy_license_files(
    records: tuple[DistributionRecord, ...],
    output_dir: Path,
) -> dict[str, list[str]]:
    """Copy wheel-provided license files into the top-level legal directory."""
    copied: dict[str, list[str]] = {}
    licenses_dir = output_dir / "python-licenses"
    for record in records:
        names: list[str] = []
        for relative in record.distribution.files or ():
            relative_text = str(relative)
            if not LICENSE_FILE_PATTERN.search(relative_text):
                continue
            source = Path(record.distribution.locate_file(relative))
            if not source.is_file():
                continue
            destination_dir = licenses_dir / f"{record.normalized_name}-{record.version}"
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / source.name
            if destination.exists():
                destination = destination_dir / relative_text.replace("/", "__")
            shutil.copyfile(source, destination)
            names.append(str(destination.relative_to(output_dir)))
        copied[record.normalized_name] = sorted(names)
    return copied


def _render_report(
    records: tuple[DistributionRecord, ...],
    copied: dict[str, list[str]],
    *,
    component_name: str,
    component_version: str,
) -> str:
    """Render the human-readable installed-distribution license report."""
    lines = [
        "# Third-party Python license report",
        "",
        f"Component: `{component_name}` `{component_version}`",
        "",
        (
            "This report was generated from the Python distributions actually "
            "installed in the image. The image-level SPDX SBOM published with "
            "the release additionally inventories the base operating system."
        ),
        "",
        "| Package | Version | License | Source | Included license files |",
        "|---|---:|---|---|---|",
    ]
    for record in records:
        source = f"<{record.source_url}>" if record.source_url else "Not declared"
        license_files = ", ".join(f"`{name}`" for name in copied.get(record.normalized_name, []))
        lines.append(
            f"| {record.name} | {record.version} | "
            f"{record.license_expression} | {source} | "
            f"{license_files or 'No file published in wheel'} |"
        )
    lines.extend(
        [
            "",
            (
                "Package names and license expressions are informational. "
                "The copied license texts and upstream terms control."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def generate(
    records: tuple[DistributionRecord, ...],
    output_dir: Path,
    *,
    component_name: str,
    component_version: str,
) -> None:
    """Write the report and copy installed license files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    copied = _copy_license_files(records, output_dir)
    report = _render_report(
        records,
        copied,
        component_name=component_name,
        component_version=component_version,
    )
    (output_dir / "THIRD-PARTY-LICENSES.md").write_text(
        report,
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    """Check installed licenses and optionally generate image artifacts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--component-name", default="validibot-validator-backend")
    parser.add_argument("--component-version", default="unknown")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        records = installed_records(policy)
        if not args.check_only:
            if args.output_dir is None:
                raise LicensePolicyError("--output-dir is required unless --check-only")
            generate(
                records,
                args.output_dir,
                component_name=args.component_name,
                component_version=args.component_version,
            )
        print(f"License policy accepted {len(records)} installed distributions.")
    except (OSError, tomllib.TOMLDecodeError, LicensePolicyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
