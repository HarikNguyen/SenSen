"""SenSen API — FastAPI gateway over a Presidio-based sensitive data classifier.

Web layer only: routes, dependency wiring, app lifecycle. Engine construction
lives in app/engine.py, scan orchestration in app/scanning.py, auth in
app/auth.py, custom recognizers in app/recognizers/recognizers.yaml.
"""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from sqlalchemy.orm import Session

from app.auth import verify_api_key
from app.database import APIKey, User, get_db, init_db
from app.engine import build_engines
from app.extract import UnsupportedFileType, extract_text
from app.scanning import run_scan
from app.schemas import RegisterRequest, RegisterResponse, ScanRequest, ScanResponse

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.analyzer, app.state.anonymizer = build_engines()
    yield


app = FastAPI(
    title="SenSen — Sensitive Data Classifier",
    description="Presidio-powered enterprise sensitive data discovery & classification API.",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


def get_analyzer(request: Request) -> AnalyzerEngine:
    return request.app.state.analyzer


def get_anonymizer(request: Request) -> AnonymizerEngine:
    return request.app.state.anonymizer


@app.post("/register", response_model=RegisterResponse)
async def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(email=payload.email)
    db.add(user)
    db.flush()  # populate user.id before creating the dependent APIKey row

    api_key = APIKey(key=uuid.uuid4().hex, user_id=user.id)
    db.add(api_key)
    db.commit()

    return RegisterResponse(email=user.email, api_key=api_key.key)


@app.post("/api/v1/scan", response_model=ScanResponse)
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


@app.post("/api/v1/scan/file", response_model=ScanResponse)
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
