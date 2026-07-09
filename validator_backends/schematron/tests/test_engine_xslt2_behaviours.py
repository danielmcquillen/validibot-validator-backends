"""XSLT-2.0 engine-behaviour tests (ADR-2026-07-01 test layer C).

The layer-A fixture engine (``lxml.isoschematron``) can only run XSLT-1.0
Schematron, so a whole class of real-world pack features — custom
``xsl:function`` definitions, XPath-2.0 quantified expressions, ``xs:*``
constructors, ``defaultPhase`` — is impossible to exercise there. Those
features are precisely why the SchematronValidator dispatches to a SaxonC-HE
container in the first place (the official EN 16931 / Peppol packs are authored
in XSLT 2.0). This module pins that capability using the REAL
SchXslt2-transpile + SaxonC-HE runtime, driving ``engine.run_schematron`` end
to end and parsing its SVRL with the canonical shared parser.

Each source here is hand-authored for the test and declares
``queryBinding="xslt2"``. A passing run proves both production stages executed
under Saxon: the SchXslt2 transpile of the ``.sch``, then the transform of the
submission by the compiled stylesheet.

Like the other layer-C modules, every test needs the vendored SchXslt2
transpiler and skips (pointing at its README) when it is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from validator_backends.schematron import engine
from validibot_shared.schematron.svrl import parse_svrl


if TYPE_CHECKING:
    from pathlib import Path


requires_transpiler = pytest.mark.skipif(
    not engine.transpiler_available(),
    reason=(
        "SchXslt2 transpiler not vendored — see validator_backends/schematron/schxslt2/README.md"
    ),
)

SCHEMA_OPEN = '<schema xmlns="http://purl.oclc.org/dsdl/schematron" queryBinding="xslt2">'


def _run(tmp_path: Path, sch_text: str, xml_text: str):
    """Write source + document, run the real engine, parse the SVRL.

    Returns the ``SvrlSummary`` for the run so each test reasons about the same
    parsed shape the production runner (``run_schematron_validation``) builds
    its ``SchematronOutputs`` from.
    """
    sch = tmp_path / "rules.sch"
    xml = tmp_path / "doc.xml"
    out = tmp_path / "report.svrl"
    sch.write_text(sch_text, encoding="utf-8")
    xml.write_text(xml_text, encoding="utf-8")
    svrl = engine.run_schematron(sch, xml, out, timeout_seconds=60)
    return parse_svrl(svrl)


# ── XSLT-2.0-only capabilities (impossible under the layer-A lxml engine) ────


@requires_transpiler
def test_custom_xsl_function_is_callable_from_a_rule(tmp_path):
    """A rule can call an author-defined ``xsl:function`` (XSLT-2.0 only).

    Real packs factor arithmetic into functions; lxml's XSLT-1.0 engine cannot
    even compile ``xsl:function``. Here ``f:net(line)`` returns qty*price and a
    rule asserts it equals the stated ``@net``. The first line (net 25 vs
    computed 20) fails; the second (net 30) passes — proving the function was
    genuinely evaluated per node, not short-circuited.
    """
    summary = _run(
        tmp_path,
        f"""{SCHEMA_OPEN}
          <ns prefix="f" uri="urn:vb:fn"/>
          <ns prefix="xs" uri="http://www.w3.org/2001/XMLSchema"/>
          <xsl:function xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
                        xmlns:xs="http://www.w3.org/2001/XMLSchema"
                        xmlns:f="urn:vb:fn" name="f:net" as="xs:decimal">
            <xsl:param name="l"/>
            <xsl:sequence select="xs:decimal($l/@qty) * xs:decimal($l/@price)"/>
          </xsl:function>
          <pattern>
            <rule context="line">
              <assert id="FN-01" flag="fatal"
                test="f:net(.) = xs:decimal(@net)"
                >Line net must equal qty * price.</assert>
            </rule>
          </pattern>
        </schema>""",
        "<order><line qty='2' price='10.00' net='25.00'/>"
        "<line qty='3' price='10.00' net='30.00'/></order>",
    )

    assert summary.fired_rule_count == 2
    assert summary.error_count == 1
    assert summary.finding_rule_ids_by_severity == {"FN-01": "ERROR"}


@requires_transpiler
def test_quantified_every_expression_is_evaluated(tmp_path):
    """An XPath-2.0 ``every $x in … satisfies …`` expression drives a rule.

    Quantified expressions are XPath-2.0 syntax with no XSLT-1.0 equivalent.
    ``every $l in line satisfies price gt 0`` fails when any line has a
    non-positive price — a whole-collection assertion a grammar cannot express
    and the layer-A engine cannot run.
    """
    summary = _run(
        tmp_path,
        f"""{SCHEMA_OPEN}
          <ns prefix="xs" uri="http://www.w3.org/2001/XMLSchema"/>
          <pattern>
            <rule context="order">
              <assert id="Q-01" flag="fatal"
                test="every $l in line satisfies xs:decimal($l/@price) gt 0"
                >Every line must have a positive price.</assert>
            </rule>
          </pattern>
        </schema>""",
        "<order><line price='10'/><line price='0'/></order>",
    )

    assert summary.error_count == 1
    assert summary.finding_rule_ids_by_severity == {"Q-01": "ERROR"}


@requires_transpiler
def test_default_phase_restricts_the_active_patterns(tmp_path):
    """``defaultPhase`` limits which patterns run — the production phase path.

    The container never passes an explicit phase, so a pack selects its subset
    via the schema's own ``defaultPhase``. Here ``defaultPhase="lax"`` activates
    only the structural pattern, so an order with a line but a zero total PASSES
    — the strict ``p-math`` rule is never evaluated. Without phase honouring,
    ``M-01`` would fire and the run would fail.
    """
    summary = _run(
        tmp_path,
        """<schema xmlns="http://purl.oclc.org/dsdl/schematron"
              queryBinding="xslt2" defaultPhase="lax">
          <ns prefix="xs" uri="http://www.w3.org/2001/XMLSchema"/>
          <phase id="lax"><active pattern="p-struct"/></phase>
          <phase id="strict">
            <active pattern="p-struct"/><active pattern="p-math"/>
          </phase>
          <pattern id="p-struct">
            <rule context="order">
              <assert id="S-01" flag="fatal" test="line">Order needs a line.</assert>
            </rule>
          </pattern>
          <pattern id="p-math">
            <rule context="order">
              <assert id="M-01" flag="fatal" test="xs:decimal(@total) gt 0"
                >Total must be positive.</assert>
            </rule>
          </pattern>
        </schema>""",
        "<order total='0'><line/></order>",
    )

    assert summary.passed
    assert "M-01" not in summary.finding_rule_ids_by_severity
    assert summary.fired_rule_count == 1


@requires_transpiler
def test_successful_report_surfaces_under_saxon(tmp_path):
    """A ``report`` fires when TRUE under Saxon too (engine parity, D3).

    The assert/report symmetry is a shared-contract behaviour, so it must hold
    on the production engine, not just the lxml fixture engine: a report whose
    test holds becomes an active WARNING finding, and with no ERROR the run
    still passes.
    """
    summary = _run(
        tmp_path,
        f"""{SCHEMA_OPEN}
          <pattern>
            <rule context="order">
              <report id="RPT-01" flag="warning" test="@legacy = 'true'"
                >Order uses the legacy format.</report>
            </rule>
          </pattern>
        </schema>""",
        "<order legacy='true'/>",
    )

    assert summary.warning_count == 1
    assert summary.error_count == 0
    assert summary.passed
    assert summary.findings[0].rule_id == "RPT-01"


@requires_transpiler
def test_diagnostic_text_is_not_currently_folded_into_the_message(tmp_path):
    """PIN: parse_svrl surfaces the assertion text, NOT the diagnostic text.

    ``sch:diagnostics`` is where authors put the actionable detail ("observed
    value was X"). SchXslt2 emits it as a separate ``svrl:diagnostic-reference``
    element, and the shared parser reads only ``svrl:text`` — so the diagnostic
    content does not reach the finding message today. This test pins that
    current behaviour (identical under lxml) so that folding diagnostics in
    later is a DELIBERATE, reviewed change rather than an accident. See the
    session notes: this is a candidate enhancement, not a settled requirement.
    """
    summary = _run(
        tmp_path,
        f"""{SCHEMA_OPEN}
          <ns prefix="xs" uri="http://www.w3.org/2001/XMLSchema"/>
          <pattern>
            <rule context="order">
              <assert id="D-01" flag="fatal" diagnostics="d-total"
                test="xs:decimal(@total) gt 0">Total must be positive.</assert>
            </rule>
          </pattern>
          <diagnostics>
            <diagnostic id="d-total">Observed total was <value-of select="@total"/>.</diagnostic>
          </diagnostics>
        </schema>""",
        "<order total='0'/>",
    )

    finding = summary.findings[0]
    assert finding.rule_id == "D-01"
    assert finding.message == "Total must be positive."
    # The diagnostic's interpolated detail is NOT present today.
    assert "Observed total" not in finding.message
