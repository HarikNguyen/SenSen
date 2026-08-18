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
    def _boom(text):
        raise AssertionError("run_deep_scan must not run when deep_scan is unset")

    monkeypatch.setattr("app.scanning.run_deep_scan", _boom)
    resp = client.post("/api/v1/scan", json={"text": "hello"}, headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    assert resp.json()["deep_scan_status"] is None


def test_deep_scan_merges_successful_extraction(client, api_key, monkeypatch):
    def _fake(text):
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


def test_deep_scan_failure_falls_back_to_regex_results(client, api_key, monkeypatch):
    monkeypatch.setattr("app.scanning.run_deep_scan", lambda text: ([], "skipped_error"))
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

    monkeypatch.setattr("app.scanning.run_deep_scan", lambda text: ([], "ok"))

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
