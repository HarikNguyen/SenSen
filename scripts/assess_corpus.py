"""Batch-scan a corpus and produce a risk-ranked Assessment Report — which of
many documents to look at first, not just what's in one file.

Usage: python scripts/assess_corpus.py [directory]   # defaults to sample_corpus/
Writes ASSESSMENT_REPORT.md and prints a summary to stdout.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine import build_engines
from app.extract import UnsupportedFileType, extract_text

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_SUFFIXES = {".txt", ".pdf", ".docx"}
CONFIDENCE_THRESHOLD = 0.5

# Weighting drives triage order below, not a safety verdict for lower tiers.
SEVERITY = {
    "INFRA_SECRET": ("critical", 3),
    "CRYPTO_PRIVATE_KEY": ("critical", 3),
    "FINANCIAL_CREDENTIAL": ("critical", 3),
    "US_SSN": ("critical", 3),
    "CREDIT_CARD": ("critical", 3),
    "CONTRACT_ID": ("sensitive", 2),
    "INTERNAL_TAX_CODE": ("sensitive", 2),
    "FINANCIAL_METRIC": ("sensitive", 2),
    "EMPLOYEE_ID": ("sensitive", 2),
    "INFRA_NETWORK_MAP": ("sensitive", 2),
    "GPS_LOCATION": ("sensitive", 2),
    "IP_SENSITIVE_MARKER": ("review", 1),
}


def iter_corpus_files(directory: Path):
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def assess_document(path: Path, analyzer) -> dict:
    raw = path.read_bytes()
    try:
        text, file_type, total_pages = extract_text(path.name, raw)
    except UnsupportedFileType as exc:
        return {"file": path.name, "error": str(exc)}

    results = analyzer.analyze(text=text, language="en", score_threshold=CONFIDENCE_THRESHOLD)

    counts: dict[str, int] = {}
    risk_score = 0
    tiers = {"critical": 0, "sensitive": 0, "review": 0}
    for r in results:
        counts[r.entity_type] = counts.get(r.entity_type, 0) + 1
        tier, weight = SEVERITY.get(r.entity_type, (None, 0))
        if tier:
            tiers[tier] += 1
            risk_score += weight

    return {
        "file": path.name,
        "file_type": file_type,
        "total_pages": total_pages,
        "chars_scanned": len(text),
        "entity_counts": counts,
        "total_entities": sum(counts.values()),
        "tiers": tiers,
        "risk_score": risk_score,
    }


def main():
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "sample_corpus"
    if not directory.exists():
        print(f"Directory not found: {directory}")
        sys.exit(1)

    analyzer, _ = build_engines()

    docs = [assess_document(p, analyzer) for p in iter_corpus_files(directory)]
    ok_docs = [d for d in docs if "error" not in d]
    error_docs = [d for d in docs if "error" in d]

    corpus_totals: dict[str, int] = {}
    for d in ok_docs:
        for entity_type, n in d["entity_counts"].items():
            corpus_totals[entity_type] = corpus_totals.get(entity_type, 0) + n

    ranked = sorted(ok_docs, key=lambda d: d["risk_score"], reverse=True)

    lines = [
        "# SenSen Assessment Report",
        "",
        f"Corpus: `{directory.relative_to(REPO_ROOT) if directory.is_relative_to(REPO_ROOT) else directory}` "
        f"— {len(ok_docs)} documents scanned"
        + (f", {len(error_docs)} skipped (unsupported/unreadable)" if error_docs else "")
        + f", confidence_threshold={CONFIDENCE_THRESHOLD}.",
        "",
        "## Summary",
        "",
        f"- **{sum(d['total_entities'] for d in ok_docs)} entities detected** across the corpus",
        f"- **{sum(d['tiers']['critical'] for d in ok_docs)} critical** "
        f"(live secrets/credentials — INFRA_SECRET, SSN, credit card)",
        f"- **{sum(d['tiers']['sensitive'] for d in ok_docs)} sensitive** "
        f"(contract IDs, tax codes, financial figures, employee IDs)",
        f"- **{sum(d['tiers']['review'] for d in ok_docs)} flagged for review** (IP/confidentiality markers)",
        "",
        "## Documents ranked by risk (review in this order)",
        "",
        "| Rank | Document | Type | Risk score | Critical | Sensitive | Review | Total entities |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, d in enumerate(ranked, 1):
        lines.append(
            f"| {i} | `{d['file']}` | {d['file_type']} | {d['risk_score']} | "
            f"{d['tiers']['critical']} | {d['tiers']['sensitive']} | {d['tiers']['review']} | "
            f"{d['total_entities']} |"
        )

    lines += ["", "## Entity type breakdown (whole corpus)", ""]
    for entity_type, n in sorted(corpus_totals.items(), key=lambda kv: -kv[1]):
        tier = SEVERITY.get(entity_type, (None, 0))[0]
        flag = f" _{tier}_" if tier else ""
        lines.append(f"- **{entity_type}**: {n}{flag}")

    if error_docs:
        lines += ["", "## Skipped files", ""]
        for d in error_docs:
            lines.append(f"- `{d['file']}`: {d['error']}")

    lines += [
        "",
        "## Reading this",
        "",
        "- This is a **triage tool, not a compliance verdict**: risk scores rank where a "
        "human should look first, they don't certify a document as safe when absent.",
        "- Same detector, same thresholds as the live API (`/api/v1/scan`) — this script "
        "just runs it over a directory instead of one request at a time.",
        "- Document text is read from disk only for the duration of this script; nothing "
        "here is written back to `saas.db` (same in-RAM-only handling as the API).",
    ]

    report = "\n".join(lines)
    print(report)
    (REPO_ROOT / "ASSESSMENT_REPORT.md").write_text(report + "\n")


if __name__ == "__main__":
    main()
