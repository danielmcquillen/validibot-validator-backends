"""Tests for the SHACL backend entrypoint envelope assembly.

The runner tests cover RDF parsing and SHACL behavior. These tests cover the
container boundary around it: the serialized report graph must be uploaded as a
generic artifact so Django can index it through artifact output ports.
"""

from __future__ import annotations

from types import SimpleNamespace

from validator_backends.shacl import main as shacl_main
from validibot_shared.shacl.envelopes import SHACLInputs, SHACLOutputs, build_shacl_input_envelope
from validibot_shared.validations.envelopes import ValidationStatus, ValidatorType


class _Validator:
    """Minimal duck-typed validator for the shared envelope builder."""

    id = "validator-1"
    validation_type = ValidatorType.SHACL
    version = "4"


def _input_envelope(tmp_path):
    """Build a minimal SHACL input envelope with local output storage."""

    submission = tmp_path / "submission.ttl"
    submission.write_text("@prefix ex: <http://example.org/> .", encoding="utf-8")
    return build_shacl_input_envelope(
        run_id="run-1",
        validator=_Validator(),
        org_id="org-1",
        org_name="Org",
        workflow_id="workflow-1",
        step_id="step-1",
        step_name="SHACL",
        submission_uri=f"file://{submission}",
        inputs=SHACLInputs(shapes_text="", rdf_format="turtle"),
        callback_url="https://example.com/callback",
        execution_bundle_uri=f"file://{tmp_path / 'bundle'}",
        skip_callback=True,
    )


def test_main_uploads_shacl_report_as_output_artifact(monkeypatch, tmp_path):
    """The entrypoint must carry report bytes through to the output envelope."""

    report_turtle = "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    captured = {}
    outputs = SHACLOutputs(
        conforms=True,
        parse_ok=True,
        parse_serialization="turtle",
        results_graph_turtle=report_turtle,
    )

    monkeypatch.setattr(
        shacl_main, "load_input_envelope", lambda _model: _input_envelope(tmp_path)
    )
    monkeypatch.setattr(
        shacl_main,
        "run_shacl_validation",
        lambda _envelope: SimpleNamespace(
            status=ValidationStatus.SUCCESS,
            messages=[],
            outputs=outputs,
        ),
    )
    monkeypatch.setattr(shacl_main, "get_output_uri", lambda _envelope: "file:///tmp/output.json")
    monkeypatch.setattr(
        shacl_main,
        "upload_envelope",
        lambda envelope, uri: captured.update({"envelope": envelope, "uri": uri}),
    )
    monkeypatch.setattr(shacl_main, "post_callback", lambda **_kwargs: None)

    exit_code = shacl_main.main()

    assert exit_code == 0
    output = captured["envelope"]
    assert output.artifacts
    artifact = output.artifacts[0]
    assert artifact.name == "shacl-report.ttl"
    assert artifact.type == "shacl-report"
    assert artifact.mime_type == "text/turtle"
    assert artifact.uri.endswith("/bundle/outputs/shacl-report.ttl")
    assert (tmp_path / "bundle" / "outputs" / "shacl-report.ttl").read_text(
        encoding="utf-8",
    ) == report_turtle
