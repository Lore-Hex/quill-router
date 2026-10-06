"""Mistral provider-pricing source contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers.mistral import parse
from scripts.pricing.providers import mistral
from tests.pinned_manifests import build_manifest_rows
from trusted_router.catalog import model_open_weights


def test_mistral_uses_dedicated_api_pricing_page() -> None:
    """The former API page redirects to the new docs pricing tables."""
    assert mistral.URL == "https://docs.mistral.ai/inference/pricing"


def test_mistral_parser_reads_server_rendered_api_price_cards() -> None:
    html = """
    <nav>
      <p>Mistral Medium 3.5</p>
      <p>Mistral Small 4</p>
    </nav>
    <article>
      <p class="text-h5 font-mistral">Mistral Medium 3.5</p>
      <div>
        <p>Input (/M tokens)</p>
        <mistral-atom-text-price><span>$1.5</span></mistral-atom-text-price>
      </div>
      <div>
        <p>Output (/M tokens)</p>
        <mistral-atom-text-price><span>$7.5</span></mistral-atom-text-price>
      </div>
      <code>mistral-medium-latest</code>
    </article>
    <article>
      <p class="text-h5 font-mistral">Mistral Small 4</p>
      <div>
        <p>Input (/M tokens)</p>
        <mistral-atom-text-price><span>$0.15</span></mistral-atom-text-price>
      </div>
      <div>
        <p>Output (/M tokens)</p>
        <mistral-atom-text-price><span>$0.6</span></mistral-atom-text-price>
      </div>
      <code>mistral-small-latest</code>
    </article>
    """

    assert parse(html) == {
        "mistralai/mistral-medium-3-5": {
            "prompt_micro_per_m": 1_500_000,
            "completion_micro_per_m": 7_500_000,
        },
        "mistralai/mistral-small-2603": {
            "prompt_micro_per_m": 150_000,
            "completion_micro_per_m": 600_000,
        },
    }


def test_mistral_parser_reads_cached_input_price_from_rendered_cards() -> None:
    """Cards that publish the explicit 'Cached input (/M tokens)' row emit it."""
    html = """
    <article>
      <p class="text-h5 font-mistral">Mistral Medium 3.5</p>
      <div>
        <p>Input (/M tokens)</p>
        <mistral-atom-text-price><span>$1.5</span></mistral-atom-text-price>
      </div>
      <div>
        <p>Cached input (/M tokens) </p>
        <mistral-atom-text-price><span>$0.15</span></mistral-atom-text-price>
      </div>
      <div>
        <p>Output (/M tokens)</p>
        <mistral-atom-text-price><span>$7.5</span></mistral-atom-text-price>
      </div>
    </article>
    <article>
      <p class="text-h5 font-mistral">Mistral Small 4</p>
      <div>
        <p>Input (/M tokens)</p>
        <mistral-atom-text-price><span>$0.15</span></mistral-atom-text-price>
      </div>
      <div>
        <p>Output (/M tokens)</p>
        <mistral-atom-text-price><span>$0.6</span></mistral-atom-text-price>
      </div>
    </article>
    """

    parsed = parse(html)
    assert parsed["mistralai/mistral-medium-3-5"] == {
        "prompt_micro_per_m": 1_500_000,
        "prompt_cached_micro_per_m": 150_000,
        "completion_micro_per_m": 7_500_000,
    }
    # A card without the cached row emits no cached key at all — absence
    # must stay distinguishable from zero.
    assert parsed["mistralai/mistral-small-2603"] == {
        "prompt_micro_per_m": 150_000,
        "completion_micro_per_m": 600_000,
    }


def test_mistral_parser_reads_cached_input_price_from_embedded_json() -> None:
    html = (
        '{\\"name\\":\\"Mistral Small 4\\",\\"price\\":['
        '{\\"value\\":\\"Input (/M tokens)\\",\\"price_dollar\\":\\"<p>$0.15</p>\\"},'
        '{\\"value\\":\\"Cached input (/M tokens)\\",\\"price_dollar\\":\\"<p>$0.015</p>\\"},'
        '{\\"value\\":\\"Output (/M tokens)\\",\\"price_dollar\\":\\"<p>$0.6</p>\\"}]}'
    )

    assert parse(html) == {
        "mistralai/mistral-small-2603": {
            "prompt_micro_per_m": 150_000,
            "prompt_cached_micro_per_m": 15_000,
            "completion_micro_per_m": 600_000,
        }
    }


def _table(name: str, input_price: str = "$0.68", output_price: str = "$2.09") -> str:
    # Reduced from the live October 6 pricing table, with reordered headers
    # to prove prices are associated by labels rather than column positions.
    return f"""
    <table><thead><tr><th>Model</th><th>Output</th><th>Input</th><th>Cached input</th></tr></thead>
    <tbody><tr><td><a href="/models/mistral-large-4-0">{name}<span>&#8599;</span></a></td>
    <td>{output_price}</td><td>{input_price}</td><td>$0.07</td></tr></tbody></table>
    """


@pytest.mark.parametrize("version", [4, 5])
def test_mistral_docs_discovers_new_numbered_models_without_a_name_allowlist(version: int) -> None:
    assert parse(_table(f"Mistral Large {version}")) == {
        f"mistralai/mistral-large-{version}": {
            "prompt_micro_per_m": 680_000,
            "completion_micro_per_m": 2_090_000,
            "prompt_cached_micro_per_m": 70_000,
        }
    }


def test_mistral_docs_keeps_large_three_separate() -> None:
    prices = parse(_table("Mistral Large 4") + _table("Mistral Large 3", "$0.5", "$1.5"))
    assert prices["mistralai/mistral-large"]["completion_micro_per_m"] == 1_500_000
    assert prices["mistralai/mistral-large-4"]["completion_micro_per_m"] == 2_090_000


@pytest.mark.parametrize("price", ["$4 /1000 Pages", "$0.68 $1.36", "Free", "-", "\u20ac0.68"])
def test_mistral_docs_rejects_ambiguous_or_non_token_rates(price: str) -> None:
    assert parse(_table("Mistral Large 4", price)) == {}


def test_mistral_docs_rejects_conflicting_duplicate_prices() -> None:
    with pytest.raises(ValueError, match="conflicting standard prices"):
        parse(_table("Mistral Large 4") + _table("Mistral Large 4", "$0.34"))


def test_mistral_docs_does_not_infer_an_ambiguous_cache_discount() -> None:
    assert parse(_table("Mistral Large 4").replace("$0.07", "$0.07 $0.14")) == {}


@pytest.mark.parametrize("hidden", ['hidden', 'aria-hidden="true"', 'data-state="inactive"'])
def test_mistral_docs_ignores_hidden_batch_tiers(hidden: str) -> None:
    html = _table("Mistral Large 4") + f"<section {hidden}>{_table('Mistral Large 4', '$0.34')}</section>"
    assert parse(html)["mistralai/mistral-large-4"]["prompt_micro_per_m"] == 680_000


def test_mistral_discovers_live_priced_large_four_with_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-only")
    monkeypatch.setattr(mistral, "MANIFEST_PATH", tmp_path / "mistral.json")
    monkeypatch.setattr(mistral, "UPSTREAM_ID_MAP", {"stale": "stale"})
    monkeypatch.setattr(mistral, "fetch_provider", lambda **_: ProviderPricingResult(
        slug="mistral", source="deterministic", fetched_url=mistral.URL,
        prices={key: ModelPrice(**value) for key, value in parse(
            _table("Mistral Large 4") + _table("Mistral Large 5")
        ).items()},
    ))
    monkeypatch.setattr(mistral, "fetch_json", lambda *_args, **_kwargs: {"data": [
        {"id": "mistral-large-4", "max_context_length": 524288, "capabilities": {
            "completion_chat": True, "function_calling": True, "vision": True, "reasoning": True,
        }},
        {"id": "mistral-large-6", "capabilities": {"completion_chat": True}},
        {"id": "mistral-large-5", "capabilities": {"completion_chat": False}},
    ]})
    result = mistral.fetch()
    assert set(result.prices) == {"mistralai/mistral-large-4"}
    mistral.write_provider_manifest(result)
    manifest = json.loads(mistral.MANIFEST_PATH.read_text())
    assert len(manifest["models"]) == 1
    row = manifest["models"][0]
    assert row["id"] == "mistralai/mistral-large-4"
    assert row["upstream_id"] == "mistral-large-4"
    assert row["context_length"] == 524288
    assert row["input_modalities"] == ["text", "image"]
    assert row["supports_reasoning"] is True
    assert row["supported_features"] == ["function-calling"]
    assert row["cached_input_token_price_per_m"] == 70_000
    assert manifest["pricing_source"] == mistral.URL
    assert mistral.UPSTREAM_ID_MAP == {"mistralai/mistral-large-4": "mistral-large-4"}
    # Exercise real route construction from this pinned discovery fixture,
    # not tomorrow's hourly prices or availability.
    models, endpoints = build_manifest_rows(monkeypatch, tmp_path, "mistral", manifest["models"])
    model = models["mistralai/mistral-large-4"]
    assert model.context_length == 524288
    assert model_open_weights(model)
    routes = list(endpoints.values())
    assert {route.usage_type for route in routes} == {"Credits", "BYOK"}
    assert all(route.upstream_id == "mistral-large-4" for route in routes)
    assert all(route.prompt_price_microdollars_per_million_tokens == 717400 for route in routes)
    assert all(route.completion_price_microdollars_per_million_tokens == 2204950 for route in routes)


def test_mistral_discovery_requires_authenticated_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr(mistral, "fetch_provider", lambda **_: ProviderPricingResult(
        slug="mistral", source="deterministic", fetched_url=mistral.URL,
        prices={"mistralai/mistral-large-4": ModelPrice(680_000, 2_090_000)},
    ))
    with pytest.raises(RuntimeError, match="cannot verify priced model availability"):
        mistral.fetch()
