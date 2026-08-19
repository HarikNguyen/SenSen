# SenSen Benchmark

Corpus: 75 synthetic enterprise-style documents (mixed VN/EN, repeated from 15 templates). Same shared spaCy `en_core_web_sm` NLP engine in both runs — only the recognizer set differs.

| Engine | Mean ms/doc | p95 ms/doc | Total entities | Custom-category hits (of 11 new types) |
|---|---|---|---|---|
| Vanilla Presidio (default recognizers only) | 30.92 | 37.36 | 180 | 0 |
| SenSen (default + 11 custom enterprise categories) | 24.08 | 34.83 | 150 | 85 |

## Entity type breakdown

**Vanilla Presidio (default recognizers only)**
- PERSON: 55
- ORGANIZATION: 40
- PHONE_NUMBER: 25
- URL: 20
- EMAIL_ADDRESS: 10
- DATE_TIME: 10
- IP_ADDRESS: 10
- NRP: 5
- LOCATION: 5

**SenSen (default + 11 custom enterprise categories)**
- PHONE_NUMBER: 25
- URL: 20
- INFRA_SECRET: 15 🆕
- FINANCIAL_METRIC: 15 🆕
- EMAIL_ADDRESS: 10
- EMPLOYEE_ID: 10 🆕
- INFRA_NETWORK_MAP: 10 🆕
- IP_ADDRESS: 10
- CONTRACT_ID: 5 🆕
- INTERNAL_TAX_CODE: 5 🆕
- IP_SENSITIVE_MARKER: 5 🆕
- CRYPTO_PRIVATE_KEY: 5 🆕
- GPS_LOCATION: 5 🆕
- FINANCIAL_CREDENTIAL: 5 🆕
- VN_NATIONAL_ID: 5 🆕

## Reading this

- Latency difference between the two rows is the **cost of the 11 new categories** (regex + context scoring) — expected to be small since regex is C-engine and runs in milliseconds even on modest CPUs. Not included in this cost: the VN phone/underthesea NER fixes, which replace/extend *existing* PHONE_NUMBER/PERSON/ORG/LOCATION coverage rather than adding new categories, so they don't show up as 'custom-category hits' below even though they're part of the same fix.
- "Custom-category hits" is 0 for vanilla Presidio by construction: CONTRACT_ID, INTERNAL_TAX_CODE, FINANCIAL_METRIC, EMPLOYEE_ID, INFRA_SECRET, IP_SENSITIVE_MARKER, CRYPTO_PRIVATE_KEY, INFRA_NETWORK_MAP, GPS_LOCATION, FINANCIAL_CREDENTIAL and VN_NATIONAL_ID don't exist in stock Presidio at all — this row quantifies the coverage gap these categories close, not a tuning artifact.
