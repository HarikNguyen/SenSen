# SenSen — Sensitive Data Classifier

A Presidio-powered API that finds standard PII *and* thirteen
enterprise-specific sensitive-data categories (Legal, Financial, HR,
Security/Infra, IP, crypto keys, network maps, GPS, financial credentials,
VN national ID, bank account numbers, full street addresses) in text, PDF
and DOCX documents, scores confidence with
context-aware validation, and can return an anonymized copy. What vanilla
Presidio (or a generic Western PII tool) genuinely can't do that this does:
a dedicated VN mobile-number/national-ID recognizer built from the real
government numbering scheme (not a generic digit pattern), a
Vietnamese-aware NER (underthesea) with a DP-syllable-segmentation repair
for PDFs that lose spaces at the font level, a gazetteer-corrected NER layer
(Vietnamese calendar terms and administrative-unit prefixes) that fixes
type-confusion underthesea gets wrong on its own, OCR for scanned PDFs that
defaults to local Tesseract (Vietnamese+English, no cloud dependency) with
an opt-in pay-per-page cloud fallback (Gemini/OpenAI/Grok) for badly
degraded scans, and an opt-in "deep scan" LLM pass adding 3 more categories
regex fundamentally can't reach (trade-secret content, sensitive HR
content, full Vietnamese company names where regex/NER boundary detection
has no safe answer). Every one of these was built and tuned against real
documents, not synthetic examples alone — see Known Limitations for what's
still imperfect and why, and the "Why it's built this way" sections below
for what was tried and rejected along the way.

Status: **working local MVP** — 124/124 automated tests passing, tested end
to end over real HTTP (not just unit-level calls) and inside a built Docker
image. Scoped to local use, not deployed publicly.

## Why it's built this way

