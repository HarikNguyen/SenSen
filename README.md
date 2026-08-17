# SenSen — Sensitive Data Classifier

A Presidio-powered API that finds standard PII *and* five enterprise-specific
sensitive-data categories (Legal, Financial, HR, Security/Infra, IP) in text,
PDF and DOCX documents, scores confidence with context-aware validation, and
can return an anonymized copy. Built from the spec in `thongtin.md`.

Status: **working local MVP** — 20/20 automated tests passing, tested end to
end over real HTTP (not just unit-level calls). Not yet deployed publicly.

## Why it's built this way

Every non-obvious choice below traded off against the 9 criteria this project
was optimized for (Azure student credit, a weak local machine, free APIs,
time, risk, real-world usefulness, a presentable web page, and leaning on
existing prior art wherever possible). Where a claim depends on something
outside this repo (a cloud free-tier limit, a library's behavior), it's cited.

### Architecture

```text
[ Client ] --(REST / file upload)--> [ 1. FastAPI: X-API-Key auth, rate/usage tracking ]
                                              │  raw text or PDF/DOCX bytes
                                              ▼
                          [ 2. Ingestion: PyMuPDF / python-docx text extraction ]
                                   (digital documents only — no OCR yet, see Roadmap)
                                              │  clean text
                                              ▼
                [ 3. Presidio AnalyzerEngine: spaCy en_core_web_sm NER
                      + registry loaded from app/recognizers/recognizers.yaml ]
                                              │  scored entities
                                              ▼
                     [ 4. Presidio AnonymizerEngine (optional masking) ]
                                              │
                                              ▼
                                     [ JSON response ]

  Layer 5 (persistence, all layers can reach it): SQLite + SQLAlchemy — User,
  APIKey, request_count only. Document text is never written to disk or DB,
  per thongtin.md's zero-trust requirement (in-RAM only, for the request's lifetime).
```

### The extensibility requirement came (almost) for free

`thongtin.md` section 2.D asks for a "modular, plugin-based" system where a
6th/7th category can be added "without touching core code." Rather than
building a custom plugin framework, this uses Presidio's own **native
config-driven recognizer registry** (`RecognizerRegistryProvider` /
`AnalyzerEngineProvider`) — confirmed against the installed package's own
`presidio_analyzer/conf/{default_recognizers,example_recognizers}.yaml` and
the [official docs](https://microsoft.github.io/presidio/analyzer/recognizer_registry_provider/).
**All 5 custom categories live entirely in `app/recognizers/recognizers.yaml`
— `app/main.py` never changes when you add a 6th.** See "Adding a new
category" below.

### Why Azure Container Apps instead of the App Service the spec suggested

`thongtin.md`'s own Azure section proposed App Service. Verified against
current [Azure pricing docs](https://azure.microsoft.com/en-us/pricing/details/app-service/)
and [Container Apps pricing](https://azure.microsoft.com/en-us/pricing/details/container-apps/):

| | Free (F1) App Service | Container Apps (Consumption) |
|---|---|---|
| Cost | Free, but... | **Always free** up to 180K vCPU-s + 360K GiB-s + 2M requests/mo — doesn't touch your $100 credit at low traffic |
| CPU quota | 60 CPU-min/**day**, hard-stops with 403 until UTC midnight when exhausted | No daily wall, scales to zero when idle |
| Cold start | No "Always On" on F1 → sleeps, cold-starts | Scale-to-zero is the *design*, same effect but no quota-exhaustion cliff |

Container Apps is a strict upgrade for this workload and still gives you the
"deployed on Azure" story for a client pitch. See `Dockerfile` — it's already
container-ready for either target.

### Why not Hugging Face Spaces

The obvious "put a Presidio demo on HF Spaces" instinct (Microsoft's [own
official demo](https://huggingface.co/spaces/presidio/presidio_demo) lives
there) turned out to be a dead end for a *free* account: per [HF's own
docs](https://huggingface.co/docs/hub/en/spaces-overview), Docker/Gradio
Spaces now require a paid personal plan to create — only Static Spaces (no
Python backend) are free. Azure Container Apps + Render.com (fallback) are
the real free options; see `Deployment` below.

### Curated recognizer set, not the full default list

`recognizers.yaml` explicitly lists ~9 predefined recognizers (email, phone,
URL, IP, credit card, IBAN, crypto, MAC address, US SSN) instead of loading
Presidio's full default set (50+ recognizers, many country-specific: UK NINO,
IN Aadhaar, KR RRN...). Benchmarked side by side in `scripts/benchmark.py` —
the curated set is actually **faster** (17.7ms vs 26.9ms mean/doc) despite
adding 5 new categories, purely from evaluating fewer irrelevant regexes per
request. Run it yourself: `python scripts/benchmark.py` (writes
`BENCHMARK.md`).

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm

uvicorn app.main:app --reload
# open http://127.0.0.1:8000/           -> demo console
# open http://127.0.0.1:8000/docs       -> Swagger UI
```

```bash
# register to get an API key
curl -X POST http://127.0.0.1:8000/register -H "Content-Type: application/json" \
  -d '{"email":"you@example.com"}'

# scan text
curl -X POST http://127.0.0.1:8000/api/v1/scan \
  -H "Content-Type: application/json" -H "X-API-Key: <your_key>" \
  -d '{"text":"Hợp đồng số HD-2026-0142, email a@b.com", "confidence_threshold":0.5}'

# scan a file (pdf/docx/txt, digital text only, no OCR)
curl -X POST http://127.0.0.1:8000/api/v1/scan/file \
  -H "X-API-Key: <your_key>" -F "file=@contract.pdf" -F "confidence_threshold=0.5"
```

Run tests / benchmark / corpus assessment:

```bash
pytest tests/ -v
python scripts/benchmark.py
python scripts/assess_corpus.py            # scans sample_corpus/, writes ASSESSMENT_REPORT.md
python scripts/assess_corpus.py /path/to/real/documents
```

`assess_corpus.py` is the batch-audit counterpart to `/api/v1/scan`: point it
at a folder, it scans every `.txt/.pdf/.docx` in it and ranks documents by a
weighted risk score (live secrets > contract/financial/HR IDs > IP markers),
so a human reviewer knows which document to open first instead of reading a
flat entity dump. This is the "Assessment Report" deliverable from
thongtin.md section 3 applied to a whole corpus rather than one file — see
`ASSESSMENT_REPORT.md` for a real run against the 7 synthetic documents in
`sample_corpus/` (all fake data, safe to commit).

## The 5 custom categories

| Category (thongtin.md 2.A) | entity_type | Signal |
|---|---|---|
| A. Legal & Contractual | `CONTRACT_ID` | `HD/HĐ/NDA-YYYY-XXXX` (strong) + generic `LETTERS-##-ALNUM` (weak, needs context) |
| B. Financial — tax code | `INTERNAL_TAX_CODE` | 10/13-digit MST, VN mobile prefixes (03/05/07/08/09) excluded to cut phone-number collisions |
| B. Financial — figures | `FINANCIAL_METRIC` | Currency-formatted amounts (VND/USD), boosted by salary/budget context |
| C. HR & Workforce | `EMPLOYEE_ID` | `NV-/EMP-/STAFF-#####` style codes |
| D. Security & Infra | `INFRA_SECRET` | AWS keys, `sk-...` keys, JWTs, DB connection strings — patterns adapted from [gitleaks' public ruleset](https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml) |
| E. Intellectual Property | `IP_SENSITIVE_MARKER` | Deliberately **low-confidence** — flags "CONFIDENTIAL/proprietary/trade secret" markers for human review. Regex fundamentally cannot detect trade secrets/source code reliably; this category is a review flag, not a detector. Stated here on purpose rather than overclaiming precision it can't have. |

### Adding a new category (no code changes)

Add a block to `app/recognizers/recognizers.yaml`:

```yaml
  - name: My New Recognizer
    supported_language: "en"
    supported_entity: "MY_ENTITY"
    patterns:
      - name: "pattern name"
        regex: "..."
        score: 0.6
    context: ["keyword1", "keyword2"]
```

Restart the app. That's the entire integration surface — this is what
"modular/plugin-based, no core code changes" means concretely.

**Gotcha to know about** (cost me a debug cycle building this): Presidio's
context matcher compares your `context` words against **individual
surrounding token lemmas** — a multi-word phrase like `"hợp đồng"` will
*silently* never match anything, because no single token's lemma equals a
two-word string. Use single words. See the comment block at the top of the
`recognizers` list in `recognizers.yaml` for the full explanation and the fix
applied throughout (`context_prefix_count`/`context_suffix_count` widened to
5/5 in `app/main.py` so context words on either side of a match count, not
just before it).

## Known limitations (read before demoing)

- **Vietnamese NER is weak.** `en_core_web_sm` is English-only; on Vietnamese
  text it mislabels ordinary words as PERSON/ORGANIZATION/DATE_TIME (verified
  empirically — see the false positives in a raw scan of Vietnamese text).
  The 5 custom categories are regex/context-based so they're mostly
  unaffected, but generic NER entities will be noisy on Vietnamese input.
  Fix requires a Vietnamese-aware model (`vi_spacy` or `underthesea`) — sized
  as a roadmap item, not day-1 scope, to protect the build timeline.
- **No OCR.** Scanned/image PDFs raise a clear 422, not a silent failure.
  Deliberate: `thongtin.md`'s own hardware analysis flags OCR as the CPU
  "sát thủ phần cứng" (hardware killer) to avoid on an i3, and Azure AI
  Document Intelligence's free F0 tier only analyzes the **first 2 pages** of
  any document regardless of the 500-pages/month pool ([official
  limits](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/service-limits?view=doc-intel-4.0.0))
  — not good enough for real multi-page contracts without upgrading to paid
  S0. See Roadmap.
- **Digit-pattern ambiguity is inherent, not fully solved.** VN tax codes and
  phone numbers are both 10 digits; the mobile-prefix exclusion handles the
  common case, not all of it. This is exactly the precision problem
  `thongtin.md` 2.B describes — mitigated, not eliminated, by regex alone.
- **`saas.db` is a local file**, fine for MVP/demo, not for concurrent
  production writers — swap the `DATABASE_URL` env var (`SENSEN_DATABASE_URL`)
  for Postgres when that matters.

## Roadmap (not built yet, sequenced by effort/value)

1. **File-upload OCR via Azure AI Document Intelligence** for scanned PDFs.
   Free F0 tier confirmed working but capped at 2 pages/doc — fine for a demo,
   needs paid S0 for real contracts. Needs your Azure key to build and test;
   not stubbed in this repo to avoid shipping untested cloud-integration code.
2. **LLM second-opinion validator** (Gemini free tier via Google AI Studio,
   no card required) for entities scoring in the ambiguous 0.4–0.7 band, to
   push precision further per `thongtin.md` 2.B. Verified free-tier access
   works, but Google cut quotas 50-80% in Dec 2025 — call it selectively
   (borderline-confidence hits only) with a skip-on-quota-exhaustion fallback,
   never as a hard dependency.
3. **Vietnamese NLP**: `vi_spacy` (spaCy-compatible, least glue code to plug
   into Presidio's existing `NlpEngine` interface) or `underthesea`.
4. **Render.com fallback deploy** if Azure setup friction blocks a demo
   deadline — free tier confirmed live (512MB RAM/0.1 CPU, 750 hrs/mo, ~15min
   inactivity sleep). Tighter on RAM than Container Apps but zero Azure setup.

## Deployment (Azure Container Apps)

The `Dockerfile` itself is verified, not theoretical: `docker build` (656MB
image) → `docker run` → `/register` → `/api/v1/scan` all passed locally,
correctly returning `CONTRACT_ID`/`EMAIL_ADDRESS` for a test document. What's
untested is only the Azure side — this environment has no `az` CLI session
available to it. Commands to run yourself from the repo root once logged in
(`az login`):

```bash
az group create -n sensen-rg -l southeastasia

az containerapp up \
  --name sensen-api \
  --resource-group sensen-rg \
  --location southeastasia \
  --source . \
  --target-port 8000 \
  --ingress external
```

`az containerapp up` builds the `Dockerfile` in this repo remotely and gives
you a public HTTPS URL. Swap `SENSEN_DATABASE_URL` for a real path if you
want the SQLite file to persist across revisions (Container Apps' local disk
is ephemeral by default — for the MVP demo this is fine since state is just
`users`/`api_keys`, re-registering is cheap).

## Reference / prior art

- Official Presidio demo: https://huggingface.co/spaces/presidio/presidio_demo
- Presidio's YAML-driven recognizer registry:
  https://microsoft.github.io/presidio/analyzer/recognizer_registry_provider/ ,
  https://microsoft.github.io/presidio/tutorial/08_no_code/
- Closest direct precedent for this project's shape (FastAPI + Presidio
  service): https://github.com/karndeb/Presidio-Service ,
  https://github.com/pvcy/presidio-microsoft
- Secret-pattern reference: https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml ,
  https://github.com/Yelp/detect-secrets
- Market validation (same problem space, commercial): Nightfall AI, BigID,
  Microsoft Purview Information Protection, Concentric AI.

## Repo layout

```text
app/
  main.py             FastAPI app, auth, /register, /api/v1/scan, /api/v1/scan/file
  database.py         SQLAlchemy models: User, APIKey
  schemas.py          Pydantic request/response contracts
  extract.py          PDF/DOCX/TXT -> plain text (no OCR)
  recognizers/
    recognizers.yaml  <- the whole extensibility story lives here
static/index.html     Minimal demo console (paste text, see highlighted hits)
tests/                17 detection tests (positive/negative/ambiguous) + 3 file-upload tests + auth
scripts/benchmark.py       Vanilla Presidio vs. this registry, speed + coverage
scripts/assess_corpus.py   Batch-scan a folder, rank documents by risk (the Assessment Report)
sample_corpus/         7 synthetic (fake-data) documents exercising all 5 categories + txt/pdf/docx
Dockerfile             Ready for Azure Container Apps / Render / any Docker host
```
