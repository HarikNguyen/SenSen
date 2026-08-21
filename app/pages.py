"""HTTP route declarations — the routing/navigation layer.

Thin on purpose: every handler parses its request, calls into app/logics.py
or app/scanning.py for the actual processing, and shapes the response.
"""

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from sqlalchemy.orm import Session

from app.auth import verify_api_key
from app.database import APIKey, get_db
from app.deep_scan import DEFAULT_MODEL_ID, list_available_models
from app.extract import UnsupportedFileType, extract_text
from app.logics import register_user
from app.ocr_api import (
    DEFAULT_GEMINI_OCR_MODEL,
    DEFAULT_OPENAI_OCR_MODEL,
    DEFAULT_XAI_OCR_MODEL,
    list_openai_style_models,
)
from app.redact import RedactionFailed, redact_docx, redact_pdf, redact_txt
from app.scanning import run_scan
from app.schemas import (
    DeepScanModelsResponse,
    OcrModelsResponse,
    RegisterRequest,
    RegisterResponse,
    ScanRequest,
    ScanResponse,
    UsageResponse,
)

BASE_DIR = Path(__file__).resolve().parent.parent

# Lifetime cap per key, not daily-rolling -- simplest guard against one
# client draining the shared free-tier quota.
MAX_DEEP_SCAN_PER_KEY = 50

# Real per-page vendor calls; two of the three OCR engines have no free tier.
MAX_OCR_API_PER_KEY = 30

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


def _resolve_ocr_engine(requested: str, api_key: APIKey, db: Session) -> str:
    """Whether the requested OCR engine may be used. "local" is always free;
    a cloud engine spends from a lifetime-per-key cap, charged on request
    like _resolve_deep_scan (not on confirmed OCR necessity).
    """
    if requested == "local":
        return "local"
    if api_key.ocr_api_count >= MAX_OCR_API_PER_KEY:
        raise HTTPException(
            status_code=422,
            detail=(
                f"OCR API quota exceeded for this key "
                f"({MAX_OCR_API_PER_KEY} cloud OCR calls used). "
                f"Use ocr_engine=local, or check GET /api/v1/usage."
            ),
        )
    api_key.ocr_api_count += 1
    db.commit()
    return requested


@router.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


@router.post("/register", response_model=RegisterResponse)
async def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    user, api_key = register_user(payload.email, db)
    return RegisterResponse(email=user.email, api_key=api_key.key)


@router.get("/api/v1/deep_scan/models", response_model=DeepScanModelsResponse)
async def deep_scan_models(api_key: APIKey = Depends(verify_api_key)):
    """List Gemini model ids the server's key can use for deep_scan's
    `model` param. Live-queried, not behind the deep-scan quota — listing
    models doesn't spend a generateContent call.
    """
    models, status = list_available_models()
    return DeepScanModelsResponse(status=status, default_model=DEFAULT_MODEL_ID, models=models)


@router.get("/api/v1/ocr/models", response_model=OcrModelsResponse)
async def ocr_models(
    engine: Literal["gemini", "openai", "grok"] = Query(...),
    api_key: APIKey = Depends(verify_api_key),
):
    """List model ids for the file-upload endpoint's `ocr_model` param, per
    cloud `engine`. Live-queried -- listing models doesn't spend a vision
    call. Separate from GET /api/v1/deep_scan/models since OCR and deep_scan
    can use different models for the same request.
    """
    if engine == "gemini":
        models, status = list_available_models()
        return OcrModelsResponse(status=status, default_model=DEFAULT_GEMINI_OCR_MODEL, models=models)
    if engine == "openai":
        models, status = list_openai_style_models(api_key_env="OPENAI_API_KEY", base_url=None)
        return OcrModelsResponse(status=status, default_model=DEFAULT_OPENAI_OCR_MODEL, models=models)
    models, status = list_openai_style_models(
        api_key_env="XAI_API_KEY", base_url="https://api.x.ai/v1"
    )
    return OcrModelsResponse(status=status, default_model=DEFAULT_XAI_OCR_MODEL, models=models)