Every non-obvious choice below traded off against a fixed set of criteria
this project was optimized for (a weak local machine, free APIs, time,
risk, real-world usefulness, a presentable web page, and leaning on
existing prior art wherever possible). Where a claim depends on
something outside this repo (a cloud free-tier limit, a library's behavior),
it's cited.

### Architecture

```text
[ Client ] --(REST / file upload)--> [ 1. FastAPI: X-API-Key auth, rate/usage tracking ]
                                              │  raw text or PDF/DOCX bytes
                                              ▼
                          [ 2. Ingestion: PyMuPDF / python-docx text extraction ]
                                   (digital text layer, or local Tesseract OCR fallback)
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

### Curated recognizer set, not the full default list

`recognizers.yaml` explicitly lists ~9 predefined recognizers (email, phone,
URL, IP, credit card, IBAN, crypto, MAC address, US SSN) instead of loading
Presidio's full default set (50+ recognizers, many country-specific: UK NINO,
IN Aadhaar, KR RRN...). Benchmarked side by side in `scripts/benchmark.py` —
the curated set is actually **faster** (21.78ms vs 26.91ms mean/doc) despite
adding 11 custom categories, purely from evaluating fewer irrelevant regexes
per request. Run it yourself: `python scripts/benchmark.py` (writes
`BENCHMARK.md`).

### Silencing spaCy's "not mapped to a Presidio entity" warning

`en_core_web_sm`'s NER head tags 18 label types (`nlp.get_pipe("ner").
labels`); Presidio's built-in `model_to_presidio_entity_mapping` only
covers 7 of them (`PERSON`/`LOC`/`GPE`/`ORG`/`DATE`/`TIME`/`NORP`). The
other 11 (`CARDINAL`, `EVENT`, `FAC`, `LANGUAGE`, `LAW`, `MONEY`,
`ORDINAL`, `PERCENT`, `PRODUCT`, `QUANTITY`, `WORK_OF_ART`) log a "not
mapped to a Presidio entity, but keeping anyway" warning by default
(`NerModelConfiguration.labels_to_ignore` is `[]` out of the box) —
harmless on its own here, since `SpacyRecognizer` is disabled in
`recognizers.yaml` and nothing in this codebase ever turns spaCy's own
entities into a `RecognizerResult` (Vietnamese `PERSON`/`ORGANIZATION`/
`LOCATION` come from `app/vi_ner.py`'s underthesea NER instead), but noisy
— spaCy's NER still runs as part of `nlp_engine.process_text()` regardless,
since other recognizers need its tokenization/lemmatization, so any of
those 11 labels showing up in scanned text (a percentage, a product name,
a monetary phrase) logs the warning. Silenced by listing all 11 in
`ner_model_configuration.labels_to_ignore` when building the engine
(`app/engine.py`'s `_SPACY_LABELS_NOT_USED_BY_ANY_RECOGNIZER`) — spelled
out explicitly rather than silencing the warning class globally, so it
stays obvious to a future reader which spaCy labels this project doesn't
use and why.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# one-time system dep for OCR on scanned PDFs (skip if you don't need OCR —
# everything else works without it, scanned PDFs just get a clear 422)
sudo apt-get install -y tesseract-ocr tesseract-ocr-vie

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

# scan a file (pdf/docx/txt — scanned PDFs OCR automatically via Tesseract;
# add -F "ocr_engine=gemini" (or openai/grok) to use a paid cloud OCR API
# instead — see "Cloud OCR API" section below)
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
See `ASSESSMENT_REPORT.md` for a real run against the 9 synthetic documents
in `sample_corpus/` (all fake data, safe to commit).

## The 13 custom categories

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

Round 3:

| Category | entity_type | Signal |
|---|---|---|
| J. VN national ID | `VN_NATIONAL_ID` | Current 12-digit CCCD format (Thông tư 07/2016/TT-BCA): province code + century/gender digit constrained to `[0-3]` + birth year + random digits — a real structural rule, not a guess |

Two more fixes from that same round aren't new *categories* — they add VN
coverage to entity types Presidio already had: a dedicated Vietnam Phone
Recognizer (mobile numbering plan: `0`/`+84` + prefix `[35789]` + 8 digits)
now emits the standard `PHONE_NUMBER` alongside the built-in
`PhoneRecognizer` (which only ships with US/GB/DE/FR/IL/IN/CA/BR by
default), and `app/vi_ner.py` (underthesea) now emits `PERSON`/
`ORGANIZATION`/`LOCATION` in place of the disabled `SpacyRecognizer`.

Round 4:

| Category | entity_type | Signal |
|---|---|---|
| K. Bank account number | `BANK_ACCOUNT_NUMBER` | Digit-grouped (`341.0100.00041`) or bare 8-16 digit run, both deliberately low base score — same "weak shape, leans on context" design as `INTERNAL_TAX_CODE`/`VN_NATIONAL_ID`'s weak patterns, since a bare digit run is inherently ambiguous with several other categories here |
| L. Full street address | `FULL_ADDRESS` | Anchored on `Số <house-number>` (the most reliable start-of-address marker in real VN documents) through 1-4 comma-separated administrative-unit segments (`phường`/`quận`/`thành phố`/`tỉnh`, abbreviations like `TP` included) — underthesea's `LOCATION` only ever tagged isolated place-name fragments, never the full address around them |

`VN_NATIONAL_ID` also gained a second, weak pattern for the old 9-digit
CMND format (Chứng minh nhân dân, phased out but still the number on file
in real older documents) — a bare 9-digit run has no internal structure to
constrain it the way CCCD's century/gender digit does, so it leans
entirely on the existing `cmnd`/`cmtnd` context words, same design as
every other weak pattern in this list. `CONTRACT_ID` gained a
number-first variant (`14/2014/HĐCBL` — serial/year/abbreviation, the
reverse order of the existing `HD-YYYY-XXX` pattern), anchored on a
4-digit year in the middle segment to avoid being too generic a shape.

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
`/api/v1/scan` call, and Gemini's free tier would be exhausted almost
immediately under real traffic (see quota note below — the exact numbers
move fast and shouldn't be trusted from a README).

Instead, `app/deep_scan.py` is called only when the caller explicitly opts
in:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/scan \
  -H "Content-Type: application/json" -H "X-API-Key: <your_key>" \
  -d '{"text": "...", "deep_scan": true, "model": "gemini-3.7-flash"}'
```

Requires the `LANGEXTRACT_API_KEY` env var (this exact name — it's what the
library itself reads; get a key at
[aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey), no
card needed). Without it, `deep_scan: true` doesn't error — the response
just carries `"deep_scan_status": "skipped_no_key"` and regex-only results,
same as any other failure mode (network error, quota exhausted, or an
unusable `model` override) collapses to `"skipped_error"`. `deep_scan`
omitted or `false` never touches this code path at all — zero behavior
change for existing clients.

**Retries up to 3 times on failure** (`_MAX_ATTEMPTS = 3` in
`app/deep_scan.py`) before reporting `"skipped_error"` — an intermittent
`langextract` schema-validation race retries instantly, a genuine Gemini
RPM 429 gets a real backoff via `app/retry.py`'s `call_with_backoff` (the
same helper `app/ocr_api.py`'s cloud OCR uses, see "Cloud OCR API"
below). `gemini_limiter` (see below) throttles every real per-chunk
`lx.extract()` HTTP call, not just the outer call — see
`CONTRIBUTING.md` for why that distinction mattered. Either way, a bounded
retry, so a genuine outage still surfaces as `"skipped_error"` instead of
retrying forever and burning quota.

**`model` is optional** and picks the Gemini model for that one call;
omitted, it falls back to `DEFAULT_MODEL_ID` in `app/deep_scan.py`
(currently `gemini-flash-lite-latest` — a "-latest" alias, not a pinned
version, on purpose: the previously pinned `gemini-2.5-flash-lite` was still
callable but had already fallen off Google AI Studio's visible free-tier
quota page within months, which is what prompted this — a moving target
isn't solvable by picking a better fixed string, only by not pinning one for
the default). `GET /api/v1/deep_scan/models` lists what your key can
actually use right now (`{"status", "default_model", "models"}`, same
`status` vocabulary as `deep_scan_status`) — **live-queried against the
Gemini API, not a hardcoded list**, for the same reason: a static list here
would just go stale the same way. Filtered server-side
(`_is_usable_text_model` in `app/deep_scan.py`) down to plain Gemini
text-in/text-out models — image/TTS/robotics/computer-use variants that
share the `gemini-` prefix are excluded since they don't fit this
extraction use case. The demo console (`static/index.html`) calls this
endpoint to populate a dropdown when its "Deep scan" checkbox is ticked.

