# SenSen — Sensitive Data Classifier

A Presidio-powered API that finds standard PII *and* eleven
enterprise-specific sensitive-data categories (Legal, Financial, HR,
Security/Infra, IP, crypto keys, network maps, GPS, financial credentials,
VN national ID) in text, PDF and DOCX documents, scores confidence with
context-aware validation, and can return an anonymized copy. Also includes
Vietnam-specific fixes found by scanning a real document — a dedicated VN
mobile-number recognizer and a Vietnamese-aware NER (underthesea) replacing
`en_core_web_sm`'s zero-Vietnamese-support NER — and an opt-in "deep scan"
LLM pass adding 2 more categories regex fundamentally can't reach
(trade-secret content, sensitive HR content).

Status: **working local MVP** — 34/34 automated tests passing, tested end to
end over real HTTP (not just unit-level calls). Not yet deployed publicly.

## Why it's built this way

Every non-obvious choice below traded off against a fixed set of criteria
this project was optimized for (Azure student credit, a weak local machine,
free APIs, time, risk, real-world usefulness, a presentable web page, and
leaning on existing prior art wherever possible). Where a claim depends on
something outside this repo (a cloud free-tier limit, a library's behavior),
it's cited.

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
  APIKey, request_count only. Document text is never written to disk or DB —
  zero-trust by design, in-RAM only for the request's lifetime.
```

### The extensibility requirement came (almost) for free

The design goal was a modular, plugin-based system where a 6th/7th category
can be added without touching core code. Rather than building a custom
plugin framework, this uses Presidio's own **native config-driven recognizer
registry** (`RecognizerRegistryProvider` / `AnalyzerEngineProvider`) —
confirmed against the installed package's own
`presidio_analyzer/conf/{default_recognizers,example_recognizers}.yaml` and
the [official docs](https://microsoft.github.io/presidio/analyzer/recognizer_registry_provider/).
**All 11 pattern-based categories live entirely in
`app/recognizers/recognizers.yaml`** — no app code changed for rounds 2 or 3
of these. That's specifically true for *regex* categories; non-pattern
integrations (the LLM-based deep scan, the underthesea Vietnamese NER) are
real Python modules by necessity, not a YAML block — see "Adding a new
category" below for where the line is.

### Why Azure Container Apps instead of App Service

App Service was the first option considered. Verified against current
[Azure pricing docs](https://azure.microsoft.com/en-us/pricing/details/app-service/)
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
the curated set is actually **faster** (21.78ms vs 26.91ms mean/doc) despite
adding 11 custom categories, purely from evaluating fewer irrelevant regexes
per request. Run it yourself: `python scripts/benchmark.py` (writes
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
flat entity dump — turns single-document scanning into a corpus-wide audit.
See `ASSESSMENT_REPORT.md` for a real run against the 8 synthetic documents
in `sample_corpus/` (all fake data, safe to commit).

## The 11 custom categories

| Category | entity_type | Signal |
|---|---|---|
| A. Legal & Contractual | `CONTRACT_ID` | `HD/HĐ/NDA-YYYY-XXXX` (strong) + generic `LETTERS-##-ALNUM` (weak, needs context) |
| B. Financial — tax code | `INTERNAL_TAX_CODE` | 10/13-digit MST, VN mobile prefixes (03/05/07/08/09) excluded to cut phone-number collisions |
| B. Financial — figures | `FINANCIAL_METRIC` | Currency-formatted amounts (VND/USD), boosted by salary/budget context |
| C. HR & Workforce | `EMPLOYEE_ID` | `NV-/EMP-/STAFF-#####` style codes |
| D. Security & Infra | `INFRA_SECRET` | AWS keys, `sk-...` keys, JWTs, DB connection strings — patterns adapted from [gitleaks' public ruleset](https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml) |
| E. Intellectual Property | `IP_SENSITIVE_MARKER` | Deliberately **low-confidence** — flags "CONFIDENTIAL/proprietary/trade secret" markers for human review. Regex fundamentally cannot detect trade secrets/source code reliably; this category is a review flag, not a detector. Stated here on purpose rather than overclaiming precision it can't have. |

Round 2 (chosen because they're regex-feasible, same risk profile as round 1
— unlike the many other candidate categories that need real semantic/topic
classification):

| Category | entity_type | Signal |
|---|---|---|
| F. Cryptographic material | `CRYPTO_PRIVATE_KEY` | PEM private-key block headers (`-----BEGIN RSA PRIVATE KEY-----` etc.) — near-zero false-positive rate |
| G. Internal network map | `INFRA_NETWORK_MAP` | RFC1918 private IPv4 ranges + any CIDR notation (`10.20.5.1/24`) — distinct from Presidio's built-in `IpRecognizer`, which only matches single IPs |
| H. GPS coordinates | `GPS_LOCATION` | Decimal lat/long pairs — deliberately weak base score (0.25), leans on context almost entirely, same design as `INTERNAL_TAX_CODE` |
| I. Financial credential | `FINANCIAL_CREDENTIAL` | PIN/OTP/password *assignment* boosted by banking context — not the account number alone (same collision risk as phone-vs-tax-code, deliberately avoided) |

Round 3 (added while fixing findings from scanning a real document — see
"Known limitations" below for the full story):

| Category | entity_type | Signal |
|---|---|---|
| J. VN national ID | `VN_NATIONAL_ID` | Current 12-digit CCCD format (Thông tư 07/2016/TT-BCA): province code + century/gender digit constrained to `[0-3]` + birth year + random digits — a real structural rule, not a guess |

Two more fixes from that same pass aren't new *categories* — they add VN
coverage to entity types Presidio already had: a dedicated Vietnam Phone
Recognizer (mobile numbering plan: `0`/`+84` + prefix `[35789]` + 8 digits)
now emits the standard `PHONE_NUMBER` alongside the built-in
`PhoneRecognizer` (which only ships with US/GB/DE/FR/IL/IN/CA/BR by
default), and `app/vi_ner.py` (underthesea) now emits `PERSON`/
`ORGANIZATION`/`LOCATION` in place of the disabled `SpacyRecognizer` — see
"Deep scan" section's sibling, right below "Known limitations".

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

**Gotchas to know about** (all three cost a debug cycle building this):

1. Presidio's context matcher compares your `context` words against
   **individual surrounding token lemmas** — a multi-word phrase like
   `"hợp đồng"` will *silently* never match anything, because no single
   token's lemma equals a two-word string. Use single words. See the comment
   block at the top of the `recognizers` list in `recognizers.yaml` for the
   full explanation and the fix applied throughout
   (`context_prefix_count`/`context_suffix_count` widened to 8/8 in
   `app/engine.py` so context words on either side of a match count, not
   just before it).
2. For any `"keyword ... value"` pattern (like `FINANCIAL_CREDENTIAL`'s
   PIN/password assignment), a fixed-width gap between the keyword and the
   value (e.g. `.{0,30}?`) will silently fail to match once real sentences
   insert enough descriptive text — "mật khẩu ngân hàng của tài khoản công
   ty là: X" alone is ~33 chars of filler, past a 30-char cap. Size the gap
   generously (60 chars here) and add a test with a realistically wordy
   sentence, not just the shortest phrasing that happens to work.
3. spaCy's English lemmatizer mangles foreign acronyms it doesn't recognize —
   `"GPS"` lemmatizes to `"gp"` (stripped as if it were a plural "s"), so a
   context word of `"gps"` silently never matches. Caught by direct
   inspection of `nlp_engine.process_text(...).keywords`, not by guessing.
   No general fix (whack-a-mole to list every mangled variant); the practical
   mitigation is keeping context words physically close to the value in your
   test/sample text, since Vietnamese-under-English-pipeline context matching
   is already documented above as best-effort, not guaranteed.

## Deep scan — LLM-based pass for semantic-only categories (opt-in)

Some categories can't be regex-detected at all — "does this paragraph
disclose a trade secret" has no fixed shape. Presidio ships a disabled-by-
default `BasicLangExtractRecognizer` that wraps Google's
[`langextract`](https://github.com/google/langextract) library (LLM-based,
few-shot extraction with built-in source-grounding — it maps each extraction
back to an exact character span, which is the hard part of turning LLM
output into a `DetectedEntity`). It is **not** wired in as a normal Presidio
recognizer, because Presidio runs every recognizer in the registry on every
`/api/v1/scan` call, and Gemini's free tier (~15 RPM / ~1,000-1,500 RPD, no
card required) would be exhausted almost immediately under real traffic.

Instead, `app/deep_scan.py` is called only when the caller explicitly opts
in:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/scan \
  -H "Content-Type: application/json" -H "X-API-Key: <your_key>" \
  -d '{"text": "...", "deep_scan": true}'
```

Requires the `LANGEXTRACT_API_KEY` env var (this exact name — it's what the
library itself reads; get a key at
[aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey), no
card needed). Without it, `deep_scan: true` doesn't error — the response
just carries `"deep_scan_status": "skipped_no_key"` and regex-only results,
same as any other failure mode (network error, quota exhausted) collapses to
`"skipped_error"`. `deep_scan` omitted or `false` never touches this code
path at all — zero behavior change for existing clients.

Pilot categories (`app/deep_scan.py`'s `EXAMPLES`, extend by adding more
few-shot examples — no other code changes needed, same story as
`recognizers.yaml`):

| entity_type | What it catches |
|---|---|
| `IP_TRADE_SECRET_CONTENT` | Upgrades the regex-only `IP_SENSITIVE_MARKER` (which only matches the literal word "confidential") into real detection of trade-secret-shaped content — proprietary algorithms, formulas, unreleased specs |
| `HR_SENSITIVE_CONTENT` | Performance-review / disciplinary content — no regex-detectable shape at all |

Two things worth knowing:
- **Scores are a fixed placeholder** (`0.6`) — langextract doesn't produce a
  Presidio-style calibrated confidence, unlike every regex-based category.
- **`anonymize=true` doesn't mask deep-scan hits** — the anonymizer only
  understands Presidio's own `RecognizerResult`, not langextract output.
  Deep-scan entities appear in `detected_entities` for visibility but aren't
  included in `anonymized_content`.

Per-key usage is capped (`MAX_DEEP_SCAN_PER_KEY = 50` in `app/pages.py`,
lifetime not daily-rolling — the simplest guard against one client draining
the shared free-tier quota; a real daily-reset quota is a reasonable
follow-up once actual usage is observed).

## Known limitations (read before demoing)

- **Vietnamese NER has known residual imprecision (not perfect, but no
  longer garbage).** `en_core_web_sm`'s `SpacyRecognizer` is disabled
  (`enabled: false` in `recognizers.yaml`) — on a real scanned contract it
  tagged ordinary phrase fragments ("một bên", "Ông/Bà") as PERSON/ORG and
  misclassified a real phone number and national ID as DATE_TIME, 0%
  precision in that test. `app/vi_ner.py` (underthesea) replaces it for
  PERSON/ORGANIZATION/LOCATION — ~109MB RAM (comparable to en_core_web_sm),
  no network call. First pass had its own noise problem, found by running it
  over the whole `sample_corpus/` (not just a clean sentence): underthesea's
  model reads ALL-CAPS headers, secret/key blobs and financial figures as
  strong entity signals, so section titles and RSA key markers came back
  tagged PERSON/LOCATION. Fixed with a Title Case + no-digits filter
  (`_looks_like_named_entity` in `app/vi_ner.py`) — real Vietnamese names and
  place names are always Title Case, headers/secrets/figures aren't; this
  dropped corpus-wide detections from 80 to 39 entities with the false
  positives gone and the real names/places intact.

  Third round, found by re-running the exact real contract PDF that started
  this fix: some PDFs drop the space glyph between certain word pairs at the
  font/kerning level (confirmed via raw `pymupdf` word-box inspection — the
  space is genuinely absent from the source file, not an extraction bug),
  and a fused run like "Trợlý" or "Chếđộlàm" was opaque to underthesea's own
  tokenizer, so it got swallowed as one token and tagged on capitalization
  alone. `_expand_fused_words()` in `app/vi_ner.py` repairs this before
  tagging: it DP-segments any non-dictionary token into known Vietnamese
  syllables (frequencies reused from underthesea's own bundled
  `Viet74K.txt`, no second dictionary shipped), only rewriting a token when
  the DP finds *full* coverage — secrets, IDs and real foreign words are
  left untouched since no full syllable coverage exists for them. Verified
  against the same real PDF: recovers a full party name ("Trịnh SỹThành")
  that was previously invisible entirely, and correctly bounds real
  compound place names ("Phường Thủ Thiêm").

  Known residual gaps, documented rather than chased further: (1) a single
  capitalized non-Vietnamese word can still slip through (e.g. "Backup");
  (2) underthesea sometimes assigns the wrong *type* even with the right
  span — "Phường Thủ Thiêm" came back as PERSON instead of LOCATION, "Chủ
  Nhật" (Sunday) as LOCATION instead of not-an-entity — a type-confusion
  issue distinct from the segmentation one, consistent with the
  already-known ORG/LOC unreliability below; (3) common single words that
  are capitalized only because they start a sentence/bullet ("Được", "Xét",
  "Cục") can still pass the Title Case filter — sentence-initial
  capitalization looking like a proper noun is a generic, hard NER problem
  in any language, not specific to this fix. Also still true: underthesea's
  ORG/LOC boundary isn't always reliable (a company name merged into a
  wrongly-tagged LOCATION span in one test), so results carry a flat 0.6
  score rather than a calibrated one.
- **No OCR.** Scanned/image PDFs raise a clear 422, not a silent failure.
  Deliberate: OCR is the CPU "sát thủ phần cứng" (hardware killer) to avoid
  on weak local hardware, and Azure AI Document Intelligence's free F0 tier
  only analyzes the **first 2 pages** of any document regardless of the
  500-pages/month pool ([official
  limits](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/service-limits?view=doc-intel-4.0.0))
  — not good enough for real multi-page contracts without upgrading to paid
  S0. See Roadmap.
- **Digit-pattern ambiguity is inherent, not fully solved.** VN tax codes and
  phone numbers are both 10 digits; the mobile-prefix exclusion handles the
  common case, not all of it — an inherent precision tradeoff of
  regex-based detection, mitigated rather than eliminated.
- **`saas.db` is a local file**, fine for MVP/demo, not for concurrent
  production writers — swap the `DATABASE_URL` env var (`SENSEN_DATABASE_URL`)
  for Postgres when that matters.

## Roadmap (not built yet, sequenced by effort/value)

1. **File-upload OCR via Azure AI Document Intelligence** for scanned PDFs.
   Free F0 tier confirmed working but capped at 2 pages/doc — fine for a demo,
   needs paid S0 for real contracts. Needs your Azure key to build and test;
   not stubbed in this repo to avoid shipping untested cloud-integration code.
2. **Render.com fallback deploy** if Azure setup friction blocks a demo
   deadline — free tier confirmed live (512MB RAM/0.1 CPU, 750 hrs/mo, ~15min
   inactivity sleep). Tighter on RAM than Container Apps but zero Azure setup.
3. **LLM confidence validator** — a different use of the same `langextract`/
   Gemini pipeline already built for deep scan: re-score *existing*
   regex-based hits that land in the ambiguous 0.4–0.7 confidence band,
   instead of discovering new entity types. Not built yet — the deep-scan
   integration (see above) covers the new-category use case first.
4. **Real daily-rolling deep-scan quota** — the current `MAX_DEEP_SCAN_PER_KEY`
   cap in `app/pages.py` is a simple lifetime counter; a proper daily reset
   is worth building once actual usage patterns are observed.
5. **Underthesea's ORG/LOC boundary confusion** — tighten `app/vi_ner.py`
   once more real-document examples are gathered (e.g. a heuristic for
   company-name suffixes like "Công ty TNHH" that keep getting merged into
   LOCATION spans instead of ORGANIZATION).

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
  main.py             App factory only: FastAPI instance, lifespan, static mount, router include
  pages.py            Routing layer: thin HTTP handlers (APIRouter), no business logic
  logics.py           Business logic the routes call (currently: registration)
  engine.py           Presidio construction (spaCy + recognizer registry)
  scanning.py         Core scan logic: analyze -> entities -> optional anonymize
  deep_scan.py        Opt-in LLM pass (langextract + Gemini) for semantic-only categories
  vi_ner.py           Vietnamese-aware NER (underthesea) — replaces disabled SpacyRecognizer
  auth.py             X-API-Key verification dependency
  database.py         SQLAlchemy models: User, APIKey
  schemas.py          Pydantic request/response contracts
  extract.py          PDF/DOCX/TXT -> plain text (no OCR)
  recognizers/
    recognizers.yaml  <- the whole regex extensibility story lives here
static/index.html     Minimal demo console (paste text, see highlighted hits)
tests/                34 tests total: detection (positive/negative/ambiguous),
                      file-upload, deep-scan, VN phone/ID/NER fixes, auth
scripts/benchmark.py       Vanilla Presidio vs. this registry, speed + coverage
scripts/assess_corpus.py   Batch-scan a folder, rank documents by risk (the Assessment Report)
sample_corpus/         8 synthetic (fake-data) documents exercising the original 10 categories + txt/pdf/docx
Dockerfile             Ready for Azure Container Apps / Render / any Docker host
```
