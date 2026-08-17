# SenSen Benchmark

Corpus: 50 synthetic enterprise-style documents (mixed VN/EN, repeated from 10 templates). Same shared spaCy `en_core_web_sm` NLP engine in both runs — only the recognizer set differs.

| Engine | Mean ms/doc | p95 ms/doc | Total entities | Custom-category hits (of 6 new types) |
|---|---|---|---|---|
| Vanilla Presidio (default recognizers only) | 26.91 | 25.28 | 125 | 0 |
| SenSen (default + 5 custom enterprise categories) | 17.71 | 22.20 | 175 | 50 |

## Entity type breakdown

**Vanilla Presidio (default recognizers only)**
- ORGANIZATION: 35
- PERSON: 35
- URL: 20
- PHONE_NUMBER: 15
- EMAIL_ADDRESS: 10
- DATE_TIME: 5
- NRP: 5

**SenSen (default + 5 custom enterprise categories)**
- ORGANIZATION: 35
- PERSON: 35
- URL: 20
- PHONE_NUMBER: 15
- INFRA_SECRET: 15 🆕
- EMAIL_ADDRESS: 10
- EMPLOYEE_ID: 10 🆕
- FINANCIAL_METRIC: 10 🆕
- CONTRACT_ID: 5 🆕
- DATE_TIME: 5
- INTERNAL_TAX_CODE: 5 🆕
- IP_SENSITIVE_MARKER: 5 🆕
- NRP: 5

## Reading this

- Latency difference between the two rows is the **cost of the 5 new categories** (regex + context scoring) — expected to be small since regex is C-engine and runs in milliseconds even on modest CPUs (see thongtin.md's own i3 analysis).
- "Custom-category hits" is 0 for vanilla Presidio by construction: CONTRACT_ID, INTERNAL_TAX_CODE, FINANCIAL_METRIC, EMPLOYEE_ID, INFRA_SECRET and IP_SENSITIVE_MARKER don't exist in stock Presidio at all — this row is the quantified version of the "gap" thongtin.md opens with, not a tuning artifact.
