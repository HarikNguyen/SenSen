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

Second round of tuning after running this over app/../sample_corpus/ (real
enterprise-style documents, not just clean prose): underthesea's model
treats ALL-CAPS headers, secret/key blobs and digit-bearing strings as
strong entity signals, so raw output on real documents tagged things like
"CHUYỂN GIAO HẠ TẦNG" (a section title), "BEGIN RSA PRIVATE KEY" and random
base64 fragments as PERSON/LOCATION. Real Vietnamese full names and place
names are consistently Title Case (each word capitalized, no digits); the
garbage above is not. `_looks_like_named_entity()` below filters spans
against that shape before they're returned — verified against every
document in sample_corpus/ that this removes effectively all of the
header/secret/figure noise while keeping true hits (personal names, "Việt
Nam") intact. Known residual gap: a single capitalized non-Vietnamese word
("Backup") can still slip through — rare and low-severity, not worth a
special case.
"""

import logging
import string
from typing import List, Optional

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
        try:
            tagged = underthesea_ner(text)
        except Exception:
            logger.warning("vi_ner: underthesea call failed", exc_info=True)
            return []

        spans = self._align_and_merge(text, tagged)
        return [
            RecognizerResult(entity_type=entity_type, start=start, end=end, score=SCORE)
            for start, end, entity_type in spans
            if entity_type in entities and _looks_like_named_entity(text[start:end])
        ]

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
