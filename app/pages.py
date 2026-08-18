"""HTTP route declarations — the routing/navigation layer.

Thin on purpose: every handler parses its request, calls into app/logics.py
or app/scanning.py for the actual processing, and shapes the response.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from sqlalchemy.orm import Session

from app.auth import verify_api_key
from app.database import APIKey, get_db
from app.extract import UnsupportedFileType, extract_text
from app.logics import register_user
from app.scanning import run_scan
from app.schemas import RegisterRequest, RegisterResponse, ScanRequest, ScanResponse

BASE_DIR = Path(__file__).resolve().parent.parent

# Lifetime cap per key, not a rolling daily window — the simplest guard that
# stops one client draining the shared Gemini free-tier quota. A real
# daily-reset quota is a documented follow-up once real usage is observed
# (see README).
MAX_DEEP_SCAN_PER_KEY = 50

router = APIRouter()


def get_analyzer(request: Request) -> AnalyzerEngine:
    return request.app.state.analyzer


def get_anonymizer(request: Request) -> AnonymizerEngine:
    return request.app.state.anonymizer


def _resolve_deep_scan(requested: bool, api_key: APIKey, db: Session) -> bool:
    """Whether the deep scan pass should actually run. Records the attempt
    against the key's quota when it does."""
    if not requested or api_key.deep_scan_count >= MAX_DEEP_SCAN_PER_KEY:
        return False
    api_key.deep_scan_count += 1
    db.commit()
    return True


@router.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@router.post("/register", response_model=RegisterResponse)
async def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    user, api_key = register_user(payload.email, db)
    return RegisterResponse(email=user.email, api_key=api_key.key)


@router.post("/api/v1/scan", response_model=ScanResponse)
async def scan(
    payload: ScanRequest,
    db: Session = Depends(get_db),
    api_key: APIKey = Depends(verify_api_key),
    analyzer: AnalyzerEngine = Depends(get_analyzer),
    anonymizer: AnonymizerEngine = Depends(get_anonymizer),
):
    allowed = _resolve_deep_scan(payload.deep_scan, api_key, db)
    response = run_scan(
        payload.text,
        payload.language,
        payload.confidence_threshold,
        payload.anonymize,
        analyzer,
        anonymizer,
        deep_scan=allowed,
    )
    if payload.deep_scan and not allowed:
        response.deep_scan_status = "skipped_quota_exceeded"
    return response


@router.post("/api/v1/scan/file", response_model=ScanResponse)
async def scan_file(
    file: UploadFile = File(...),
    language: str = Form("en"),
    confidence_threshold: float = Form(0.7),
    anonymize: bool = Form(False),
    deep_scan: bool = Form(False),
    db: Session = Depends(get_db),
    api_key: APIKey = Depends(verify_api_key),
    analyzer: AnalyzerEngine = Depends(get_analyzer),
    anonymizer: AnonymizerEngine = Depends(get_anonymizer),
):
    """Upload a .pdf (digital text layer), .docx or .txt file to scan.

    No OCR in this MVP — scanned/image PDFs raise a 422 pointing at the
    Azure Document Intelligence roadmap item in README.md.
    """
    raw = await file.read()
    try:
        text, file_type, total_pages = extract_text(file.filename, raw)
    except UnsupportedFileType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    allowed = _resolve_deep_scan(deep_scan, api_key, db)
    response = run_scan(
        text,
        language,
        confidence_threshold,
        anonymize,
        analyzer,
        anonymizer,
        deep_scan=allowed,
        file_name=file.filename,
        file_type=file_type,
        processing_mode="direct_text_extraction",
        total_pages=total_pages,
    )
    if deep_scan and not allowed:
        response.deep_scan_status = "skipped_quota_exceeded"
    return response
