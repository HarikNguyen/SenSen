"""SenSen API — FastAPI gateway over a Presidio-based sensitive data classifier.

Architecture: see README.md. Custom recognizers live entirely in
app/recognizers/recognizers.yaml (Presidio's own config-driven registry) —
this file only wires the engines together and exposes the HTTP surface.
"""

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.recognizer_registry import RecognizerRegistryProvider
from presidio_anonymizer import AnonymizerEngine
from sqlalchemy.orm import Session

from app.database import APIKey, User, get_db, init_db
from app.extract import UnsupportedFileType, extract_text
from app.schemas import (
    AnonymizedContent,
    DetectedEntity,
    DocumentMetadata,
    EntityLocation,
    RegisterRequest,
    RegisterResponse,
    ScanRequest,
    ScanResponse,
)

BASE_DIR = Path(__file__).resolve().parent.parent
RECOGNIZERS_CONF = BASE_DIR / "app" / "recognizers" / "recognizers.yaml"

CONTEXT_WINDOW = 40  # chars of surrounding text captured in context_snippet
MAX_TEXT_LENGTH = 50_000  # guardrail for the target low-RAM (i3) host
SUPPORTED_LANGUAGES = {"en"}  # custom recognizers are tagged supported_language: en;
# Vietnamese-specific NLP (vi_spacy/underthesea) is a roadmap item, see README.
# Vietnamese *content* is still scanned fine under the "en" profile since all
# 5 custom categories are regex/context-based, not dependent on spaCy's NER.


def build_engines() -> tuple[AnalyzerEngine, AnonymizerEngine]:
    """Construct the analyzer/anonymizer once. Called from lifespan (singleton)."""
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    ).create_engine()

    registry = RecognizerRegistryProvider(
        conf_file=str(RECOGNIZERS_CONF), nlp_engine=nlp_engine
    ).create_recognizer_registry()

    # Widen the context window to look both before AND after a match (default is
    # prefix-only, 5 words). thongtin.md 2.B asks for context words "xung quanh"
    # (around) a match, not just preceding it — matters a lot for Vietnamese
    # sentences where the qualifying word often comes after the number/code.
    context_enhancer = LemmaContextAwareEnhancer(
        context_prefix_count=5, context_suffix_count=5
    )

    analyzer = AnalyzerEngine(
        registry=registry,
        nlp_engine=nlp_engine,
        supported_languages=["en"],
        context_aware_enhancer=context_enhancer,
    )
    anonymizer = AnonymizerEngine()
    return analyzer, anonymizer


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


def verify_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> APIKey:
    api_key = db.query(APIKey).filter(APIKey.key == x_api_key).first()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )
    api_key.request_count += 1
    db.commit()
    return api_key


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


def _run_scan(
    text: str,
    language: str,
    confidence_threshold: float,
    anonymize: bool,
    analyzer: AnalyzerEngine,
    anonymizer: AnonymizerEngine,
    *,
    file_name: Optional[str] = None,
    file_type: str = "text",
    processing_mode: str = "direct_text_extraction",
    total_pages: int = 1,
) -> ScanResponse:
    """Shared core: analyze -> build entity list -> optional anonymize.

    Used by both /api/v1/scan (raw text) and /api/v1/scan/file (PDF/DOCX/TXT
    upload) so the two entry points can never drift in behavior.
    """
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
    )


@app.post("/api/v1/scan", response_model=ScanResponse)
async def scan(
    payload: ScanRequest,
    api_key: APIKey = Depends(verify_api_key),
    analyzer: AnalyzerEngine = Depends(get_analyzer),
    anonymizer: AnonymizerEngine = Depends(get_anonymizer),
):
    return _run_scan(
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

    return _run_scan(
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
