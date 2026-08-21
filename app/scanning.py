"""Core scan logic: analyze -> build entity list -> optional anonymize.

Shared by /api/v1/scan (raw text) and /api/v1/scan/file (PDF/DOCX/TXT
upload) in main.py so the two entry points can never drift in behavior.
"""

from typing import Optional

from fastapi import HTTPException
from presidio_analyzer import AnalyzerEngine, RecognizerResult
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
# Vietnamese content still scans fine under "en" -- categories are regex-based.


def _drop_lower_scored_exact_duplicates(results: list) -> list:
    """When two categories match the exact same [start, end) span, keep only
    the highest-scoring one -- e.g. a VN national ID also matching some
    other country's phone-number shape at a lower score.
    """
    best_by_span: dict = {}
    for r in results:
        key = (r.start, r.end)
        current = best_by_span.get(key)
        if current is None or r.score > current.score:
            best_by_span[key] = r
    return list(best_by_span.values())


# Entity types deep scan can also produce -- an overlap means deep scan's
# version wins. Excludes the two whole-sentence-flag types, which routinely
# overlap unrelated nested entities.
_DEEP_SCAN_OVERLAP_TYPES = {
    "ORGANIZATION", "PERSON", "LOCATION",
    "EMAIL_ADDRESS", "PHONE_NUMBER", "URL", "IP_ADDRESS", "CREDIT_CARD",
    "IBAN_CODE", "CRYPTO", "MAC_ADDRESS", "US_SSN",
    "CONTRACT_ID", "INTERNAL_TAX_CODE", "FINANCIAL_METRIC", "EMPLOYEE_ID",
    "INFRA_SECRET", "IP_SENSITIVE_MARKER", "CRYPTO_PRIVATE_KEY",
    "INFRA_NETWORK_MAP", "GPS_LOCATION", "FINANCIAL_CREDENTIAL",
    "VN_NATIONAL_ID", "BANK_ACCOUNT_NUMBER", "FULL_ADDRESS",
}

# Deep-scan categories that flag a whole sentence's topic, not a value to mask.
_ANONYMIZE_EXCLUDED_TYPES = {"HR_SENSITIVE_CONTENT", "IP_TRADE_SECRET_CONTENT"}


def _drop_regex_ner_entities_overlapped_by_deep_scan(
    base_entities: list, deep_entities: list
) -> list:
    """Drop a free-path entity when deep scan found an overlapping one of a
    type it can produce -- deep scan's span is usually the fuller/correct
    one (e.g. underthesea's truncated company name vs. deep scan's full one).
    """
    competing_deep = [d for d in deep_entities if d.entity_type in _DEEP_SCAN_OVERLAP_TYPES]
    if not competing_deep:
        return base_entities

    def overlaps(a: DetectedEntity, b: DetectedEntity) -> bool:
        return a.location.start < b.location.end and b.location.start < a.location.end

    return [
        e
        for e in base_entities
        if not (
            e.entity_type in _DEEP_SCAN_OVERLAP_TYPES
            and any(overlaps(e, d) for d in competing_deep)
        )
    ]


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
    results = _drop_lower_scored_exact_duplicates(results)
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
        entities = _drop_regex_ner_entities_overlapped_by_deep_scan(entities, deep_entities)
        entities.extend(deep_entities)
        entities.sort(key=lambda e: e.location.start)

    anonymized_content = None
    if anonymize:
        # Built from the final deduped `entities`, not raw `results`, so
        # masking matches what detected_entities reports.
        anonymize_results = [
            RecognizerResult(
                entity_type=e.entity_type,
                start=e.location.start,
                end=e.location.end,
                score=e.score,
            )
            for e in entities
            if e.entity_type not in _ANONYMIZE_EXCLUDED_TYPES
        ]
        anon_result = anonymizer.anonymize(text=text, analyzer_results=anonymize_results)
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
