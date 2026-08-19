# SenSen Assessment Report

Corpus: `sample_corpus` — 9 documents scanned, confidence_threshold=0.5.

## Summary

- **76 entities detected** across the corpus
- **14 critical** (live secrets/credentials — INFRA_SECRET, SSN, credit card)
- **20 sensitive** (contract IDs, tax codes, financial figures, employee IDs)
- **0 flagged for review** (IP/confidentiality markers)

## Documents ranked by risk (review in this order)

| Rank | Document | Type | Risk score | Critical | Sensitive | Review | Total entities |
|---|---|---|---|---|---|---|---|
| 1 | `full_coverage_demo.txt` | text | 38 | 8 | 7 | 0 | 37 |
| 2 | `devops_handover.txt` | text | 12 | 2 | 3 | 0 | 8 |
| 3 | `infra_notes.txt` | text | 9 | 3 | 0 | 0 | 4 |
| 4 | `finance_report.txt` | text | 8 | 0 | 4 | 0 | 5 |
| 5 | `contract_001.txt` | text | 4 | 0 | 2 | 0 | 10 |
| 6 | `employment_addendum.docx` | docx | 4 | 0 | 2 | 0 | 4 |
| 7 | `hr_review_q3.txt` | text | 4 | 0 | 2 | 0 | 4 |
| 8 | `tech_report.pdf` | pdf | 3 | 1 | 0 | 0 | 4 |
| 9 | `meeting_notes.txt` | text | 0 | 0 | 0 | 0 | 0 |

## Entity type breakdown (whole corpus)

- **URL**: 10
- **FINANCIAL_METRIC**: 8 _sensitive_
- **PERSON**: 8
- **INFRA_SECRET**: 7 _critical_
- **EMAIL_ADDRESS**: 6
- **LOCATION**: 6
- **IP_ADDRESS**: 5
- **PHONE_NUMBER**: 4
- **INFRA_NETWORK_MAP**: 4 _sensitive_
- **EMPLOYEE_ID**: 3 _sensitive_
- **CONTRACT_ID**: 2 _sensitive_
- **CRYPTO_PRIVATE_KEY**: 2 _critical_
- **FINANCIAL_CREDENTIAL**: 2 _critical_
- **GPS_LOCATION**: 2 _sensitive_
- **CREDIT_CARD**: 1 _critical_
- **IBAN_CODE**: 1
- **CRYPTO**: 1
- **MAC_ADDRESS**: 1
- **US_SSN**: 1 _critical_
- **VN_NATIONAL_ID**: 1 _critical_
- **INTERNAL_TAX_CODE**: 1 _sensitive_

## Reading this

- This is a **triage tool, not a compliance verdict**: risk scores rank where a human should look first, they don't certify a document as safe when absent.
- Same detector, same thresholds as the live API (`/api/v1/scan`) — this script just runs it over a directory instead of one request at a time.
- Document text is read from disk only for the duration of this script; nothing here is written back to `saas.db` (same in-RAM-only handling as the API).