Pilot categories (`app/deep_scan.py`'s `EXAMPLES`, extend by adding more
few-shot examples — no other code changes needed, same story as
`recognizers.yaml`):

| entity_type | What it catches |
|---|---|
| `IP_TRADE_SECRET_CONTENT` | Upgrades the regex-only `IP_SENSITIVE_MARKER` (which only matches the literal word "confidential") into real detection of trade-secret-shaped content — proprietary algorithms, formulas, unreleased specs |
| `HR_SENSITIVE_CONTENT` | Performance-review / disciplinary content — no regex-detectable shape at all |

**Full coverage.** `PROMPT_DESCRIPTION`/`EXAMPLES` also cover every other
recognizer-registry category, as a verification pass — deliberately
redundant with regex/NER for exact-shape categories (email, credit card,
...), genuinely useful for the free-text ones NER struggles with (PERSON,
LOCATION, FULL_ADDRESS):

| Group | entity_type values |
|---|---|
| NER (weakest underthesea types, same rationale as `ORGANIZATION`) | `PERSON`, `LOCATION` |
| Standard PII (built-in Presidio, kept in `recognizers.yaml`) | `EMAIL_ADDRESS`, `PHONE_NUMBER`, `URL`, `IP_ADDRESS`, `CREDIT_CARD`, `IBAN_CODE`, `CRYPTO`, `MAC_ADDRESS`, `US_SSN` |
| VN / enterprise custom categories | `CONTRACT_ID`, `INTERNAL_TAX_CODE`, `FINANCIAL_METRIC`, `EMPLOYEE_ID`, `INFRA_SECRET`, `IP_SENSITIVE_MARKER`, `CRYPTO_PRIVATE_KEY`, `INFRA_NETWORK_MAP`, `GPS_LOCATION`, `FINANCIAL_CREDENTIAL`, `VN_NATIONAL_ID`, `BANK_ACCOUNT_NUMBER`, `FULL_ADDRESS` |

27 classes across 14 `ExampleData` entries, grouped into realistic
multi-entity snippets rather than one isolated example per class.
Verified live against the real Gemini API on a synthetic VN
identity/contract paragraph: all 8 of its expected types came back with
correct spans, none duplicated alongside its regex/NER equivalent.
`test_deep_scan_examples_are_grounded_and_cover_every_expected_class` and
`test_deep_scan_overlap_types_matches_examples_coverage` in
`tests/test_api.py` pin the example data and its sync with
`app/scanning.py`'s `_DEEP_SCAN_OVERLAP_TYPES`.

Two things worth knowing:
- **Scores are a fixed placeholder** (`0.6`) — langextract doesn't produce a
  Presidio-style calibrated confidence, unlike every regex-based category.
- **`anonymize=true` masks every value class from deep scan, not
  `HR_SENSITIVE_CONTENT`/`IP_TRADE_SECRET_CONTENT`.** A value class (a
  company name, an email address, ...) — `app/scanning.py` converts it back
  to a `RecognizerResult` and feeds it into `AnonymizerEngine` alongside the
  regex/NER hits, from the same final deduped entity list `detected_entities`
  reports (so what gets masked always matches what gets reported). The two
  content-flag types are deliberately excluded: they flag a whole sentence's
  *topic*, not a value to redact, and `AnonymizerEngine` resolves overlapping
  spans by letting the wider one win (verified directly) — including them
  would silently swallow a real `PHONE_NUMBER` or similar mentioned inside
  the flagged sentence into one opaque `<HR_SENSITIVE_CONTENT>` tag,
  destroying the sentence structure for no benefit.
- **When deep scan finds a value class that a free regex/NER recognizer
  also handles, deep scan's version wins on any span overlap** —
  `app/scanning.py`'s `_drop_regex_ner_entities_overlapped_by_deep_scan`
  drops the free-path entity, doesn't stack a duplicate alongside it. Not
  restricted to same-type overlaps on purpose: the failure this exists for
  is a free-path *mistype* (e.g. underthesea tagging a truncated company
  name fragment as `LOCATION` instead of `ORGANIZATION`), so a same-type-
  only check would miss exactly the cases that motivated it — pinned by
  `test_deep_scan_organization_merges_into_response`, which asserts a
  wrong-type `LOCATION` fragment gets dropped, not just a same-type one.

Per-key usage is capped (`MAX_DEEP_SCAN_PER_KEY = 50` in `app/pages.py`,
lifetime not daily-rolling — the simplest guard against one client draining
the shared free-tier quota; a real daily-reset quota is a reasonable
follow-up once actual usage is observed).

## Cloud OCR API — opt-in alternative to local Tesseract

Local Tesseract (`ocr_engine=local`, the default) stays free/offline/
private — nothing changes for existing callers. `POST /api/v1/scan/file`
now also accepts `ocr_engine=gemini|openai|grok` for scanned PDFs: the page
image is sent to that vendor's vision model instead of the local pipeline,
which reads badly degraded scans (heavy blur/rotation/noise) noticeably
better than a median-filtered local pass can recover, at the cost of a
per-page vendor call and sending the page image to a third party.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/scan/file \
  -H "X-API-Key: <your_key>" -F "file=@scanned.pdf" -F "ocr_engine=gemini"
