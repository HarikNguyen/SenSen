"""Core scan logic: analyze -> build entity list -> optional anonymize.

Shared by /api/v1/scan (raw text) and /api/v1/scan/file (PDF/DOCX/TXT
upload) in main.py so the two entry points can never drift in behavior.
"""

from typing import Optional

from fastapi import HTTPException
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

from app.deep_scan import run_deep_scan
from app.schemas import (
    AnonymizedContent,
    DetectedEntity,
    DocumentMetadata,
    EntityLocation,
    ScanResponse,
)

CONTEXT_WINDOW = 40  # chars of surrounding text captured in context_snippet
MAX_TEXT_LENGTH = 50_000  # guardrail for the target low-RAM (i3) host
SUPPORTED_LANGUAGES = {"en"}
# Vietnamese content still scans fine under "en" (categories are regex-based,
# not NER-dependent) — a real Vietnamese model is a roadmap item, see README.


def run_scan(
    text: str,
    language: str,
    confidence_threshold: float,
    anonymize: bool,
    analyzer: AnalyzerEngine,
    anonymizer: AnonymizerEngine,
    *,
    deep_scan: bool = False,
    deep_scan_model: Optional[str] = None,
    file_name: Optional[str] = None,
    file_type: str = "text",
    processing_mode: str = "direct_text_extraction",
    total_pages: int = 1,
) -> ScanResponse:
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"language='{language}' is not supported in this MVP "
                f"(only 'en'). See README roadmap for Vietnamese-specific NLP."
            ),
        )
    if len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=413,
            detail=f"text exceeds {MAX_TEXT_LENGTH} characters (MVP limit for the target host).",
        )

    results = analyzer.analyze(
        text=text, language=language, score_threshold=confidence_threshold
    )
    results = sorted(results, key=lambda r: r.start)

    entities = [
        DetectedEntity(
            entity_type=r.entity_type,
            location=EntityLocation(start=r.start, end=r.end),
            text_val=text[r.start : r.end],
            score=round(r.score, 3),
            context_snippet=text[
                max(0, r.start - CONTEXT_WINDOW) : min(len(text), r.end + CONTEXT_WINDOW)
            ],
        )
        for r in results
    ]

    deep_scan_status = None
    if deep_scan:
        deep_entities, deep_scan_status = run_deep_scan(text, model_id=deep_scan_model)
        entities.extend(deep_entities)
        entities.sort(key=lambda e: e.location.start)
        # Note: deep_entities are not passed through anonymizer.anonymize()
        # below — that only understands Presidio's own RecognizerResult, not
        # langextract output. anonymize=true masks regex-found spans only.

    anonymized_content = None
    if anonymize:
        anon_result = anonymizer.anonymize(text=text, analyzer_results=results)
        anonymized_content = AnonymizedContent(text=anon_result.text)

    return ScanResponse(
        status="success",
        document_metadata=DocumentMetadata(
            file_name=file_name,
            file_type=file_type,
            processing_mode=processing_mode,
            total_pages=total_pages,
        ),
        detected_entities=entities,
        anonymized_content=anonymized_content,
        deep_scan_status=deep_scan_status,
    )
