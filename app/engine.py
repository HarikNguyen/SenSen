"""Presidio engine construction — the only module that talks to Presidio directly.

Recognizers are configured entirely in app/recognizers/recognizers.yaml;
this file just wires spaCy + that registry into an AnalyzerEngine/AnonymizerEngine.
"""

from pathlib import Path

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_analyzer.nlp_engine import NlpEngine, NlpEngineProvider
from presidio_analyzer.recognizer_registry import RecognizerRegistryProvider
from presidio_anonymizer import AnonymizerEngine

from app.vi_ner import VietnameseNerRecognizer

RECOGNIZERS_CONF = Path(__file__).resolve().parent / "recognizers" / "recognizers.yaml"


def build_nlp_engine() -> NlpEngine:
    """Build the shared spaCy NLP engine (en_core_web_sm)."""
    return NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    ).create_engine()


def build_engines() -> tuple[AnalyzerEngine, AnonymizerEngine]:
    """Construct the analyzer/anonymizer once. Called from main.py's lifespan (singleton)."""
    nlp_engine = build_nlp_engine()

    registry = RecognizerRegistryProvider(
        conf_file=str(RECOGNIZERS_CONF), nlp_engine=nlp_engine
    ).create_recognizer_registry()

    # Not YAML-declarable (not a regex/pattern recognizer) — registered here
    # instead. Replaces SpacyRecognizer's PERSON/ORGANIZATION/LOCATION role
    # for Vietnamese content; SpacyRecognizer itself is disabled in the YAML.
    registry.add_recognizer(VietnameseNerRecognizer())

    # Widened to 8/8 (Presidio default: 5, prefix-only) — Vietnamese phrasing
    # often puts several filler words between a label and its value.
    context_enhancer = LemmaContextAwareEnhancer(
        context_prefix_count=8, context_suffix_count=8
    )

    analyzer = AnalyzerEngine(
        registry=registry,
        nlp_engine=nlp_engine,
        supported_languages=["en"],
        context_aware_enhancer=context_enhancer,
    )
    anonymizer = AnonymizerEngine()
    return analyzer, anonymizer
