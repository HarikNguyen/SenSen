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

router = APIRouter()


def get_analyzer(request: Request) -> AnalyzerEngine:
    return request.app.state.analyzer


def get_anonymizer(request: Request) -> AnonymizerEngine:
    return request.app.state.anonymizer


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
    api_key: APIKey = Depends(verify_api_key),
    analyzer: AnalyzerEngine = Depends(get_analyzer),
    anonymizer: AnonymizerEngine = Depends(get_anonymizer),
):
    return run_scan(
        payload.text,
        payload.language,
        payload.confidence_threshold,
        payload.anonymize,
        analyzer,
        anonymizer,
    )


@router.post("/api/v1/scan/file", response_model=ScanResponse)
async def scan_file(
    file: UploadFile = File(...),
    language: str = Form("en"),
    confidence_threshold: float = Form(0.7),
    anonymize: bool = Form(False),
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

    return run_scan(
        text,
        language,
        confidence_threshold,
        anonymize,
        analyzer,
        anonymizer,
        file_name=file.filename,
        file_type=file_type,
        processing_mode="direct_text_extraction",
        total_pages=total_pages,
    )
