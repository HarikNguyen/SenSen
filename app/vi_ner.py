"""Vietnamese-aware NER via underthesea — replaces SpacyRecognizer's role
(PERSON/ORGANIZATION/LOCATION) for Vietnamese content, since en_core_web_sm
has zero Vietnamese support (see app/recognizers/recognizers.yaml, where
SpacyRecognizer is disabled).

Registered programmatically in app/engine.py via registry.add_recognizer()
rather than YAML, since it isn't a regex/pattern recognizer.

Verified before writing this: ~109MB RSS to load (comparable to
en_core_web_sm, acceptable on the target low-RAM host), no network call
(model ships inside the pip package, confirmed — no ~/.cache download seen),
~0.7s per call. Real accuracy tradeoff found in testing, not hidden: PERSON
and real place names came back correct, but a company name ("Công ty TNHH
Thiên Tứ") got merged into a wrongly-tagged LOCATION span in one test
sentence — underthesea's ORG/LOC boundary isn't reliable. Base score (0.6)
reflects that mixed precision; not treated as a high-confidence category.

Second round of tuning after running this over sample_corpus/ (real
enterprise-style documents, not just clean prose): underthesea's model
treats ALL-CAPS headers, secret/key blobs and digit-bearing strings as
strong entity signals, so raw output on real documents tagged things like
"CHUYỂN GIAO HẠ TẦNG" (a section title), "BEGIN RSA PRIVATE KEY" and random
base64 fragments as PERSON/LOCATION. Real Vietnamese full names and place
names are consistently Title Case (each word capitalized, no digits); the
garbage above is not. `_looks_like_named_entity()` filters spans against
that shape before they're returned.

Third round, found by re-running the exact real contract PDF that started
this whole fix: some PDF exports drop the space glyph between certain word
pairs at the font/kerning level (confirmed via raw pymupdf word-box
inspection — the space is genuinely absent from the source file, not an
extraction bug). A fused run like "Trợlý" or "Chếđộlàm" is opaque to
underthesea's own tokenizer (it segments on whitespace first), so the whole
glued run got swallowed as one token and tagged on capitalization alone.
`_expand_fused_words()` repairs this *before* tagging: it walks each
whitespace-delimited token that isn't already a recognized dictionary word
and tries to split it into known Vietnamese syllables via a DP/Viterbi
segmentation (shortest path over -log(frequency) costs, syllable
frequencies counted from underthesea's own bundled Viet74K.txt — reused
rather than shipping a second dictionary). A token only gets rewritten if
the DP finds *full* coverage using 2+ recognized syllables; anything it
can't fully explain (secrets, IDs, real foreign words like "Backup") is left
untouched, so this can't corrupt content it doesn't understand. The
inserted spaces exist only in the copy fed to underthesea — final entity
offsets are mapped back through `_ExpansionMap` to the original text, so
`RecognizerResult` spans and text_val always reflect the real document.

Verified end-to-end against the actual hopdong.pdf that surfaced the
missing-space problem: recovers a full party name ("Trịnh SỹThành") that
was previously invisible entirely (its two syllables were glued and never
resolved to anything NER would tag as PERSON), and correctly bounds real
compound place names ("Phường Thủ Thiêm") that used to be split wrong.
Residual, not fixed by this: underthesea sometimes assigns PERSON to a
place name or LOCATION to a day-of-week ("Chủ Nhật") even with the right
span boundaries — a type-confusion issue, not a segmentation one. Also
residual: common single words that are capitalized only because they start
a sentence/bullet ("Được", "Xét", "Cục") can still pass the Title Case
filter — sentence-initial capitalization looking like a proper noun is a
generic, hard NER problem, not specific to the fused-word fix, and not
chased further here (see README known limitations).
"""

import logging
import math
import re
import string
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import underthesea
from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts
from underthesea import ner as underthesea_ner

logger = logging.getLogger("sensen.vi_ner")

