from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import refresh
from scripts.pricing.providers import _direct_openai, meta


def pricing_html() -> str:
    return """<h3>Standard tier</h3><p>Models: muse-spark-1.1, muse-spark-1.2, muse-spark-1.3</p>
    <table><tr><th>Usage</th><th>Price per 1M tokens</th></tr>
    <tr><td>Cached input</td><td>$0.15</td></tr>
    <tr><td>Input</td><td>$1.25</td></tr><tr><td>Output</td><td>$4.25</td></tr></table>
    <h3>Contributor tier</h3><p>muse-spark-1.3-contributor</p>
    <table><tr><td>Input</td><td>$0.10</td></tr><tr><td>Output</td><td>$0.20</td></tr></table>"""


def test_meta_prices_are_first_party_standard_rates() -> None:
    prices = meta._parse_prices(pricing_html())
    assert set(prices) == set(meta.EXPECTED_MODELS)
    for price in prices.values():
        assert price.prompt_micro_per_m == 1_250_000
        assert price.completion_micro_per_m == 4_250_000
        assert price.tiers[0].prompt_cached_micro_per_m == 150_000


@pytest.mark.parametrize("old,new", [
    ("Standard tier", "Other tier"), ("Price per 1M tokens", "Price per token"),
    ("$1.25", "$0"), ("$1.25", "$NaN"), ("$1.25", "$-1"),
    ("$1.25", "$1.0000001"), ("$0.15", "$2.00"),
    ("muse-spark-1.2,", ""), ("muse-spark-1.1,", "muse-spark-1.1-contributor,"),
    ("<tr><td>Output</td><td>$4.25</td></tr>", ""),
])
def test_meta_price_changes_fail_closed(old: str, new: str) -> None:
    with pytest.raises(RuntimeError):
        meta._parse_prices(pricing_html().replace(old, new))


def test_duplicate_standard_section_fails_closed() -> None:
    with pytest.raises(RuntimeError):
        meta._parse_prices(pricing_html() * 2)


@pytest.mark.parametrize("healthy", [True, False])
def test_direct_discovery_requires_usage_and_never_admits_contributor_or_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, healthy: bool,
) -> None:
    catalog = _direct_openai.DirectOpenAIProvider(meta.CATALOG.spec, manifest_path=tmp_path / "meta.json")
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-use")
    rows = [{"id": native} for native in meta.EXPLICIT_MODEL_MAP]
    rows.extend({"id": native} for native in ("muse-spark-1.3-contributor", "muse-image-1.0", "sam-3.1", "muse-voice-transcribe-1.0", "muse-spark-9.9"))
    calls: list[dict[str, Any]] = []

    def fetch(url: str, **kwargs: Any) -> dict[str, Any]:
        assert url == "https://api.meta.ai/v1/models"
        assert kwargs["extra_headers"] == {"Authorization": "Bearer meta-test-key"}
        return {"data": rows}

    def canary(**kwargs: Any) -> bool:
        calls.append(kwargs)
        assert kwargs["base_url"] == meta.BASE_URL
        assert kwargs["api_key"] == "meta-test-key"
        assert kwargs["require_usage"] and kwargs["require_message"]
        assert kwargs["expected_content"] == "PONG"
        assert kwargs["max_tokens"] == 512
        assert kwargs["extra_body"] == {"reasoning_effort": "minimal"}
        return healthy

    monkeypatch.setattr(_direct_openai, "fetch_json", fetch)
    monkeypatch.setattr(meta, "fetch_html", lambda _url: pricing_html())
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", canary)
    result = catalog.fetch()
    catalog.write_provider_manifest(result)
    data = json.loads(catalog.manifest_path.read_text())
    assert {row["id"] for row in data["models"]} == set(meta.EXPECTED_MODELS)
    assert len(calls) == 3
    for row in data["models"]:
        assert row["upstream_id"] in meta.EXPLICIT_MODEL_MAP
        assert row["routable"] is healthy
        assert row["context_length"] == 1_048_576
        assert row["input_modalities"] == ["text", "image"]


def test_openrouter_key_cannot_replace_missing_meta_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-use")
    with pytest.raises(RuntimeError, match="META_API_KEY"):
        meta.fetch()


def test_meta_not_classified_as_openrouter_transport() -> None:
    assert "meta" not in refresh._OPENROUTER_BACKED_PROVIDER_SLUGS
    assert "openrouter" in refresh._OPENROUTER_BACKED_PROVIDER_SLUGS
