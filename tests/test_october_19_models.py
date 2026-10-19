from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers import minimax as minimax_parser
from scripts.pricing.providers import _direct_openai, inception, minimax
from trusted_router.provider_lifecycle import provider_model_retired

INCEPTION_DOC = """
| Model | Input Price (1M Tokens) | Cached Input Price (1M Tokens) | Output Price (1M Tokens) | Supported Endpoints | Context Window | Max Output Tokens | Features | Supported Formats |
| **Mercury 3** | ~~$0.20~~ -> **$0.10** | ~~$0.02~~ -> **$0.01** | ~~$0.70~~ -> **$0.35** | `v1/chat/completions` | Chat: 1M | 131,072 | `Tool Calling` | `Text` |
| **Mercury Decide** | $0.02 | $0 | $0 | `v1/decisions` | - | - | Classification | JSON |
"""


def test_minimax_indented_launch_table_does_not_mix_m3_or_priority_prices() -> None:
    source = """
    | **MiniMax-M3.1-Flash** | \\$0.15 / M tokens | \\$0.60 / M tokens | \\$0.015 / M tokens |
    <Tab title="Standard">
      | **MiniMax-M3**<br />\u2264 512k input tokens | $0.30 | $1.20 | $0.06 |
      | **MiniMax-M3**<br />> 512k input tokens | $0.60 | $2.40 | $0.12 |
    </Tab>
    <Tab title="Priority*">
      | **MiniMax-M3**<br />\u2264 512k input tokens | $0.45 | $1.80 | $0.09 |
      | **MiniMax-M3**<br />> 512k input tokens | $0.90 | $3.60 | $0.18 |
    </Tab>
    """
    prices = minimax_parser.parse(source)
    assert prices["minimax/minimax-m3.1-flash"] == {
        "prompt_micro_per_m": 150_000,
        "completion_micro_per_m": 600_000,
        "prompt_cached_micro_per_m": 15_000,
    }
    assert prices["minimax/minimax-m3"]["tiers"] == [
        {"max_prompt_tokens": 512_000, "prompt_micro_per_m": 300_000,
         "completion_micro_per_m": 1_200_000, "prompt_cached_micro_per_m": 60_000},
        {"max_prompt_tokens": None, "prompt_micro_per_m": 600_000,
         "completion_micro_per_m": 2_400_000, "prompt_cached_micro_per_m": 120_000},
    ]


def test_minimax_discovery_has_documented_context_and_ignores_retired_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "test")
    monkeypatch.setenv("TR_LIFECYCLE_CLOCK_OVERRIDE", "2026-10-20T00:00:00Z")
    monkeypatch.setattr(minimax, "fetch_json", lambda *a, **kw: {"data": [
        {"id": "MiniMax-M3.1-Flash"}, {"id": "MiniMax-M3"}, {"id": "MiniMax-M2.7"},
    ]})
    rows = minimax._live_model_rows()
    assert set(rows) == {"minimax/minimax-m3", "minimax/minimax-m3.1-flash"}
    launch = rows["minimax/minimax-m3.1-flash"]
    assert launch["upstream_id"] == "MiniMax-M3.1-Flash"
    assert launch["context_length"] == 1_000_000
    assert "context_length_source" not in launch
    assert launch["input_modalities"] == ["text", "image"]
    at = datetime(2026, 10, 20, tzinfo=UTC)
    assert provider_model_retired("minimax", "minimax/minimax-m2.7", at=at)
    assert not provider_model_retired("deepinfra", "minimax/minimax-m2.1", at=at)


def _mercury_row(native_id: str = "mercury-3") -> dict:
    return {
        "id": native_id, "name": "Inception: Mercury 3", "context_length": 1_000_000,
        "max_output_length": 131_072, "supported_endpoints": ["chat.completions"],
        "pricing": {"prompt": "0", "completion": "0", "input_cache_reads": "0"},
    }


def test_minimax_confirmed_retirements_do_not_block_repeated_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "minimax.json"
    old_rows = [{"id": f"minimax/{native.lower()}", "upstream_id": native} for native in (
        "MiniMax-M2", "MiniMax-M2.1", "MiniMax-M2.5", "MiniMax-M2.7", "MiniMax-M3",
    )]
    manifest.write_text(json.dumps({"models": old_rows}))
    monkeypatch.setenv("TR_LIFECYCLE_CLOCK_OVERRIDE", "2026-10-20T00:00:00Z")
    monkeypatch.setattr(minimax, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(minimax, "_DISCOVERED_MANIFEST_ROWS", {
        "minimax/minimax-m3": {"id": "minimax/minimax-m3", "upstream_id": "MiniMax-M3"},
    })
    result = ProviderPricingResult(slug="minimax", prices={
        "minimax/minimax-m3": ModelPrice(300_000, 1_200_000),
    }, source="api")
    # A missing EXPECTED_MODELS entry is advisory, not a permanent publication
    # gate. The reviewed retirement also must not trip the mass-prune guard.
    for _ in range(2):
        minimax.write_provider_manifest(result)
    rows = json.loads(manifest.read_text())["models"]
    assert all(row.get("routable") is False for row in rows if "minimax-m2" in row["id"])
    assert next(row for row in rows if row["id"] == "minimax/minimax-m3")["input_token_price_per_m"] == 300_000
    result.prices = {}
    with pytest.raises(RuntimeError, match="unpriced live"):
        minimax.write_provider_manifest(result)


def test_inception_uses_current_documented_price_not_zero_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inception, "fetch_html", lambda url: INCEPTION_DOC)
    row = _mercury_row()
    normalized = inception._normalize_rows([row])
    assert normalized[0]["pricing"] == {
        "prompt": "1E-7", "input_cache_reads": "1E-8", "completion": "3.5E-7",
    }
    assert row["pricing"]["prompt"] == "0"
    assert inception.CATALOG.model_id("mercury-4") == "inception/mercury-4"


