"""Cloud OCR fallback for scanned PDFs -- opt-in alternative to local
Tesseract (app/extract.py). Gemini goes through google-genai directly;
OpenAI and Grok share one function since xAI's Responses API mirrors
OpenAI's field-for-field.
"""

import base64
import io
import json
import logging
import os
from typing import NamedTuple, Optional

from google import genai
from google.genai import types as genai_types
from openai import OpenAI
from PIL import Image
from pydantic import BaseModel

from app.rate_limiter import SlidingWindowRateLimiter, gemini_limiter, openai_limiter, xai_limiter
from app.retry import call_with_backoff

logger = logging.getLogger("sensen.ocr_api")


class OcrWord(NamedTuple):
    """OCR'd line of text + its pixel-space bbox (x0, y0, x1, y1)."""

    text: str
    bbox: tuple[float, float, float, float]

OCR_API_ENGINES = {"gemini", "openai", "grok"}

# Also read by GET /api/v1/ocr/models so the reported default can't drift.
DEFAULT_GEMINI_OCR_MODEL = "gemini-flash-lite-latest"
DEFAULT_OPENAI_OCR_MODEL = "gpt-5.6"
DEFAULT_XAI_OCR_MODEL = "grok-4.6"

_OCR_PROMPT = (
    "Extract all text from this document image, verbatim, preserving the "
    "original line breaks and layout order. The document may mix "
    "Vietnamese and English. Output only the extracted text -- no "
    "commentary, no markdown formatting, no translation."
)

# OpenAI-compatible /v1/models has no capability metadata, so this is a
# best-effort exclusion list for obviously non-chat model families.
_NON_VISION_MODEL_SUBSTRINGS = (
    "whisper", "tts", "embedding", "moderation", "dall-e", "davinci",
    "babbage", "audio", "realtime", "transcribe", "search", "imagine",
)


class OcrApiNotConfigured(Exception):
    """Raised when the requested engine's API key env var isn't set."""

    def __init__(self, engine: str, env_var: str):
        self.engine = engine
        self.env_var = env_var
        super().__init__(f"{engine} OCR requested but {env_var} isn't set")


class OcrApiError(Exception):
    """Provider call itself failed (network, auth, bad model id, rate limit)."""


def ocr_image_via_api(engine: str, png_bytes: bytes, model: Optional[str] = None) -> str:
    """`model` is independent from deep_scan's own model param."""
    if engine == "gemini":
        api_key = os.getenv("LANGEXTRACT_API_KEY")
        if not api_key:
            raise OcrApiNotConfigured("gemini", "LANGEXTRACT_API_KEY")
        return _ocr_gemini(png_bytes, api_key, model)

    if engine == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise OcrApiNotConfigured("openai", "OPENAI_API_KEY")
        resolved_model = model or os.getenv("OPENAI_OCR_MODEL", DEFAULT_OPENAI_OCR_MODEL)
        return _ocr_responses_api(
            png_bytes, api_key=api_key, base_url=None, model=resolved_model, limiter=openai_limiter
        )

    if engine == "grok":
        api_key = os.getenv("XAI_API_KEY")
        if not api_key:
            raise OcrApiNotConfigured("grok", "XAI_API_KEY")
        resolved_model = model or os.getenv("XAI_OCR_MODEL", DEFAULT_XAI_OCR_MODEL)
        return _ocr_responses_api(
            png_bytes,
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            model=resolved_model,
            limiter=xai_limiter,
        )

    raise ValueError(f"Unknown OCR API engine {engine!r} (expected one of {OCR_API_ENGINES})")


