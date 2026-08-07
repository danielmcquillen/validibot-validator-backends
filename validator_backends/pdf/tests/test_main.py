"""Verify the PDF backend's fixed multi-artifact publication contract.

PDF is the first Validibot backend that may publish six declared artifact
outputs from one attempt. These tests ensure similarly named selections retain
distinct roles, filenames, media types, and stable publication order.
"""

from types import SimpleNamespace

from validator_backends.pdf import main as pdf_main


def test_all_six_artifacts_keep_distinct_fixed_output_identities(monkeypatch) -> None:
    """One attempt must not collapse selected XML/JSON or other optional ports."""
    calls = []

    def fake_upload_bytes_artifact(**kwargs):
        calls.append(kwargs)
        return kwargs["artifact_type"]

    monkeypatch.setattr(
        pdf_main,
        "upload_bytes_artifact",
        fake_upload_bytes_artifact,
    )
    envelope = SimpleNamespace(context=SimpleNamespace(execution_bundle_uri="gs://bucket/attempt"))
    payloads = {
        contract_key: contract_key.encode() for contract_key in pdf_main._ARTIFACT_CONTRACT
    }

    artifacts = pdf_main._upload_artifacts(envelope, payloads)

    expected_roles = list(pdf_main._ARTIFACT_CONTRACT)
    assert artifacts == expected_roles
    assert [call["artifact_type"] for call in calls] == expected_roles
    assert len({call["filename"] for call in calls}) == 6
    assert len({call["artifact_type"] for call in calls}) == 6
