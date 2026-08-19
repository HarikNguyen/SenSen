"""Text extraction for PDF/DOCX/TXT — digital text layer first, OCR fallback.

OCR runs via Tesseract (local, free, no API key/network call) rather than a
cloud OCR service — this project is scoped to run locally, not deployed
online, so a cloud dependency for this would be the wrong tool. Needs the
system `tesseract` binary + Vietnamese language data installed separately
(see README) — pip alone can't provide this, `pytesseract` is just a thin
wrapper that shells out to it.
"""

import io
import logging
from pathlib import Path

import pymupdf
import pytesseract
from docx import Document
from PIL import Image

logger = logging.getLogger("sensen.extract")

# OCR is meaningfully more CPU-intensive than reading an existing text
# layer — the project's own stated design constraint is a weak local
# machine, so this caps the worst case rather than OCR-ing an arbitrarily
# large scanned document with no bound.
MAX_OCR_PAGES = 20
OCR_LANGUAGES = "vie+eng"  # documents in this project are routinely mixed VN/EN


class UnsupportedFileType(ValueError):
    pass


def extract_text(filename: str, raw: bytes) -> tuple[str, str, int, str]:
    """Extract (text, file_type, total_pages, processing_mode) from a PDF,
    DOCX or TXT file. processing_mode is "direct_text_extraction" or "ocr"
    for PDFs, "direct_text_extraction" for DOCX/TXT (they have no OCR path).
    """
    suffix = Path(filename or "").suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf(raw)
    if suffix == ".docx":
        return _extract_docx(raw)
    if suffix == ".txt":
        return raw.decode("utf-8", errors="replace"), "text", 1, "direct_text_extraction"

    raise UnsupportedFileType(
        f"Unsupported file type '{suffix or '(none)'}'. MVP supports .pdf "
        f"(digital text or scanned via OCR), .docx and .txt."
    )


def _extract_pdf(raw: bytes) -> tuple[str, str, int, str]:
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        pages = [page.get_text() for page in doc]
        text = "\n".join(pages)
        if text.strip():
            return text, "pdf", len(pages), "direct_text_extraction"

        if len(doc) > MAX_OCR_PAGES:
            raise UnsupportedFileType(
                f"This PDF has no extractable text layer (likely scanned) and "
                f"has {len(doc)} pages, over the {MAX_OCR_PAGES}-page OCR limit "
                f"— OCR is CPU-heavy, capped to keep it bounded on modest hardware."
            )

        ocr_text = "\n".join(_ocr_page(page) for page in doc)
        if not ocr_text.strip():
            raise UnsupportedFileType(
                "This PDF has no extractable text layer, and OCR found no "
                "readable text either (likely blank pages or very low scan quality)."
            )
        return ocr_text, "pdf", len(pages), "ocr"
    finally:
        doc.close()


def _ocr_page(page) -> str:
    # 200 DPI: enough resolution for OCR accuracy without the memory/CPU
    # cost of a full print-resolution render on a low-RAM host.
    pix = page.get_pixmap(dpi=200)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    try:
        return pytesseract.image_to_string(img, lang=OCR_LANGUAGES)
    except pytesseract.TesseractNotFoundError:
        logger.error(
            "OCR requested but the tesseract binary isn't installed — "
            "see README for the system package to install (pip alone can't provide it)."
        )
        raise UnsupportedFileType(
            "This PDF has no extractable text layer and needs OCR, but the "
            "server's tesseract binary isn't installed. See README."
        ) from None


def _extract_docx(raw: bytes) -> tuple[str, str, int, str]:
    document = Document(io.BytesIO(raw))
    parts = [p.text for p in document.paragraphs]

    # Flatten tables to pipe-delimited rows so cell values keep context for scoring.
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))

    return "\n".join(parts), "docx", 1, "direct_text_extraction"
