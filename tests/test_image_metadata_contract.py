"""Static tests for validator backend image version metadata.

The Validibot app records validator backend image digests as the trust root.
Human-readable backend release/version information lives in OCI image labels.
Every build passes the selected backend's ``backends.toml`` release version to
the Dockerfile's required ``ARG VALIDATOR_BACKEND_VERSION``.

These tests pin the contract:

1. **``backends.toml`` is the single source of truth for versions.**
   Dockerfiles have no version default, and build paths pass the inventory
   value explicitly.
2. **The OCI label set is consistent across backends.** Every Dockerfile
   stamps ``org.opencontainers.image.{title,version,revision,source}``
   plus ``io.validibot.validator-backend.slug``.
3. **Build recipes don't shell out to a helper script.** Earlier in the
   project's history this lived in ``scripts/resolve-backend-image-version.py``
   (which read ``BACKEND_IMAGE_VERSION`` from each ``__metadata__.py``).
   That layer was deleted because there should be no Python constant or AST
   parser to maintain. Schema 2 now keeps the wrapper version in
   ``backends.toml`` and passes it into every build.
4. **Operator docs explain the required build argument.** Manual Docker builds
   read the selected inventory value instead of inventing a version.
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
        assert 'org.opencontainers.image.revision="${VALIDATOR_BACKEND_REVISION}"' in text
        assert "io.validibot.validator-backend.slug=" in text


def test_dockerfiles_require_inventory_version_build_argument():
    """No Dockerfile may retain an independent authoritative version."""
    for path in sorted((REPO_ROOT / "validator_backends").glob("*/Dockerfile")):
        text = path.read_text(encoding="utf-8")
        declarations = re.findall(
            r"^ARG[ \t]+VALIDATOR_BACKEND_VERSION(?:[ \t]*=.*)?$",
            text,
            flags=re.MULTILINE,
        )
        assert declarations == ["ARG VALIDATOR_BACKEND_VERSION"]


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
    # slug) so OCI metadata stays useful. The version must also be passed from
    # the inventory.
    assert "--build-arg VALIDATOR_BACKEND_REVISION" in text
    assert "--build-arg VALIDATOR_BACKEND_SLUG" in text
    assert "--build-arg VALIDATOR_BACKEND_VERSION" in text
    assert "VALIDATOR_VERSION=" not in text


def test_docs_explain_inventory_is_canonical_version_source():
    """README and RELEASING name backends.toml as the version source."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    releasing = (REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8")
    combined = f"{readme}\n{releasing}"

    assert "org.opencontainers.image.version" in combined
    assert "backends.toml" in combined
    assert "release_version" in combined
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