SCORE = 0.6
TAG_TO_ENTITY = {
    "PER": "PERSON",
    "ORG": "ORGANIZATION",
    "LOC": "LOCATION",
}

_STRIP_CHARS = string.punctuation + "—–“”\"'"

# Reuses underthesea's own bundled word list as a syllable-frequency source
# instead of shipping a second Vietnamese dictionary — see module docstring.
_SYLLABLE_DICT_PATH = Path(underthesea.__file__).resolve().parent / "corpus" / "data" / "Viet74K.txt"
_MIN_SYLLABLE_LEN = 2  # excludes stray single-letter entries (not real VN syllables)
_MAX_SYLLABLE_LEN = 8  # longest real VN syllable with diacritics is well under this
_TOKEN_RE = re.compile(r"\S+|\s+")


def _looks_like_named_entity(span_text: str) -> bool:
    """Real Vietnamese names/places are Title Case with no digits; headers,
    secrets and figures aren't. See module docstring for how this was derived.
    """
    if any(ch.isdigit() for ch in span_text):
        return False
    words = [w.strip(_STRIP_CHARS) for w in span_text.split()]
    words = [w for w in words if w]
    if not words:
        return False
    return all(len(w) >= 2 and w.istitle() for w in words)


@lru_cache(maxsize=1)
def _load_syllable_freq() -> dict:
    """syllable -> occurrence count, derived by splitting every entry in
    underthesea's Viet74K.txt on whitespace/hyphens. That file is a general
    word/phrase list (not syllable frequencies), so this is a proxy: a
    syllable used in many different dictionary entries is common; used in
    few (or none) is rare or a name — good enough for a DP segmentation cost,
    not treated as a calibrated frequency.
    """
    freq: dict = {}
    try:
        with open(_SYLLABLE_DICT_PATH, encoding="utf-8") as f:
            for line in f:
                for tok in line.strip().replace("-", " ").split():
                    key = tok.lower()
                    if len(key) < _MIN_SYLLABLE_LEN:
                        continue
                    freq[key] = freq.get(key, 0) + 1
    except OSError:
        logger.warning(
            "vi_ner: could not load %s for word-fusion repair — proceeding "
            "without it (fused-word tokens will be left as-is)",
            _SYLLABLE_DICT_PATH,
            exc_info=True,
        )
    return freq


def _segment_fused_token(token: str, freq: dict, log_total: float) -> Optional[List[str]]:
    """DP/Viterbi split of `token` into known Vietnamese syllables. Returns
    None if no full-coverage segmentation into 2+ syllables exists — the
    caller then leaves the token untouched rather than forcing a bad split.
    """
    n = len(token)
    lower = token.lower()
    best_cost: List[Optional[float]] = [None] * (n + 1)
    best_cut = [0] * (n + 1)
    best_cost[0] = 0.0
    for i in range(1, n + 1):
        for j in range(max(0, i - _MAX_SYLLABLE_LEN), i):
            if best_cost[j] is None:
                continue
            count = freq.get(lower[j:i])
            if count is None:
                continue
            cost = best_cost[j] + (log_total - math.log(count))
            if best_cost[i] is None or cost < best_cost[i]:
                best_cost[i] = cost
                best_cut[i] = j
    if best_cost[n] is None:
        return None
    cuts = []
    i = n
    while i > 0:
        j = best_cut[i]
        cuts.append((j, i))
        i = j
    if len(cuts) < 2:
        return None
    cuts.reverse()
    return [token[j:i] for j, i in cuts]


