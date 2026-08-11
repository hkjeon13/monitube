"""Mandatory Korean MeCab and English NLTK noun extraction."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import unicodedata
from typing import Any

from konlpy.tag import Mecab
import nltk
from nltk import pos_tag
from nltk.tokenize import word_tokenize


ANALYZER_VERSION = "mecab-nltk-v1"
_KOREAN_NOUN_TAGS = frozenset({"NNG", "NNP"})
_ENGLISH_NOUN_TAGS = frozenset({"NN", "NNS", "NNP", "NNPS"})
_NLTK_RESOURCES = (
    "tokenizers/punkt_tab",
    "taggers/averaged_perceptron_tagger_eng",
)
_STOP_WORDS = frozenset(
    {
        "영상",
        "정말",
        "사람",
        "부분",
        "경우",
        "관련",
        "통해",
        "the",
        "and",
        "this",
        "that",
        "with",
        "from",
    }
)


class AnalyzerUnavailableError(RuntimeError):
    """Raised instead of silently switching to a lower-quality tokenizer."""


class MecabNltkNounAnalyzer:
    """Extract Korean and English nouns without a fallback analyzer."""

    version = ANALYZER_VERSION

    def __init__(self, *, dictionary_path: str | None = None) -> None:
        missing: list[str] = []
        for resource in _NLTK_RESOURCES:
            try:
                nltk.data.find(resource)
            except (LookupError, OSError):
                missing.append(resource)
        if missing:
            raise AnalyzerUnavailableError(
                "Required NLTK resources are missing: " + ", ".join(missing)
            )
        try:
            self._mecab = Mecab(dicpath=dictionary_path) if dictionary_path else Mecab()
        except Exception as exc:  # pragma: no cover - host installation boundary
            raise AnalyzerUnavailableError(
                "MeCab or the Korean dictionary could not be initialized"
            ) from exc

    @staticmethod
    def _normalized(term: str) -> str | None:
        value = unicodedata.normalize("NFC", term).strip().casefold()
        if len(value) < 2 or value in _STOP_WORDS or value.isdecimal():
            return None
        return value

    @staticmethod
    def _english_nouns(tokens: list[str]) -> list[str]:
        if not tokens:
            return []
        tagged = pos_tag(word_tokenize(" ".join(tokens)))
        return [word for word, tag in tagged if tag in _ENGLISH_NOUN_TAGS]

    def extract(self, text: str | None) -> list[str]:
        normalized_text = unicodedata.normalize(
            "NFC", (text or "").replace("\x00", " ")
        ).strip()
        if not normalized_text:
            return []
        try:
            tagged: list[tuple[str, str]] = self._mecab.pos(normalized_text)
        except Exception as exc:
            raise AnalyzerUnavailableError("MeCab analysis failed") from exc

        output: list[str] = []
        english_run: list[str] = []

        def flush_english() -> None:
            if not english_run:
                return
            for word in self._english_nouns(english_run):
                value = self._normalized(word)
                if value:
                    output.append(value)
            english_run.clear()

        for token, tag in tagged:
            if tag == "SL":
                english_run.append(token)
                continue
            flush_english()
            if tag in _KOREAN_NOUN_TAGS:
                value = self._normalized(token)
                if value:
                    output.append(value)
        flush_english()
        return output


@lru_cache(maxsize=1)
def get_noun_analyzer() -> MecabNltkNounAnalyzer:
    return MecabNltkNounAnalyzer()


def analyzer_health() -> dict[str, Any]:
    analyzer = get_noun_analyzer()
    fixture = analyzer.extract("영상 분석 OpenAI market")
    if fixture[:1] != ["분석"] or "openai" not in fixture:
        raise AnalyzerUnavailableError("MeCab+NLTK analyzer fixture did not match")
    return {
        "status": "ok",
        "version": analyzer.version,
        "fixtureHash": hashlib.sha256("\0".join(fixture).encode("utf-8")).hexdigest()[
            :16
        ],
    }
