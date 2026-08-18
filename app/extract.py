"""Digital-text extraction for PDF/DOCX/TXT — no OCR.

Scanned/image PDFs raise UnsupportedFileType; OCR is a roadmap item (see README).
"""

import io
from pathlib import Path

import pymupdf
from docx import Document


class UnsupportedFileType(ValueError):
    pass


def extract_text(filename: str, raw: bytes) -> tuple[str, str, int]:
    """Extract (text, file_type, total_pages) from a digital PDF, DOCX or TXT file."""
    suffix = Path(filename or "").suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(raw)
    if suffix == ".docx":
        return _extract_docx(raw)
    if suffix == ".txt":
        return raw.decode("utf-8", errors="replace"), "text", 1

    raise UnsupportedFileType(
        f"Unsupported file type '{suffix or '(none)'}'. MVP supports .pdf "
        f"(digital text layer only), .docx and .txt. Scanned/image PDFs need "
        f"OCR — see README roadmap (Azure AI Document Intelligence)."
    )


def _extract_pdf(raw: bytes) -> tuple[str, str, int]:
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        pages = [page.get_text() for page in doc]
    finally:
        doc.close()

    text = "\n".join(pages)
    if not text.strip():
        raise UnsupportedFileType(
            "This PDF has no extractable text layer (likely a scanned image). "
            "OCR is not enabled in this MVP — see README roadmap."
        )
    return text, "pdf", len(pages)


def _extract_docx(raw: bytes) -> tuple[str, str, int]:
    document = Document(io.BytesIO(raw))
    parts = [p.text for p in document.paragraphs]

    # Flatten tables to pipe-delimited rows so cell values keep context for scoring.
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))

    return "\n".join(parts), "docx", 1
