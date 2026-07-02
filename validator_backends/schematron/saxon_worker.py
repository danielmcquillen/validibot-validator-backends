"""Saxon Schematron worker — transpile the .sch, run it, write the SVRL.

Executed by ``engine.run_schematron`` via
``python -m validator_backends.schematron.saxon_worker <sch> <xml> <out>``
so the parent can enforce a **hard wall-clock timeout** over the whole
compile-and-run: SaxonC executes native code that Python signals cannot
interrupt, but a subprocess can always be killed. A worker crash (Saxon
abort, OOM) also stays contained — the parent maps it to the D9
engine-error taxonomy instead of taking the whole container down.

Compilation is the SchXslt2 transpiler (vendored under ``schxslt2/`` as
fixed engine tooling, like pyshacl is for SHACL — see its README for
provenance and upgrade instructions):

    .sch → transpile.xsl → (XSLT 3.0 validation stylesheet)
         → run over the submission → SVRL

SchXslt2 (MIT, <https://codeberg.org/SchXslt/schxslt2>) is the maintained
successor to both the classic ISO "skeleton" and SchXslt 1 — one
stylesheet that assembles includes, expands abstract patterns, and emits
the SVRL-producing XSLT in a single pass, run here under SaxonC-HE.

Security posture (ADR-2026-07-01 D8b): the author's rules get no ambient
authority. We apply SaxonC-HE's available lockdown configuration
best-effort (HE exposes fewer knobs than PE/EE), and the hard boundary is
the container itself: no network egress, read-only rootfs, locked-down
service account — so even a retrieval-function gap cannot reach anything.

Exit codes: 0 = SVRL written; 3 = the .sch failed to COMPILE (an authoring
problem → ``rules_invalid``); 2 = any other transform error.
"""

from __future__ import annotations

import sys
from pathlib import Path


# The vendored transpiler (see schxslt2/README.md).
SCHXSLT2_DIR = Path(__file__).parent / "schxslt2"
TRANSPILER_STYLESHEET = "transpile.xsl"

# SaxonC configuration properties applied best-effort before compiling.
# Property support varies across SaxonC-HE releases, so each is attempted
# independently; a property the build doesn't recognise is skipped (the
# container-level isolation is the enforcement of last resort).
# ``allowedProtocols`` is intentionally empty. The engine hands the submitted
# .sch and XML paths to Saxon directly through the API, so URI retrieval
# functions (doc(), document(), unparsed-text(), collection()) need no ambient
# file/http authority. Re-enable this only with a run-scoped resolver/bundle
# model; ``file`` here lets author rules read arbitrary container-local XML.
SECURE_PROCESSOR_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("http://saxon.sf.net/feature/allow-external-functions", "false"),
    ("http://saxon.sf.net/feature/xInclude-aware", "false"),
    ("http://saxon.sf.net/feature/allowedProtocols", ""),
)

EXIT_OK = 0
EXIT_TRANSFORM_ERROR = 2
EXIT_COMPILE_ERROR = 3
EXPECTED_ARG_COUNT = 3


def run(sch_path: str, xml_path: str, output_path: str) -> int:
    """Transpile the Schematron source, run it, write the SVRL report."""
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
            transpiler = xslt.compile_stylesheet(
                stylesheet_file=str(SCHXSLT2_DIR / TRANSPILER_STYLESHEET),
            )

            # ── Compile: transpile the .sch into its validation stylesheet ──
            # BOTH the transpile and the compile of its output count as the
            # "compile" phase: a malformed schema fails the transpile, while
            # a bad XPath inside a rule only surfaces when Saxon compiles
            # the generated stylesheet. Either way the author's rules are at
            # fault (rules_invalid), not the engine. The intermediate lives
            # beside the output file in the runner's per-run temp directory.
            work_dir = Path(output_path).parent
            try:
                validation_source = transpiler.transform_to_string(
                    source_file=sch_path,
                )
                if validation_source is None:
                    print(
                        "saxon_worker: transpile produced no output",
                        file=sys.stderr,
                    )
                    return EXIT_COMPILE_ERROR
                validation_path = work_dir / "validation_stylesheet.xsl"
                validation_path.write_text(validation_source, encoding="utf-8")
                executable = xslt.compile_stylesheet(
                    stylesheet_file=str(validation_path),
                )
            except PySaxonApiError as exc:
                print(
                    f"saxon_worker: schematron compile failed: {exc}",
                    file=sys.stderr,
                )
                return EXIT_COMPILE_ERROR

            # ── Run: execute the compiled rules over the submission ──
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
            "<sch_path> <xml_path> <output_path>",
            file=sys.stderr,
        )
        return EXIT_TRANSFORM_ERROR
    return run(argv[0], argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
