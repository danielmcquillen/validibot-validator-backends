"""Replay a verified immutable output without repeating domain compute."""

from __future__ import annotations

from typing import TYPE_CHECKING

from validator_backends.core.callback_client import post_callback
from validator_backends.core.envelope_loader import get_output_uri
from validator_backends.core.output_identity import output_identity_for
from validator_backends.core.storage_client import download_envelope, stored_object_exists


if TYPE_CHECKING:
    from pydantic import BaseModel


def replay_existing_output(
    input_envelope: BaseModel,
    output_envelope_class: type[BaseModel],
) -> bool:
    """Verify and callback an existing exact output, returning whether replayed.

    Absence is the only signal to recompute.  An existing malformed or
    identity-conflicting object raises and fences the delivery for trusted
    reconciliation rather than overwriting immutable evidence.
    """
    output_uri = get_output_uri(input_envelope)
    if not stored_object_exists(output_uri):
        return False
    output_envelope = download_envelope(
        output_uri,
        output_envelope_class,
        configure_refresh=False,
    )
    expected_identity = output_identity_for(input_envelope, output_uri)
    for field_name, expected_value in expected_identity.items():
        if str(getattr(output_envelope, field_name, "")) != expected_value:
            raise ValueError(
                f"Existing output conflicts with attempt identity field {field_name}."
            )
    if str(getattr(output_envelope, "run_id", "")) != str(input_envelope.run_id):
        raise ValueError("Existing output conflicts with the input run identity.")
    if output_envelope.validator != input_envelope.validator:
        raise ValueError("Existing output conflicts with the input validator identity.")
    post_callback(
        callback_url=(
            str(input_envelope.context.callback_url)
            if input_envelope.context.callback_url
            else None
        ),
        run_id=str(input_envelope.run_id),
        status=output_envelope.status,
        result_uri=output_uri,
        callback_id=input_envelope.context.callback_id,
        callback_nonce=input_envelope.context.callback_nonce,
        skip_callback=input_envelope.context.skip_callback,
    )
    return True