```

Each engine needs its own env var (server-side, not sent by the client):
`LANGEXTRACT_API_KEY` for `gemini` (same key deep_scan already uses),
`OPENAI_API_KEY` for `openai`, `XAI_API_KEY` for `grok`. Missing the
relevant key gives a clear 422, same pattern as the existing
"tesseract binary isn't installed" error — never a silent fallback to a
different engine, since that would misreport `processing_mode`.

**Why only these three, not every vendor:** DeepSeek's public chat API
(`deepseek-chat`/`deepseek-reasoner`) is text-only as of 2026-08 — no
image input. DeepSeek does publish a separate open-weight OCR model
(`DeepSeek-OCR`), but that needs a different host entirely (e.g.
DeepInfra) under a different account/key, not "the same DeepSeek API with
an image added" — different enough in shape that it was left out rather
than bolted on as a special case. OpenAI and Grok (xAI) share one
implementation (`app/ocr_api.py`) because xAI's Responses API mirrors
OpenAI's field-for-field (`input=[{"role": "user", "content": [...]}]`
with `input_image`/`input_text` blocks) — verified against both vendors'
own docs — so both go through the `openai` SDK pointed at a different
`base_url`. Gemini goes through `google-genai` directly, same SDK
`app/deep_scan.py` already depends on.

**Model ids are env-overridable**
(`GEMINI_OCR_MODEL`/`OPENAI_OCR_MODEL`/`XAI_OCR_MODEL`, defaulting to
`gemini-flash-lite-latest`/`gpt-5.6`/`grok-4.6`) because vision model
names churn fast at every vendor. A stale default just fails with a clear
provider error (never a silently wrong answer), so override the env var
if a default 404s rather than treating it as a bug in this codebase.
`gemini-flash-lite-latest` specifically (not the plain `gemini-flash-
latest`) also avoids a real `503 "high demand"` overload that hits the
plain variant more often on image+text requests specifically — see
`CONTRIBUTING.md`.

**Per-request model override, independent from deep_scan's:** `/api/v1/
scan/file` also accepts `ocr_model` alongside `ocr_engine`, and
`GET /api/v1/ocr/models?engine=gemini|openai|grok` lists what's available
for the demo console's picker — deliberately a separate endpoint and a
separate dropdown from `GET /api/v1/deep_scan/models`, because OCR (a
vision call) and deep_scan (a pure-text extraction call) can legitimately
want different Gemini models for the same upload, and the OpenAI/Grok
engines have no deep_scan equivalent to share a picker with anyway. For
`engine=gemini` this reuses `app/deep_scan.py`'s own `list_available_
models()` outright rather than duplicating the listing/filtering logic —
the same models are multimodal (they accept image input, not just text),
so the criteria that make a model "usable for deep_scan" also make it
usable for OCR. OpenAI/Grok have no equivalent capability metadata in
their `/v1/models` response (Gemini's does, which is what `_is_usable_
text_model` filters on) — `list_openai_style_models()` in `app/ocr_api.py`
instead excludes obviously-non-vision model families by name
(`_NON_VISION_MODEL_SUBSTRINGS`: whisper, tts, embedding, moderation,
dall-e, ...), the same exclusion-list philosophy as the Gemini side, just
without a hard capability signal backing it — a listed model that turns
out not to support vision still just surfaces as a normal `OcrApiError` on
the next call, same as any other bad model id.

**Rate-limit handling, two independent layers — proactive and reactive:**

(1) **Proactive: `app/rate_limiter.py`** blocks *before* a call, not just
after a failure. `SlidingWindowRateLimiter.acquire()` tracks real call
timestamps in a 60s rolling window and sleeps until a slot is free the
moment the configured RPM budget would otherwise be exceeded — one
instance per provider (`gemini_limiter`, `openai_limiter`, `xai_limiter`),
env-overridable (`GEMINI_RPM_LIMIT`, `OPENAI_RPM_LIMIT`, `XAI_RPM_LIMIT`;
default 10/60/60 — the Gemini default is deliberately conservative,
comfortably under the observed real free-tier RPM). Critically,
**`gemini_limiter` is one shared instance between `app/ocr_api.py`'s
Gemini engine and `app/deep_scan.py`** — the two features draw down the
same real quota on the same key (both default to
`gemini-flash-lite-latest`), so throttling them independently would let
each think it had the full budget and still blow through the real limit
combined. This replaces waiting on a fixed per-page delay regardless of
actual load — the limiter only waits when the budget is genuinely tight.

Deliberately **minute-scale only, not day-scale**: blocking a few seconds
for an RPM slot is reasonable inside one HTTP request; blocking for
however long is left on a daily quota is not — a request can't reasonably
hang for hours. Day-scale protection is the existing per-key lifetime caps
(`MAX_DEEP_SCAN_PER_KEY`, `MAX_OCR_API_PER_KEY`, see `GET /api/v1/usage`
above) already provide, which *reject* once a threshold is hit rather than
wait — the right response shape at that timescale.

(2) **Reactive: `app/retry.py`** (shared between `app/ocr_api.py` and
`app/deep_scan.py`, pulled out into its own module for the same
"one implementation, not two that could drift" reason as the limiter
above) retries a transient status (429 rate-limit, or 500/502/503/504 —
the provider's own infra temporarily overloaded) up to 3 attempts total
with backoff (3s, then 10s) before giving up — checked via `openai.
APIStatusError.status_code` (covers `RateLimitError`, `InternalServerError`,
etc. in one check) for OpenAI/Grok, `google.genai.errors.APIError.code`
(covers both `ClientError` and `ServerError`) for Gemini. A permanent error
(401 bad key, 404 bad model id, ...) still fails immediately.

The two layers cover different gaps: the limiter avoids most 429s from
*this* process in the first place; the retry recovers from the ones that
still happen anyway (a second SenSen process or another client sharing the
same key, a burst that lands right at the window edge, etc.). Neither
layer can show the vendor's actual live "requests remaining" — none of the
three expose that via a public read endpoint — so together they soften
rate limits rather than eliminating them; a sustained outage still
surfaces as a 422 after 3 attempts instead of hanging or retrying forever.

**Per-key usage cap**, same reasoning as `MAX_DEEP_SCAN_PER_KEY`, applied
more directly since two of the three engines have no free tier at all:
`MAX_OCR_API_PER_KEY = 30` lifetime cloud-OCR calls per key
(`app/pages.py`), consumed on request (a caller who explicitly picks a
paid engine for an upload is expected to need it), not on confirmed OCR
necessity — same simplification `_resolve_deep_scan` already makes.
`GET /api/v1/usage` reports a key's own counts against both caps
(`deep_scan_used/limit`, `ocr_api_used/limit`) — this is SenSen's own
tracked usage, not the vendor's live quota, since no vendor here exposes
that. The demo console shows it under the API key box and refreshes it
after any scan that used deep_scan or a cloud OCR engine.

## File redaction — a real redacted PDF/DOCX/TXT file, not just masked text

`anonymize=true` on `/api/v1/scan` / `/api/v1/scan/file` only ever
returned masked **text** (`AnonymizedContent.text`) — for a file upload,
the extracted/OCR'd text with entities replaced by tags like
`<EMAIL_ADDRESS>`, never touching the original file. Confirmed directly
before building this: there was no way to get back an actual PDF with the
sensitive content blacked out. `POST /api/v1/redact/file` does that, for
`.pdf`, `.docx` and `.txt` alike:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/redact/file \
  -H "X-API-Key: <your_key>" -F "file=@contract.pdf" -F "confidence_threshold=0.5" \
  -o redacted_contract.pdf
```

