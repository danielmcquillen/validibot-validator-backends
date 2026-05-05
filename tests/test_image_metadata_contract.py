"""Static tests for validator backend image version metadata.

The Validibot app records validator backend image digests as the trust root.
Human-readable backend release/version information lives in OCI image labels,
stamped onto each container at build time from the Dockerfile's
``ARG VALIDATOR_BACKEND_VERSION`` default.

These tests pin the contract:

1. **Each backend's Dockerfile is the single source of truth for its
   version.** The ``ARG VALIDATOR_BACKEND_VERSION`` default is what
   stamps onto the image; there is no Python-side version constant
   to drift from it.
2. **The OCI label set is consistent across backends.** Every Dockerfile
   stamps ``org.opencontainers.image.{title,version,revision,source}``
   plus ``io.validibot.validator-backend.slug``.
3. **Build recipes don't shell out to a helper script.** Earlier in the
   project's history this lived in ``scripts/resolve-backend-image-version.py``
   (which read ``BACKEND_IMAGE_VERSION`` from each ``__metadata__.py``).
   That layer was deleted because the Dockerfile is the more honest
   single source of truth — no Python constant to drift, no AST parser
   to maintain, and the version of the bundled binary (e.g. EnergyPlus
   25.2.0) is *deliberately separate* from the wrapper backend's version
   (e.g. 0.1.0).
4. **Operator docs explain the override path.** Release engineering can
   override the Dockerfile default via ``--build-arg
   VALIDATOR_BACKEND_VERSION=...`` for ad-hoc / RC builds without
   editing source.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfiles_define_oci_version_labels():
    """Every first-party backend image must expose version/revision labels."""
    for path in sorted((REPO_ROOT / "validator_backends").glob("*/Dockerfile")):
        text = path.read_text(encoding="utf-8")

        assert "ARG VALIDATOR_BACKEND_VERSION" in text
        assert "ARG VALIDATOR_BACKEND_REVISION" in text
        assert 'org.opencontainers.image.version="${VALIDATOR_BACKEND_VERSION}"' in text
        assert (
            'org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}"' in text
        )
        assert "io.validibot.validator-backend.slug=" in text


def test_dockerfiles_bake_version_default():
    """The Dockerfile's ARG default IS the canonical backend version.

    Earlier we kept the version in a Python constant + a resolver
    script + a build-arg flow. That created a drift surface (constant
    must match Dockerfile must match recipe). The simpler design is:
    the Dockerfile's default is the version, period. ``docker inspect``
    reads it via the OCI label after build.

    Defaulting to ``"unknown"`` means an operator skipped the version
    bump or the Dockerfile is malformed — the test catches that.
    """
    for path in sorted((REPO_ROOT / "validator_backends").glob("*/Dockerfile")):
        text = path.read_text(encoding="utf-8")
        match = re.search(
            r'^ARG VALIDATOR_BACKEND_VERSION="([^"]+)"',
            text,
            re.MULTILINE,
        )
        assert match is not None, (
            f"{path} must declare ``ARG VALIDATOR_BACKEND_VERSION=\"<version>\"`` "
            "with an explicit default — that's the single source of truth."
        )
        version = match.group(1)
        assert version != "unknown", (
            f"{path} still has the placeholder default ``\"unknown\"``. "
            "Bake the actual backend wrapper version (e.g. \"0.1.0\")."
        )
        # Match a permissive semver-ish shape; we don't care about the
        # specific value, only that it's a real version string. Pre-
        # release suffixes (``0.1.0-rc1``) are accepted.
        assert re.match(r"^\d+\.\d+\.\d+", version), (
            f"{path} ARG default {version!r} should be vX.Y.Z[-suffix]."
        )


def test_metadata_does_not_redeclare_image_version():
    """``__metadata__.py`` must NOT carry a duplicate version constant.

    The Dockerfile is the single source. Re-introducing
    ``BACKEND_IMAGE_VERSION`` in metadata.py would put us back in the
    drift trap the simplification removed.
    """
    for path in sorted((REPO_ROOT / "validator_backends").glob("*/__metadata__.py")):
        text = path.read_text(encoding="utf-8")
        assert "BACKEND_IMAGE_VERSION" not in text, (
            f"{path} contains BACKEND_IMAGE_VERSION; the version belongs "
            "in the Dockerfile's ARG default, not here."
        )
        # ``get_metadata()`` should not surface a synthetic
        # ``backend_image_version`` either — that key implied a
        # Python-side source of truth that no longer exists.
        assert "backend_image_version" not in text, (
            f"{path} returns ``backend_image_version`` from get_metadata(); "
            "remove it. Operators read the version from the OCI image "
            "label via ``docker inspect``."
        )


def test_resolver_script_is_gone():
    """The ``resolve-backend-image-version.py`` helper must not return."""
    legacy = REPO_ROOT / "scripts" / "resolve-backend-image-version.py"
    assert not legacy.exists(), (
        f"{legacy} should have been deleted. The version is now read "
        "directly from each backend's Dockerfile."
    )


def test_justfile_does_not_call_legacy_resolver():
    """Build recipes must not shell out to the deleted resolver script."""
    text = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    assert "resolve-backend-image-version.py" not in text, (
        "justfile still references the deleted resolver script."
    )
    # Build recipes should still set the OTHER per-build args (revision,
    # slug) so OCI metadata stays useful — only the version comes from
    # the Dockerfile default.
    assert "--build-arg VALIDATOR_BACKEND_REVISION" in text
    assert "--build-arg VALIDATOR_BACKEND_SLUG" in text
    assert "VALIDATOR_VERSION=" not in text


def test_docs_explain_dockerfile_is_canonical_version_source():
    """README and RELEASING name the Dockerfile as the version source."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    releasing = (REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8")
    combined = f"{readme}\n{releasing}"

    assert "org.opencontainers.image.version" in combined
    # The Dockerfile is the version source.
    assert "ARG VALIDATOR_BACKEND_VERSION" in combined
    # Per-backend slug args are still per-backend.
    assert "--build-arg VALIDATOR_BACKEND_SLUG=energyplus" in combined
    assert "--build-arg VALIDATOR_BACKEND_SLUG=fmu" in combined
    # The wrapper-vs-bundled distinction must be explicit somewhere
    # in the docs — release engineers need to know that bumping
    # VALIDATOR_BACKEND_VERSION does not imply bumping the bundled
    # library version, and vice versa.
    assert "wrapper" in combined.lower(), (
        "Docs must explain the wrapper/bundled-library distinction "
        "so release engineers don't conflate the two version axes."
    )
