"""Benchmark: vanilla Presidio vs. SenSen's custom registry.

Run from the repo root with the venv active:

    python scripts/benchmark.py

Writes results to BENCHMARK.md and also prints them to stdout.
"""

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.recognizer_registry import RecognizerRegistryProvider

from app.engine import RECOGNIZERS_CONF, build_nlp_engine

REPO_ROOT = Path(__file__).resolve().parent.parent

CUSTOM_ENTITY_TYPES = {
    "CONTRACT_ID",
    "INTERNAL_TAX_CODE",
    "FINANCIAL_METRIC",
    "EMPLOYEE_ID",
    "INFRA_SECRET",
    "IP_SENSITIVE_MARKER",
    "CRYPTO_PRIVATE_KEY",
    "INFRA_NETWORK_MAP",
    "GPS_LOCATION",
    "FINANCIAL_CREDENTIAL",
    "VN_NATIONAL_ID",
}

SAMPLE_DOCS = [
    "Hợp đồng số HD-2026-0142 giữa Công ty A và ông Nguyễn Văn B, email nguyenvanb@example.com, SĐT 0912345678.",
    "Mã số thuế doanh nghiệp: 1234567890, đăng ký tại Sở Kế hoạch Đầu tư Hà Nội.",
    "API key nội bộ: sk-live-51Hh8x9AbCdEfGhIjKlMnOpQrStUvWxYz01234567 — không được chia sẻ ra ngoài.",
    "Database connection string: postgresql://admin:S3cretPass@db.internal:5432/prod",
    "Lương thưởng tháng này của nhân viên NV-00231 là 50.000.000 VND, đã bao gồm thưởng KPI.",
    "Bản đánh giá hiệu suất nhân viên quý 3, mã nhân viên EMP-4471, xếp loại xuất sắc.",
    "Tài liệu này CONFIDENTIAL, chứa source code độc quyền và bí quyết kinh doanh của công ty.",
    "Điều khoản bồi thường vi phạm NDA quy định mức phạt tối đa 200.000.000 VND.",
    "Vui lòng liên hệ qua email support@company.com hoặc hotline 0987654321 để được hỗ trợ.",
    "JWT token phiên đăng nhập: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "Server backup key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    "Sơ đồ mạng nội bộ: gateway tại 10.20.5.1/24, subnet backup 192.168.1.0/24, firewall chặn ngoài VPC.",
    "Toạ độ GPS kho hàng: 21.038300, 105.782900 — cần bảo mật.",
    "Mật khẩu ngân hàng của tài khoản công ty là: Xk9pL2, chỉ kế toán trưởng được biết.",
    "Ông Nguyễn Xuân Hùng, số CCCD 051195344431, làm việc tại Thành phố Hồ Chí Minh.",
]
DOCS = SAMPLE_DOCS * 5  # 50 docs total — enough for a stable timing signal


def run(analyzer: AnalyzerEngine, label: str):
    durations = []
    entity_counts: dict[str, int] = {}
    for doc in DOCS:
        t0 = time.perf_counter()
        results = analyzer.analyze(text=doc, language="en", score_threshold=0.3)
        durations.append((time.perf_counter() - t0) * 1000)
        for r in results:
            entity_counts[r.entity_type] = entity_counts.get(r.entity_type, 0) + 1

    custom_hits = sum(v for k, v in entity_counts.items() if k in CUSTOM_ENTITY_TYPES)
    return {
        "label": label,
        "docs": len(DOCS),
        "mean_ms": statistics.mean(durations),
        "p95_ms": sorted(durations)[int(len(durations) * 0.95) - 1],
        "total_entities": sum(entity_counts.values()),
        "custom_category_hits": custom_hits,
        "entity_counts": entity_counts,
    }


def main():
    nlp_engine = build_nlp_engine()  # shared across both engines — isolates the comparison to *recognizers*, not model load

    baseline = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
    registry = RecognizerRegistryProvider(
        conf_file=str(RECOGNIZERS_CONF), nlp_engine=nlp_engine
    ).create_recognizer_registry()
    custom = AnalyzerEngine(
        registry=registry, nlp_engine=nlp_engine, supported_languages=["en"]
    )

    results = [
        run(baseline, "Vanilla Presidio (default recognizers only)"),
        run(custom, "SenSen (default + 11 custom enterprise categories)"),
    ]

    lines = [
        "# SenSen Benchmark",
        "",
        f"Corpus: {len(DOCS)} synthetic enterprise-style documents (mixed VN/EN, "
        f"repeated from {len(SAMPLE_DOCS)} templates). Same shared spaCy "
        f"`en_core_web_sm` NLP engine in both runs — only the recognizer set differs.",
        "",
        "| Engine | Mean ms/doc | p95 ms/doc | Total entities | Custom-category hits (of 11 new types) |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['label']} | {r['mean_ms']:.2f} | {r['p95_ms']:.2f} | "
            f"{r['total_entities']} | {r['custom_category_hits']} |"
        )

    lines += ["", "## Entity type breakdown", ""]
    for r in results:
        lines.append(f"**{r['label']}**")
        for k, v in sorted(r["entity_counts"].items(), key=lambda kv: -kv[1]):
            flag = " 🆕" if k in CUSTOM_ENTITY_TYPES else ""
            lines.append(f"- {k}: {v}{flag}")
        lines.append("")

    lines += [
        "## Reading this",
        "",
        "- Latency difference between the two rows is the **cost of the 11 new "
        "categories** (regex + context scoring) — expected to be small since "
        "regex is C-engine and runs in milliseconds even on modest CPUs. Not "
        "included in this cost: the VN phone/underthesea NER fixes, which "
        "replace/extend *existing* PHONE_NUMBER/PERSON/ORG/LOCATION coverage "
        "rather than adding new categories, so they don't show up as "
        "'custom-category hits' below even though they're part of the same fix.",
        "- \"Custom-category hits\" is 0 for vanilla Presidio by construction: "
        "CONTRACT_ID, INTERNAL_TAX_CODE, FINANCIAL_METRIC, EMPLOYEE_ID, "
        "INFRA_SECRET, IP_SENSITIVE_MARKER, CRYPTO_PRIVATE_KEY, INFRA_NETWORK_MAP, "
        "GPS_LOCATION, FINANCIAL_CREDENTIAL and VN_NATIONAL_ID don't exist in "
        "stock Presidio at all — this row quantifies the coverage gap these "
        "categories close, not a tuning artifact.",
    ]

    report = "\n".join(lines)
    print(report)
    (REPO_ROOT / "BENCHMARK.md").write_text(report + "\n")


if __name__ == "__main__":
    main()