Same params as `/api/v1/scan/file` (`confidence_threshold`, `ocr_engine`,
`ocr_model`, `deep_scan`, `model`) minus `anonymize`/`language` (redaction
implies masking; English-pipeline-only is an existing constraint, not new
here). Any other extension gets a clear 422. Draws from the same
`MAX_OCR_API_PER_KEY`/`MAX_DEEP_SCAN_PER_KEY` quota pools as the scan
endpoints, not a separate budget.

### PDF — two real redaction mechanisms, chosen per page

`app/redact.py`'s `redact_pdf()` processes the document **per page** rather than as one joined string the
way `app/scanning.py`'s `run_scan()` does — redaction needs to know which
page (and where on it) an entity sits, which the joined-string design
never tracked (`EntityLocation.page` in `app/schemas.py` has existed since
early on but `run_scan()` never actually populated it). A minor, known,
honest trade-off from this: a page-scoped `analyze()` call can very
occasionally score an entity slightly differently than the whole-document
call would, if context words happen to fall just across a page boundary —
`/api/v1/scan` and `/api/v1/scan/file` are completely unaffected, only
this endpoint analyzes per-page.

- **A page with a native text layer**: `pymupdf`'s `page.search_for(text)`
  finds real bounding-box `Rect`s for a literal string, and
  `add_redact_annot()` + `apply_redactions()` **deletes the underlying
  text content** — confirmed directly (redact a test string, reopen the
  output, re-extract: the string is completely gone from `get_text()`,
  not just visually covered by a box a PDF editor could peel back).
- **A scanned (image-only) page**: OCR gives word/line boxes — local
  Tesseract via `pytesseract.image_to_data` (real per-*word* boxes, the
  same `dpi=200` render `app/extract.py`'s `_ocr_page` already uses for
  the non-redaction OCR path), or a cloud engine via `app/ocr_api.py`'s
  new `ocr_words_via_api()` (per-*line* boxes — see below for why line-,
  not word-level, for the cloud path). A black rectangle is drawn directly
  onto the rendered page image for each matched entity via
  `PIL.ImageDraw`, then the page is replaced with the redacted image
  (`doc.delete_page()` + `doc.new_page(pno=...)` + `insert_image()` at the
  same index, verified directly that page order and untouched pages
  survive this). Matching an entity's text to the OCR word(s)/line(s) that
  cover it is plain interval overlap on already-known `[start, end)`
  offsets (both the reconstructed page text and each OCR chunk's position
  in it are built together, see `_tesseract_data_to_text_and_spans`/
  `_lines_to_text_and_spans`), not a second fuzzy text search — sidesteps
  the exact class of bug the `_find_token` cascade-desync fix above had to
  solve, rather than re-introducing a new version of it.

