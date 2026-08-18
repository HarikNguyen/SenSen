# SenSen Assessment Report

Corpus: `sample_corpus` — 8 documents scanned, confidence_threshold=0.5.

## Summary

- **89 entities detected** across the corpus
- **6 critical** (live secrets/credentials — INFRA_SECRET, SSN, credit card)
- **13 sensitive** (contract IDs, tax codes, financial figures, employee IDs)
- **0 flagged for review** (IP/confidentiality markers)

## Documents ranked by risk (review in this order)

| Rank | Document | Type | Risk score | Critical | Sensitive | Review | Total entities |
|---|---|---|---|---|---|---|---|
| 1 | `devops_handover.txt` | text | 12 | 2 | 3 | 0 | 11 |
| 2 | `infra_notes.txt` | text | 9 | 3 | 0 | 0 | 10 |
| 3 | `finance_report.txt` | text | 8 | 0 | 4 | 0 | 9 |
| 4 | `contract_001.txt` | text | 4 | 0 | 2 | 0 | 20 |
| 5 | `employment_addendum.docx` | docx | 4 | 0 | 2 | 0 | 8 |
| 6 | `hr_review_q3.txt` | text | 4 | 0 | 2 | 0 | 9 |
| 7 | `tech_report.pdf` | pdf | 3 | 1 | 0 | 0 | 7 |
| 8 | `meeting_notes.txt` | text | 0 | 0 | 0 | 0 | 15 |

## Entity type breakdown (whole corpus)

- **PERSON**: 31
- **ORGANIZATION**: 19
- **FINANCIAL_METRIC**: 7 _sensitive_
- **URL**: 7
- **EMAIL_ADDRESS**: 5
- **INFRA_SECRET**: 4 _critical_
- **NRP**: 3
- **INFRA_NETWORK_MAP**: 2 _sensitive_
- **IP_ADDRESS**: 2
- **EMPLOYEE_ID**: 2 _sensitive_
- **LOCATION**: 2
- **CONTRACT_ID**: 1 _sensitive_
- **CRYPTO_PRIVATE_KEY**: 1 _critical_
- **FINANCIAL_CREDENTIAL**: 1 _critical_
- **GPS_LOCATION**: 1 _sensitive_
- **DATE_TIME**: 1

## Reading this

- This is a **triage tool, not a compliance verdict**: risk scores rank where a human should look first, they don't certify a document as safe when absent.
- Same detector, same thresholds as the live API (`/api/v1/scan`) — this script just runs it over a directory instead of one request at a time.
- Document text is read from disk only for the duration of this script; nothing here is written back to `saas.db` (same in-RAM-only handling as the API).
