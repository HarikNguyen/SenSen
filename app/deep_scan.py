"""Optional LLM-based second pass for categories regex can't reach.

The only module that imports langextract, mirroring how app/engine.py is the
only one that imports presidio_analyzer. Never called automatically — only
when a caller explicitly opts in (see app/scanning.py's deep_scan flag),
because every call costs a Gemini free-tier request, and free-tier RPM/RPD
limits vary by model and change over time (see GET /api/v1/deep_scan/models
and DEFAULT_MODEL_ID above — don't trust a number hardcoded in a comment).

Pilot categories (extend by adding more ExampleData entries below, no other
code changes needed — same story as app/recognizers/recognizers.yaml):
  - IP_TRADE_SECRET_CONTENT: upgrades the regex-only IP_SENSITIVE_MARKER
    (which just matches the literal word "confidential") into real detection
    of trade-secret-shaped content — proprietary algorithms, formulas,
    unreleased specs.
  - HR_SENSITIVE_CONTENT: performance-review / disciplinary content, which
    has no regex-detectable shape at all.

Known bug, found by testing against sample_corpus/full_coverage_demo.txt:
langextract 1.6.0's Gemini provider intermittently raises a pydantic
ValidationError building its response_schema ("Extra inputs are not
permitted" on type/properties/required) — looks like a version mismatch
between langextract's schema construction and the installed google-genai
SDK, and it's a race, not deterministic: the exact same call failed once
then succeeded on immediate retry with the same text/model/key. Both
langextract (1.6.0) and google-genai (2.18.1) were already at their latest
release when this was found (checked via `pip index versions`), so there
was no upstream fix to pull in — this is a genuinely open bug in the pinned
dependency, not something this codebase was behind on. First response was
to just degrade safely and document it (a transient failure shouldn't be
silently retried without being asked); once "deep scan cần phải fix" became
an explicit requirement, `run_deep_scan` now retries once
(`_MAX_ATTEMPTS = 2`) before reporting `"skipped_error"` — a bounded,
logged retry rather than an unbounded one, so a real outage still surfaces
instead of retrying forever.
"""

import logging
import os
from typing import Optional

import langextract as lx
from google import genai

from app.schemas import DetectedEntity, EntityLocation

logger = logging.getLogger("sensen.deep_scan")

# "-latest" aliases track whichever model Google currently designates as
# standard, so the default doesn't go stale the way a pinned version does —
# found the hard way: the previously pinned "gemini-2.5-flash-lite" was
# still callable, but Google AI Studio had already moved the visible
# free-tier quota UI on to newer models by the time this was checked
# (2026-08-19). Flash-Lite over plain Flash for the same reason as before:
# more free-tier headroom for a background/opt-in pass like this.
DEFAULT_MODEL_ID = "gemini-flash-lite-latest"

# Coarse allowlist for the ?model= override (see list_available_models() and
# GET /api/v1/deep_scan/models): only plain-text Gemini models, not image/
# audio/tooling variants that exist under the same "gemini-" prefix but
# don't fit langextract's text-in/text-out extraction use case.
_EXCLUDED_MODEL_SUBSTRINGS = (
    "image", "tts", "robotics", "computer-use", "customtools", "omni",
)

# LangExtract doesn't produce a Presidio-style calibrated confidence score;
# this is a fixed placeholder until real precision/recall data justifies
# something more granular.
DEEP_SCAN_SCORE = 0.6

# Retries the known intermittent langextract schema-validation race (see
# module docstring) once before giving up — verified this is a real race,
# not a deterministic failure: the identical call failed once and succeeded
# on immediate retry with the same text/model/key.
_MAX_ATTEMPTS = 2

PROMPT_DESCRIPTION = (
    "Identify sentences or clauses that disclose a company trade secret "
    "(proprietary algorithms, formulas, unreleased product specs, "
    "manufacturing know-how) as IP_TRADE_SECRET_CONTENT, that discuss "
    "sensitive HR matters (performance reviews, disciplinary action, "
    "employee complaints) as HR_SENSITIVE_CONTENT, or that name a "
    "Vietnamese company with its full legal entity type (e.g. 'Công ty "
    "TNHH X', 'Công ty Cổ phần Y', 'Tập đoàn Z') as ORGANIZATION. For "
    "IP_TRADE_SECRET_CONTENT and HR_SENSITIVE_CONTENT, extract the exact "
    "sentence containing the sensitive content, verbatim. For "
    "ORGANIZATION, extract only the company name phrase itself (including "
    "its legal entity type prefix), not any surrounding text."
)

