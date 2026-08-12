import json
from pathlib import Path

from monitube_api.nlp.analyzer import MecabNltkNounAnalyzer as LegacyAnalyzer
from monitube_tokenizer.analyzer import MecabNltkNounAnalyzer as ServiceAnalyzer


def test_service_analyzer_matches_legacy_and_golden_corpus() -> None:
    fixtures = json.loads(
        (Path(__file__).with_name("golden_tokens.json")).read_text(encoding="utf-8")
    )
    legacy = LegacyAnalyzer()
    service = ServiceAnalyzer()

    for fixture in fixtures:
        expected = fixture["tokens"]
        assert legacy.extract(fixture["text"]) == expected
        assert service.extract(fixture["text"]) == expected
