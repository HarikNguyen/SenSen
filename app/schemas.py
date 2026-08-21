"""Pydantic request/response contracts for the scan API."""

from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr


class RegisterResponse(BaseModel):
    email: str
    api_key: str


class ScanRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Raw text to scan")
    language: str = Field(default="en", description="Language code, e.g. en, vi")
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    anonymize: bool = Field(default=False, description="Return a masked copy of the text")
    deep_scan: bool = Field(
        default=False,
        description="Also run an LLM-based pass for semantic-only categories (costs a Gemini call)",
    )
    model: Optional[str] = Field(
        default=None,
        description=(
            "Gemini model id for deep_scan, e.g. from GET /api/v1/deep_scan/models. "
            "Ignored unless deep_scan is true; omit to use the server default."
        ),
    )


class EntityLocation(BaseModel):
    start: int
    end: int
    page: Optional[int] = None


class DetectedEntity(BaseModel):
    entity_type: str
    location: EntityLocation
    text_val: str
    score: float
    context_snippet: str


class AnonymizedContent(BaseModel):
    text: str


class DocumentMetadata(BaseModel):
    file_name: Optional[str] = None
    file_type: str = "text"
    processing_mode: str = "direct_text_extraction"
    total_pages: Optional[int] = None


class ScanResponse(BaseModel):
    status: str
    document_metadata: DocumentMetadata
    detected_entities: list[DetectedEntity]
    anonymized_content: Optional[AnonymizedContent] = None
    deep_scan_status: Optional[str] = Field(
        default=None,
        description=(
            "null if deep_scan wasn't requested; otherwise one of "
            "'ok', 'skipped_no_key', 'skipped_quota_exceeded', 'skipped_error'"
        ),
    )


class DeepScanModelsResponse(BaseModel):
    status: str = Field(description="'ok', 'skipped_no_key', or 'skipped_error' — same vocabulary as deep_scan_status")
    default_model: str
    models: list[str]


class UsageResponse(BaseModel):
    deep_scan_used: int
    deep_scan_limit: int
    ocr_api_used: int
    ocr_api_limit: int


class OcrModelsResponse(BaseModel):
    status: str = Field(description="'ok', 'skipped_no_key', or 'skipped_error' — same vocabulary as deep_scan_status")
    default_model: str
    models: list[str]
