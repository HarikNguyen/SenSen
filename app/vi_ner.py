"""Vietnamese-aware NER via underthesea -- replaces SpacyRecognizer's
PERSON/ORGANIZATION/LOCATION for Vietnamese (SpacyRecognizer disabled in
recognizers.yaml; en_core_web_sm has no VN support). Registered
programmatically in app/engine.py, not YAML.

Raw underthesea output needs cleanup before use: `_score_entity()` grades
matches instead of an all-or-nothing gate; `_expand_fused_words()` DP-
segments space-fused tokens ("Trợlý"); `_collapse_repeated_punctuation()`
shrinks dot-leader runs that otherwise stall tokenization;
`_corrected_entity_type()` fixes known type-confusion cases. Both text
transforms carry an offset mapping back to the original text so spans
stay correct.
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

TAG_TO_ENTITY = {
    "PER": "PERSON",
    "ORG": "ORGANIZATION",
    "LOC": "LOCATION",
}

_STRIP_CHARS = string.punctuation + "—–“”\"'"
# ":" excluded on purpose -- "Ông/Bà: Nguyễn Xuân Hùng" is a common place a
# real name appears, so treating it as a sentence boundary hurt precision.
_SENTENCE_BOUNDARY_CHARS = ".-—–"

# Reuses underthesea's own bundled word list instead of a second dictionary.
_SYLLABLE_DICT_PATH = Path(underthesea.__file__).resolve().parent / "corpus" / "data" / "Viet74K.txt"
_MIN_SYLLABLE_LEN = 2  # excludes stray single-letter entries (not real VN syllables)
_MAX_SYLLABLE_LEN = 8  # longest real VN syllable with diacritics is well under this
_TOKEN_RE = re.compile(r"\S+|\s+")

_BASE_SCORE = 0.5
_MULTI_WORD_BONUS = 0.15
_SINGLE_WORD_COMMON_WORD_PENALTY = 0.25
_SENTENCE_INITIAL_PENALTY = 0.1

# Vietnamese day names, a closed set of 7 -- never PII, so this is a flat
# rejection (score 0), not a type correction.
_CALENDAR_TERMS = {
    "thứ hai", "thứ ba", "thứ tư", "thứ năm", "thứ sáu", "thứ bảy", "chủ nhật",
}

# Reliable LOCATION signal when underthesea gets the boundary right but the type wrong.
_ADMIN_UNIT_PREFIXES = {
    "phường", "quận", "huyện", "tỉnh", "xã", "thị trấn", "thành phố",
}
# Two-word prefixes ("thị trấn") need the longest-leading-group match tried
# first, or a single-word check never matches them.
_ADMIN_UNIT_PREFIX_MAX_WORDS = max(len(p.split()) for p in _ADMIN_UNIT_PREFIXES)


def _normalized_words(span_text: str) -> List[str]:
    words = [w.strip(_STRIP_CHARS) for w in span_text.split()]
    return [w for w in words if w]


def _corrected_entity_type(span_text: str, entity_type: str) -> str:
    words = _normalized_words(span_text)
    for n in range(min(_ADMIN_UNIT_PREFIX_MAX_WORDS, len(words)), 0, -1):
        if " ".join(w.lower() for w in words[:n]) in _ADMIN_UNIT_PREFIXES:
            return "LOCATION"
    return entity_type


def _is_sentence_initial(text: str, start: int) -> bool:
    """True if `start` begins a new sentence/line/bullet."""
    i = start
    saw_newline = False
    while i > 0 and text[i - 1].isspace():
        if text[i - 1] == "\n":
            saw_newline = True
        i -= 1
    if saw_newline or i == 0:
        return True
    return text[i - 1] in _SENTENCE_BOUNDARY_CHARS


_MAX_TOKEN_LOOKAHEAD = 100
# Unbounded search can match a wrong distant occurrence and desync every later token.


def _find_token(text: str, word: str, cursor: int) -> Optional[tuple]:
    """Exact match first, then spaces-as-whitespace-run (underthesea
    sometimes returns a space where the source had a newline)."""
    window_end = cursor + _MAX_TOKEN_LOOKAHEAD
    idx = text.find(word, cursor, window_end)
    if idx != -1:
        return idx, idx + len(word)
    if " " in word:
        pattern = re.escape(word).replace(r"\ ", r"\s+")
        m = re.compile(pattern).search(text, cursor, window_end)
        if m:
            return m.start(), m.end()
    return None


def _split_on_newlines(text: str, start: int, end: int) -> List[tuple]:
    """Yield (sub_start, sub_end) for each newline-delimited piece of text[start:end]."""
    pieces = []
    chunk_start = start
    for i in range(start, end):
        if text[i] == "\n":
            if i > chunk_start:
                pieces.append((chunk_start, i))
            chunk_start = i + 1
    if end > chunk_start:
        pieces.append((chunk_start, end))
    return pieces


def _score_entity(span_text: str, sentence_initial: bool, freq: dict) -> float:
    """Graduated confidence instead of a hard keep/reject gate."""
    if any(ch.isdigit() for ch in span_text):
        return 0.0
    words = _normalized_words(span_text)
    if not words or not all(len(w) >= 2 and w.istitle() for w in words):
        return 0.0  # not ambiguous: headers/secrets/figures aren't names at all
    if " ".join(w.lower() for w in words) in _CALENDAR_TERMS:
        return 0.0  # a day name is never itself PII

    score = _BASE_SCORE
    if len(words) >= 2:
        score += _MULTI_WORD_BONUS
    elif words[0].lower() in freq:
        score -= _SINGLE_WORD_COMMON_WORD_PENALTY
    if sentence_initial:
        score -= _SENTENCE_INITIAL_PENALTY
    return max(0.0, min(1.0, score))


@lru_cache(maxsize=1)
def _load_syllable_freq() -> dict:
    """syllable -> occurrence count across underthesea's Viet74K.txt --
    a proxy frequency, good enough for DP segmentation cost."""
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
    """DP/Viterbi split into known syllables; None if no full-coverage
    2+ syllable segmentation exists."""
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
    """(expanded_text, mapping); mapping[k] is expanded_text[k]'s index in
    `text`, or -1 for an inserted space. Only rewrites tokens that fully
    DP-segment into 2+ syllables -- everything else passes through unchanged."""
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


_REPEATED_PUNCT_MIN_RUN = 4
_REPEATED_PUNCT_KEEP = 3
# A long repeated-punctuation run (e.g. a dot-leader blank-fill line in VN
# forms) makes underthesea stop tokenizing everything after it -- shrink first.


def _collapse_repeated_punctuation(text: str) -> tuple[str, List[int]]:
    """Same mapping contract as _expand_fused_words, but never inserts
    (no -1 entries -- only removes characters)."""
    chars: List[str] = []
    mapping: List[int] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isalnum() or ch.isspace():
            chars.append(ch)
            mapping.append(i)
            i += 1
            continue
        j = i
        while j < n and text[j] == ch:
            j += 1
        run_len = j - i
        keep = _REPEATED_PUNCT_KEEP if run_len >= _REPEATED_PUNCT_MIN_RUN else run_len
        for k in range(keep):
            chars.append(ch)
            mapping.append(i + k)
        i = j
    return "".join(chars), mapping


def _compose_mappings(outer: List[int], inner: List[int]) -> List[int]:
    """Chains outer (indexes into `inner`'s text) with inner into one
    mapping back to the original text, propagating -1 through either stage."""
    composed = []
    for idx in outer:
        composed.append(-1 if idx == -1 or inner[idx] == -1 else inner[idx])
    return composed


def _map_span_to_original(start: int, end: int, mapping: List[int]) -> Optional[tuple]:
    """[start, end) in expanded-text space -> original text, skipping -1 entries."""
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
    character offsets (it gives none itself) and merging B-/I- spans."""

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
        expanded_text, expand_mapping = _expand_fused_words(text)
        tag_text, collapse_mapping = _collapse_repeated_punctuation(expanded_text)
        mapping = _compose_mappings(collapse_mapping, expand_mapping)

        try:
            tagged = underthesea_ner(tag_text)
        except Exception:
            logger.warning("vi_ner: underthesea call failed", exc_info=True)
            return []

        freq = _load_syllable_freq()
        spans = self._align_and_merge(tag_text, tagged)
        results = []
        for start, end, raw_entity_type in spans:
            # correct type before filtering, so a mistyped LOCATION still surfaces
            entity_type = _corrected_entity_type(tag_text[start:end], raw_entity_type)
            if entity_type not in entities:
                continue
            # underthesea ignores "\n" as a boundary, fusing a name with the next line
            for sub_start, sub_end in _split_on_newlines(tag_text, start, end):
                sentence_initial = _is_sentence_initial(tag_text, sub_start)
                score = _score_entity(tag_text[sub_start:sub_end], sentence_initial, freq)
                if score <= 0.0:
                    continue
                mapped = _map_span_to_original(sub_start, sub_end, mapping)
                if mapped is None:
                    continue
                orig_start, orig_end = mapped
                results.append(
                    RecognizerResult(
                        entity_type=entity_type, start=orig_start, end=orig_end, score=score
                    )
                )
        return results

    @staticmethod
    def _align_and_merge(text: str, tagged) -> List[tuple]:
        """(word, pos, chunk, bio_tag) tuples -> [(start, end, entity_type), ...].
        Cursor moves forward so repeated words align to the right occurrence."""
        spans = []
        cursor = 0
        current: Optional[list] = None  # [start, end, entity_type]

        for word, _pos, _chunk, tag in tagged:
            found = _find_token(text, word, cursor)
            if found is None:
                continue
            start, end = found
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
