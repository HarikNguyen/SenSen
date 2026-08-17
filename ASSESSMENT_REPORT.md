# SenSen Assessment Report

Corpus: `sample_corpus` — 7 documents scanned, confidence_threshold=0.5.

## Summary

- **77 entities detected** across the corpus
- **4 critical** (live secrets/credentials — INFRA_SECRET, SSN, credit card)
- **9 sensitive** (contract IDs, tax codes, financial figures, employee IDs)
- **0 flagged for review** (IP/confidentiality markers)

## Documents ranked by risk (review in this order)

| Rank | Document | Type | Risk score | Critical | Sensitive | Review | Total entities |
|---|---|---|---|---|---|---|---|
| 1 | `infra_notes.txt` | text | 9 | 3 | 0 | 0 | 10 |
| 2 | `finance_report.txt` | text | 6 | 0 | 3 | 0 | 8 |
| 3 | `contract_001.txt` | text | 4 | 0 | 2 | 0 | 20 |
| 4 | `employment_addendum.docx` | docx | 4 | 0 | 2 | 0 | 8 |
| 5 | `hr_review_q3.txt` | text | 4 | 0 | 2 | 0 | 9 |
| 6 | `tech_report.pdf` | pdf | 3 | 1 | 0 | 0 | 7 |
| 7 | `meeting_notes.txt` | text | 0 | 0 | 0 | 0 | 15 |

## Entity type breakdown (whole corpus)

- **PERSON**: 29
- **ORGANIZATION**: 17
- **URL**: 7
- **FINANCIAL_METRIC**: 6 _sensitive_
- **EMAIL_ADDRESS**: 5
- **INFRA_SECRET**: 4 _critical_
- **NRP**: 3
- **EMPLOYEE_ID**: 2 _sensitive_
- **LOCATION**: 2
- **CONTRACT_ID**: 1 _sensitive_
- **DATE_TIME**: 1

## Reading this

- This is a **triage tool, not a compliance verdict**: risk scores rank where a human should look first, they don't certify a document as safe when absent.
- Same detector, same thresholds as the live API (`/api/v1/scan`) — this script just runs it over a directory instead of one request at a time.
- Document text is read from disk only for the duration of this script; nothing here is written back to `saas.db` (same in-RAM-only handling as the API).
