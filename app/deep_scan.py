"""Optional LLM-based second pass for categories regex can't reach.

The only module that imports langextract, mirroring how app/engine.py is the
only one that imports presidio_analyzer. Never called automatically — only
when a caller explicitly opts in (see app/scanning.py's deep_scan flag),
because every call costs a Gemini free-tier request (~15 RPM / ~1-1.5k RPD,
no card required, but easy to exhaust under real traffic).

Pilot categories (extend by adding more ExampleData entries below, no other
code changes needed — same story as app/recognizers/recognizers.yaml):
  - IP_TRADE_SECRET_CONTENT: upgrades the regex-only IP_SENSITIVE_MARKER
    (which just matches the literal word "confidential") into real detection
    of trade-secret-shaped content — proprietary algorithms, formulas,
    unreleased specs.
  - HR_SENSITIVE_CONTENT: performance-review / disciplinary content, which
    has no regex-detectable shape at all.
"""

import logging
import os

import langextract as lx

from app.schemas import DetectedEntity, EntityLocation

logger = logging.getLogger("sensen.deep_scan")

# The library's own default (lx.extract's model_id default) is plain
# "gemini-3.5-flash", which is NOT the free-tier-friendly choice — Flash-Lite
# has materially more headroom. Verify this exact string against Google AI
# Studio's current model list at setup time; model ids shift between releases.
MODEL_ID = "gemini-2.5-flash-lite"

# LangExtract doesn't produce a Presidio-style calibrated confidence score;
# this is a fixed placeholder until real precision/recall data justifies
# something more granular.
DEEP_SCAN_SCORE = 0.6

PROMPT_DESCRIPTION = (
    "Identify sentences or clauses that disclose a company trade secret "
    "(proprietary algorithms, formulas, unreleased product specs, "
    "manufacturing know-how) as IP_TRADE_SECRET_CONTENT, or that discuss "
    "sensitive HR matters (performance reviews, disciplinary action, "
    "employee complaints) as HR_SENSITIVE_CONTENT. Extract the exact "
    "sentence containing the sensitive content, verbatim."
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
]


def run_deep_scan(text: str) -> tuple[list[DetectedEntity], str]:
    """Run the LLM extraction pass. Never raises.

    Returns (entities, status) where status is one of "ok" (call succeeded,
    entities may still be empty if nothing was found), "skipped_no_key", or
    "skipped_error" — the caller needs this distinction to report an honest
    deep_scan_status, since an empty entity list alone can't tell "nothing
    found" apart from "the call never happened".
    """
    api_key = os.getenv("LANGEXTRACT_API_KEY")
    if not api_key:
        return [], "skipped_no_key"

    try:
        result = lx.extract(
            text_or_documents=text,
            prompt_description=PROMPT_DESCRIPTION,
            examples=EXAMPLES,
            model_id=MODEL_ID,
            api_key=api_key,
            show_progress=False,
        )
    except Exception:
        logger.warning("deep_scan: langextract call failed", exc_info=True)
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