def test_inception_promotion_expiry_tracks_new_documented_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = INCEPTION_DOC.replace("~~$0.20~~ -> **$0.10**", "$0.20").replace(
        "~~$0.02~~ -> **$0.01**", "$0.02",
    ).replace("~~$0.70~~ -> **$0.35**", "$0.70")
    monkeypatch.setattr(inception, "fetch_html", lambda url: doc)
    assert inception._normalize_rows([_mercury_row()])[0]["pricing"] == {
        "prompt": "2E-7", "input_cache_reads": "2E-8", "completion": "7E-7",
    }


def test_inception_preserves_native_positive_prices_and_skips_non_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inception, "fetch_html", lambda url: pytest.fail("no fallback needed"))
    row = _mercury_row()
    row["pricing"] = {"prompt": "0.00000012", "completion": "0.0000004"}
    decision = {**_mercury_row("mercury-decide"), "supported_endpoints": ["decisions"]}
    assert inception._normalize_rows([row, decision]) == [row]


def test_inception_ambiguous_prices_fail_closed() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        inception._parse_documented_prices(INCEPTION_DOC.replace("~~$0.20~~", "$0.20"))
    with pytest.raises(ValueError, match="conflicting"):
        inception._parse_documented_prices(INCEPTION_DOC + INCEPTION_DOC.replace("$0.10", "$0.11"))


def test_inception_launch_price_discovery_canary_and_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "inception.json"
    catalog = _direct_openai.DirectOpenAIProvider(inception.CATALOG.spec, manifest_path=manifest)
    monkeypatch.setenv("INCEPTION_API_KEY", "test")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *a, **kw: {"data": [_mercury_row()]})
    monkeypatch.setattr(inception, "fetch_html", lambda url: INCEPTION_DOC)
    calls: list[str] = []

    def probe(**kwargs: object) -> bool:
        calls.append(str(kwargs["model"]))
        assert kwargs["require_usage"] is True
        return True

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    result = catalog.fetch()
    assert result.prices == {"inception/mercury-3": ModelPrice(100_000, 350_000, prompt_cached_micro_per_m=10_000)}
    catalog.write_provider_manifest(result)
    row = json.loads(manifest.read_text())["models"][0]
    assert row["context_length"] == 1_000_000
    assert row["max_output_tokens"] == 131_072
    assert row["routable"] is True
    assert calls == ["mercury-3"]


def test_inception_unknown_zero_price_is_never_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = _direct_openai.DirectOpenAIProvider(inception.CATALOG.spec, manifest_path=tmp_path / "inception.json")
    monkeypatch.setenv("INCEPTION_API_KEY", "test")
    monkeypatch.setattr(_direct_openai, "fetch_json", lambda *a, **kw: {"data": [_mercury_row("mercury-99")]})
    monkeypatch.setattr(inception, "fetch_html", lambda url: INCEPTION_DOC)
    monkeypatch.setattr(_direct_openai, "probe_openai_chat", lambda **kw: pytest.fail("unpriced canary"))
    with pytest.raises(RuntimeError, match="no priced chat models"):
        catalog.fetch()


def test_launch_catalog_prices_limits_capabilities_and_weight_labels() -> None:
    from trusted_router.catalog import MODELS, endpoints_for_model, model_open_weights
    from trusted_router.catalog_ingest import _customer_price
    from trusted_router.request_capabilities import endpoint_capabilities

    for model_id, provider, native, efforts, vision in (
        ("minimax/minimax-m3.1-flash", "minimax", "MiniMax-M3.1-Flash",
         ["low", "medium", "high", "xhigh", "max"], True),
        ("inception/mercury-3", "inception", "mercury-3",
         ["low", "medium", "high"], False),
    ):
        model = MODELS[model_id]
        assert model.context_length == 1_000_000
        assert not model_open_weights(model)
        endpoint = next(e for e in endpoints_for_model(model_id) if e.usage_type == "Credits" and e.provider == provider)
        assert endpoint.upstream_id == native
        manifest = Path(__file__).parents[1] / "src/trusted_router/data/provider_models" / f"{provider}.json"
        row = next(row for row in json.loads(manifest.read_text())["models"] if row["id"] == model_id)
        prices = tuple(_customer_price(row[field]) for field in (
            "input_token_price_per_m", "output_token_price_per_m", "cached_input_token_price_per_m",
        ))
        tier = endpoint.price_tiers[0]
        assert (tier.prompt_price_microdollars_per_million_tokens,
                tier.completion_price_microdollars_per_million_tokens,
                tier.prompt_cached_price_microdollars_per_million_tokens) == prices
        capabilities = endpoint_capabilities(model, endpoint)
        assert capabilities["reasoning_effort"] == efforts
        assert capabilities["tools"] is True
        assert capabilities["vision"] is vision
        assert capabilities["confidential"] is False
