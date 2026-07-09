"""Rules-source hardening under the REAL Saxon sandbox (ADR-2026-07-01 D4/D8b).

The submitted XML is not the only untrusted input: **the Schematron rules are
author-supplied code** (compiled Schematron is XSLT, a full language). A hostile
``.sch`` can reach this container by a path Django's authoring guard never
covered (a forged envelope, an admin/import-created ruleset, a future non-form
authoring surface), so the container defends the rules in **two layers**:

1. :func:`engine.guard_rules` — a deterministic pre-guard the runner applies
   BEFORE Saxon: the defusedxml stance (no DTD / entities / external refs) plus
   the Schematron-root and size/depth checks, rejecting a hostile ``.sch`` with a
   clean ``rules_invalid`` instead of leaning on Saxon's incidental behaviour.
2. The **Saxon lockdown** (``allowedProtocols=""``, external functions off) — the
   last line for a rule that *is* valid Schematron but reaches for a forbidden
   capability (``doc()`` and friends) at run time.

``test_engine.py`` already pins ONE facet of the lockdown — ``doc()`` cannot read
a local file. This module pins both layers: the deterministic pre-guard, and the
rest of the URI-retrieval surface, so a regression in either (a dropped guard, a
re-enabled protocol) fails a test instead of shipping:

- **URI-retrieval / exfiltration & SSRF** — ``document()``, ``unparsed-text()``,
  ``collection()`` over ``file://`` (local file disclosure) and ``doc()`` over
  ``http://`` to a cloud-metadata address (SSRF). ``allowedProtocols=""`` is the
  control; each must be blocked and must never copy secret content into the
  message or the SVRL.
- **DTD / XXE / entity-expansion in the rules document itself** — a ``.sch``
  carrying a DTD with an external entity or a "billion laughs" bomb must fail
  fast (no external resolution, no unbounded expansion, no hang), not be parsed
  and executed.

The invariant every test asserts is the security property, not a brittle Saxon
error string: **the secret token never appears anywhere the run can surface it,
and a hostile run never completes as a clean success.** Whether Saxon rejects at
compile or at transform time is an implementation detail that may shift between
Saxon releases; the leak-prevention must not.

Like the other layer-C modules, every test needs the vendored SchXslt2
transpiler and skips (pointing at its README) when it is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from validator_backends.schematron import engine


if TYPE_CHECKING:
    from pathlib import Path

requires_transpiler = pytest.mark.skipif(
    not engine.transpiler_available(),
    reason=(
        "SchXslt2 transpiler not vendored — see validator_backends/schematron/schxslt2/README.md"
    ),
)

# A token written to a local file the hostile rules will try to exfiltrate.
# If it ever surfaces in an exception message or the SVRL, the sandbox failed.
SECRET_TOKEN = "LEAK_VALIDIBOT_RULES_HARDENING_TOKEN"

SCHEMA_XSLT2 = '<schema xmlns="http://purl.oclc.org/dsdl/schematron" queryBinding="xslt2">'


def _plant_secret(tmp_path: Path) -> Path:
    """Write a secret file the hostile rules will attempt to read."""
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET_TOKEN, encoding="utf-8")
    return secret


def _assert_no_leak(tmp_path: Path, sch_text: str) -> None:
    """Run a hostile ``.sch`` and assert the secret never surfaces.

    The security contract holds whether the engine raises (the current
    behaviour) or returns a report: the secret token must appear in NEITHER the
    raised message NOR the SVRL output, and a hostile run must never complete as
    a clean success carrying the leaked value. Assertions are on the invariant,
    not on Saxon's (version-dependent) error wording.
    """
    sch = tmp_path / "rules.sch"
    xml = tmp_path / "doc.xml"
    out = tmp_path / "report.svrl"
    sch.write_text(sch_text, encoding="utf-8")
    xml.write_text("<order/>", encoding="utf-8")

    surfaced = ""
    try:
        surfaced = engine.run_schematron(sch, xml, out, timeout_seconds=15)
    except (engine.SchematronEngineError, engine.SchematronTransformTimeout) as exc:
        surfaced = str(exc)

    assert SECRET_TOKEN not in surfaced
    if out.exists():
        assert SECRET_TOKEN not in out.read_text(encoding="utf-8")


# ── URI retrieval / exfiltration & SSRF (allowedProtocols="" is the control) ──


@requires_transpiler
def test_document_function_cannot_read_a_local_file(tmp_path):
    """``document('file://…')`` in a rule must not disclose local files.

    The XSLT-1.0/2.0 sibling of ``doc()``; the lockdown must cover it too, or a
    rule could read any XML file visible in the container and copy it into SVRL.
    """
    secret = _plant_secret(tmp_path)
    _assert_no_leak(
        tmp_path,
        f"""{SCHEMA_XSLT2}<pattern><rule context="/">
          <assert id="X-DOCUMENT" test="false()"
            >leak=<value-of select="document('file://{secret}')"/></assert>
        </rule></pattern></schema>""",
    )


@requires_transpiler
def test_unparsed_text_cannot_read_a_local_file(tmp_path):
    """``unparsed-text('file://…')`` must not disclose local (non-XML) files.

    ``unparsed-text()`` is especially dangerous — it reads ANY file as a string,
    not just well-formed XML — so it is a prime exfiltration primitive that the
    protocol lockdown has to deny.
    """
    secret = _plant_secret(tmp_path)
    _assert_no_leak(
        tmp_path,
        f"""{SCHEMA_XSLT2}<pattern><rule context="/">
          <assert id="X-UNPARSED" test="false()"
            >leak=<value-of select="unparsed-text('file://{secret}')"/></assert>
        </rule></pattern></schema>""",
    )


@requires_transpiler
def test_collection_cannot_enumerate_local_files(tmp_path):
    """``collection('file://…')`` must not enumerate or read local directories.

    ``collection()`` can walk a directory of documents; denying it prevents a
    rule from harvesting whatever files share the container filesystem.
    """
    secret = _plant_secret(tmp_path)
    _assert_no_leak(
        tmp_path,
        f"""{SCHEMA_XSLT2}<pattern><rule context="/">
          <assert id="X-COLLECTION" test="false()"
            >leak=<value-of select="collection('file://{secret.parent}?select=*.txt')"/></assert>
        </rule></pattern></schema>""",
    )


@requires_transpiler
def test_http_uri_retrieval_is_blocked_ssrf(tmp_path):
    """A rule cannot reach out over ``http://`` (SSRF, e.g. cloud metadata).

    The most damaging network vector: a rule fetching
    ``http://169.254.169.254/…`` could pull cloud instance credentials into the
    report. ``allowedProtocols=""`` blocks every protocol, http included — the
    run must fail without ever completing the request.
    """
    sch = tmp_path / "rules.sch"
    xml = tmp_path / "doc.xml"
    out = tmp_path / "report.svrl"
    sch.write_text(
        f"""{SCHEMA_XSLT2}<pattern><rule context="/">
          <assert id="X-SSRF" test="false()"
            >meta=<value-of select="doc('http://169.254.169.254/latest/meta-data/')"/></assert>
        </rule></pattern></schema>""",
        encoding="utf-8",
    )
    xml.write_text("<order/>", encoding="utf-8")

    # It must not silently succeed in fetching the URL; the run fails instead.
    with pytest.raises((engine.SchematronEngineError, engine.SchematronTransformTimeout)):
        engine.run_schematron(sch, xml, out, timeout_seconds=15)


# ── DTD / XXE / entity-expansion carried by the rules document itself ─────────


@requires_transpiler
def test_external_entity_in_the_rules_document_is_not_resolved(tmp_path):
    """A DTD external entity in the ``.sch`` must not read a local file.

    The rules document is XML too, so it can carry its own XXE. A ``.sch`` whose
    DTD defines ``&xxe;`` as a ``file://`` reference must fail without resolving
    it — the entity's target content must never reach the compiled rules or SVRL.
    """
    secret = _plant_secret(tmp_path)
    _assert_no_leak(
        tmp_path,
        f'<?xml version="1.0"?>'
        f'<!DOCTYPE schema [<!ENTITY xxe SYSTEM "file://{secret}">]>'
        f'<schema xmlns="http://purl.oclc.org/dsdl/schematron"><pattern>'
        f'<rule context="/"><assert id="X-XXE" test="false()">leak &xxe;</assert>'
        f"</rule></pattern></schema>",
    )


@requires_transpiler
def test_entity_expansion_bomb_in_the_rules_document_fails_fast(tmp_path):
    """A "billion laughs" DTD in the ``.sch`` must fail fast, not expand.

    Entity expansion is a memory/CPU exhaustion vector. A ``.sch`` carrying a
    nested-entity bomb must be rejected quickly (well within the wall-clock
    budget) rather than expanded — proving the rules parse does not amplify
    attacker-controlled entities into gigabytes.
    """
    entities = '<!ENTITY lol0 "lol">' + "".join(
        f"<!ENTITY lol{i} "
        f'"&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};'
        f'&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};&lol{i - 1};">'
        for i in range(1, 9)
    )
    sch = tmp_path / "rules.sch"
    xml = tmp_path / "doc.xml"
    out = tmp_path / "report.svrl"
    sch.write_text(
        f'<?xml version="1.0"?><!DOCTYPE schema [{entities}]>'
        f'<schema xmlns="http://purl.oclc.org/dsdl/schematron"><pattern>'
        f'<rule context="/"><assert id="X-BOMB" test="false()">&lol8;</assert>'
        f"</rule></pattern></schema>",
        encoding="utf-8",
    )
    xml.write_text("<order/>", encoding="utf-8")

    # Fails as an engine error; must not hang (a raised timeout would also
    # satisfy the type, but in practice the parse is rejected in milliseconds).
    with pytest.raises(engine.SchematronEngineError):
        engine.run_schematron(sch, xml, out, timeout_seconds=15)


# ── The deterministic rules pre-guard (guard_rules, D8b) ──────────────────────
# guard_rules runs BEFORE Saxon in the runner. Unlike the Saxon-layer tests
# above (which prove the last-line backstop), these assert the CLEAN, labelled
# rejection the pre-guard gives — a deterministic ``rules_invalid`` rather than
# Saxon's opaque compile error. No transpiler is needed: the guard is pure
# defusedxml, so these run even without the SchXslt2 toolchain.

GENEROUS_LIMIT = 10_000_000


def test_guard_rules_rejects_a_dtd_bearing_rules_document(tmp_path):
    """``guard_rules`` refuses a ``.sch`` carrying a DTD/entity as rules_invalid.

    This is the deterministic counterpart to the Saxon-layer XXE test: the
    pre-guard rejects the DTD outright (defusedxml ``forbid_dtd``) with a clear
    ``rules_invalid`` code, before Saxon is ever invoked and without resolving
    the entity.
    """
    secret = _plant_secret(tmp_path)
    sch = tmp_path / "rules.sch"
    sch.write_text(
        f'<?xml version="1.0"?>'
        f'<!DOCTYPE schema [<!ENTITY xxe SYSTEM "file://{secret}">]>'
        f'<schema xmlns="http://purl.oclc.org/dsdl/schematron"><pattern>'
        f'<rule context="/"><assert id="X" test="false()">&xxe;</assert>'
        f"</rule></pattern></schema>",
        encoding="utf-8",
    )
    with pytest.raises(engine.SchematronEngineError, match="forbidden constructs") as exc:
        engine.guard_rules(sch, max_bytes=GENEROUS_LIMIT, max_depth=engine.HARD_MAX_INPUT_DEPTH)
    assert exc.value.error_code == "rules_invalid"
    assert SECRET_TOKEN not in str(exc.value)


def test_guard_rules_rejects_a_non_schematron_root(tmp_path):
    """A well-formed XML that is not Schematron is refused as rules_invalid.

    An XSD, an invoice, or any random XML uploaded where rules were expected must
    be rejected up front — never compiled and run as if it were rules.
    """
    sch = tmp_path / "rules.sch"
    sch.write_text("<not-schematron/>", encoding="utf-8")
    with pytest.raises(engine.SchematronEngineError, match="not a Schematron") as exc:
        engine.guard_rules(sch, max_bytes=GENEROUS_LIMIT, max_depth=engine.HARD_MAX_INPUT_DEPTH)
    assert exc.value.error_code == "rules_invalid"


def test_guard_rules_enforces_size_and_depth_caps(tmp_path):
    """Oversize / pathologically deep rules are refused as rules_invalid.

    Symmetry with ``guard_submission``: an author cannot ship a giant or absurdly
    nested ``.sch`` that would burden the compile, and the refusal is labelled an
    authoring problem, not a document failure.
    """
    ns = 'xmlns="http://purl.oclc.org/dsdl/schematron"'

    big = tmp_path / "big.sch"
    big.write_text(f"<schema {ns}>" + "<pattern/>" * 50 + "</schema>", encoding="utf-8")
    with pytest.raises(engine.SchematronEngineError, match="too large") as exc:
        engine.guard_rules(big, max_bytes=200, max_depth=engine.HARD_MAX_INPUT_DEPTH)
    assert exc.value.error_code == "rules_invalid"

    deep = tmp_path / "deep.sch"
    deep.write_text(
        f"<schema {ns}><pattern><rule context='/'>"
        "<assert test='true()'><nested/></assert></rule></pattern></schema>",
        encoding="utf-8",
    )
    with pytest.raises(engine.SchematronEngineError, match="deeper") as exc:
        engine.guard_rules(deep, max_bytes=GENEROUS_LIMIT, max_depth=3)
    assert exc.value.error_code == "rules_invalid"


def test_guard_rules_accepts_a_benign_rules_document(tmp_path):
    """A well-formed ISO Schematron document passes the pre-guard untouched."""
    sch = tmp_path / "ok.sch"
    sch.write_text(
        '<schema xmlns="http://purl.oclc.org/dsdl/schematron"><pattern>'
        '<rule context="/"><assert test="true()">ok</assert></rule>'
        "</pattern></schema>",
        encoding="utf-8",
    )
    # Does not raise.
    engine.guard_rules(sch, max_bytes=GENEROUS_LIMIT, max_depth=engine.HARD_MAX_INPUT_DEPTH)
