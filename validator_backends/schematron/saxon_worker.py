"""Saxon XSLT transform worker — run as an isolated subprocess.

Executed by ``engine.run_transform`` via
``python -m validator_backends.schematron.saxon_worker <xslt> <xml> <out>``
so the parent can enforce a **hard wall-clock timeout**: SaxonC executes in
native code, which Python signals cannot interrupt mid-transform, but a
subprocess can always be killed. A worker crash (Saxon abort, OOM) also
stays contained — the parent maps it to the D9 engine-error taxonomy
instead of taking the whole container down.

Security posture (ADR-2026-07-01 D8b): the pack XSLT is curated/pinned but
still gets no ambient authority. We apply SaxonC-HE's available lockdown
configuration best-effort (HE exposes fewer knobs than PE/EE), and the hard
boundary is the container itself: no network egress, read-only rootfs,
locked-down service account — so even a retrieval-function gap cannot reach
anything.

Exit codes: 0 = SVRL written to the output path; 2 = compile/transform
error (detail on stderr).
"""

from __future__ import annotations

import sys
from pathlib import Path


# SaxonC configuration properties applied best-effort before compiling the
# stylesheet. Property support varies across SaxonC-HE releases, so each is
# attempted independently; a property the build doesn't recognise is skipped
# (the container-level isolation is the enforcement of last resort).
SECURE_PROCESSOR_PROPERTIES: tuple[tuple[str, str], ...] = (
    # Disable calls to reflexive/external functions from the stylesheet.
    ("http://saxon.sf.net/feature/allow-external-functions", "false"),
    # No XInclude processing while building source trees.
    ("http://saxon.sf.net/feature/xInclude", "false"),
)

EXIT_OK = 0
EXIT_TRANSFORM_ERROR = 2
EXPECTED_ARG_COUNT = 3


def run(xslt_path: str, xml_path: str, output_path: str) -> int:
    """Compile the pack stylesheet, transform the document, write the SVRL."""
    from saxonche import PySaxonApiError, PySaxonProcessor

    try:
        with PySaxonProcessor(license=False) as processor:
            for name, value in SECURE_PROCESSOR_PROPERTIES:
                try:
                    processor.set_configuration_property(name, value)
                except Exception as exc:
                    print(
                        f"saxon_worker: could not set {name}: {exc}",
                        file=sys.stderr,
                    )

            xslt = processor.new_xslt30_processor()
            executable = xslt.compile_stylesheet(stylesheet_file=xslt_path)
            svrl = executable.transform_to_string(source_file=xml_path)
    except PySaxonApiError as exc:
        print(f"saxon_worker: transform failed: {exc}", file=sys.stderr)
        return EXIT_TRANSFORM_ERROR

    if svrl is None:
        print("saxon_worker: transform produced no output", file=sys.stderr)
        return EXIT_TRANSFORM_ERROR

    Path(output_path).write_text(svrl, encoding="utf-8")
    return EXIT_OK


def main(argv: list[str]) -> int:
    if len(argv) != EXPECTED_ARG_COUNT:
        print(
            "usage: python -m validator_backends.schematron.saxon_worker "
            "<xslt_path> <xml_path> <output_path>",
            file=sys.stderr,
        )
        return EXIT_TRANSFORM_ERROR
    return run(argv[0], argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
