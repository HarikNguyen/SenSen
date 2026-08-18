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
