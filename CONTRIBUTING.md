# Contributing to SenSen

This file is for anyone modifying this codebase — dev setup, conventions,
and a detailed engineering history of bugs found and fixed during
development. If you just want to *use* the API, read `README.md` instead;
none of what's in here affects current behavior.

## Development setup

Same as the README's Quickstart:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
sudo apt-get install -y tesseract-ocr tesseract-ocr-vie   # only if testing OCR
uvicorn app.main:app --reload
```

Run the test suite before and after any change:

```bash
pytest tests/ -v
```

124 tests as of this writing. A change that doesn't keep this green isn't
done.

## Code style

- Default to no comment. Add one only when the WHY is genuinely non-obvious
  (a hidden constraint, a workaround, an invariant a reader would need) —
  one line, two only if truly unavoidable. Never a multi-paragraph comment
  block or a docstring that reads like a development log.
- Don't reference the current task, a specific bug, or "found live" framing
  in code comments — that belongs here, in a PR description, or a commit
  message, not in the code itself (it rots as the codebase evolves).
- Extending a category is a YAML-only change (`app/recognizers/
  recognizers.yaml`) for anything regex-shaped — see README's "Adding a new
  category" section. Only reach for a real Python module (like
  `app/vi_ner.py` or `app/deep_scan.py`) when the detection genuinely can't
  be expressed as a pattern.

## Development history — bugs found and fixed

Everything below describes a bug that shipped fixed and tested. None of it
is a current limitation (see README's "Known limitations" for what's
actually still true today) — kept here because the debugging process and
root causes are useful context for anyone touching these modules again.

### Vietnamese NER alignment & scoring (`app/vi_ner.py`)

`app/vi_ner.py` (underthesea) replaces the disabled `SpacyRecognizer` for
PERSON/ORGANIZATION/LOCATION on Vietnamese content. Getting it from "0%
precision" to reliable took eight rounds of fixes against real documents,
not synthetic examples:

**Round 1 — baseline.** `en_core_web_sm`'s `SpacyRecognizer`, tested on a
real scanned contract: tagged ordinary phrase fragments ("một bên",
"Ông/Bà") as PERSON/ORG and misclassified a real phone number and national
ID as DATE_TIME — 0% precision. Replaced with underthesea for
PERSON/ORGANIZATION/LOCATION (`app/vi_ner.py`), ~109MB RAM, no network
call (model ships in the pip package).

**Round 2 — false-positive noise.** Running underthesea over the whole
`sample_corpus/`, not just a clean sentence, showed it reading ALL-CAPS
headers, secret/key blobs, and financial figures as strong entity
signals — section titles and RSA key markers came back tagged
PERSON/LOCATION. Fixed with a Title Case + no-digits filter
(`_looks_like_named_entity`): real Vietnamese names/places are always
Title Case, headers/secrets/figures aren't. Dropped corpus-wide detections
from 80 to 39 entities, false positives gone, real names/places intact.

**Round 3 — fused words.** Re-running the same real contract PDF: some
PDFs drop the space glyph between certain word pairs at the font/kerning
level (confirmed via raw `pymupdf` word-box inspection — the space is
genuinely absent from the source, not an extraction bug). A fused run like
"Trợlý" or "Chếđộlàm" was opaque to underthesea's tokenizer and got tagged
on capitalization alone. `_expand_fused_words()` repairs this before
tagging: DP-segments any non-dictionary token into known Vietnamese
syllables (frequencies reused from underthesea's own bundled
`Viet74K.txt`), only rewriting a token when the DP finds *full* coverage —
secrets/IDs/real foreign words are left untouched. Recovered a full party
name ("Trịnh SỹThành") that was previously invisible, and correctly
bounded real compound place names ("Phường Thủ Thiêm").

**Round 4 — confidence, not a hard gate.** The round-2 filter was a hard
keep/reject gate, and every kept result got a flat `0.6` — this recognizer
ignored the caller's `confidence_threshold` entirely, unlike every other
category. Tried fixing remaining single-word noise ("Được", "Xét", "Cục")
via raw syllable frequency — didn't work ("cục"=61 sits right next to real
name syllables like "thủ"=143). Replaced the hard gate with
`_score_entity()`: dictionary membership + a multi-word bonus (a real name
is almost always 2+ words) + a sentence/bullet-initial penalty. Real
names/places landed at `0.65`, false positives at `0.15`–`0.25` — and now
respects `confidence_threshold` like every other category.

**Round 5 — two more type-confusion fixes.** A small, fully-enumerable
gazetteer correction after BIO merge: Vietnamese day names (`Thứ
Hai`...`Chủ Nhật`, a closed set of 7) are rejected outright — a day name
is never itself PII — fixing "Chủ Nhật"/"Thứ Bảy" tagged LOCATION.
Administrative-unit prefixes (`Phường`, `Quận`, `Huyện`, `Tỉnh`, `Xã`,
`Thị trấn`, `Thành phố`) as a span's first word force-correct the type to
LOCATION without touching the boundary, fixing "Phường Thủ Thiêm" tagged
PERSON.

A third type-confusion case (a company name truncated to its last 1-2
words, e.g. "Toàn Cầu" from "Công ty Cổ phần Đầu Tư Toàn Cầu", tagged
LOCATION) turned out to be a span-boundary problem, not a type problem —
the gazetteer approach doesn't apply. Two regex attempts were tried and
rejected: case-insensitive matching over-matched into unrelated trailing
text on 4 of 5 real test sentences; a case-sensitive `PatternRecognizer`
(registered in Python with its own `global_regex_flags`, since Presidio's
YAML schema only exposes one global flag for the whole file) got 4 of 5
exactly right but still swallowed one extra word on the 5th, since the
following label is also capitalized in Vietnamese with no delimiter. The
shipped fix is different: deep scan's `ORGANIZATION` extraction class
(opt-in, LLM-based — no "no reliable delimiter" problem for an LLM) got 5
of 5 right on the same test sentences. This only fixes it when
`deep_scan=true`; the free/default path keeps this limitation (see
README's Known Limitations).

First version of the deep-scan fix showed both results side by side when
`deep_scan=true` — the correct `ORGANIZATION` and the free path's wrong
`LOCATION` for the same company. `_drop_regex_ner_entities_overlapped_by_
deep_scan()` in `app/scanning.py` now drops the free-path entity on
overlap (not exact-span dedup, since the spans genuinely differ in
width). Deliberately scoped to value types only —
`HR_SENSITIVE_CONTENT`/`IP_TRADE_SECRET_CONTENT` flag a whole sentence and
routinely overlap unrelated entities mentioned inside it, so those two
never trigger the drop.

**Round 6 — newline-fused tokens.** Testing a two-column-layout document
(requested explicitly as a "hard" scenario) surfaced a bug that also
explained a round-3 case. underthesea's tokenizer doesn't treat `"\n"` as
a boundary, so it sometimes fuses a real name with the next line's label
word into one token (`"Trần Thị Hoa"` + the next line's `"Số"` from `"Số
CCCD:"`). The fused token then failed exact-substring lookup (the source
has a newline where the token has a plain space), silently dropping the
*entire* entity. Fixed in two parts: `_find_token()` falls back to
whitespace-flexible matching when the exact substring isn't found, and
`_split_on_newlines()` scores each newline-delimited piece of a recovered
span independently. This surfaced a second, older bug:
`_is_sentence_initial` checked `text[:start].rstrip()[-1]`, silently
stripping the newline itself before checking it, so a bare line break
with no punctuation before it was never recognized as sentence-initial.
Fixed by walking back over whitespace to find the actual boundary instead
of `.rstrip()`-ing it away.

