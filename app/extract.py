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
from PIL import Image, ImageFilter

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
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except pymupdf.FileDataError as exc:
        # Covers both a corrupt/malformed PDF and an empty (0-byte) upload —
        # EmptyFileError subclasses FileDataError. Without this, either one
        # crashed as an unhandled 500 instead of the documented clear 422
        # (found via adversarial file-upload testing, not a hypothetical).
        raise UnsupportedFileType(
            f"This file isn't a valid PDF (couldn't be opened: {exc}). "
            f"Check it isn't corrupted or empty."
        ) from exc
    try:
        pages = [page.get_text() for page in doc]
        # Per-page, not just document-wide: a PDF can mix digital-text pages
        # with scanned/image ones (e.g. a typed contract with a scanned
        # signature page) -- a single whole-document text.strip() check would
        # treat the whole file as "has a text layer" and silently skip OCR
        # for the blank/scanned pages, dropping their content with no error.
        if all(p.strip() for p in pages):
            return "\n".join(pages), "pdf", len(pages), "direct_text_extraction"

        if len(doc) > MAX_OCR_PAGES:
            raise UnsupportedFileType(
                f"This PDF has no extractable text layer (likely scanned) and "
                f"has {len(doc)} pages, over the {MAX_OCR_PAGES}-page OCR limit "
                f"— OCR is CPU-heavy, capped to keep it bounded on modest hardware."
            )

        # OCR only the pages that lack a text layer; reuse the digital text
        # already extracted above for the rest, instead of re-OCR-ing pages
        # that don't need it.
        merged = [p if p.strip() else _ocr_page(page) for p, page in zip(pages, doc)]
        merged_text = "\n".join(merged)
        if not merged_text.strip():
            raise UnsupportedFileType(
                "This PDF has no extractable text layer, and OCR found no "
                "readable text either (likely blank pages or very low scan quality)."
            )
        # Reaching here means at least one page had no text layer and was
        # OCR'd (the all-pages-have-text case already returned above).
        return merged_text, "pdf", len(pages), "ocr"
    finally:
        doc.close()


def _ocr_page(page) -> str:
    # 200 DPI: enough resolution for OCR accuracy without the memory/CPU
    # cost of a full print-resolution render on a low-RAM host.
    pix = page.get_pixmap(dpi=200)
    # Build the PIL image directly from the pixmap's raw samples rather than
    # round-tripping through a PNG encode (tobytes("png")) + decode -- pure
    # overhead on a path this module's own docstring calls CPU-heavy.
    mode = "RGBA" if pix.alpha else "RGB"
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    # Median filter: found via testing a realistic degraded scan (blur +
    # slight rotation + photocopy-style speckle noise) that Tesseract's
    # accuracy craters specifically on the noise component — blur and
    # rotation alone didn't hurt it (still 6/6 entities correct either way),
    # but noise alone turned a 6/6 detection into 0/6 correct (CCCD and
    # phone numbers fragmented by spurious spaces, names unrecoverable).
    # Kernel size mattered more than expected: size=3 barely helped once
    # actually measured through this function's real dpi=200 pixmap (an
    # earlier check that looked promising used a lower, non-representative
    # DPI by mistake) — each noise pixel from the source scan gets
    # upsampled into a multi-pixel blob at 200 DPI, bigger than a 3x3
    # kernel. size=5 fully recovered the noise-only case but still lost
    # entities on the combined blur+rotate+noise case; size=7 recovers
    # that too (6/6, matching the clean-scan baseline) with no regression
    # on an actually-clean scan (re-verified after each kernel-size change,
    # not just the noisy case).
    img = img.filter(ImageFilter.MedianFilter(size=7))
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
    except pytesseract.TesseractError as exc:
        # Distinct from TesseractNotFoundError: the binary is present but a
        # call still failed (most commonly missing/corrupt language data,
        # e.g. tesseract-ocr installed without tesseract-ocr-vie) -- without
        # this, it propagates as an unhandled 500 instead of the documented
        # clear 422.
        logger.error(
            "OCR requested but tesseract failed to process the page (often "
            "missing language data, e.g. tesseract-ocr-vie) — see README: %s",
            exc,
        )
        raise UnsupportedFileType(
            "This PDF has no extractable text layer and needs OCR, but the "
            "server's tesseract call failed (often missing language data, "
            "e.g. tesseract-ocr-vie). See README."
        ) from None


def _extract_docx(raw: bytes) -> tuple[str, str, int, str]:
    try:
        document = Document(io.BytesIO(raw))
    except Exception as exc:
        # python-docx doesn't guarantee one exception type for a malformed
        # file — a non-zip raises zipfile.BadZipFile, a valid zip that isn't
        # a real docx (e.g. missing [Content_Types].xml) raises a plain
        # KeyError, confirmed empirically for both. Catching broadly here
        # (scoped tightly to just this constructor call, same pattern as
        # the third-party-library try/excepts elsewhere in this codebase)
        # rather than enumerating every possible internal exception type.
        raise UnsupportedFileType(
            f"This file isn't a valid .docx (couldn't be opened: {exc}). "
            f"Check it isn't corrupted."
        ) from exc
    parts = [p.text for p in document.paragraphs]

    # Flatten tables to pipe-delimited rows so cell values keep context for scoring.
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))

    return "\n".join(parts), "docx", 1, "direct_text_extraction"
