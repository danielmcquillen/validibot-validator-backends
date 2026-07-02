"""Schematron validator backend (ADR-2026-07-01).

Compiles author-uploaded Schematron rules (e.g. EN 16931 / Peppol BIS
Billing 3.0 ``.sch`` files, arriving inline in the input envelope) with the
vendored SchXslt2 transpiler and runs the result against XML submissions
using SaxonC-HE, in an isolated container. The isolation is the point:
author-supplied rules become executable XSLT that runs over untrusted
submitted XML, and that must never happen next to the Validibot worker's
credentials, identity, or network.

Modules:

- ``main.py`` — container entrypoint (envelope in, envelope out, callback).
- ``runner.py`` — orchestration: download, guard, compile-and-run, parse.
- ``engine.py`` — guard/compile-and-run primitives + the D8 hard caps.
- ``saxon_worker.py`` — the SchXslt2 transpile + Saxon transform, run as a
  subprocess so the wall-clock timeout is a hard kill, not a polite request.
"""