**Round 7 — unbounded cursor search.** Found against a real 9-page scanned
bank-guarantee letter (OCR'd via the cloud-OCR path), the real explanation
for a "way too few entities detected" report. `_align_and_merge` maps
underthesea's token list back to character offsets by searching forward
from a cursor — but the search was **unbounded**: `text.find(word,
cursor)` could match anywhere in the rest of the document. "Kiên Giang"
(a province name, repeated many times) appeared once split across a
newline, which the exact match couldn't find at its true position — so
the unbounded search matched a completely different, unrelated later
mention hundreds of characters away instead, permanently desyncing every
token search after it for the rest of the page. Confirmed by direct trace:
one token jumped 1,043 chars to a decoy instead of the true ~903, then a
396-char jump on the very next token. Fixed with `_MAX_TOKEN_LOOKAHEAD =
100` — real token-to-token gaps on this document were 0-2 chars, so 100 is
generous for any legitimate gap while far too small to reach a same-word
occurrence in an unrelated later paragraph. Entity count on the affected
page went from 15 to 29 at `score_threshold=0.0`.

**Round 8 — the hardest one: a gap in underthesea's own output.** Even
with round 7 shipped, a real bank-guarantee PDF still lost a real name
("Nguyễn Thị Mĩnh") after cloud OCR — found by re-testing against the
full ~24,500-character document, not a short reconstruction (which scored
the name correctly in isolation — that mismatch is what first pointed at
"something about the *real*, full-length output is different"). Root
cause, confirmed by diffing underthesea's raw tagged output against the
source: a long dot-leader blank-fill line (common in VN official forms)
made underthesea's own tagger silently skip tokenizing an entire
**~1,200-character** stretch of real text right after it. Not an
alignment bug at all — the gap was already in underthesea's own output
before any of this module's code ran.

First attempt: teach `_find_token` to detect a "stuck" cursor and escalate
to a wider rescue window. This worked for the motivating case but needed
an increasingly complicated pile of heuristics to stop the widened search
from landing on a wrong coincidental match — a bare short token (a lone
`"."`, or `"Điều"`) is essentially guaranteed to "find" *some* occurrence
in a several-thousand-character window, and picking the wrong one
cascaded into more wrong picks all the way to the end of the document. A
word-length + frequency filter fixed that case but a bare `"2016"` (not a
dictionary syllable) reproduced the same cascade. Abandoned as
fundamentally fragile rather than adding a fourth heuristic to patch the
third.

Shipped fix instead: remove the confusing input *before* underthesea ever
sees it. `_collapse_repeated_punctuation()` collapses any run of 4+
repeated identical non-word, non-whitespace characters down to 3, applied
after `_expand_fused_words()` but before `underthesea_ner()`;
`_compose_mappings()` chains the two transformations' offset maps back to
the original text. Entity count on the affected document: 36 → 73
(`score_threshold=0.0`), including three real people's names that were
silently missing the same way.

### Deep scan reliability (`app/deep_scan.py`)

**Schema-validation race.** `langextract` 1.6.0's Gemini provider
intermittently raised a pydantic `ValidationError` building its
`response_schema` ("Extra inputs are not permitted"). The exact same call
(same text/model/key) returned `skipped_error` once and `"ok"` on an
immediate retry — a genuine race in the pinned dependency, not a version
mismatch fixable by bumping (both packages were already at latest).
`run_deep_scan` retries instantly on this failure mode before reporting
`skipped_error`.

**RPM limit surviving langextract's own retry.** A genuine Gemini RPM
429, found live with a real free-tier key, surfaced as
`InferenceRuntimeError` wrapping a `google.genai.errors.APIError(429)` in
`.original` — langextract's own provider already retries transient errors
internally before raising this. An instant retry does nothing for a
per-minute quota, so this case gets a real backoff sleep via
`app/retry.py`'s `call_with_backoff`, unwrapping `.original` first.

**Throttle-granularity bug (the real fix).** The RPM 429 kept happening
even with `gemini_limiter.acquire()` called before every `lx.extract()`
attempt. Root cause: `lx.extract()` splits a long document into several
chunks (`max_char_buffer`, 1000 chars by default) and fires up to
`max_workers` (10 by default) of them at Gemini **concurrently** — a
single `acquire()` before the outer call only throttled that outer call,
not the real per-chunk HTTP requests underneath it. Confirmed directly: a
real 7200-char `lx.extract()` call made 9 separate chunk requests, not 1.
Fixed by patching `GeminiLanguageModel._process_single_prompt` (the one
place every actual chunk request funnels through) to call
`gemini_limiter.acquire()` itself, once per real HTTP call.

### Cloud OCR JSON reliability (`app/ocr_api.py`)

Asking Gemini for bounding-box JSON via a plain-text prompt turned out not
to be robust enough — three separate failure shapes surfaced in
production before this was fixed at the root:

1. `json.loads` in strict mode rejects a raw, un-escaped control character
   inside a string — Gemini's `"text"` values routinely contained a
   literal newline where the spec requires `\n`, failing the whole page's
   redaction with `Invalid control character at: line N column M`.
2. Gemini didn't reliably use the exact key names the prompt asked for.
   Repeated identical calls on the same image showed it substituting
   `"label"` for `"text"` (inconsistently, even within one call's item
   array) and/or `"box"` for `"box_2d"` (consistent within one call, but
   flipping call to call) — both silently produced 0 usable words.
3. A third shape surfaced on a real user document: `Expecting ','
   delimiter: line N column M` (most likely an unescaped quote inside a
   "text" value).

Root-cause fix: Gemini's structured-output feature (`response_schema`),
not more prompt engineering or another parser patch.
`_ocr_words_gemini` now passes `response_schema=list[_GeminiBboxItem]` (a
Pydantic model, `{text: str, box_2d: list[float]}`) alongside
`response_mime_type="application/json"` — this constrains the model's
actual token-level decoding to the declared shape, so a field can't be
renamed and string values come out correctly escaped by construction, not
free text that merely resembles JSON. Confirmed live: repeated calls with
the schema all used exactly `["text", "box_2d"]`; three earlier calls
*without* the schema had all three drifted. `_parse_bbox_json`'s
`strict=False` and `_items_to_words`' text/label + box_2d/box/bbox
fallbacks are kept as defense in depth, and because OpenAI/Grok still go
through prompt-only JSON, not this schema mechanism.

### PDF redaction bloat (`app/redact.py`)

The first working version's `doc.tobytes()` call (no arguments) produced
an 11MB+ file for a single tiny test page that should have been ~25KB — a
redacted scanned page's freshly-embedded image came out uncompressed, and
the deleted original page's data wasn't garbage-collected. Fixed by
calling `doc.tobytes(garbage=4, deflate=True, clean=True)` instead — a
no-op cost for digital-text pages (they never embed a new image).

### Adversarial file-upload testing (`app/extract.py`)

Explicit request to test worst-case/hostile inputs, not just happy-path
ones, found and fixed two real crash bugs: a corrupted or empty `.pdf`
and a corrupted `.docx` both crashed with an unhandled 500 instead of the
documented 422. `pymupdf.open()` raises `FileDataError` (its
`EmptyFileError` subclass covers the 0-byte case) for a malformed PDF, and
`python-docx`'s `Document()` doesn't guarantee one exception type for a
malformed `.docx` (confirmed empirically: `zipfile.BadZipFile` for a
non-zip file, a plain `KeyError` for a valid zip that isn't a real docx
package) — neither was caught before, both are now.

Other worst-case scenarios tested and found already handled correctly, no
fix needed: a 21-page scanned PDF (over `MAX_OCR_PAGES`), text at and
above `MAX_TEXT_LENGTH`, a 25MB upload, a missing filename, a 50,000-char
unbroken "word" fed to the DP word-segmentation repair (~1.5s, no
blowup), and the `FINANCIAL_CREDENTIAL` pattern's lazy quantifier against
a crafted no-match input (bounded by its own `{0,60}` cap, no catastrophic
backtracking).

A follow-up review pass caught two more before they shipped further: (1)
`_extract_pdf` checked `text.strip()` over the whole joined document, so a
PDF mixing digital-text pages with a scanned page (e.g. a typed contract
with a scanned signature page) was treated as fully "has a text layer"
and the scanned page's content was silently dropped, no OCR attempt —
fixed to check per page. (2) `_ocr_page` only caught
`TesseractNotFoundError`; a present-but-broken install
(`tesseract-ocr` without `tesseract-ocr-vie`) raised
`pytesseract.TesseractError` instead, propagating as an unhandled 500 —
added the missing except branch.

### OCR degraded-scan tuning (`app/extract.py`)

Pushed to test something closer to a real bad document than synthetic
garbage bytes: a test PDF simulating a photocopy/photo of a contract
(Gaussian blur + 2.5° rotation + photocopy-style speckle noise), each
factor tested isolated and combined. Blur and rotation alone didn't hurt
accuracy at all (6/6 entities correct either way). Speckle noise alone
was the actual culprit — dropped detection to 0/6 (the CCCD number got
OCR'd with spurious internal spaces breaking its 12-digit pattern, real
names became unrecoverable garbage, a phone number was invented from
noise). Fix: a median filter in `_ocr_page` before handing the image to
Tesseract. A first attempt (3x3 kernel) barely helped once measured
through the real `dpi=200` render this function uses (an earlier
promising-looking check had used a lower, non-representative resolution
by mistake). At 200 DPI each noise pixel gets upsampled into a
multi-pixel blob, too big for 3x3; a 5x5 kernel fixed the noise-only case
but still lost entities combined with blur+rotate; a 7x7 kernel recovers
that too (6/6, matching the clean-scan baseline).

### Same-span category duplicates (`app/scanning.py`)

Found via the full-coverage test doc: Presidio's built-in multi-region
`PhoneRecognizer` also matched a CIDR block, a VN national ID, and a VN
tax code — each under some *other* country's phone-number shape
(`DE`/`IN`/`BR`), always at a lower score than the correct category.
Narrowing `PhoneRecognizer`'s `supported_regions` was considered and
rejected — this product needs broad phone detection, not just VN/US, so
trading away region coverage to fix a scoring artifact was the wrong
tradeoff. Fixed at the application layer instead:
`_drop_lower_scored_exact_duplicates()` keeps only the highest-scoring
result when two categories match the *exact same* `[start, end)` span —
the text can't genuinely be two different identifier types at once. All 8
phone regions stay fully active.
