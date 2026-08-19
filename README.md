# SenSen — Sensitive Data Classifier

A Presidio-powered API that finds standard PII *and* eleven
enterprise-specific sensitive-data categories (Legal, Financial, HR,
Security/Infra, IP, crypto keys, network maps, GPS, financial credentials,
VN national ID) in text, PDF and DOCX documents, scores confidence with
context-aware validation, and can return an anonymized copy. What vanilla
Presidio (or a generic Western PII tool) genuinely can't do that this does:
a dedicated VN mobile-number/national-ID recognizer built from the real
government numbering scheme (not a generic digit pattern), a
Vietnamese-aware NER (underthesea) with a DP-syllable-segmentation repair
for PDFs that lose spaces at the font level, a gazetteer-corrected NER layer
(Vietnamese calendar terms and administrative-unit prefixes) that fixes
type-confusion underthesea gets wrong on its own, local OCR (Tesseract,
Vietnamese+English) for scanned PDFs with no cloud dependency, and an opt-in
"deep scan" LLM pass adding 3 more categories regex fundamentally can't
reach (trade-secret content, sensitive HR content, full Vietnamese company
names where regex/NER boundary detection has no safe answer). Every one of these was
built and tuned against real documents, not synthetic examples alone — see
Known Limitations for what's still imperfect and why, and the "Why it's
built this way" sections below for what was tried and rejected along the way.

Status: **working local MVP** — 56/56 automated tests passing, tested end to
end over real HTTP (not just unit-level calls) and inside a built Docker
image. Scoped to local use, not deployed publicly.

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

# scan a file (pdf/docx/txt — scanned PDFs OCR automatically via Tesseract)
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

