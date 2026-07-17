"""Build the immutable execution-attempt identity echoed by validator outputs.

Every backend must bind its output to the exact input envelope it consumed.
Keeping that assembly here prevents success and failure paths from drifting or
from accidentally hashing a lossy re-serialization of the envelope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from validibot_shared.canonicalization import sha256_hex_for_model


if TYPE_CHECKING:
    from pydantic import BaseModel


def output_identity_for(input_envelope: BaseModel, output_uri: str) -> dict[str, str]:
    """Return the strict attempt identity fields for an output envelope.

    The storage helper may read a local environment override, but it may not
    redirect a validator away from the URI committed to by the input envelope.
    """
    context = input_envelope.context
    expected_output_uri = str(context.expected_output_uri)
    if output_uri != expected_output_uri:
        msg = (
            "Output URI does not match the execution-attempt contract: "
            f"expected {expected_output_uri!r}, got {output_uri!r}"
        )
        raise ValueError(msg)

    return {
        "step_run_id": str(context.step_run_id),
        "execution_attempt_id": str(context.execution_attempt_id),
        "attempt_contract_version": str(context.attempt_contract_version),
        "input_envelope_sha256": sha256_hex_for_model(input_envelope),
        "output_uri": output_uri,
    }


__all__ = ["output_identity_for"]
