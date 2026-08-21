"""Optional LLM verification pass -- opt-in (app/scanning.py's deep_scan
flag), every call spends real Gemini quota. Covers every recognizer
category, not just the two content-flag ones regex can't reach at all
(see PROMPT_DESCRIPTION/EXAMPLES); an overlapping finding replaces the
regex/NER one (app/scanning.py's `_DEEP_SCAN_OVERLAP_TYPES`).

Patches `GeminiLanguageModel._process_single_prompt` so `gemini_limiter`
throttles every real per-chunk HTTP call, not just the outer
`lx.extract()` -- langextract fires chunks at Gemini concurrently, which
otherwise bypasses the limiter.
"""

import logging
import os
import time
from typing import Optional

import langextract as lx
from google import genai
from langextract.providers.gemini import GeminiLanguageModel

from app.rate_limiter import gemini_limiter
from app.retry import BACKOFF_SECONDS, is_transient_error
from app.schemas import DetectedEntity, EntityLocation

logger = logging.getLogger("sensen.deep_scan")

# lx.extract() fires chunks concurrently; throttle the real per-chunk call, not just the outer one.
_original_process_single_prompt = GeminiLanguageModel._process_single_prompt


def _throttled_process_single_prompt(self, prompt, config):
    gemini_limiter.acquire()
    return _original_process_single_prompt(self, prompt, config)


GeminiLanguageModel._process_single_prompt = _throttled_process_single_prompt

# "-latest" alias so the default doesn't go stale as Google rotates models.
DEFAULT_MODEL_ID = "gemini-flash-lite-latest"

# Excludes non-text Gemini variants (image/tts/etc.) sharing the "gemini-" prefix.
_EXCLUDED_MODEL_SUBSTRINGS = (
    "image", "tts", "robotics", "computer-use", "customtools", "omni",
)

# langextract has no calibrated confidence score; fixed placeholder.
DEEP_SCAN_SCORE = 0.6

# One shot for the schema-validation race, one for a backed-off retry, one spare.
_MAX_ATTEMPTS = 3