@router.get("/api/v1/usage", response_model=UsageResponse)
async def usage(api_key: APIKey = Depends(verify_api_key)):
    """This key's usage against SenSen's own lifetime caps -- not the
    vendor's live quota, which none of the three engines expose a read
    endpoint for.
    """
    return UsageResponse(
        deep_scan_used=api_key.deep_scan_count,
        deep_scan_limit=MAX_DEEP_SCAN_PER_KEY,
        ocr_api_used=api_key.ocr_api_count,
        ocr_api_limit=MAX_OCR_API_PER_KEY,
    )


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
        deep_scan_model=payload.model,
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
    model: Optional[str] = Form(None),
    ocr_engine: Literal["local", "gemini", "openai", "grok"] = Form("local"),
    ocr_model: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    api_key: APIKey = Depends(verify_api_key),
    analyzer: AnalyzerEngine = Depends(get_analyzer),
    anonymizer: AnonymizerEngine = Depends(get_anonymizer),
):
    """Upload a .pdf (digital text, or scanned via OCR -- local Tesseract
    by default, or an opt-in cloud engine via ocr_engine), .docx or .txt
    file to scan.
    """
    raw = await file.read()
    resolved_ocr_engine = _resolve_ocr_engine(ocr_engine, api_key, db)
    try:
        text, file_type, total_pages, processing_mode = extract_text(
            file.filename, raw, resolved_ocr_engine, ocr_model
        )
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
        deep_scan_model=model,
        file_name=file.filename,
        file_type=file_type,
        processing_mode=processing_mode,
        total_pages=total_pages,
    )
    if deep_scan and not allowed:
        response.deep_scan_status = "skipped_quota_exceeded"
    return response


# One endpoint for all three formats, matching scan_file's uniform handling.
_REDACT_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}


@router.post("/api/v1/redact/file")
async def redact_file(
    file: UploadFile = File(...),
    confidence_threshold: float = Form(0.7),
    deep_scan: bool = Form(False),
    model: Optional[str] = Form(None),
    ocr_engine: Literal["local", "gemini", "openai", "grok"] = Form("local"),
    ocr_model: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    api_key: APIKey = Depends(verify_api_key),
    analyzer: AnalyzerEngine = Depends(get_analyzer),
    anonymizer: AnonymizerEngine = Depends(get_anonymizer),
):
    """Upload a .pdf/.docx/.txt and get back an actual redacted file --
    sensitive content genuinely removed, not just masked text like
    `/api/v1/scan/file?anonymize=true` returns. See app/redact.py for
    mechanisms and the DOCX out-of-scope list.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _REDACT_MEDIA_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Redaction supports .pdf, .docx and .txt (got {suffix or '(none)'}).",
        )

    raw = await file.read()
    resolved_ocr_engine = _resolve_ocr_engine(ocr_engine, api_key, db)
    allowed_deep_scan = _resolve_deep_scan(deep_scan, api_key, db)

    try:
        if suffix == ".pdf":
            redacted = redact_pdf(
                raw,
                analyzer=analyzer,
                confidence_threshold=confidence_threshold,
                ocr_engine=resolved_ocr_engine,
                ocr_model=ocr_model,
                deep_scan=allowed_deep_scan,
                deep_scan_model=model,
            )
        elif suffix == ".docx":
            redacted = redact_docx(
                raw,
                analyzer=analyzer,
                confidence_threshold=confidence_threshold,
                ocr_engine=resolved_ocr_engine,
                ocr_model=ocr_model,
                deep_scan=allowed_deep_scan,
                deep_scan_model=model,
            )
        else:
            redacted = redact_txt(
                raw,
                analyzer=analyzer,
                anonymizer=anonymizer,
                confidence_threshold=confidence_threshold,
                deep_scan=allowed_deep_scan,
                deep_scan_model=model,
            )
    except UnsupportedFileType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RedactionFailed as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    safe_name = Path(file.filename or f"document{suffix}").name
    return Response(
        content=redacted,
        media_type=_REDACT_MEDIA_TYPES[suffix],
        headers={"Content-Disposition": f'attachment; filename="redacted_{safe_name}"'},
    )