**Cloud OCR-with-boxes, per vendor** (`app/ocr_api.py`'s
`ocr_words_via_api`, a separate function from the plain-text
`ocr_image_via_api` the normal scan path uses — no reason to add
bbox-request complexity to a call site that never needs boxes):
line-level, not word-level, is the deliberate unit for all three cloud
engines — that's what these models can place reliably, and using it for
redaction over-covers a little non-sensitive text on the same line rather
than under-covering, the acceptable side of that trade-off for something
whose whole point is not leaking PII.
- **Gemini** has a documented bounding-box contract: request JSON with
  `box_2d: [ymin, xmin, ymax, xmax]` normalized 0-1000
  ([ai.google.dev/gemini-api/docs/image-understanding](https://ai.google.dev/gemini-api/docs/image-understanding)).
- **OpenAI**'s newer vision models document one too: `[x_min, y_min,
  x_max, y_max]` normalized 0-999, `"detail": "original"` recommended on
  the input image for coordinate-sensitive tasks like this
  ([developers.openai.com/cookbook/.../document_and_multimodal_understanding_tips](https://developers.openai.com/cookbook/examples/multimodal/document_and_multimodal_understanding_tips)).
- **Grok (xAI) has no equivalent documented bounding-box feature** —
  nothing found in its public docs as of 2026-08. The same JSON-prompt
  contract is tried anyway (Grok is a general vision-language model), but
  this is materially lower-confidence than the other two; a live check
  with a real key is recommended before trusting its output quality.
  Parsing is defensive regardless (`_parse_bbox_json` strips a markdown
  code fence models routinely add despite being told not to;
  `_items_to_words` silently skips any malformed entry rather than
  crashing on one bad item) — but the actual safety net if a vendor's
  boxes turn out unreliable in practice is the fallback below, not the
  parser.

**Gemini's response is schema-constrained, not just prompted.**
`_ocr_words_gemini` passes `response_schema=list[_GeminiBboxItem]` (a
Pydantic model, `{text: str, box_2d: list[float]}`) alongside
`response_mime_type="application/json"` — this constrains the model's
actual token-level decoding to the declared shape, so field names can't
drift and string values come out correctly escaped, unlike free text that
merely resembles JSON (see `CONTRIBUTING.md` for the failure modes this
replaced). `_parse_bbox_json`'s `strict=False` and `_items_to_words`'
text/label + box_2d/box/bbox fallbacks are kept as defense in depth, and
because OpenAI/Grok still go through prompt-only JSON, not this schema
mechanism.

**Safety fallback — the single most important property of this feature**:
if a detected entity's text can't be confidently located on the page
(missing/empty OCR boxes for a scanned page, `search_for` finding nothing
for a digital page), the **whole request fails** with a clear error
naming the page (`RedactionFailed`, surfaced as a 422) — never a PDF that
looks redacted but silently missed something. Under-redaction is a
security failure, not a degraded-but-acceptable result the way a missing
regex match is elsewhere in this project; pinned directly by
`test_redact_digital_page_fails_safely_when_entity_cant_be_located` and
`test_redact_cloud_engine_no_boxes_returned_gives_clear_422` in
`tests/test_api.py`.

Output is written via `doc.tobytes(garbage=4, deflate=True, clean=True)`,
not a plain `doc.tobytes()` — the plain form leaves a redacted scanned
page's embedded image uncompressed and the deleted original page's data
uncollected (see `CONTRIBUTING.md` for the 11MB-file bug this fixed).
Pinned by `test_redact_output_is_reasonably_sized_not_bloated`. A no-op
cost for digital-text pages, which never embed a new image.

### DOCX — run-splicing for text, the same OCR-and-blackout pipeline for embedded images

`redact_docx()` covers three paragraph sources with one routine: body
`document.paragraphs`, every table cell's paragraphs (`document.tables` →
`.rows` → `.cells` → `.paragraphs`, mirroring `_extract_docx`'s existing
table-flattening), and every section's header/footer paragraphs
(`document.sections[i].header/footer.paragraphs` — structurally identical
`Paragraph`/`Run` objects to the body, confirmed directly).

- **Text** — for each paragraph, entities are detected against
  `paragraph.text`, then resolved to the run(s) they overlap via the same
  span/offset interval-overlap technique the PDF path uses for OCR
  words/lines (`_run_spans` builds `(start, end, run)` tuples over the
  paragraph's runs). A run partially covered by an entity keeps its
  non-overlapping prefix/suffix — only the covered slice is deleted
  (`_splice_entity_from_runs`, applied in reverse start order so earlier
  splices don't shift not-yet-processed offsets). This is **real XML-level
  deletion**, confirmed directly: after redacting and saving, the original
  string is completely gone from the raw `word/document.xml` inside the
  saved `.docx` (it's a zip) — not a formatting change a user could strip
  back off. Pinned by `test_redact_docx_removes_sensitive_text_for_real`
  (asserts both the friendly `python-docx` API view and the raw XML) and
  `test_redact_docx_covers_tables_and_headers`.
- **Embedded images** — every image part in `document.part.related_parts`
  (filtered to `image/*` content types) is decoded and run through the
  *exact same* `_ocr_and_redact_image()` the PDF scanned-page path uses
  (local Tesseract or a cloud engine's `ocr_words_via_api`, entity
  detection, blackout via `PIL.ImageDraw`) — not a reimplementation.
  The redacted PNG is written back via the private `part._blob = <new
  bytes>` attribute; `python-docx` has no public setter for replacing an
  image's content, confirmed directly that no other path persists a
  replacement (saved, reopened, re-decoded the image part's blob back to
  a `PIL.Image` to check). Same safety fallback as the PDF scanned path:
  unusable OCR boxes or an unlocatable entity fails the whole request
  (`RedactionFailed` → 422) rather than shipping a document with one
  silently-unredacted image. Pinned by
  `test_redact_docx_embedded_image_blacks_out_the_image` (redacts, then
  re-extracts and re-OCRs the image from the *returned* file to confirm
  the sensitive text no longer reads out) and
  `test_redact_docx_unsupported_embedded_image_format_gives_clear_422`.
- **Explicitly out of scope, documented not silently ignored**: comments,
  tracked-changes/revision history (Word can retain old deleted text in
  the file's revision history even after a visible edit — a real gap this
  pass does not close), text boxes/shapes (`python-docx` has no real API
  for these), embedded OLE objects, and document core/custom properties
  (author name, etc. in `document.core_properties`). Same "clear about
  what isn't covered" approach the PDF path already takes for cloud-OCR
  engine confidence differences.

### TXT — the existing masked text, as a downloadable file

No new detection or masking logic: `redact_txt()` runs the exact same
`AnonymizerEngine.anonymize()` pass `anonymize=true` already uses on the
whole file's text (no per-page concept for a `.txt`), and returns that
masked string as a `text/plain` file response instead of embedded JSON.
Pinned by `test_redact_txt_masks_sensitive_text`.

The demo console's file-upload tab shows a "Tải file đã ẩn danh hoá (xoá
thật nội dung nhạy cảm)" button whenever a `.pdf`, `.docx` or `.txt` is
selected — triggers a real browser download of the returned file
(`redacted_<original filename>`, extension matching the upload), separate
from the existing "Quét dữ liệu nhạy cảm" button (one returns JSON for
on-screen review, the other a file to save).

## Known limitations (read before demoing)

*Engineering history — what bugs were found and how they were fixed during
development — moved to `CONTRIBUTING.md` to keep this section focused on
what's actually still true today.*

- **Vietnamese NER has residual imprecision, not perfect.** `app/vi_ner.py`
  (underthesea) handles PERSON/ORGANIZATION/LOCATION for Vietnamese
  content. One known open gap: a company name truncated to its last 1-2
  words is occasionally still mistyped as LOCATION on the free/default
  path — the fix (an LLM-based `ORGANIZATION` extraction, 5/5 correct on
  the cases that motivated this) only applies when `deep_scan=true` (see
  "Deep scan" above); Vietnamese has no reliable regex "end of proper
  name" delimiter to fix this on the free path itself.
- **OCR** needs `tesseract-ocr` + `tesseract-ocr-vie` installed on the host
  (the Dockerfile installs them automatically; locally run
  `sudo apt-get install -y tesseract-ocr tesseract-ocr-vie`) — without it, a
  scanned PDF gives a clear 422 naming the missing binary. Capped at
  `MAX_OCR_PAGES = 20` pages (a clear 422 beyond that, not a silent hang).
  Accuracy degrades under heavy photocopy-style speckle noise; a median
  filter mitigates this but doesn't fully eliminate it. A pay-per-page
  cloud OCR opt-in (`ocr_engine=gemini/openai/grok`) exists for scans local
  Tesseract struggles with — see "Cloud OCR API" above.
- **Digit-pattern ambiguity is inherent, not fully solved.** VN tax codes
  and phone numbers are both 10 digits; the mobile-prefix exclusion
  handles the common case, not all of it — an inherent precision tradeoff
  of regex-based detection, mitigated rather than eliminated.
- **`saas.db` is a local file**, fine for MVP/demo, not for concurrent
  production writers — swap the `DATABASE_URL` env var
  (`SENSEN_DATABASE_URL`) for Postgres when that matters.
- **Large uploads are read fully into memory before any size check**
  (`await file.read()` in `app/pages.py`) — realistic sizes (tens of MB)
  are rejected in well under a second once decoded, but an upload in the
  hundreds-of-MB-to-GB range would consume that much memory before
  `MAX_TEXT_LENGTH` gets a chance to reject it. Needs a genuine policy
  decision (a hard upload-size cap or a streaming read), not built since
  this project isn't exposed publicly.
- **A persistent deep-scan failure still surfaces as `"skipped_error"`**,
  not folded into "nothing sensitive here" — `deep_scan_status` is a
  separate response field specifically so the regex-side results are
  still trusted as regex results, not silently treated as a complete scan.

## Roadmap (not built yet, sequenced by effort/value)

1. **LLM confidence validator** — a different use of the same `langextract`/
   Gemini pipeline already built for deep scan: re-score *existing*
   regex-based hits that land in the ambiguous 0.4–0.7 confidence band,
   instead of discovering new entity types. Not built yet — the deep-scan
   integration (see above) covers the new-category use case first.
2. **Real daily-rolling deep-scan quota** — the current `MAX_DEEP_SCAN_PER_KEY`
   cap in `app/pages.py` is a simple lifetime counter; a proper daily reset
   is worth building once actual usage patterns are observed.
3. **Company-name span boundaries** — the one remaining ORG/LOC issue
   (see Known Limitations) is a span-boundary problem investigated and
   explicitly not solved with regex, since Vietnamese has no reliable
   "end of proper name" delimiter under this file's case-insensitive
   matching. Not on the "next thing to build" list — the honest resolution
   path is a real Vietnamese ORG-name gazetteer or a small classifier, not
   a bigger regex, and that's a materially bigger lift than anything else
   in this file.

*(Done, not roadmap anymore: OCR — see "Known limitations"; deep-scan
retry — see "Deep scan" section above; underthesea's day-name and
administrative-prefix type confusion — see `CONTRIBUTING.md`.)*

## Deployment

The `Dockerfile` is verified, not theoretical — `docker build` → `docker
run` → `/register` → `/api/v1/scan` → `/api/v1/usage` → `/api/v1/redact/file`
(a `.txt` upload, real masked output) all pass against a fresh container
built from the current codebase. `google-genai` was missing from
`requirements.txt` as an explicit dependency (only installed transitively
via `langextract`, despite `app/deep_scan.py`/`app/ocr_api.py` importing
it directly) — added and re-verified. The three cloud OCR providers are
verified by mocked unit/HTTP tests (dispatch, error handling, rate-limit
retry, quota) plus SDK-signature introspection against the
actually-installed package versions. Gemini specifically is also verified
live, against a real 9-page scanned document (a genuine Vietcombank
bank-guarantee amendment letter with stamps and a signature, not a
synthetic sample) with a real `LANGEXTRACT_API_KEY`: the OCR text came
back fully correct, diacritics included. OpenAI/Grok have no test key
available in this environment to run the same live check, so those two
still rely on the mocked-test + introspection verification only — worth a
real check with your own key before depending on them.

This project is scoped to local use, not deployed publicly — the
`Dockerfile` is ready for any Docker host if that changes, but picking and
setting up one isn't part of the current scope.

Set `LANGEXTRACT_API_KEY` as an env var (or secret, per your host) if you
want deep_scan (and `ocr_engine=gemini`, which reuses the same key) to
actually work, not just report `skipped_no_key`. `OPENAI_API_KEY`/
`XAI_API_KEY` are the same pattern for `ocr_engine=openai`/`grok` — all
three are optional, cloud OCR falls back to a clear 422 per engine if its
key is missing, same as deep_scan does.

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
  extract.py          PDF/DOCX/TXT -> plain text; OCR fallback (local Tesseract by
                      default, or ocr_engine=gemini/openai/grok) for scanned PDFs
  ocr_api.py          Cloud OCR providers for the ocr_engine opt-in (Gemini direct,
                      OpenAI/Grok sharing one Responses-API code path)
  retry.py            Shared reactive transient-error (429/5xx) backoff-retry, used by
                      both ocr_api.py's cloud OCR and deep_scan.py's LangExtract/Gemini pass
  rate_limiter.py      Shared proactive sliding-window RPM throttle -- same two callers,
                      one gemini_limiter instance so both draw down the real shared quota
  redact.py            Real PDF/DOCX/TXT redaction (POST /api/v1/redact/file) -- PDF via
                      pymupdf search_for+apply_redactions (digital) or image blackout
                      (scanned), DOCX via run-splicing (text) + the same OCR-and-blackout
                      pipeline (embedded images), TXT via the existing masked-text output;
                      local Tesseract or cloud OCR-with-boxes word/line positions throughout
  recognizers/
    recognizers.yaml  <- the whole regex extensibility story lives here
static/index.html     Demo console — self-serve register/API key + usage/quota display,
                      paste-text or file-upload (with OCR engine + model pickers, separate
                      from deep scan's own model picker, plus a redact-to-file-download
                      button for .pdf/.docx/.txt) tabs, confidence slider, anonymize +
                      deep scan, color-coded highlight + entity table
tests/                124 tests total: detection (positive/negative/ambiguous),
                      file-upload, OCR fallback (local + cloud engines, rate-limit
                      retry + proactive throttle, quota), PDF/DOCX/TXT redaction (digital
                      text deletion, scanned-page/embedded-image blackout, safety
                      fallback), deep-scan (incl. retry), VN phone/ID/NER fixes, auth
scripts/benchmark.py       Vanilla Presidio vs. this registry, speed + coverage
scripts/assess_corpus.py   Batch-scan a folder, rank documents by risk (the Assessment Report)
sample_corpus/         9 synthetic (fake-data) documents; full_coverage_demo.txt hits all 22
                      entity types (every custom category + all curated Presidio types +
                      VN NER + both deep-scan categories) in one file for manual testing
Dockerfile             Ready for any Docker host
CONTRIBUTING.md        Dev setup, code style, and the engineering history of
                      bugs found and fixed during development
```
