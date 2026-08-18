# SenSen Benchmark

Corpus: 70 synthetic enterprise-style documents (mixed VN/EN, repeated from 14 templates). Same shared spaCy `en_core_web_sm` NLP engine in both runs — only the recognizer set differs.

| Engine | Mean ms/doc | p95 ms/doc | Total entities | Custom-category hits (of 10 new types) |
|---|---|---|---|---|
| Vanilla Presidio (default recognizers only) | 24.84 | 26.88 | 165 | 0 |
| SenSen (default + 10 custom enterprise categories) | 18.34 | 26.36 | 245 | 80 |

## Entity type breakdown

**Vanilla Presidio (default recognizers only)**
- PERSON: 55
- ORGANIZATION: 40
- URL: 20
- PHONE_NUMBER: 20
- EMAIL_ADDRESS: 10
- IP_ADDRESS: 10
- DATE_TIME: 5
- NRP: 5

**SenSen (default + 10 custom enterprise categories)**
- PERSON: 55
- ORGANIZATION: 40
- URL: 20
- PHONE_NUMBER: 20
- INFRA_SECRET: 15 🆕
- FINANCIAL_METRIC: 15 🆕
- EMAIL_ADDRESS: 10
- EMPLOYEE_ID: 10 🆕
- INFRA_NETWORK_MAP: 10 🆕
- IP_ADDRESS: 10
- CONTRACT_ID: 5 🆕
- DATE_TIME: 5
- INTERNAL_TAX_CODE: 5 🆕
- IP_SENSITIVE_MARKER: 5 🆕
- NRP: 5
- CRYPTO_PRIVATE_KEY: 5 🆕
- GPS_LOCATION: 5 🆕
- FINANCIAL_CREDENTIAL: 5 🆕

## Reading this

- Latency difference between the two rows is the **cost of the 5 new categories** (regex + context scoring) — expected to be small since regex is C-engine and runs in milliseconds even on modest CPUs (see thongtin.md's own i3 analysis).
- "Custom-category hits" is 0 for vanilla Presidio by construction: CONTRACT_ID, INTERNAL_TAX_CODE, FINANCIAL_METRIC, EMPLOYEE_ID, INFRA_SECRET, IP_SENSITIVE_MARKER, CRYPTO_PRIVATE_KEY, INFRA_NETWORK_MAP, GPS_LOCATION and FINANCIAL_CREDENTIAL don't exist in stock Presidio at all — this row is the quantified version of the "gap" thongtin.md opens with, not a tuning artifact.
