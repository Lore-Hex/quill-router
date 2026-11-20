"""Atlas Cloud's long-context price tiers (pricing.overrides in /v1/models)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.check_price_spike import _load_provider_manifests
from scripts.pricing.base import PriceTier
from scripts.pricing.openai_catalog import discover_openai_chat_catalog
from scripts.pricing.providers import atlas_cloud

ROOT = Path(__file__).resolve().parents[1]
# https://api.atlascloud.ai/v1/models on 2026-11-19, trimmed to seven rows.
FIXTURE = ROOT / "tests" / "fixtures" / "pricing" / "atlas_cloud_models_2026-11-19.json"
COMMITTED = ROOT / "src" / "trusted_router" / "data" / "provider_models" / "atlas-cloud.json"


def _rows() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]


def _row(native_id: str) -> dict:
    return copy.deepcopy(next(row for row in _rows() if row["id"] == native_id))


def _discover(rows: list[dict], **kwargs):
    return discover_openai_chat_catalog(
        rows,
        explicit_map=atlas_cloud._NATIVE_TO_MODEL_ID,
        upstream_id_map={},
        include=atlas_cloud._supports_chat_completions,
        **kwargs,
    )


def test_live_feed_publishes_each_long_context_tier():
    prices, _ = _discover(_rows(), price_of=atlas_cloud.atlas_model_price)
    # Atlas: $2 / $10 / $0.20 cache read per 1M up to 272K input tokens, then
    # $4 / $15 / $0.40 (the pricing page's "272k<input" tier).
    assert prices["openai/gpt-6-sol-codex"].tiers == [
        PriceTier(272_000, 2_000_000, 10_000_000, 200_000),
        PriceTier(None, 4_000_000, 15_000_000, 400_000),
    ]
    assert prices["anthropic/claude-sonnet-4.5-20250929"].tiers == [
        PriceTier(200_000, 3_000_000, 15_000_000, 300_000),
        PriceTier(None, 6_000_000, 22_500_000, 600_000),
    ]
    assert prices["qwen/qwen3.6-plus"].tiers[0].max_prompt_tokens == 256_000
    # A row without overrides keeps one flat tier.
    assert len(prices["deepseek/deepseek-v4-flash"].tiers) == 1
    assert prices["deepseek/deepseek-v4-flash"].tiers[0].max_prompt_tokens is None


def test_the_shared_default_parser_is_unchanged():
    # Other providers using discover_openai_chat_catalog keep the flat parse.
    prices, _ = _discover(_rows())
    assert len(prices["openai/gpt-6-sol-codex"].tiers) == 1


@pytest.mark.parametrize("extra", [{}, {"unit": "per 1K tokens"}], ids=["plain", "with-unknown-field"])
def test_an_override_without_a_valid_base_holds_the_provider(extra):
    # Returning None here would drop a still-listed model and later tombstone it.
    row = _row("openai/gpt-6-sol-codex")
    del row["pricing"]["prompt"]
    row["pricing"]["overrides"][0].update(extra)
    with pytest.raises(RuntimeError, match="without a valid base rate"):
        atlas_cloud.atlas_model_price(row)


def test_a_row_without_overrides_and_without_a_base_is_skipped_as_before():
    row = _row("deepseek-ai/deepseek-v4-flash")
    del row["pricing"]["prompt"]
    assert atlas_cloud.atlas_model_price(row) is None


def test_empty_overrides_is_flat():
    row = _row("openai/gpt-6-sol-codex")
    row["pricing"]["overrides"] = []
    assert len(atlas_cloud.atlas_model_price(row).tiers) == 1


def _mutate(change):
    row = _row("openai/gpt-6-sol-codex")
    change(row["pricing"])
    return row


@pytest.mark.parametrize(("change", "message"), [
    (lambda p: p["overrides"].append(dict(p["overrides"][0], min_context=500000)), "exactly one"),
    (lambda p: p.__setitem__("overrides", p["overrides"][0]), "exactly one"),
    (lambda p: p["overrides"][0].__setitem__("max_context", 1000000), "unreviewed"),
    (lambda p: p["overrides"][0].__setitem__("unit", "per 1K tokens"), "unreviewed"),
    (lambda p: p["overrides"][0].pop("min_context"), "incomplete"),
    (lambda p: p["overrides"][0].__setitem__("min_context", 0), "incomplete"),
    (lambda p: p["overrides"][0].__setitem__("min_context", "272K"), "incomplete"),
    (lambda p: p["overrides"][0].pop("prompt"), "incomplete"),
    (lambda p: p["overrides"][0].__setitem__("completion", "free"), "incomplete"),
    (lambda p: p["overrides"][0].pop("input_cache_read"), "only one tier"),
    (lambda p: p["overrides"][0].__setitem__("input_cache_read", "n/a"), "invalid cached-input"),
    (lambda p: p["overrides"][0].__setitem__("prompt", "0.000001"), "below the base rate"),
    (lambda p: p["overrides"][0].__setitem__("input_cache_read", "0.0000001"), "below the base rate"),
], ids=["two-overrides", "not-a-list", "max-context", "unit", "no-threshold", "zero-threshold",
        "text-threshold", "no-prompt", "bad-completion", "cached-one-tier", "bad-cached",
        "cheaper-prompt", "cheaper-cached"])
def test_an_unreviewed_override_shape_holds_the_provider(change, message):
    with pytest.raises(RuntimeError, match=message):
        atlas_cloud.atlas_model_price(_mutate(change))


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return json.loads(FIXTURE.read_text(encoding="utf-8"))


class _Client:
    def __init__(self, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def get(self, url, headers):
        assert url == atlas_cloud.URL and headers["Authorization"] == "Bearer test-key"
        return _Response()


def test_fetch_writes_price_tiers_and_keeps_the_headline(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_CLOUD_API_KEY", "test-key")
    monkeypatch.setattr(atlas_cloud.httpx, "Client", _Client)
    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    fixture_ids = {atlas_cloud._NATIVE_TO_MODEL_ID.get(row["id"], row["id"]) for row in _rows()}
    seeded = {**committed, "models": [row for row in committed["models"] if row["id"] in fixture_ids]}
    manifest = tmp_path / "atlas-cloud.json"
    manifest.write_text(json.dumps(seeded), encoding="utf-8")
    monkeypatch.setattr(atlas_cloud, "MANIFEST_PATH", manifest)

    result = atlas_cloud.fetch()
    atlas_cloud.write_provider_manifest(result)

    rows = {row["id"]: row for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]}
    codex = rows["openai/gpt-6-sol-codex"]
    # The headline (what the spike gate and /v1/models compare) is the base tier.
    assert (codex["input_token_price_per_m"], codex["output_token_price_per_m"],
            codex["cached_input_token_price_per_m"]) == (2_000_000, 10_000_000, 200_000)
    assert codex["price_tiers"] == [
        {"max_prompt_tokens": 272_000, "input_token_price_per_m": 2_000_000,
         "output_token_price_per_m": 10_000_000, "cached_input_token_price_per_m": 200_000},
        {"max_prompt_tokens": None, "input_token_price_per_m": 4_000_000,
         "output_token_price_per_m": 15_000_000, "cached_input_token_price_per_m": 400_000},
    ]
    assert "price_tiers" not in rows["deepseek/deepseek-v4-flash"]

    # The spike gate keeps comparing the same headline routes (so existing
    # approvals still apply) and adds the tiers as new routes.
    routes = _load_provider_manifests(tmp_path)
    key = "openai/gpt-6-sol-codex [atlas-cloud:atlas-cloud:openai/gpt-6-sol-codex]"
    assert routes[key] == {"prompt": "0.000002", "completion": "0.00001"}
    assert routes[f"{key} tier[max=272000]"] == {"prompt": "0.000002", "completion": "0.00001"}
    assert routes[f"{key} tier[uncapped]"] == {"prompt": "0.000004", "completion": "0.000015"}
    assert routes[f"{key} tier[uncapped] cached-input"]["prompt"] == "4E-7"
