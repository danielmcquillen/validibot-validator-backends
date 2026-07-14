"""Real-Saxon layer-C tests for the calibration-certificate demo rules.

These fixtures (``fixtures/calibration/``) back the public Validibot blog post
that pairs an XSD structural check with a Schematron business-rule check on a
small DCC-inspired calibration certificate. This module is where the blog's
Schematron claims are actually *executed* against the production engine, so the
worked example cannot silently rot.

Like ``test_engine.py``, this is **layer C** — the only layer that runs the real
SaxonC-HE runtime. ``calibration-rules-demo.sch`` declares
``queryBinding="xslt2"`` and leans on XPath 2.0 throughout (``xs:date``,
``xs:decimal``, ``abs()``), so a passing compile-and-run proves the full
production path: the SchXslt2 transpile under Saxon, then the compiled rules
under Saxon. It is *because* these rules are XSLT-2.0-only that the community
repo cannot cover them with ``lxml.isoschematron`` (XSLT 1.0) — hence this test
lives here, next to the engine that can run them.

The canonical copy of these fixtures lives in the community repo at
``tests/assets/schematron/calibration/`` (that is what the blog links to). A
copy is kept here because the backend test suite must be self-contained — its CI
does not check out the community repo.

Expected behaviour (verified against the real engine when this was authored):

- ``calibration-certificate-valid.xml`` → every rule fires, **no** failed
  assertions. The certificate is internally coherent.
- ``calibration-certificate-invalid.xml`` → still shaped like a certificate
  (it is XSD-valid; see the community ``test_calibration_xsd``), but violates
  the cross-field rules a grammar cannot express: issue date before the
  calibration date (``CAL-DATE-001``), an accredited issuer with no
  accreditation id (``CAL-ACCRED-001``), a ``psi`` result unit against an
  ``MPa`` instrument (``CAL-UNIT-001``), a point outside the instrument range
  (``CAL-RANGE-001``), and pass verdicts that contradict the tolerance maths
  (``CAL-VERDICT-001``), among others.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from validator_backends.schematron import engine


FIXTURES = Path(__file__).parent / "fixtures" / "calibration"

SCH = FIXTURES / "calibration-rules-demo.sch"

# The fatal (flag="fatal") rule ids the invalid certificate must trip. These are
# exactly the failures the blog post narrates; asserting each individually keeps
# the published example and the engine's real output in lockstep.
EXPECTED_FATAL_IDS = (
    "CAL-DATE-001",  # issue date precedes the calibration date
    "CAL-ACCRED-001",  # accredited calibration, but no accreditation id
    "CAL-DATE-002",  # next-due date not after the calibration date
    "CAL-UNIT-001",  # result unit (psi) != instrument unit (MPa)
    "CAL-RANGE-001",  # a nominal point sits outside the instrument range
    "CAL-MATH-001",  # correction != nominal - indicated
    "CAL-MATH-002",  # expanded uncertainty != standard * coverage factor
    "CAL-VERDICT-001",  # a "pass" verdict contradicts the tolerance maths
)

# Non-fatal (flag="warning") ids the invalid certificate also trips. They must
# surface too — the engine reports warnings, and downstream severity mapping is
# the community layer's job, not something to pre-filter in the engine.
EXPECTED_WARNING_IDS = (
    "CAL-ENV-001",  # calibration temperature outside the 15-25 C demo band
    "CAL-ENV-002",  # calibration humidity outside the 20-80 % demo band
)

requires_transpiler = pytest.mark.skipif(
    not engine.transpiler_available(),
    reason=(
        "SchXslt2 transpiler not vendored — see validator_backends/schematron/schxslt2/README.md"
    ),
)


# ── The coherent certificate passes every rule ───────────────────────────────
# Baseline: a certificate whose fields agree with each other yields SVRL with
# fired rules and zero failed assertions. Establishes that the rules are not
# vacuously failing before we assert the richer invalid case.


@requires_transpiler
def test_valid_certificate_has_no_failed_assertions(tmp_path):
    """The blog's clean certificate trips none of the Schematron rules.

    Every rule still *fires* (proving the transform actually ran over the
    document), but nothing fails — the maths reconciles, the dates order
    correctly, and the units match. This is the "certificate the workflow
    should accept" half of the demo.
    """
    out = tmp_path / "valid.svrl"
    svrl = engine.run_schematron(
        SCH,
        FIXTURES / "calibration-certificate-valid.xml",
        out,
        timeout_seconds=60,
    )

    assert "fired-rule" in svrl
    assert "failed-assert" not in svrl


# ── The business-invalid certificate trips the expected named rules ──────────
# This is the payoff: a document that an XSD would happily accept fails the
# cross-field rules with *named* findings, which is what makes the Schematron
# layer useful to a human (they see WHICH rule failed, not just "rejected").


@requires_transpiler
def test_invalid_certificate_fails_the_expected_named_rules(tmp_path):
    """The invalid certificate fails each rule the blog post names.

    Asserting every expected id (rather than a bare "something failed") is what
    keeps the published worked example honest: if a rule or fixture drifts so a
    documented failure stops firing, this test breaks and flags the divergence.
    """
    out = tmp_path / "invalid.svrl"
    svrl = engine.run_schematron(
        SCH,
        FIXTURES / "calibration-certificate-invalid.xml",
        out,
        timeout_seconds=60,
    )

    assert "failed-assert" in svrl
    for rule_id in EXPECTED_FATAL_IDS:
        assert rule_id in svrl, f"expected fatal rule {rule_id} to fail"
    for rule_id in EXPECTED_WARNING_IDS:
        assert rule_id in svrl, f"expected warning rule {rule_id} to fire"


@requires_transpiler
def test_invalid_certificate_does_not_flag_rules_it_satisfies(tmp_path):
    """Rules the invalid document *satisfies* produce no finding.

    SVRL emits an element only for a failed assertion, so a satisfied rule
    leaves no trace. The invalid certificate keeps three in-order measurement
    points, so the point-count and ordering rules pass — their ids must be
    absent. This proves the engine reports only genuine violations, not every
    rule it evaluated.
    """
    out = tmp_path / "invalid.svrl"
    svrl = engine.run_schematron(
        SCH,
        FIXTURES / "calibration-certificate-invalid.xml",
        out,
        timeout_seconds=60,
    )

    assert "CAL-POINT-001" not in svrl  # has the required 3+ points
    assert "CAL-POINT-002" not in svrl  # points are in increasing order