def _ocr_gemini(png_bytes: bytes, api_key: str, model_override: Optional[str] = None) -> str:
    # gemini-flash-lite-latest, not gemini-flash-latest -- the latter 503s
    # on image+text requests under load even when plain-text calls succeed.
    model = model_override or os.getenv("GEMINI_OCR_MODEL", DEFAULT_GEMINI_OCR_MODEL)
    client = genai.Client(api_key=api_key)

    def _call() -> str:
        gemini_limiter.acquire()  # shared quota with app/deep_scan.py
        response = client.models.generate_content(
            model=model,
            contents=[
                genai_types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                _OCR_PROMPT,
            ],
        )
        return (response.text or "").strip()

    try:
        return call_with_backoff(_call, label=f"gemini OCR ({model})")
    except Exception as exc:
        raise OcrApiError(f"Gemini OCR call failed ({model}): {exc}") from exc


def _ocr_responses_api(
    png_bytes: bytes,
    *,
    api_key: str,
    base_url: Optional[str],
    model: str,
    limiter: SlidingWindowRateLimiter,
) -> str:
    data_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    def _call() -> str:
        limiter.acquire()
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": data_url},
                        {"type": "input_text", "text": _OCR_PROMPT},
                    ],
                }
            ],
        )
        return (response.output_text or "").strip()

    try:
        return call_with_backoff(_call, label=f"OCR ({model})")
    except Exception as exc:
        raise OcrApiError(f"OCR call failed ({model}): {exc}") from exc


def list_openai_style_models(*, api_key_env: str, base_url: Optional[str]) -> tuple[list[str], str]:
    """Best-effort: no capability metadata confirms vision support, so a
    returned model can still fail on the next call."""
    api_key = os.getenv(api_key_env)
    if not api_key:
        return [], "skipped_no_key"

    try:
        client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        names = sorted(
            m.id for m in client.models.list()
            if not any(bad in m.id.lower() for bad in _NON_VISION_MODEL_SUBSTRINGS)
        )
    except Exception:
        logger.warning("ocr_api: model listing failed for %s", api_key_env, exc_info=True)
        return [], "skipped_error"

    return names, "ok"


# ---------------------------------------------------------- OCR with boxes ----
# For app/redact.py -- per-line boxes. Gemini: box_2d [ymin,xmin,ymax,xmax]
# / 0-1000. OpenAI: [x_min,y_min,x_max,y_max] / 0-999. Grok: undocumented.

_GEMINI_BBOX_PROMPT = (
    "Detect every line of text in this document image. For each line, give "
    "its text (verbatim) and its box_2d: [ymin, xmin, ymax, xmax], "
    "normalized to a 0-1000 scale relative to the image width/height. The "
    "document may mix Vietnamese and English."
)


class _GeminiBboxItem(BaseModel):
    """response_schema for _ocr_words_gemini -- forces valid JSON and exact
    key names (Gemini's free-text JSON used to drift, e.g. "label"/"box").
    """

    text: str
    box_2d: list[float]

_OPENAI_BBOX_PROMPT = (
    "Detect every line of text in this document image. Respond with ONLY "
    "a JSON array (no markdown, no commentary) where each item is "
    '{"text": "<line text, verbatim>", "box_2d": [x_min, y_min, x_max, y_max]}. '
    "Coordinates are normalized to a 0-999 scale relative to the image "
    "width/height, top-left origin. The document may mix Vietnamese and English."
)


def _image_size(png_bytes: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png_bytes)) as img:
        return img.size  # (width, height)


