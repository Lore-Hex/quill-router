from pathlib import Path

import pytest

from scripts.pricing import base
from scripts.pricing.providers import deepseek


def test_morph_prices_1017_independently_of_neighbouring_models() -> None:
    from scripts.pricing.parsers import morph

    html = '<table><tr><td>morph-dsv4flash</td><td>$0.14/1M</td><td>$0.036/1M</td><td>$0.40/1M</td></tr><tr><td>morph-dsv41flash-1017</td><td>$0.12/1M</td><td>$0.008/1M</td><td>$0.80/1M</td></tr></table>'
    for source in (html, base.normalize_parser_input(html)):
        parsed = morph.parse(source)
        assert parsed["deepseek/deepseek-v4.1-flash-1017"] == {
            "prompt_micro_per_m": 120000, "completion_micro_per_m": 800000,
            "prompt_cached_micro_per_m": 8000,
        }
        assert parsed["deepseek/deepseek-v4-flash"]["completion_micro_per_m"] == 400000


@pytest.mark.parametrize("hint", ["deepseek/deepseek-v4.1-flash-1017", "deepseek/unannounced-future"])
def test_aggregator_version_hint_cannot_rewrite_direct_parser(
    monkeypatch: pytest.MonkeyPatch, hint: str,
) -> None:
    fixture = Path(__file__).parent / "fixtures/pricing/deepseek_pricing_2026-09-10.html"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(base, "fetch_html", lambda *a, **k: fixture.read_text())
    monkeypatch.setattr(base, "self_heal_parser", lambda **k: pytest.fail("unexpected parser rewrite"))
    monkeypatch.setattr(deepseek, "fetch_json", lambda *a, **k: {"data": [{
        "id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash",
        "context_window": 1048576, "max_output_tokens": 393216,
        "input_modalities": ["text", "image"], "output_modalities": ["text"],
    }]})
    base.configure_runtime_required_models({"deepseek": {hint}})
    try:
        result = deepseek.fetch()
    finally:
        base.configure_runtime_required_models({})
    assert result.source == "api"
    assert hint not in result.prices
    assert hint not in deepseek._DISCOVERED_MANIFEST_ROWS
    row = deepseek._DISCOVERED_MANIFEST_ROWS["deepseek/deepseek-flash"]
    assert row["upstream_id"] == "deepseek-flash"
    assert row["context_length"] == 1048576
    assert row["max_output_tokens"] == 393216
    assert row["input_modalities"] == ["text", "image"]
    assert result.prices["deepseek/deepseek-flash"].completion_micro_per_m == 600000
