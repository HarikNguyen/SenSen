"""Positive / negative / ambiguous detection cases + auth flow."""

import io
import uuid

import pymupdf
from docx import Document

from app.schemas import DetectedEntity, EntityLocation


def entity_types(resp):
    return {e["entity_type"] for e in resp.json()["detected_entities"]}


# ---------------------------------------------------------------- auth ----


def test_register_returns_api_key(client):
    resp = client.post("/register", json={"email": f"x-{uuid.uuid4().hex[:8]}@sensen.dev"})
    assert resp.status_code == 200
    assert "api_key" in resp.json()


def test_duplicate_email_rejected(client):
    email = f"dup-{uuid.uuid4().hex[:8]}@sensen.dev"
    assert client.post("/register", json={"email": email}).status_code == 200
    assert client.post("/register", json={"email": email}).status_code == 400


def test_scan_without_key_rejected(client):
    resp = client.post("/api/v1/scan", json={"text": "hello"})
    assert resp.status_code == 422  # missing required header


def test_scan_with_invalid_key_rejected(client):
    resp = client.post(
        "/api/v1/scan", json={"text": "hello"}, headers={"X-API-Key": "not-a-real-key"}
    )
    assert resp.status_code == 401


def test_unsupported_language_rejected(scan):
    resp = scan("hello", language="vi")
    assert resp.status_code == 400


# ------------------------------------------------------------ positive ----


def test_detects_email(scan):
    resp = scan("Reach me at john.doe@example.com please.")
    assert resp.status_code == 200
    assert "EMAIL_ADDRESS" in entity_types(resp)


def test_detects_contract_id_with_context(scan):
    resp = scan("Hợp đồng số HD-2026-0142 đã được ký.", confidence_threshold=0.5)
    assert "CONTRACT_ID" in entity_types(resp)


def test_detects_aws_access_key(scan):
    resp = scan("Leaked credential: AKIAABCDEFGHIJKLMNOP was found in the log file.")
    assert "INFRA_SECRET" in entity_types(resp)


def test_detects_db_connection_string(scan):
    resp = scan("conn_str = postgresql://admin:pass123@db.internal:5432/prod")
    assert "INFRA_SECRET" in entity_types(resp)


def test_detects_financial_metric_with_context(scan):
    resp = scan(
        "Lương thưởng tháng này là 50.000.000 VND cho toàn bộ nhân viên.",
        confidence_threshold=0.5,
    )
    assert "FINANCIAL_METRIC" in entity_types(resp)


def test_detects_ip_confidentiality_marker(scan):
    resp = scan(
        "Tài liệu này CONFIDENTIAL, chứa source code độc quyền của công ty.",
        confidence_threshold=0.3,
    )
    assert "IP_SENSITIVE_MARKER" in entity_types(resp)


def test_anonymize_masks_email(scan):
    resp = scan("Contact test@example.com now", anonymize=True)
    body = resp.json()
    assert "test@example.com" not in body["anonymized_content"]["text"]
    assert "<EMAIL_ADDRESS>" in body["anonymized_content"]["text"]


# ------------------------------------------------------------ negative ----


def test_generic_sentence_has_no_high_confidence_hits(scan):
    # Avoids names/dates/orgs so spaCy's NER has nothing to misfire on.
    resp = scan("The quick brown fox jumps over the lazy dog.", confidence_threshold=0.7)
    assert resp.json()["detected_entities"] == []


def test_short_numbers_not_flagged_as_contract(scan):
    # A random number sequence must not be mistaken for a sensitive code.
    resp = scan("Row values: 42, 17, 93, 8", confidence_threshold=0.7)
    assert "CONTRACT_ID" not in entity_types(resp)


# ----------------------------------------------------------- ambiguous ----


