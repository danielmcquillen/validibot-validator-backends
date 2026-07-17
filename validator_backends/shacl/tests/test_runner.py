"""End-to-end tests for the SHACL container runner.

These drive ``run_shacl_validation(input_envelope)`` exactly as ``main`` does,
but with a ``file://`` submission and ``skip_callback=True`` — i.e. the same path
the local Docker (synchronous) backend and manual testing use. They confirm the
full assembly: download → parse → pyshacl → findings/output values → result-handling →
SPARQL-ASK assertions → ``SHACLOutputs`` + status.

Result-handling behaviour is the subtle part and is pinned explicitly:
- ``fail_after_assertions`` (default): SHACL violations block (FAILED_VALIDATION).
- ``report_only``: violations are surfaced as counts/report only, not as blocking
  findings, so the status stays SUCCESS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from validator_backends.shacl.runner import run_shacl_validation
from validibot_shared.shacl.envelopes import (
    SHACL_RESULT_REPORT_ONLY,
    SHACLInputs,
    SHACLSparqlAssertionSpec,
    build_shacl_input_envelope,
)
from validibot_shared.validations.envelopes import ValidationStatus, ValidatorType


if TYPE_CHECKING:
    from pathlib import Path


SHAPES_TTL = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:PersonShape a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [ sh:path ex:name ; sh:minCount 1 ; sh:datatype xsd:string ] .
"""

CONFORMING_TTL = """
@prefix ex: <http://example.org/> .
ex:alice a ex:Person ; ex:name "Alice" .
"""

VIOLATING_TTL = """
@prefix ex: <http://example.org/> .
ex:bob a ex:Person .
"""


class _Validator:
    """Minimal duck-typed validator for the envelope builder."""

    id = "val-1"
    validation_type = ValidatorType.SHACL
    version = "2"


def _envelope(tmp_path: Path, *, submission: str, inputs: SHACLInputs):
    """Write ``submission`` to a temp file and build a file:// input envelope."""
    sub = tmp_path / "submission.ttl"
    sub.write_text(submission, encoding="utf-8")
    return build_shacl_input_envelope(
        run_id="run-1",
        validator=_Validator(),
        org_id="org-1",
        org_name="ValidiBot",
        workflow_id="wf-1",
        step_id="step-1",
        step_name="SHACL",
        submission_uri=f"file://{sub}",
        inputs=inputs,
        callback_url="https://example.com/cb",
        execution_bundle_uri=f"file://{tmp_path}",
        execution_attempt_id="attempt-1",
        step_run_id="step-run-1",
        expected_output_uri=f"file://{tmp_path / 'output.json'}",
        skip_callback=True,
    )


def test_runner_conforming_graph_succeeds(tmp_path):
    """A conforming graph yields SUCCESS, conforms=True, and the o.* output_values."""
    env = _envelope(
        tmp_path,
        submission=CONFORMING_TTL,
        inputs=SHACLInputs(shapes_text=SHAPES_TTL, rdf_format="turtle", inference_mode="none"),
    )
    result = run_shacl_validation(env)

    assert result.status == ValidationStatus.SUCCESS
    assert result.outputs.conforms is True
    assert result.outputs.shacl_violation_count == 0
    assert result.outputs.parse_ok is True
    # The o.* namespace-detection signal works against the data graph.
    assert result.outputs.triple_count > 0


def test_runner_violation_fails_by_default(tmp_path):
    """A violating graph fails (FAILED_VALIDATION) under the default handling.

    The default mode (fail_after_assertions) treats SHACL violations as blocking,
    so the status is FAILED_VALIDATION and the finding is surfaced with its meta.
    """
    env = _envelope(
        tmp_path,
        submission=VIOLATING_TTL,
        inputs=SHACLInputs(shapes_text=SHAPES_TTL, rdf_format="turtle", inference_mode="none"),
    )
    result = run_shacl_validation(env)

    assert result.status == ValidationStatus.FAILED_VALIDATION
    assert result.outputs.conforms is False
    assert result.outputs.shacl_violation_count >= 1
    # Finding fidelity: rich SHACL meta survives into outputs.findings.
    assert any(f.meta.get("shacl_focus_node") for f in result.outputs.findings)
    # And the generic message list carries the ERROR text for the error summary.
    assert any(m.severity.value == "ERROR" for m in result.messages)


def test_runner_report_only_does_not_block(tmp_path):
    """report_only surfaces violation *counts* but keeps status SUCCESS.

    This mirrors the in-process engine's ``_blocking_shacl_issues`` behaviour:
    the report and output values expose the violations, but they don't fail the step.
    """
    env = _envelope(
        tmp_path,
        submission=VIOLATING_TTL,
        inputs=SHACLInputs(
            shapes_text=SHAPES_TTL,
            rdf_format="turtle",
            inference_mode="none",
            shacl_result_handling=SHACL_RESULT_REPORT_ONLY,
        ),
    )
    result = run_shacl_validation(env)

    assert result.status == ValidationStatus.SUCCESS
    # Counts/report still exposed even though nothing blocked.
    assert result.outputs.shacl_violation_count >= 1
    assert result.outputs.results_graph_turtle
    # No SHACL violation finding is surfaced as a blocking issue.
    assert not any(f.code.startswith("shacl.MinCount") for f in result.outputs.findings)


def test_runner_folds_failing_sparql_ask_into_assertion_counts(tmp_path):
    """A failing ERROR SPARQL-ASK fails the run and counts as an assertion failure.

    These counts are what Django folds with its CEL/Basic assertion counts in
    post_execute_validate, so getting them right here keeps the final totals
    accurate.
    """
    env = _envelope(
        tmp_path,
        submission=CONFORMING_TTL,
        inputs=SHACLInputs(
            shapes_text=SHAPES_TTL,
            rdf_format="turtle",
            inference_mode="none",
            sparql_ask_assertions=[
                SHACLSparqlAssertionSpec(
                    target_graph="data",
                    query="ASK { ?s <http://example.org/missing> ?o }",
                    severity="ERROR",
                    description="requires a missing predicate",
                    assertion_id=42,
                ),
            ],
        ),
    )
    result = run_shacl_validation(env)

    assert result.outputs.assertion_total == 1
    assert result.outputs.assertion_failures == 1
    assert result.status == ValidationStatus.FAILED_VALIDATION
    # The failing assertion is attributed back to its RulesetAssertion pk.
    assert any(f.assertion_id == 42 for f in result.outputs.findings)
