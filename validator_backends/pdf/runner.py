"""Download, verify, and inspect one PDF package in an isolated workspace."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from validator_backends.core.storage_client import download_verified_file
from validator_backends.pdf.engine import PdfEngineResult, inspect_pdf


if TYPE_CHECKING:
    from validibot_shared.pdf import PdfInputEnvelope


def run_pdf_validation(input_envelope: PdfInputEnvelope) -> PdfEngineResult:
    """Run the bounded PDF engine against the envelope's declared input port."""
    if len(input_envelope.input_files) != 1:
        raise ValueError("PDF validation requires exactly one input file.")
    input_file = input_envelope.input_files[0]
    if input_file.port_key != "pdf_document":
        raise ValueError("PDF input must use the pdf_document port.")

    with tempfile.TemporaryDirectory(prefix="validibot-pdf-") as tmp:
        pdf_path = Path(tmp) / "document.pdf"
        download_verified_file(input_file, pdf_path)
        return inspect_pdf(
            pdf_path,
            source_name=input_file.name,
            inputs=input_envelope.inputs,
        )