**Retries once on failure** (`_MAX_ATTEMPTS = 2` in `app/deep_scan.py`)
before reporting `"skipped_error"` — found a real, reproducible bug where
`langextract`'s Gemini provider intermittently raises a schema-validation
error on an otherwise-valid call (same text/model/key failed once, then
succeeded on immediate retry — see Known Limitations for the full
diagnosis). A bounded, logged retry rather than an unbounded one, so a
genuine outage still surfaces as `"skipped_error"` instead of retrying
forever and burning quota.

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
| `ORGANIZATION` | Full Vietnamese company names ("Công ty TNHH/Cổ phần X") — fixes a documented span-boundary limitation the free regex+NER path can't safely solve on its own (see Known Limitations) |

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

  Fourth round: the Title Case filter above was a hard keep/reject gate,
  and every kept result got a flat `0.6` — which meant this recognizer,
  alone among every other one in this codebase, ignored the caller's
  `confidence_threshold` entirely. Tried fixing the remaining single-word
  noise ("Được", "Xét", "Cục") with the syllable-frequency table already
  built for word-fusion repair — checking raw frequency doesn't work
  (verified: "cục"=61 sits right next to real name syllables like
  "thủ"=143), so that alone isn't a usable signal. Replaced the hard gate
  with `_score_entity()`: a graduated score built from *dictionary
  membership* (not frequency) plus two more signals — multi-word spans get
  a bonus (a real full name or place is almost always 2+ words) and
  sentence/bullet-initial single words get a penalty. Verified this
  actually separates cleanly on the real documents that surfaced the
  problem: every real name/place lands at `0.65`, every false positive at
  `0.15`–`0.25` — and unlike the flat score, this respects
  `confidence_threshold` like every other category, so a caller asking for
  a normal operating threshold (`0.5`+) sees none of the noise, without a
  hard-coded denylist. Bonus catch: this also cleared the round-2 "Backup"
  gap (a single capitalized non-Vietnamese word) at `0.5`+, since it has
  no multi-word bonus and no dictionary-membership penalty to offset its
  sentence-initial one.

  Fifth round: two of the three remaining type-confusion cases turned out
  to be fixable after all, once re-examined with the same "add a signal"
  approach that fixed the single-word noise above rather than accepted as
  permanent. `app/vi_ner.py` now applies a small, fully-enumerable
  gazetteer correction after BIO merge: Vietnamese day names (`Thứ
  Hai`...`Chủ Nhật`, a closed set of exactly 7) are rejected outright — a
  day name is never itself PII — fixing the "Chủ Nhật"/"Thứ Bảy" as
  LOCATION case from real documents; and administrative-unit prefixes
  (`Phường`, `Quận`, `Huyện`, `Tỉnh`, `Xã`, `Thị trấn`, `Thành phố`) as a
  span's first word force-correct the type to LOCATION without touching the
  span boundary, fixing "Phường Thủ Thiêm" tagged PERSON. Verified against
  every real document gathered across this whole NER fix (`hopdong.pdf`,
  `sample_corpus/full_coverage_demo.txt`, the rest of `sample_corpus/`):
  both false positives gone, every previously-correct entity unchanged.

  One type-confusion case investigated at length on the regex/NER side and
  ultimately solved a different way: a company name truncated to its last
  1-2 words ("Toàn Cầu" from "Công ty Cổ phần Đầu Tư Toàn Cầu") tagged
  LOCATION instead of ORGANIZATION — a span-boundary problem, not a type
  problem, so the gazetteer approach above doesn't apply. Tried anchoring
  on Vietnamese legal-entity prefixes ("Công ty TNHH/Cổ phần/...") via
  regex twice: first case-insensitively (matching `recognizers.yaml`'s
  global `IGNORECASE` flag), both unbounded and capped at 4 words — 4 of 5
  real test sentences over-matched into unrelated trailing text ("Công ty
  TNHH Thiên Tứ Điện thoại", "...và bà", "...làm việc"), and underthesea
  itself often detects *no* organization span at all for these names in
  isolation, leaving nothing to type-correct either. Second attempt:
  registered a dedicated `PatternRecognizer` in Python (not YAML) with its
  own `global_regex_flags` *without* `IGNORECASE` — Presidio's YAML schema
  only exposes one global flag for the whole file, but a programmatically-
  registered recognizer can set its own, letting real capitalization act
  as the "end of name" signal this problem needed. That got 4 of 5 cases
  exactly right (including recovering the *full* "Công ty Cổ phần Đầu Tư
  Toàn Cầu" — better than underthesea managed on its own) and reduced the
  5th case's error from swallowing a whole trailing clause down to one
  extra word, since the immediately-following label ("Điện thoại:") is
  also capitalized in Vietnamese and there's no delimiter between them.
  Given that residual and the effort already spent, the actual fix shipped
  is different: extended deep scan (opt-in, see below) with a third
  extraction class, `ORGANIZATION` — an LLM doesn't have a "no reliable
  delimiter" problem here, it can use real semantic understanding of what
  a company name is. Verified against the same 5 real test sentences via
  the live Gemini API: 5 of 5 exactly right, including the one case the
  case-sensitive regex still got wrong. This only fixes it when a caller
  opts into `deep_scan=true`; the free/default regex+NER path keeps its
  documented limitation, since fixing this on that path specifically was
  the part that turned out not to have a safe answer.

  First version of this shipped with both results visible when
  `deep_scan=true`: the correct full `ORGANIZATION` from deep scan *and*
  the free path's wrong/truncated `LOCATION` for the same real company,
  side by side — redundant, not wrong, but pointed out as worth cleaning
  up. `_drop_regex_ner_entities_overlapped_by_deep_scan()` in
  `app/scanning.py` now drops the free-path entity when deep scan finds an
  overlapping `ORGANIZATION`/`PERSON`/`LOCATION` result — span *overlap*,
  not the exact-span dedup used elsewhere in this file
  (`_drop_lower_scored_exact_duplicates`), since the two spans genuinely
  differ in width (underthesea's is a truncated substring of deep scan's
  fuller one), not just in score. Deliberately scoped to just those three
  types: `HR_SENSITIVE_CONTENT`/`IP_TRADE_SECRET_CONTENT` flag a whole
  sentence and routinely overlap a `PHONE_NUMBER` or similar mentioned
  inside it — a genuinely separate finding, not a competing interpretation
  of the same value — so those two categories never trigger this drop,
  verified with a test where a `PHONE_NUMBER` nested inside an
  `HR_SENSITIVE_CONTENT` span survives untouched.

  Sixth round, found by testing a two-column-layout document (a realistic
  "hard" scenario, requested explicitly instead of more synthetic
  corruption tests) — this turned out to explain the `"Tên\nNguyễn Xuân
  Hùng"` case from round three too, not just a new one. Root cause:
  underthesea's own tokenizer doesn't treat `"\n"` as a boundary, so it
  sometimes fuses a real name with the next line's label word into one
  token (`"Trần Thị Hoa"` + the next line's `"Số"` from `"Số CCCD:"`, one
  fused token). The fused token then failed the exact-substring lookup that
  maps it back to character offsets — the source text has a newline where
  the token has a plain space — silently dropping the *entire* entity with
  no error, losing a real name completely rather than just mis-scoring it.
  Fixed in two parts, both in `app/vi_ner.py`: `_find_token()` falls back
  to whitespace-flexible matching when the exact substring isn't found, and
  `_split_on_newlines()` then scores each newline-delimited piece of a
  recovered span independently, so `"Trần Thị Hoa"` is judged on its own
  merits instead of as part of a `"Trần Thị Hoa Số CCCD"` blob that fails
  the Title Case check as a whole. Fixing this then *surfaced* a second,
  older latent bug rather than introducing one: `_is_sentence_initial`
  checked `text[:start].rstrip()[-1]`, which silently strips the newline
  itself before checking it, so a bare line break with no punctuation
  before it (the common case — most lines in these documents don't end in
  a period) was never actually recognized as sentence-initial, only a
  newline preceded by `.`/`-` was. Previously invisible because the first
  bug above was dropping the affected spans entirely; once real spans
  started reaching the scorer, single English label words placed right
  after a bare newline (`"Internal API key:"`, `"Email liên hệ:"`) stopped
  getting the sentence-initial penalty and started passing the default
  0.5 threshold. Fixed by walking back over whitespace to find the actual
  boundary instead of `.rstrip()`-ing it away first. Verified against every
  real document gathered across this whole fix (`hopdong.pdf`,
  `sample_corpus/full_coverage_demo.txt`, the rest of `sample_corpus/`):
  real names recovered, no new noise introduced by either fix.
- **OCR (local Tesseract, not a cloud service).** Scanned/image PDFs go
  through `pytesseract` + the system `tesseract-ocr`/`tesseract-ocr-vie`
  packages (`app/extract.py`) instead of raising a 422 — chosen over Azure
  AI Document Intelligence specifically because this project is scoped to
  run locally, not deployed to the cloud, so a service needing a cloud key
  and a network call is the wrong tool here. Capped at `MAX_OCR_PAGES = 20`
  pages (`app/extract.py`) — OCR is meaningfully more CPU-intensive than
  reading an existing text layer, so an unbounded scanned document isn't
  safe to accept on the project's target weak-hardware host; beyond that
  cap it's a clear 422, not a silent hang. Verified end-to-end inside a
  built Docker image (this machine has no local `tesseract` binary or
  passwordless `sudo` to install one, so Docker — which builds as root —
  is how this got tested): a synthetic scanned PDF rendered with a
  Vietnamese-capable font OCR'd back to exact diacritics-correct text, and
  `VN_NATIONAL_ID`/`LOCATION` detected from it. First attempt at this test
  used PyMuPDF's default font, which silently drops Vietnamese diacritic
  glyphs — that was a test-methodology bug, not a Tesseract limitation,
  caught by inspecting the rendered image directly before trusting the OCR
  output. `document_metadata.processing_mode` reports `"ocr"` vs
  `"direct_text_extraction"` so a caller can tell which path ran. **Only
  works if `tesseract-ocr` + `tesseract-ocr-vie` are installed on the host**
  — the Dockerfile installs them automatically; for local non-Docker
  `uvicorn --reload` use, run
  `sudo apt-get install -y tesseract-ocr tesseract-ocr-vie` once (this one
  step needs your password, which isn't available in this environment to
  do it for you) — without it, a scanned PDF gives a clear 422 naming the
  missing binary rather than a silent failure.

  A follow-up `/code-review high --fix` pass caught two real bugs in the
  first version of this before they shipped further: (1) `_extract_pdf`
  checked `text.strip()` over the whole joined document, so a PDF mixing
  digital-text pages with a scanned page (e.g. a typed contract with a
  scanned signature page) got treated as fully "has a text layer" and the
  scanned page's content was silently dropped, no error, no OCR attempt —
  fixed to check per page and OCR only the pages that need it. (2)
  `_ocr_page` only caught `TesseractNotFoundError`; a present-but-broken
  install (`tesseract-ocr` without `tesseract-ocr-vie`, a plausible partial
  install given the two-package instruction above) raised
  `pytesseract.TesseractError` instead, which propagated as an unhandled
  500 instead of the documented 422 — added the missing except branch.
  Both re-verified in the same Docker setup: a mixed digital+scanned test
  PDF now returns both pages' content, and a real tesseract call still
  works correctly after also swapping the OCR image-build path from a
  PNG-encode-then-decode round-trip to `Image.frombytes` directly off the
  pixmap (an efficiency fix from the same pass — verified it doesn't change
  OCR output).

  **A more realistic worst case than a corrupt file: a genuinely degraded
  scan.** Pushed on to test something closer to a real bad document instead
  of synthetic garbage bytes — built a test PDF simulating a photocopy/photo
  of a contract (Gaussian blur + a 2.5° rotation + photocopy-style speckle
  noise) and tested each factor isolated and combined. Blur and rotation
  alone didn't hurt accuracy at all — still 6/6 real entities detected
  correctly either way. Speckle noise alone was the actual culprit: it
  alone dropped detection to 0/6 correct (the CCCD number got OCR'd with
  spurious internal spaces breaking its 12-digit pattern, real names became
  unrecoverable garbage, a phone number was invented from noise). Root
  cause and fix: added a median filter to `_ocr_page` in `app/extract.py`
  before handing the image to Tesseract — but the first attempt (a 3x3
  kernel) barely helped once actually measured through the real `dpi=200`
  render this function uses (an earlier check that looked promising had
  used a lower, non-representative resolution by mistake — a reminder to
  verify against the exact code path, not a shortcut that looks similar).
  At 200 DPI each noise pixel from the source gets upsampled into a
  multi-pixel blob, too big for a 3x3 kernel; a 5x5 kernel fully fixed the
  noise-only case but still lost entities on the combined blur+rotate+noise
  case; a 7x7 kernel recovers that too (6/6, matching the clean-scan
  baseline), with no regression re-verified on an actually-clean scan at
  each step. This specific accuracy check isn't part of the automated
  `pytest` suite — it inherently needs real Tesseract output quality, which
  only exists inside the Docker verification, not in this local dev
  environment (no local `tesseract` binary, see above) or a mock.
- **Digit-pattern ambiguity is inherent, not fully solved.** VN tax codes and
  phone numbers are both 10 digits; the mobile-prefix exclusion handles the
  common case, not all of it — an inherent precision tradeoff of
  regex-based detection, mitigated rather than eliminated.
- **Same-span duplicates across categories, fixed without narrowing phone
  coverage.** Found via the full-coverage test doc: Presidio's built-in
  multi-region `PhoneRecognizer` also matched a CIDR block, a VN national
  ID and a VN tax code — each under some *other* country's phone-number
  shape (`DE`/`IN`/`BR` specifically), always at a lower score than the
  correct category. The direct fix — narrowing `supported_regions` — was
  considered and explicitly rejected: this product needs phone detection to
  work broadly, not just VN/US, so trading away region coverage to fix a
  scoring artifact was the wrong tradeoff. Fixed instead at the application
  layer: `_drop_lower_scored_exact_duplicates()` in `app/scanning.py` keeps
  only the highest-scoring result when two categories match the *exact
  same* `[start, end)` span — the text can't genuinely be two different
  identifier types at once, so a lower-scoring duplicate on the identical
  span is redundant noise, not a second real finding. All 8 phone regions
  stay fully active; a real US/GB/etc. number is untouched, only the
  redundant low-score duplicate disappears.
- **`saas.db` is a local file**, fine for MVP/demo, not for concurrent
  production writers — swap the `DATABASE_URL` env var (`SENSEN_DATABASE_URL`)
  for Postgres when that matters.
- **Adversarial file-upload testing found and fixed 2 real crash bugs.**
  Explicit request to test worst-case/hostile inputs, not just happy-path
  ones — a corrupted or empty `.pdf` and a corrupted `.docx` both crashed
  with an unhandled 500 instead of the documented clear 422:
  `pymupdf.open()` raises its own `FileDataError` (its `EmptyFileError`
  subclass covers the 0-byte case too) for a malformed PDF, and
  `python-docx`'s `Document()` doesn't guarantee one exception type for a
  malformed `.docx` (confirmed empirically: `zipfile.BadZipFile` for a
  non-zip file, a plain `KeyError` for a valid zip that isn't a real docx
  package) — neither was caught before, both are now (`app/extract.py`).
  Other worst-case scenarios tested and found already handled correctly,
  no fix needed: a 21-page scanned PDF (over `MAX_OCR_PAGES`), text at and
  above `MAX_TEXT_LENGTH`, a 25MB upload (rejected fast once decoded — see
  below for the residual risk this doesn't cover), a missing filename, an
  adversarially long unbroken "word" fed to the DP word-segmentation repair
  (50,000 chars in ~1.5s, no blowup), and the `FINANCIAL_CREDENTIAL`
  pattern's lazy quantifier against a crafted no-match input (bounded by
  its own `{0,60}` cap, no catastrophic backtracking). Residual, not fixed:
  `scan_file` reads the entire upload into memory before any size check
  (`await file.read()` in `app/pages.py`) — a 25MB file is rejected in
  under 0.3s once decoded, so this is low-severity at realistic sizes, but
  an upload sized in the hundreds of MB to GB range would still consume
  that much memory before `MAX_TEXT_LENGTH` ever gets a chance to reject
  it. Not fixed here since it needs a genuine policy decision (a hard
  upload-size cap, likely in FastAPI/uvicorn config or a streaming read
  with an early bailout) rather than a one-line patch, and the project's
  own scoping (local use, not exposed publicly) makes this lower priority
  than the two crash bugs above.
- **Deep scan used to fail intermittently — this was a real, reproduced
  bug, not FUD, now mitigated with a retry.** Found by testing
  `sample_corpus/full_coverage_demo.txt`: the exact same call (same text,
  model, key) returned `skipped_error` once and `"ok"` with correct
  extractions on an immediate retry. Root cause is inside `langextract`
  1.6.0's Gemini provider — a pydantic `ValidationError` building its
  `response_schema` ("Extra inputs are not permitted" on
  `type`/`properties`/`required`), which looks like a schema-shape mismatch
  against the installed `google-genai` 2.18.1. Both packages were already at
  their latest release when this was found, so there was no version bump
  available to fix it at the source. `run_deep_scan` now retries once before
  reporting `"skipped_error"` (see the Deep Scan section above) — this
  doesn't eliminate the bug, it papers over a transient one; a *persistent*
  failure (bad key, real outage, genuinely broken schema every time) still
  surfaces as `"skipped_error"` after both attempts, and should still be
  read as "deep scan didn't run," not folded into "nothing sensitive here"
  — checking the regex-side results is still necessary, which is why
  `deep_scan_status` is a separate field instead of silently merged into
  `detected_entities`.

## Roadmap (not built yet, sequenced by effort/value)

1. **Render.com fallback deploy** if Azure setup friction blocks a demo
   deadline — free tier confirmed live (512MB RAM/0.1 CPU, 750 hrs/mo, ~15min
   inactivity sleep). Tighter on RAM than Container Apps but zero Azure setup.
2. **LLM confidence validator** — a different use of the same `langextract`/
   Gemini pipeline already built for deep scan: re-score *existing*
   regex-based hits that land in the ambiguous 0.4–0.7 confidence band,
   instead of discovering new entity types. Not built yet — the deep-scan
   integration (see above) covers the new-category use case first.
3. **Real daily-rolling deep-scan quota** — the current `MAX_DEEP_SCAN_PER_KEY`
   cap in `app/pages.py` is a simple lifetime counter; a proper daily reset
   is worth building once actual usage patterns are observed.
4. **Company-name span boundaries** — the one remaining ORG/LOC issue
   (see Known Limitations) is a span-boundary problem investigated and
   explicitly not solved with regex, since Vietnamese has no reliable
   "end of proper name" delimiter under this file's case-insensitive
   matching. Not on the "next thing to build" list — the honest resolution
   path is a real Vietnamese ORG-name gazetteer or a small classifier, not
   a bigger regex, and that's a materially bigger lift than anything else
   in this file.

*(Done, not roadmap anymore: OCR — see "Known limitations"; deep-scan
retry — see "Deep scan" section above; underthesea's day-name and
administrative-prefix type confusion — see "Known limitations".)*

## Deployment (Azure Container Apps)

The `Dockerfile` itself is verified, not theoretical — re-verified after
every round of work in this README, most recently adding `tesseract-ocr` +
`tesseract-ocr-vie` (image now 944MB, up from 656MB before underthesea/
langextract/OCR): `docker build` → `docker run` → `/register` →
`/api/v1/scan` → `/api/v1/scan/file` (both a digital-text PDF and a
synthetic scanned one, OCR path included) → `deep_scan` all passed against
the current codebase inside the built container. What's untested is only
the Azure side — this environment has no `az` CLI session available to it,
and deployment is the one thing in this whole project that's genuinely
blocked on you, not on more building: it needs your interactive `az login`
and your Azure credit, neither of which can happen from here. This project
is currently scoped to local use, so this section is here for if/when that
changes, not an active next step. Commands to run yourself from the repo
root once logged in (`az login`):

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

Set `LANGEXTRACT_API_KEY` as a secret afterward if you want deep_scan to
actually work in production, not just report `skipped_no_key`:

```bash
az containerapp secret set --name sensen-api --resource-group sensen-rg \
  --secrets langextract-key=<your_key>
az containerapp update --name sensen-api --resource-group sensen-rg \
  --set-env-vars LANGEXTRACT_API_KEY=secretref:langextract-key
```

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
  extract.py          PDF/DOCX/TXT -> plain text; OCR fallback (Tesseract) for scanned PDFs
  recognizers/
    recognizers.yaml  <- the whole regex extensibility story lives here
static/index.html     Minimal demo console (paste text, see highlighted hits)
tests/                56 tests total: detection (positive/negative/ambiguous),
                      file-upload, OCR fallback, deep-scan (incl. retry),
                      VN phone/ID/NER fixes, auth
scripts/benchmark.py       Vanilla Presidio vs. this registry, speed + coverage
scripts/assess_corpus.py   Batch-scan a folder, rank documents by risk (the Assessment Report)
sample_corpus/         9 synthetic (fake-data) documents; full_coverage_demo.txt hits all 22
                      entity types (every custom category + all curated Presidio types +
                      VN NER + both deep-scan categories) in one file for manual testing
Dockerfile             Ready for Azure Container Apps / Render / any Docker host
```