def _expand_fused_words(text: str) -> tuple[str, List[int]]:
    """Return (expanded_text, mapping) where mapping[k] is the index in the
    original `text` that expanded_text[k] corresponds to, or -1 for a space
    this function inserted. Only rewrites tokens that aren't already a
    recognized whole word and that DP-segment fully into 2+ syllables —
    everything else (secrets, IDs, real single words) passes through
    unchanged. See module docstring for why this exists.
    """
    freq = _load_syllable_freq()
    if not freq:
        return text, list(range(len(text)))
    log_total = math.log(sum(freq.values()))

    chars: List[str] = []
    mapping: List[int] = []

    def _copy(chunk: str, start: int) -> None:
        for k, ch in enumerate(chunk):
            chars.append(ch)
            mapping.append(start + k)

    for m in _TOKEN_RE.finditer(text):
        chunk = m.group(0)
        start = m.start()
        if chunk[0].isspace():
            _copy(chunk, start)
            continue

        core = chunk.strip(_STRIP_CHARS)
        pieces = None
        if core and core.isalpha() and core.lower() not in freq:
            pieces = _segment_fused_token(core, freq, log_total)
        if not pieces:
            _copy(chunk, start)
            continue

        lead_len = chunk.index(core)
        _copy(chunk[:lead_len], start)
        orig_idx = start + lead_len
        for piece_idx, piece in enumerate(pieces):
            if piece_idx > 0:
                chars.append(" ")
                mapping.append(-1)
            _copy(piece, orig_idx)
            orig_idx += len(piece)
        _copy(chunk[lead_len + len(core) :], orig_idx)

    return "".join(chars), mapping


def _map_span_to_original(start: int, end: int, mapping: List[int]) -> Optional[tuple]:
    """Convert an [start, end) span in expanded-text space back to the
    original text, skipping over inserted spaces (mapping value -1).
    """
    i = start
    while i < end and mapping[i] == -1:
        i += 1
    if i >= end:
        return None
    j = end - 1
    while j >= i and mapping[j] == -1:
        j -= 1
    return mapping[i], mapping[j] + 1


class VietnameseNerRecognizer(EntityRecognizer):
    """Wraps underthesea.ner(), realigning its token-level BIO output to
    character offsets (underthesea gives no offsets itself, unlike
    langextract's char_interval) and merging consecutive B-/I- spans.
    """

    def __init__(self):
        super().__init__(
            supported_entities=list(set(TAG_TO_ENTITY.values())),
            supported_language="en",
            name="VietnameseNerRecognizer",
        )

    def load(self) -> None:
        pass

    def analyze(
        self, text: str, entities: List[str], nlp_artifacts: Optional[NlpArtifacts] = None
    ) -> List[RecognizerResult]:
        expanded_text, mapping = _expand_fused_words(text)

        try:
            tagged = underthesea_ner(expanded_text)
        except Exception:
            logger.warning("vi_ner: underthesea call failed", exc_info=True)
            return []

        spans = self._align_and_merge(expanded_text, tagged)
        results = []
        for start, end, entity_type in spans:
            if entity_type not in entities:
                continue
            if not _looks_like_named_entity(expanded_text[start:end]):
                continue
            mapped = _map_span_to_original(start, end, mapping)
            if mapped is None:
                continue
            orig_start, orig_end = mapped
            results.append(
                RecognizerResult(entity_type=entity_type, start=orig_start, end=orig_end, score=SCORE)
            )
        return results

    @staticmethod
    def _align_and_merge(text: str, tagged) -> List[tuple]:
        """(word, pos, chunk, bio_tag) tuples -> [(start, end, entity_type), ...].

        Searches forward from a cursor so repeated words align to their
        correct (not first) occurrence; skips a token if it can't be found
        (should be rare — underthesea tokens are substrings of the input).
        """
        spans = []
        cursor = 0
        current: Optional[list] = None  # [start, end, entity_type]

        for word, _pos, _chunk, tag in tagged:
            idx = text.find(word, cursor)
            if idx == -1:
                continue
            start, end = idx, idx + len(word)
            cursor = end

            bio, _, tag_type = tag.partition("-")
            entity_type = TAG_TO_ENTITY.get(tag_type)

            if bio == "B" and entity_type:
                if current:
                    spans.append(tuple(current))
                current = [start, end, entity_type]
            elif bio == "I" and current and current[2] == entity_type:
                current[1] = end
            else:
                if current:
                    spans.append(tuple(current))
                current = None

        if current:
            spans.append(tuple(current))
        return spans