EXAMPLES = [
    lx.data.ExampleData(
        text=(
            "Thuật toán xếp hạng sản phẩm của chúng tôi dùng trọng số 0.6 cho "
            "điểm đánh giá và 0.4 cho số lượt mua — công thức độc quyền chưa "
            "từng công bố ra ngoài."
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="IP_TRADE_SECRET_CONTENT",
                extraction_text=(
                    "Thuật toán xếp hạng sản phẩm của chúng tôi dùng trọng số "
                    "0.6 cho điểm đánh giá và 0.4 cho số lượt mua — công thức "
                    "độc quyền chưa từng công bố ra ngoài."
                ),
            )
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Quy trình pha chế bí mật gồm 3 bước xử lý nhiệt độc quyền, "
            "không được chia sẻ cho đối tác gia công bên thứ ba."
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="IP_TRADE_SECRET_CONTENT",
                extraction_text=(
                    "Quy trình pha chế bí mật gồm 3 bước xử lý nhiệt độc "
                    "quyền, không được chia sẻ cho đối tác gia công bên thứ ba."
                ),
            )
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Nhân viên có biểu hiện thiếu tập trung trong công việc, đã bị "
            "cảnh cáo bằng văn bản do vi phạm nội quy 2 lần trong quý này."
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="HR_SENSITIVE_CONTENT",
                extraction_text=(
                    "Nhân viên có biểu hiện thiếu tập trung trong công việc, "
                    "đã bị cảnh cáo bằng văn bản do vi phạm nội quy 2 lần "
                    "trong quý này."
                ),
            )
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Kết quả đánh giá hiệu suất quý này: nhân viên không đạt chỉ "
            "tiêu doanh số, đề xuất đưa vào diện theo dõi cải thiện (PIP)."
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="HR_SENSITIVE_CONTENT",
                extraction_text=(
                    "Kết quả đánh giá hiệu suất quý này: nhân viên không đạt "
                    "chỉ tiêu doanh số, đề xuất đưa vào diện theo dõi cải "
                    "thiện (PIP)."
                ),
            )
        ],
    ),
    # ORGANIZATION: added because the regex-free default path (underthesea
    # NER in app/vi_ner.py) has a documented, explicitly-not-fixed limit —
    # it often truncates a Vietnamese company name to its last 1-2 words
    # and/or mistypes it as LOCATION, because there's no reliable "end of
    # proper name" delimiter available to a regex/NER approach here (see
    # README Known Limitations for the full investigation). An LLM doesn't
    # have that problem — it can use real semantic understanding of what a
    # company name is, not just capitalization. This only runs when the
    # caller opts into deep_scan=true, so it doesn't change the free/
    # default path's behavior at all — see module docstring above.
    lx.data.ExampleData(
        text=(
            "Đại diện cho Công ty TNHH Thiên Phú và bà Nguyễn Thị Lan Anh, "
            "đại diện phòng Pháp chế."
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="ORGANIZATION",
                extraction_text="Công ty TNHH Thiên Phú",
            )
        ],
    ),
    lx.data.ExampleData(
        text="Bên B ký hợp đồng hợp tác với đại diện Công ty Cổ phần Đầu Tư Toàn Cầu.",
        extractions=[
            lx.data.Extraction(
                extraction_class="ORGANIZATION",
                extraction_text="Công ty Cổ phần Đầu Tư Toàn Cầu",
            )
        ],
    ),
]


def _is_usable_text_model(name: str) -> bool:
    if not name.startswith("gemini-"):
        return False
    if not ("flash" in name or "pro" in name):
        return False
    return not any(bad in name for bad in _EXCLUDED_MODEL_SUBSTRINGS)


def list_available_models() -> tuple[list[str], str]:
    """List Gemini text-extraction-capable model ids callable with the
    server's key, for the /api/v1/deep_scan/models endpoint. Never raises —
    same (result, status) contract as run_deep_scan, reusing the same status
    vocabulary ("ok" / "skipped_no_key" / "skipped_error").

    Live-queried rather than a hardcoded list on purpose: found firsthand
    while building this that pinned model ids go stale within months (see
    DEFAULT_MODEL_ID above) — a static list here would just repeat that.
    """
    api_key = os.getenv("LANGEXTRACT_API_KEY")
    if not api_key:
        return [], "skipped_no_key"

    try:
        client = genai.Client(api_key=api_key)
        names = set()
        for m in client.models.list():
            if "generateContent" not in (m.supported_actions or []):
                continue
            name = m.name.removeprefix("models/")
            if _is_usable_text_model(name):
                names.add(name)
    except Exception:
        logger.warning("deep_scan: model listing failed", exc_info=True)
        return [], "skipped_error"

    return sorted(names), "ok"


def run_deep_scan(text: str, model_id: Optional[str] = None) -> tuple[list[DetectedEntity], str]:
    """Run the LLM extraction pass. Never raises.

    Returns (entities, status) where status is one of "ok" (call succeeded,
    entities may still be empty if nothing was found), "skipped_no_key", or
    "skipped_error" — the caller needs this distinction to report an honest
    deep_scan_status, since an empty entity list alone can't tell "nothing
    found" apart from "the call never happened". `model_id` defaults to
    DEFAULT_MODEL_ID; an explicit override that isn't a recognized plain-text
    Gemini model (see _is_usable_text_model) is rejected as "skipped_error"
    before spending an API call.
    """
    api_key = os.getenv("LANGEXTRACT_API_KEY")
    if not api_key:
        return [], "skipped_no_key"

    model_id = model_id or DEFAULT_MODEL_ID
    if not _is_usable_text_model(model_id):
        logger.warning("deep_scan: rejected unusable model override %r", model_id)
        return [], "skipped_error"

    result = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            result = lx.extract(
                text_or_documents=text,
                prompt_description=PROMPT_DESCRIPTION,
                examples=EXAMPLES,
                model_id=model_id,
                api_key=api_key,
                show_progress=False,
            )
            break
        except Exception:
            if attempt < _MAX_ATTEMPTS:
                logger.warning(
                    "deep_scan: langextract call failed on attempt %d/%d, retrying "
                    "(known intermittent schema-validation race — see module docstring)",
                    attempt, _MAX_ATTEMPTS, exc_info=True,
                )
            else:
                logger.warning(
                    "deep_scan: langextract call failed on final attempt %d/%d",
                    attempt, _MAX_ATTEMPTS, exc_info=True,
                )
                return [], "skipped_error"

    entities = []
    for extraction in result.extractions:
        if extraction.char_interval is None:
            continue  # ungrounded — LLM's text didn't align to an exact span
        start = extraction.char_interval.start_pos
        end = extraction.char_interval.end_pos
        entities.append(
            DetectedEntity(
                entity_type=extraction.extraction_class,
                location=EntityLocation(start=start, end=end),
                text_val=text[start:end],
                score=DEEP_SCAN_SCORE,
                context_snippet=extraction.extraction_text,
            )
        )
    return entities, "ok"