PROMPT_DESCRIPTION = (
    "Identify every sensitive item in the text and label it with the exact "
    "extraction_class listed below. For the two content-flag classes, "
    "extract the whole sentence/clause verbatim, since they flag a topic, "
    "not a value. For every other class, extract only the value itself, "
    "verbatim, the shortest exact substring that is the value (no "
    "surrounding words). Extract every instance found, even several of the "
    "same class in one text. Never invent a value that is not verbatim in "
    "the text; skip a class entirely if the text doesn't contain one.\n\n"
    "Content-flag classes (extract the whole sentence):\n"
    "- IP_TRADE_SECRET_CONTENT: discloses a company trade secret "
    "(proprietary algorithms, formulas, unreleased product specs, "
    "manufacturing know-how).\n"
    "- HR_SENSITIVE_CONTENT: sensitive HR matters (performance reviews, "
    "disciplinary action, employee complaints).\n\n"
    "Value classes (extract only the value):\n"
    "- ORGANIZATION: a Vietnamese company's full legal name, including its "
    "legal entity type prefix (e.g. 'Công ty TNHH X', 'Công ty Cổ phần Y', "
    "'Tập đoàn Z').\n"
    "- PERSON: a real person's full name.\n"
    "- LOCATION: a real place name (city, province, district, ward, "
    "country, landmark).\n"
    "- EMAIL_ADDRESS, PHONE_NUMBER, URL, IP_ADDRESS, CREDIT_CARD, "
    "IBAN_CODE, CRYPTO (a cryptocurrency wallet address), MAC_ADDRESS, "
    "US_SSN: standard identifiers/contact info, in their usual written "
    "form.\n"
    "- CONTRACT_ID: a contract/agreement reference number or code.\n"
    "- INTERNAL_TAX_CODE: a Vietnamese business tax code (mã số thuế).\n"
    "- FINANCIAL_METRIC: a specific monetary amount (salary, revenue, "
    "budget, penalty, a VND/USD figure).\n"
    "- EMPLOYEE_ID: an employee code (e.g. 'NV-004521').\n"
    "- INFRA_SECRET: an API key, access key, token, password, JWT, or "
    "database connection string.\n"
    "- IP_SENSITIVE_MARKER: a confidentiality marker phrase "
    "('confidential', 'bí mật kinh doanh', 'tài liệu mật', 'internal use "
    "only').\n"
    "- CRYPTO_PRIVATE_KEY: a PEM private key block header/body.\n"
    "- INFRA_NETWORK_MAP: an internal IP address, subnet, or CIDR block.\n"
    "- GPS_LOCATION: a decimal latitude/longitude coordinate pair.\n"
    "- FINANCIAL_CREDENTIAL: an assigned banking PIN, OTP, or password.\n"
    "- VN_NATIONAL_ID: a Vietnamese CCCD/CMND national ID number.\n"
    "- BANK_ACCOUNT_NUMBER: a bank account number.\n"
    "- FULL_ADDRESS: a full Vietnamese street address (house number "
    "through ward/district/city/province)."
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
    # ORGANIZATION: underthesea's NER often truncates/mistypes VN company names.
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
    # PERSON/LOCATION: same underthesea weakness as ORGANIZATION above.
    lx.data.ExampleData(
        text=(
            "Ông Trần Văn Bình đã ký hợp đồng tại Đà Nẵng trước sự chứng "
            "kiến của bà Lê Thị Hoa."
        ),
        extractions=[
            lx.data.Extraction(extraction_class="PERSON", extraction_text="Trần Văn Bình"),
            lx.data.Extraction(extraction_class="LOCATION", extraction_text="Đà Nẵng"),
            lx.data.Extraction(extraction_class="PERSON", extraction_text="Lê Thị Hoa"),
        ],
    ),
    # Remaining examples cover every other recognizer value class, grouped
    # into realistic multi-entity snippets rather than one per class.
    lx.data.ExampleData(
        text=(
            "Nguyễn Văn A, số CCCD 079203001234, địa chỉ Số 12, đường Lê "
            "Lợi, phường Bến Nghé, quận 1, Thành phố Hồ Chí Minh, điện "
            "thoại 0912345678, email nguyenvana@example.com, số tài khoản "
            "0071000123456 tại Vietcombank."
        ),
        extractions=[
            lx.data.Extraction(extraction_class="PERSON", extraction_text="Nguyễn Văn A"),
            lx.data.Extraction(extraction_class="VN_NATIONAL_ID", extraction_text="079203001234"),
            lx.data.Extraction(
                extraction_class="FULL_ADDRESS",
                extraction_text=(
                    "Số 12, đường Lê Lợi, phường Bến Nghé, quận 1, Thành "
                    "phố Hồ Chí Minh"
                ),
            ),
            lx.data.Extraction(extraction_class="PHONE_NUMBER", extraction_text="0912345678"),
            lx.data.Extraction(
                extraction_class="EMAIL_ADDRESS", extraction_text="nguyenvana@example.com"
            ),
            lx.data.Extraction(
                extraction_class="BANK_ACCOUNT_NUMBER", extraction_text="0071000123456"
            ),
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Hợp đồng số HD-2026-0142 giữa Công ty TNHH ABC (MST "
            "0312345678) quy định mức phạt vi phạm là 50.000.000 VND."
        ),
        extractions=[
            lx.data.Extraction(extraction_class="CONTRACT_ID", extraction_text="HD-2026-0142"),
            lx.data.Extraction(extraction_class="ORGANIZATION", extraction_text="Công ty TNHH ABC"),
            lx.data.Extraction(extraction_class="INTERNAL_TAX_CODE", extraction_text="0312345678"),
            lx.data.Extraction(
                extraction_class="FINANCIAL_METRIC", extraction_text="50.000.000 VND"
            ),
        ],
    ),
    lx.data.ExampleData(
        text="Nhân viên mã số NV-004521 đã hoàn tất thủ tục chấm công tháng này.",
        extractions=[
            lx.data.Extraction(extraction_class="EMPLOYEE_ID", extraction_text="NV-004521"),
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Server nội bộ tại 10.0.5.12/24 dùng khóa "
            "AKIA1234567890ABCDEF để truy cập, và khối khóa riêng bắt đầu "
            "bằng -----BEGIN RSA PRIVATE KEY-----."
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="INFRA_NETWORK_MAP", extraction_text="10.0.5.12/24"
            ),
            lx.data.Extraction(
                extraction_class="INFRA_SECRET", extraction_text="AKIA1234567890ABCDEF"
            ),
            lx.data.Extraction(
                extraction_class="CRYPTO_PRIVATE_KEY",
                extraction_text="-----BEGIN RSA PRIVATE KEY-----",
            ),
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Toạ độ kho hàng là 10.762622, 106.660172; mật khẩu ứng dụng "
            "ngân hàng là: A1b2C3d4."
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="GPS_LOCATION", extraction_text="10.762622, 106.660172"
            ),
            lx.data.Extraction(
                extraction_class="FINANCIAL_CREDENTIAL", extraction_text="A1b2C3d4"
            ),
        ],
    ),
    lx.data.ExampleData(
        text=(
            "Card 4111 1111 1111 1111 was charged; wire to IBAN GB29 NWBK "
            "6016 1331 9268 19; wallet address "
            "9xQFvVQyq4jVQmXHXY9Bz2WvHc5UGZpN3T; device MAC "
            "00:1A:2B:3C:4D:5E; SSN 123-45-6789; see "
            "https://example.com/portal from IP 203.0.113.7."
        ),
        extractions=[
            lx.data.Extraction(
                extraction_class="CREDIT_CARD", extraction_text="4111 1111 1111 1111"
            ),
            lx.data.Extraction(
                extraction_class="IBAN_CODE", extraction_text="GB29 NWBK 6016 1331 9268 19"
            ),
            lx.data.Extraction(
                extraction_class="CRYPTO", extraction_text="9xQFvVQyq4jVQmXHXY9Bz2WvHc5UGZpN3T"
            ),
            lx.data.Extraction(
                extraction_class="MAC_ADDRESS", extraction_text="00:1A:2B:3C:4D:5E"
            ),
            lx.data.Extraction(extraction_class="US_SSN", extraction_text="123-45-6789"),
            lx.data.Extraction(
                extraction_class="URL", extraction_text="https://example.com/portal"
            ),
            lx.data.Extraction(extraction_class="IP_ADDRESS", extraction_text="203.0.113.7"),
        ],
    ),
    lx.data.ExampleData(
        text="Tài liệu này được đánh dấu là bí mật kinh doanh, không được sao chép.",
        extractions=[
            lx.data.Extraction(
                extraction_class="IP_SENSITIVE_MARKER", extraction_text="bí mật kinh doanh"
            ),
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
    """List Gemini text-extraction models available to the server's key.
    Never raises -- same (result, status) contract as run_deep_scan.
    Live-queried since pinned model ids go stale within months.
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

    Returns (entities, status); status is "ok", "skipped_no_key", or
    "skipped_error" -- an empty entity list alone can't distinguish
    "nothing found" from "the call never happened".
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
            gemini_limiter.acquire()
            result = lx.extract(
                text_or_documents=text,
                prompt_description=PROMPT_DESCRIPTION,
                examples=EXAMPLES,
                model_id=model_id,
                api_key=api_key,
                show_progress=False,
            )
            break
        except Exception as exc:
            if attempt == _MAX_ATTEMPTS:
                logger.warning(
                    "deep_scan: langextract call failed on final attempt %d/%d",
                    attempt, _MAX_ATTEMPTS, exc_info=True,
                )
                return [], "skipped_error"

            # unwrap langextract's InferenceRuntimeError to check the real provider error
            underlying = exc
            if isinstance(exc, lx.exceptions.InferenceRuntimeError) and exc.original is not None:
                underlying = exc.original

            if is_transient_error(underlying):
                delay = BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    "deep_scan: rate-limited/overloaded on attempt %d/%d "
                    "(langextract's own internal retry already tried a shorter "
                    "backoff and still failed) -- backing off %ds before retry",
                    attempt, _MAX_ATTEMPTS, delay, exc_info=True,
                )
                time.sleep(delay)
            else:
                logger.warning(
                    "deep_scan: langextract call failed on attempt %d/%d, retrying "
                    "immediately (known intermittent schema-validation race — see "
                    "module docstring)",
                    attempt, _MAX_ATTEMPTS, exc_info=True,
                )

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
