# SchXslt2 transpiler (vendored tooling)

This directory holds **SchXslt2** — the Schematron-to-XSLT-3.0 transpiler
this container uses to compile an author's uploaded `.sch` source into the
runnable XSLT that produces SVRL. It plays the same role pyshacl plays for
the SHACL backend: engine tooling baked into the image, never user content.

## What's vendored

| File | Purpose |
| --- | --- |
| `transpile.xsl` | The whole transpiler — one XSLT 3.0 stylesheet |
| `LICENSE` | MIT, © David Maus (retain verbatim; also copied to `/app/THIRD_PARTY_NOTICES/` in the image) |
| `VERSION` | Release version, read at runtime for the `engine` provenance field |

Vendored from **SchXslt2 v1.11.1** — the `schxslt2-1.11.1.zip` asset at
<https://codeberg.org/SchXslt/schxslt2/releases>. The zip's `transpile.xsl`
was cross-checked against the `v1.11.1` git tag (identical except Maven's
`${project.version}` → `1.11.1` stamp on one line).

## Why SchXslt2

The classic ISO Schematron "skeleton" (`github.com/Schematron/schematron`)
is retired, and its recommended successor SchXslt 1 is archived too — the
GitHub URL in the skeleton's retirement notice is a dead link. SchXslt2
(same author, MIT) is the maintained implementation: a single-pass
transpiler that assembles `<include>`s, expands abstract patterns, and
emits an XSLT 3.0 validation stylesheet that reports in SVRL. It requires
an XSLT 3.0 processor, which this container's SaxonC-HE is. Official
`queryBinding="xslt2"` artefacts (EN 16931 / Peppol BIS Billing — including
their embedded `xsl:function` helpers) compile and run under it.

## Upgrading

Download the new release zip from the releases page, extract, and replace
`transpile.xsl`, `LICENSE`, and `VERSION` here — then run the Saxon test
suite (`pytest validator_backends/schematron/tests/`). The engine
(`engine.transpiler_available()`) and the Dockerfile build guard check for
`transpile.xsl`, so an image can never ship without it.
