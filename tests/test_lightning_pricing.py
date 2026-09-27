"""Lightning's public catalog lists third-party apps under the model id they wrap.

Rows from https://lightning.ai/api/v1/models on 2026-09-27: "anthropic/claude-opus-4-8"
appeared three times, once at $5/$25 per million and twice at $10/$50 under app
names, so whichever row came last set the published price and name. A listing
that shares its id with another does not speak for the model: its name is never
published, and listings that disagree on price publish no price at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.check_price_spike import _load_provider_manifests, check
from scripts.pricing.providers import lightning

OPUS = "anthropic/claude-opus-4-8"
OPUS_LISTINGS = [
    (OPUS, "Agent 3", 5e-06, 2.5e-05),
    (OPUS, "BRUTHA - SQUIRE", 1e-05, 5e-05),
    (OPUS, "Ovarix IA Assistant", 1e-05, 5e-05),
]
GPT4_TURBO_PREVIEW_LISTINGS = [
    ("openai/gpt-4-turbo-preview", "Lightning SDK expert", 1e-05, 3e-05),
    ("openai/gpt-4-turbo-preview", "LitLogger helper", 1e-05, 3e-05),
]
GEMMA_LISTING = ("lightning-ai/gemma-4-31B-it", "gemma-4-31B-it", 1.4e-07, 4e-07)
GPT41_LISTING = ("openai/gpt-4.1", "GPT 4.1", 2e-06, 8e-06)


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse({"data": rows})

    monkeypatch.setattr(lightning.httpx, "Client", FakeClient)
    return rows


def _listings(*listings: tuple[str, str, float, float]) -> list[dict[str, Any]]:
    return [
        {
            "id": model_id,
            "name": name,
            "context_length": 1_048_576,
            "pricing": {"input_cost_per_token": prompt, "output_cost_per_token": completion},
        }
        for model_id, name, prompt, completion in listings
    ]


@pytest.mark.parametrize("order", [1, -1], ids=["base-listing-first", "base-listing-last"])
def test_listings_that_disagree_on_price_publish_no_price(
    feed: list[dict[str, Any]], order: int
) -> None:
    feed.extend(_listings(*OPUS_LISTINGS[::order], GEMMA_LISTING))

    result = lightning.fetch()

    assert OPUS not in result.prices
    assert lightning._DISCOVERED_MANIFEST_ROWS[OPUS] == {
        "id": OPUS,
        "upstream_id": OPUS,
        "endpoints": ["chat/completions"],
    }
    assert f"{OPUS}: 3 listings disagree on price; no price published" in result.notes
    # A model listed once is unaffected.
    assert result.prices["google/gemma-4-31b-it"].tiers[0].prompt_micro_per_m == 140_000


def test_listings_that_agree_publish_the_price_but_no_listing_name(
    feed: list[dict[str, Any]],
) -> None:
    feed.extend(_listings(*GPT4_TURBO_PREVIEW_LISTINGS, GEMMA_LISTING))

    result = lightning.fetch()

    tier = result.prices["openai/gpt-4-turbo-preview"].tiers[0]
    assert (tier.prompt_micro_per_m, tier.completion_micro_per_m) == (10_000_000, 30_000_000)
    discovered = lightning._DISCOVERED_MANIFEST_ROWS["openai/gpt-4-turbo-preview"]
    assert "display_name" not in discovered and "context_length" not in discovered
    # The only listing of a model still names it.
    gemma = lightning._DISCOVERED_MANIFEST_ROWS["google/gemma-4-31b-it"]
    assert gemma["display_name"] == "gemma-4-31B-it"
    assert gemma["context_length"] == 1_048_576


def _manifest(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(
            {
                "provider": "lightning",
                "price_scale": "microdollars_per_million",
                "models": rows,
            }
        ),
        encoding="utf-8",
    )


def test_a_price_conflict_pauses_the_route_without_tripping_the_spike_gate(
    feed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    committed = [
        {
            "id": OPUS,
            "upstream_id": OPUS,
            "display_name": "Claude Opus 4.8",
            "endpoints": ["chat/completions"],
            "input_token_price_per_m": 5_000_000,
            "output_token_price_per_m": 25_000_000,
        },
        {
            "id": "google/gemma-4-31b-it",
            "upstream_id": "lightning-ai/gemma-4-31B-it",
            "display_name": "gemma-4-31B-it",
            "endpoints": ["chat/completions"],
            "input_token_price_per_m": 140_000,
            "output_token_price_per_m": 400_000,
        },
        # Pausing one of three routes stays under the writer's 50% mass-prune guard.
        {
            "id": "openai/gpt-4.1",
            "upstream_id": "openai/gpt-4.1",
            "display_name": "GPT 4.1",
            "endpoints": ["chat/completions"],
            "input_token_price_per_m": 2_000_000,
            "output_token_price_per_m": 8_000_000,
        },
    ]
    before_dir, after_dir = tmp_path / "before", tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    _manifest(before_dir / "lightning.json", committed)
    _manifest(after_dir / "lightning.json", committed)
    monkeypatch.setattr(lightning, "MANIFEST_PATH", after_dir / "lightning.json")
    feed.extend(_listings(*OPUS_LISTINGS, GEMMA_LISTING, GPT41_LISTING))

    notes = lightning.write_provider_manifest(lightning.fetch())

    assert notes == [
        "lightning: refreshed provider_models/lightning.json "
        "(2 priced rows, tombstoned 1 unavailable)"
    ]
    rows = {
        row["id"]: row
        for row in json.loads((after_dir / "lightning.json").read_text())["models"]
    }
    opus = rows[OPUS]
    assert opus["routable"] is False and opus["routable_reason"] == "price-unavailable"
    assert opus["display_name"] == "Claude Opus 4.8"
    assert "input_token_price_per_m" not in opus and "output_token_price_per_m" not in opus
    failures, _changes, removed = check(
        _load_provider_manifests(before_dir), _load_provider_manifests(after_dir)
    )
    assert failures == []
    assert removed == [f"{OPUS} [lightning:lightning:{OPUS}]"]