def test_generic_code_shape_filtered_at_default_threshold_without_context(scan):
    # Same shape as a contract id but with no legal context nearby.
    text = "Reference AB-12-XY9Z used for internal tracking only."
    strict = scan(text, confidence_threshold=0.7)
    loose = scan(text, confidence_threshold=0.2)
    assert "CONTRACT_ID" not in entity_types(strict)
    assert "CONTRACT_ID" in entity_types(loose)


def test_mobile_number_not_misread_as_tax_code(scan):
    # VN mobile numbers must not collide with INTERNAL_TAX_CODE (see recognizers.yaml).
    resp = scan("Gọi tôi qua số 0912345678 nhé.", confidence_threshold=0.2)
    assert "INTERNAL_TAX_CODE" not in entity_types(resp)


# --------------------------------------------------------- file upload ----


def test_scan_pdf_file_extracts_and_detects(client, api_key):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Contact john@example.com for details.")
    raw = doc.tobytes()
    doc.close()

    resp = client.post(
        "/api/v1/scan/file",
        files={"file": ("contract.pdf", raw, "application/pdf")},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_metadata"]["file_type"] == "pdf"
    assert "EMAIL_ADDRESS" in entity_types(resp)


def test_scan_docx_file_extracts_and_detects(client, api_key):
    document = Document()
    document.add_paragraph("Hợp đồng số HD-2026-0142 đã được ký kết.")
    buf = io.BytesIO()
    document.save(buf)

    resp = client.post(
        "/api/v1/scan/file",
        files={
            "file": (
                "contract.docx",
                buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"confidence_threshold": "0.5"},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200, resp.text
    assert "CONTRACT_ID" in entity_types(resp)


def test_scan_unsupported_file_type_rejected(client, api_key):
    resp = client.post(
        "/api/v1/scan/file",
        files={"file": ("notes.csv", b"a,b,c", "text/csv")},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 422


def test_corrupt_pdf_rejected_cleanly_not_a_500(client, api_key):
    # Found via adversarial file-upload testing: pymupdf.open() raises its
    # own FileDataError for a malformed PDF, which used to propagate as an
    # unhandled 500 instead of the documented clear 422.
    resp = client.post(
        "/api/v1/scan/file",
        files={"file": ("garbage.pdf", b"this is not a valid pdf at all", "application/pdf")},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 422
    assert "pdf" in resp.json()["detail"].lower()


def test_empty_pdf_rejected_cleanly_not_a_500(client, api_key):
    # Same root cause as the corrupt-PDF case: pymupdf.EmptyFileError is a
    # FileDataError subclass, previously uncaught.
    resp = client.post(
        "/api/v1/scan/file",
        files={"file": ("empty.pdf", b"", "application/pdf")},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 422


def test_corrupt_docx_rejected_cleanly_not_a_500(client, api_key):
    # python-docx's Document() doesn't guarantee one exception type for a
    # malformed file (confirmed empirically: zipfile.BadZipFile for a
    # non-zip, a plain KeyError for a valid zip that isn't a real docx) —
    # previously uncaught either way, an unhandled 500.
    resp = client.post(
        "/api/v1/scan/file",
        files={
            "file": (
                "garbage.docx",
                b"not a real docx zip file",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 422
    assert "docx" in resp.json()["detail"].lower()


def _build_image_only_pdf(num_pages: int = 1) -> bytes:
    # A page with only an inserted image, no text layer -- same shape as a
    # real scanned document, exercises app/extract.py's OCR fallback path.
    src = pymupdf.open()
    src.new_page(width=200, height=100)
    pix = src[0].get_pixmap()
    img_bytes = pix.tobytes("png")
    src.close()

    out = pymupdf.open()
    for _ in range(num_pages):
        page = out.new_page(width=pix.width, height=pix.height)
        page.insert_image(page.rect, stream=img_bytes)
    raw = out.tobytes()
    out.close()
    return raw


def test_scanned_pdf_falls_back_to_ocr(client, api_key, monkeypatch):
    monkeypatch.setattr(
        "app.extract.pytesseract.image_to_string",
        lambda img, lang=None: "Hợp đồng số HD-2026-7777",
    )
    resp = client.post(
        "/api/v1/scan/file",
        files={"file": ("scanned.pdf", _build_image_only_pdf(), "application/pdf")},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_metadata"]["processing_mode"] == "ocr"
    assert "CONTRACT_ID" in entity_types(resp)


def test_scanned_pdf_without_tesseract_installed_gives_clear_422(client, api_key, monkeypatch):
    import pytesseract

    def _raise_not_found(img, lang=None):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr("app.extract.pytesseract.image_to_string", _raise_not_found)
    resp = client.post(
        "/api/v1/scan/file",
        files={"file": ("scanned.pdf", _build_image_only_pdf(), "application/pdf")},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 422
    assert "tesseract" in resp.json()["detail"].lower()


def test_scanned_pdf_over_page_limit_rejected_before_ocr_attempt(client, api_key, monkeypatch):
    from app.extract import MAX_OCR_PAGES

    def _boom(img, lang=None):
        raise AssertionError("OCR must not run once the page-count cap is exceeded")

    monkeypatch.setattr("app.extract.pytesseract.image_to_string", _boom)
    resp = client.post(
        "/api/v1/scan/file",
        files={
            "file": (
                "scanned.pdf",
                _build_image_only_pdf(num_pages=MAX_OCR_PAGES + 1),
                "application/pdf",
            )
        },
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 422
    assert str(MAX_OCR_PAGES) in resp.json()["detail"]


# ---------------------------------------------------- round 2 categories ----


def test_detects_pem_private_key(scan):
    resp = scan(
        "Server config:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n"
        "-----END RSA PRIVATE KEY-----\nDo not share."
    )
    assert "CRYPTO_PRIVATE_KEY" in entity_types(resp)


def test_detects_private_ip_cidr(scan):
    resp = scan(
        "Sơ đồ mạng: gateway nội bộ tại 10.20.5.1/24, subnet backup 192.168.1.0/24.",
        confidence_threshold=0.4,
    )
    assert "INFRA_NETWORK_MAP" in entity_types(resp)


def test_gps_coordinates_need_context_to_clear_default_threshold(scan):
    # Same shape appears in non-location text (e.g. pixel dims) without context.
    no_context = scan("Kích thước ảnh: 21.038300, 105.782900 pixel.", confidence_threshold=0.7)
    with_context = scan(
        "Toạ độ GPS của kho hàng: 21.038300, 105.782900.", confidence_threshold=0.3
    )
    assert "GPS_LOCATION" not in entity_types(no_context)
    assert "GPS_LOCATION" in entity_types(with_context)


def test_financial_credential_with_banking_context_outscores_bare(scan):
    # Longer gap here is deliberate — an earlier 30-char cap missed this phrasing.
    with_ctx = scan(
        "Mật khẩu ngân hàng của tài khoản công ty là: Xk9pL2.", confidence_threshold=0.3
    )
    without_ctx = scan("Mật khẩu wifi quán cafe là: Xk9pL2.", confidence_threshold=0.3)

    ctx_scores = [
        e["score"] for e in with_ctx.json()["detected_entities"] if e["entity_type"] == "FINANCIAL_CREDENTIAL"
    ]
    bare_scores = [
        e["score"]
        for e in without_ctx.json()["detected_entities"]
        if e["entity_type"] == "FINANCIAL_CREDENTIAL"
    ]
    assert ctx_scores, "banking-context password should be detected"
    assert bare_scores, "bare password assignment should still be detected (weakly)"
    assert max(ctx_scores) > max(bare_scores)


# ------------------------------------------------------------- deep scan ----
# app.scanning imports run_deep_scan via `from app.deep_scan import run_deep_scan`,
# so patches must target app.scanning.run_deep_scan (the bound name in that
# module's namespace), not app.deep_scan.run_deep_scan.


def test_deep_scan_off_by_default_never_calls_langextract(client, api_key, monkeypatch):
    def _boom(text, model_id=None):
        raise AssertionError("run_deep_scan must not run when deep_scan is unset")

    monkeypatch.setattr("app.scanning.run_deep_scan", _boom)
    resp = client.post("/api/v1/scan", json={"text": "hello"}, headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["deep_scan_status"] is None


def test_deep_scan_merges_successful_extraction(client, api_key, monkeypatch):
    def _fake(text, model_id=None):
        entity = DetectedEntity(
            entity_type="HR_SENSITIVE_CONTENT",
            location=EntityLocation(start=0, end=5),
            text_val=text[0:5],
            score=0.6,
            context_snippet=text[0:5],
        )
        return [entity], "ok"

    monkeypatch.setattr("app.scanning.run_deep_scan", _fake)
    resp = client.post(
        "/api/v1/scan",
        json={"text": "Nhân viên bị kỷ luật.", "deep_scan": True},
        headers={"X-API-Key": api_key},
    )
    body = resp.json()
    assert body["deep_scan_status"] == "ok"
    assert "HR_SENSITIVE_CONTENT" in entity_types(resp)


def test_deep_scan_organization_examples_are_well_formed():
    # Sanity check on the few-shot data itself: added to fix a documented,
    # explicitly-not-fixed limitation of the free/default path (underthesea
    # NER truncates Vietnamese company names) -- verified for real against
    # the live Gemini API when this was built (not part of this offline
    # check, since that needs a real network call and API key), this just
    # guards against a future edit breaking the example data's shape.
    from app.deep_scan import EXAMPLES

    org_extractions = [
        ext
        for ex in EXAMPLES
        for ext in ex.extractions
        if ext.extraction_class == "ORGANIZATION"
    ]
    assert len(org_extractions) >= 2
    for ext in org_extractions:
        assert ext.extraction_text.startswith("Công ty")


def test_deep_scan_organization_merges_into_response(client, api_key, monkeypatch):
    # Full-company-name extraction only runs when deep_scan=true is
    # explicitly requested -- the free/default regex+NER path is unchanged
    # (still has the documented truncation limitation on its own).
    def _fake(text, model_id=None):
        idx = text.index("Công ty")
        entity = DetectedEntity(
            entity_type="ORGANIZATION",
            location=EntityLocation(start=idx, end=idx + len("Công ty Cổ phần Đầu Tư Toàn Cầu")),
            text_val="Công ty Cổ phần Đầu Tư Toàn Cầu",
            score=0.6,
            context_snippet=text,
        )
        return [entity], "ok"

    monkeypatch.setattr("app.scanning.run_deep_scan", _fake)
    resp = client.post(
        "/api/v1/scan",
        json={
            "text": "đại diện Công ty Cổ phần Đầu Tư Toàn Cầu.",
            "deep_scan": True,
        },
        headers={"X-API-Key": api_key},
    )
    body = resp.json()
    assert body["deep_scan_status"] == "ok"
    entities = {(e["entity_type"], e["text_val"]) for e in body["detected_entities"]}
    assert ("ORGANIZATION", "Công ty Cổ phần Đầu Tư Toàn Cầu") in entities
    # The free regex+NER path finds "Toàn Cầu" as its own (wrong-type,
    # truncated) LOCATION on this exact text -- deep scan's overlapping,
    # fuller ORGANIZATION should replace it, not sit alongside it.
    assert ("LOCATION", "Toàn Cầu") not in entities


def test_deep_scan_overlap_dedup_does_not_drop_unrelated_nested_entities(
    client, api_key, monkeypatch
):
    # Guard against the overlap-based dedup above being too aggressive:
    # HR_SENSITIVE_CONTENT/IP_TRADE_SECRET_CONTENT flag a whole sentence and
    # routinely contain a PHONE_NUMBER or similar mentioned inside it --
    # that's a separate real finding, not a competing interpretation of the
    # same value, and must never be dropped just for being inside the span.
    text = "Nhân viên bị khiển trách do vi phạm, SĐT liên hệ 0912345678."

    def _fake(text, model_id=None):
        entity = DetectedEntity(
            entity_type="HR_SENSITIVE_CONTENT",
            location=EntityLocation(start=0, end=len(text)),
            text_val=text,
            score=0.6,
            context_snippet=text,
        )
        return [entity], "ok"

    monkeypatch.setattr("app.scanning.run_deep_scan", _fake)
    resp = client.post(
        "/api/v1/scan",
        json={"text": text, "deep_scan": True, "confidence_threshold": 0.3},
        headers={"X-API-Key": api_key},
    )
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    assert ("HR_SENSITIVE_CONTENT", text) in entities
    assert ("PHONE_NUMBER", "0912345678") in entities


def test_deep_scan_failure_falls_back_to_regex_results(client, api_key, monkeypatch):
    monkeypatch.setattr("app.scanning.run_deep_scan", lambda text, model_id=None: ([], "skipped_error"))
    resp = client.post(
        "/api/v1/scan",
        json={"text": "Contact test@example.com", "deep_scan": True},
        headers={"X-API-Key": api_key},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["deep_scan_status"] == "skipped_error"
    assert "EMAIL_ADDRESS" in entity_types(resp)


def test_deep_scan_quota_exceeded_after_cap(client, monkeypatch):
    from app.pages import MAX_DEEP_SCAN_PER_KEY

    monkeypatch.setattr("app.scanning.run_deep_scan", lambda text, model_id=None: ([], "ok"))

    email = f"quota-{uuid.uuid4().hex[:8]}@sensen.dev"
    key = client.post("/register", json={"email": email}).json()["api_key"]

    for _ in range(MAX_DEEP_SCAN_PER_KEY):
        resp = client.post(
            "/api/v1/scan", json={"text": "hello", "deep_scan": True}, headers={"X-API-Key": key}
        )
        assert resp.json()["deep_scan_status"] == "ok"

    resp = client.post(
        "/api/v1/scan", json={"text": "hello", "deep_scan": True}, headers={"X-API-Key": key}
    )
    assert resp.json()["deep_scan_status"] == "skipped_quota_exceeded"


def test_run_deep_scan_retries_once_on_langextract_race_then_succeeds(monkeypatch):
    # Unit-level test of app.deep_scan.run_deep_scan itself (not the
    # app.scanning wrapper) -- exercises the retry loop, not the fallback
    # behavior around it. Simulates the real bug found in this codebase:
    # langextract's Gemini provider intermittently raises on schema
    # validation, then succeeds on immediate retry with identical inputs.
    from app import deep_scan

    calls = {"n": 0}

    def flaky_extract(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated langextract schema-validation race")

        class FakeResult:
            extractions = []

        return FakeResult()

    monkeypatch.setenv("LANGEXTRACT_API_KEY", "fake-key")
    monkeypatch.setattr(deep_scan.lx, "extract", flaky_extract)
    entities, status = deep_scan.run_deep_scan("some text")
    assert status == "ok"
    assert calls["n"] == 2


def test_run_deep_scan_gives_up_after_max_attempts(monkeypatch):
    from app import deep_scan

    calls = {"n": 0}

    def always_fails(**kwargs):
        calls["n"] += 1
        raise RuntimeError("simulated permanent langextract failure")

    monkeypatch.setenv("LANGEXTRACT_API_KEY", "fake-key")
    monkeypatch.setattr(deep_scan.lx, "extract", always_fails)
    entities, status = deep_scan.run_deep_scan("some text")
    assert status == "skipped_error"
    assert calls["n"] == deep_scan._MAX_ATTEMPTS


def test_deep_scan_model_override_passed_through(client, api_key, monkeypatch):
    captured = {}

    def _fake(text, model_id=None):
        captured["model_id"] = model_id
        return [], "ok"

    monkeypatch.setattr("app.scanning.run_deep_scan", _fake)
    client.post(
        "/api/v1/scan",
        json={"text": "hello", "deep_scan": True, "model": "gemini-3.7-flash"},
        headers={"X-API-Key": api_key},
    )
    assert captured["model_id"] == "gemini-3.7-flash"


def test_deep_scan_rejects_unusable_model_override(monkeypatch):
    # Validated before any network call — an image/tts/etc. model id under
    # the same "gemini-" prefix is rejected without spending an API call.
    from app.deep_scan import run_deep_scan

    monkeypatch.setenv("LANGEXTRACT_API_KEY", "fake-key-for-validation-test")
    entities, status = run_deep_scan("some text", model_id="gemini-3-pro-image")
    assert status == "skipped_error"
    assert entities == []


def test_deep_scan_models_endpoint_reports_no_key(client, api_key, monkeypatch):
    monkeypatch.delenv("LANGEXTRACT_API_KEY", raising=False)
    resp = client.get("/api/v1/deep_scan/models", headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "skipped_no_key"
    assert body["models"] == []
    assert body["default_model"]


def test_deep_scan_models_endpoint_requires_auth(client):
    resp = client.get("/api/v1/deep_scan/models")
    assert resp.status_code == 422  # missing required X-API-Key header


# ------------------------------------------ real-document findings fix ----
# Found by scanning an actual filled-in contract PDF, not hypothetical.


def test_vn_mobile_number_detected_as_phone(scan):
    resp = scan("Điện thoại: 0932843132, liên hệ ngay.")
    assert "PHONE_NUMBER" in entity_types(resp)


def test_vn_national_id_context_outscores_bare_digits(scan):
    with_ctx = scan("Số CCCD: 051195344431 cấp tại Hà Nội.", confidence_threshold=0.3)
    without_ctx = scan("Mã đơn hàng: 051195344431 đã xử lý.", confidence_threshold=0.3)

    ctx_scores = [
        e["score"] for e in with_ctx.json()["detected_entities"] if e["entity_type"] == "VN_NATIONAL_ID"
    ]
    bare_scores = [
        e["score"]
        for e in without_ctx.json()["detected_entities"]
        if e["entity_type"] == "VN_NATIONAL_ID"
    ]
    assert ctx_scores, "CCCD-shaped number with context should be detected"
    assert not bare_scores or max(ctx_scores) > max(bare_scores)


def test_exact_span_duplicate_across_categories_keeps_only_highest_score(scan):
    # Presidio's built-in PhoneRecognizer (kept at its full default region
    # list on purpose -- see app/scanning.py's _drop_lower_scored_exact_
    # duplicates docstring for why narrowing it was rejected) also matches
    # a VN national ID's digit shape under some other region's numbering
    # plan, always at a lower score than the correct category. The
    # lower-scoring PHONE_NUMBER duplicate on the exact same span should be
    # dropped, not shown alongside the correct VN_NATIONAL_ID hit.
    resp = scan("Số CCCD: 051195344431 cấp tại Hà Nội.", confidence_threshold=0.3)
    entities = resp.json()["detected_entities"]
    same_span = [e for e in entities if e["location"]["start"] == 9 and e["location"]["end"] == 21]
    assert {e["entity_type"] for e in same_span} == {"VN_NATIONAL_ID"}


def test_vietnamese_ner_now_correct_instead_of_noisy(scan):
    # Before: SpacyRecognizer (English NER) tagged this exact sentence shape
    # with PERSON/ORGANIZATION/LOCATION garbage on fragments like "Ông/Bà".
    # After: SpacyRecognizer disabled, underthesea (Vietnamese-aware) added —
    # it correctly finds the real name and the real country, not fragments.
    resp = scan(
        "Một bên là Ông/Bà: Nguyễn Xuân Hùng, Quốc tịch: Việt Nam. Chức vụ: Giám đốc điều hành.",
        confidence_threshold=0.3,
    )
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    assert ("PERSON", "Nguyễn Xuân Hùng") in entities
    assert ("LOCATION", "Việt Nam") in entities
    # Known residual imprecision, not hidden: fragments like "Ông/Bà" or
    # "Chức vụ" must NOT also get tagged — only the real name/country.
    garbage = {(t, v) for t, v in entities if v not in ("Nguyễn Xuân Hùng", "Việt Nam")}
    assert not garbage, f"unexpected NER hits: {garbage}"


def test_vietnamese_ner_score_is_graduated_not_flat(scan):
    # app/vi_ner.py used to hard-filter (keep/reject) then assign every kept
    # result a flat 0.6, silently ignoring confidence_threshold. Real
    # multi-word entities should now score meaningfully high on their own,
    # not just "whatever passed a binary gate".
    resp = scan(
        "Một bên là Ông/Bà: Nguyễn Xuân Hùng, Quốc tịch: Việt Nam.",
        confidence_threshold=0.1,
    )
    scores = {
        e["entity_type"]: e["score"]
        for e in resp.json()["detected_entities"]
        if e["entity_type"] in ("PERSON", "LOCATION")
    }
    assert scores["PERSON"] > 0.6
    assert scores["LOCATION"] > 0.6


def test_vietnamese_ner_bullet_initial_common_word_filtered_at_normal_threshold(scan):
    # "Được"/"Xét" are common verbs capitalized only because they start a
    # bulleted clause, not proper nouns. Real real-document finding (this
    # exact shape came from a labor contract's benefits section). Should
    # score low enough to disappear at a normal operating threshold, via
    # graduated scoring rather than a hard-coded denylist.
    resp = scan(
        "- Được trả lương vào ngày 05 hàng tháng.\n- Xét nâng lương định kỳ 12 tháng/lần.",
        confidence_threshold=0.5,
    )
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    noise = {(t, v) for t, v in entities if t in ("PERSON", "LOCATION", "ORGANIZATION")}
    assert not noise, f"unexpected NER hits: {noise}"


def test_name_split_across_newline_by_underthesea_still_recovered(scan):
    # Real finding from a two-column-layout test document: underthesea's
    # own tokenizer doesn't treat "\n" as a boundary, so it sometimes fuses
    # a real name with the next line's label word into one token (here,
    # "Trần Thị Hoa" + "Số" from the next line's "Số CCCD:"). The fused
    # token then failed exact-substring lookup against the source text
    # (which has a newline, not a space, between them), silently dropping
    # the whole entity -- app/vi_ner.py's _find_token() now falls back to
    # whitespace-flexible matching, and _split_on_newlines() then scores
    # each line's piece on its own merits.
    resp = scan(
        "Nhân viên: Trần Thị Hoa\nSố CCCD: 038196045678\nPhòng ban: Kế toán",
        confidence_threshold=0.3,
    )
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    assert ("PERSON", "Trần Thị Hoa") in entities


def test_bare_newline_before_single_word_still_penalized_as_sentence_initial(scan):
    # Regression guard for a bug found while fixing the case above:
    # _is_sentence_initial used to check text[:start].rstrip()[-1], which
    # silently strips away the newline itself before checking it, so a
    # bare line break (no punctuation before it) was never recognized as a
    # boundary -- only a newline preceded by "." or "-" was. A single
    # capitalized English word straight after a bare newline (e.g. a
    # section's "Internal API key:" label) must still be penalized enough
    # to disappear at a normal operating threshold.
    resp = scan(
        "Server config:\nInternal API key: sk-live-51Hh8x9AbCdEfGhIjKlMnOpQrStUvWxYz01234567",
        confidence_threshold=0.5,
    )
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    noise = {(t, v) for t, v in entities if t in ("PERSON", "LOCATION", "ORGANIZATION")}
    assert not noise, f"unexpected NER hits: {noise}"


def test_fused_word_no_longer_falsely_tagged(scan):
    # Some PDF exports drop the space glyph between certain word pairs
    # (verified via raw pymupdf word-box inspection on the real contract
    # that surfaced this — genuinely absent from the source file, not an
    # extraction bug). "Chếđộlàm" used to look like one Title Case word to
    # the NER filter and get tagged PERSON/LOCATION; the DP syllable
    # segmentation in app/vi_ner.py now splits it before tagging.
    resp = scan("Điều 2: Chếđộlàm việc theo quy định công ty.", confidence_threshold=0.3)
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    noise = {(t, v) for t, v in entities if t in ("PERSON", "LOCATION", "ORGANIZATION")}
    assert not noise, f"unexpected NER hits on a fused-word run: {noise}"


def test_fused_name_recovered_via_syllable_segmentation(scan):
    # The same fusion artifact glued a real party's name into "SỹThành" in
    # the source contract — previously invisible to NER entirely. The
    # syllable-segmentation repair recovers the full name.
    resp = scan(
        "Và một bên là Ông/Bà: Trịnh SỹThành Quốc tịch: Đài Loan.",
        confidence_threshold=0.3,
    )
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    assert ("PERSON", "Trịnh SỹThành") in entities


def test_vietnamese_ner_no_hallucination_on_fragment_only_text(scan):
    # No real person/org/location name anywhere in this sentence — any NER
    # hit here would be a genuine false positive, not a residual imprecision.
    resp = scan(
        "Mọi thắc mắc vui lòng liên hệ trong giờ hành chính để được hỗ trợ.",
        confidence_threshold=0.3,
    )
    assert not (entity_types(resp) & {"PERSON", "ORGANIZATION", "LOCATION"})


def test_vietnamese_day_names_rejected_not_tagged_location(scan):
    # Real finding from a labor contract's benefits section: "Thứ Bảy",
    # "Chủ Nhật" (Saturday, Sunday) were getting tagged LOCATION. Day names
    # are a closed set of 7 and never PII -- rejected outright, not merely
    # down-scored.
    resp = scan(
        "Nghỉ hàng tuần 02 ngày (Thứ Bảy, Chủ Nhật); nghỉ phép năm 12 ngày.",
        confidence_threshold=0.1,
    )
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    noise = {(t, v) for t, v in entities if t in ("PERSON", "LOCATION", "ORGANIZATION")}
    assert not noise, f"day names should never be tagged as entities: {noise}"


def test_admin_unit_prefix_corrects_type_to_location(scan):
    # Real finding: underthesea gets the span boundary right ("Phường Thủ
    # Thiêm") but the type wrong (tagged PERSON). "Phường" is a reliable
    # administrative-unit signal -- corrected to LOCATION without touching
    # the span itself.
    resp = scan(
        "Địa chỉ: 93A/51/223 Ngô Gia Tự, Phường Thủ Thiêm, TP. Hồ Chí Minh.",
        confidence_threshold=0.3,
    )
    entities = {(e["entity_type"], e["text_val"]) for e in resp.json()["detected_entities"]}
    assert ("LOCATION", "Phường Thủ Thiêm") in entities
    assert ("PERSON", "Phường Thủ Thiêm") not in entities


def test_tax_code_context_outscores_bare_digits(scan):
    with_ctx = scan("Mã số thuế doanh nghiệp: 1234567890", confidence_threshold=0.2)
    without_ctx = scan("Random id: 1234567890", confidence_threshold=0.2)

    ctx_scores = [
        e["score"] for e in with_ctx.json()["detected_entities"] if e["entity_type"] == "INTERNAL_TAX_CODE"
    ]
    bare_scores = [
        e["score"]
        for e in without_ctx.json()["detected_entities"]
        if e["entity_type"] == "INTERNAL_TAX_CODE"
    ]
    assert ctx_scores, "context-boosted tax code should be detected at threshold 0.2"
    assert not bare_scores or max(ctx_scores) > max(bare_scores)