def _parse_bbox_json(raw: str) -> list:
    """Strips a markdown fence; strict=False for an unescaped control character."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if "\n" in cleaned:
            first_line, rest = cleaned.split("\n", 1)
            cleaned = rest if first_line.strip().lower() in ("json", "") else cleaned
    return json.loads(cleaned, strict=False)


def _items_to_words(
    items: list, width: int, height: int, *, coord_order: str, scale: int
) -> list[OcrWord]:
    words = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Vendors don't always match the prompt's exact field names.
        text = str(item.get("text") or item.get("label") or "").strip()
        box = item.get("box_2d") or item.get("box") or item.get("bbox")
        if not text or not isinstance(box, list) or len(box) != 4:
            continue
        try:
            a, b, c, d = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        if coord_order == "ymin_xmin_ymax_xmax":
            ymin, xmin, ymax, xmax = a, b, c, d
        else:
            xmin, ymin, xmax, ymax = a, b, c, d
        words.append(
            OcrWord(
                text=text,
                bbox=(
                    xmin / scale * width,
                    ymin / scale * height,
                    xmax / scale * width,
                    ymax / scale * height,
                ),
            )
        )
    return words


def ocr_words_via_api(engine: str, png_bytes: bytes, model: Optional[str] = None) -> list[OcrWord]:
    """Like ocr_image_via_api, but returns per-line (text, bbox) pairs."""
    if engine == "gemini":
        api_key = os.getenv("LANGEXTRACT_API_KEY")
        if not api_key:
            raise OcrApiNotConfigured("gemini", "LANGEXTRACT_API_KEY")
        return _ocr_words_gemini(png_bytes, api_key, model)

    if engine == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise OcrApiNotConfigured("openai", "OPENAI_API_KEY")
        resolved_model = model or os.getenv("OPENAI_OCR_MODEL", DEFAULT_OPENAI_OCR_MODEL)
        return _ocr_words_responses_api(
            png_bytes, api_key=api_key, base_url=None, model=resolved_model, limiter=openai_limiter
        )

    if engine == "grok":
        api_key = os.getenv("XAI_API_KEY")
        if not api_key:
            raise OcrApiNotConfigured("grok", "XAI_API_KEY")
        resolved_model = model or os.getenv("XAI_OCR_MODEL", DEFAULT_XAI_OCR_MODEL)
        return _ocr_words_responses_api(
            png_bytes,
            api_key=api_key,
            base_url="https://api.x.ai/v1",
            model=resolved_model,
            limiter=xai_limiter,
        )

    raise ValueError(f"Unknown OCR API engine {engine!r} (expected one of {OCR_API_ENGINES})")


def _ocr_words_gemini(
    png_bytes: bytes, api_key: str, model_override: Optional[str] = None
) -> list[OcrWord]:
    model = model_override or os.getenv("GEMINI_OCR_MODEL", DEFAULT_GEMINI_OCR_MODEL)
    client = genai.Client(api_key=api_key)
    width, height = _image_size(png_bytes)

    def _call() -> list[OcrWord]:
        gemini_limiter.acquire()
        response = client.models.generate_content(
            model=model,
            contents=[
                genai_types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                _GEMINI_BBOX_PROMPT,
            ],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=list[_GeminiBboxItem],
            ),
        )
        items = _parse_bbox_json(response.text or "[]")
        return _items_to_words(items, width, height, coord_order="ymin_xmin_ymax_xmax", scale=1000)

    try:
        return call_with_backoff(_call, label=f"gemini OCR-boxes ({model})")
    except Exception as exc:
        raise OcrApiError(f"Gemini OCR-boxes call failed ({model}): {exc}") from exc


def _ocr_words_responses_api(
    png_bytes: bytes,
    *,
    api_key: str,
    base_url: Optional[str],
    model: str,
    limiter: SlidingWindowRateLimiter,
) -> list[OcrWord]:
    data_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    width, height = _image_size(png_bytes)

    def _call() -> list[OcrWord]:
        limiter.acquire()
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        # "original" avoids downscaling, needed for box precision.
                        {"type": "input_image", "image_url": data_url, "detail": "original"},
                        {"type": "input_text", "text": _OPENAI_BBOX_PROMPT},
                    ],
                }
            ],
        )
        items = _parse_bbox_json(response.output_text or "[]")
        return _items_to_words(items, width, height, coord_order="xmin_ymin_xmax_ymax", scale=999)

    try:
        return call_with_backoff(_call, label=f"OCR-boxes ({model})")
    except Exception as exc:
        raise OcrApiError(f"OCR-boxes call failed ({model}): {exc}") from exc
