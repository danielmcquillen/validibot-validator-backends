"""Schematron validator backend (ADR-2026-07-01).

Runs curated, checksum-pinned Schematron rule packs (compiled XSLT 2.0,
e.g. EN 16931 / Peppol BIS Billing 3.0) against XML submissions using
SaxonC-HE, in an isolated container. The isolation is the point: rule-pack
XSLT executes over untrusted submitted XML, and that must never happen next
to the Validibot worker's credentials, identity, or network.

Modules:

- ``main.py`` — container entrypoint (envelope in, envelope out, callback).
- ``runner.py`` — orchestration: download, verify, guard, transform, parse.
- ``engine.py`` — checksum/guard/transform primitives + the D8 hard caps.
- ``saxon_worker.py`` — the Saxon transform, run as a subprocess so the
  wall-clock timeout is a hard kill, not a polite request.
"""
