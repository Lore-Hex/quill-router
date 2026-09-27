"""Lightning's public catalog lists third-party apps under the model id they wrap.

Rows from https://lightning.ai/api/v1/models on 2026-09-27: "anthropic/claude-opus-4-8"
appeared three times, once at $5/$25 per million and twice at $10/$50 under app
names, so whichever row came last set the published price and name. Nothing
marks which listing is the model's own, so a native id listed more than once
never names the model, advertises the smallest context any listing claims, and
publishes no price when its listings disagree on one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.check_price_spike import _load_provider_manifests, check
from scripts.pricing.model_ids import mapped_or_canonical_model_id
from scripts.pricing.providers import lightning

OPUS = "anthropic/claude-opus-4-8"
OPUS_ROUTE = f"{OPUS} [lightning:lightning:{OPUS}]"


def _listing(
    native_id: str,
    name: str,
    prompt: float | None,
    completion: float | None,
    context: int = 1_048_576,
) -> dict[str, Any]:
    pricing = (
        {}
        if prompt is None
        else {"input_cost_per_token": prompt, "output_cost_per_token": completion}
    )
    return {"id": native_id, "name": name, "context_length": context, "pricing": pricing}


OPUS_LISTINGS = [
    _listing(OPUS, "Agent 3", 5e-06, 2.5e-05, context=1_000_000),
    _listing(OPUS, "BRUTHA - SQUIRE", 1e-05, 5e-05),
    _listing(OPUS, "Ovarix IA Assistant", 1e-05, 5e-05),
]
GEMMA = _listing("lightning-ai/gemma-4-31B-it", "gemma-4-31B-it", 1.4e-07, 4e-07)
GPT41 = _listing("openai/gpt-4.1", "GPT 4.1", 2e-06, 8e-06)


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


def _rates(result: Any, model_id: str) -> tuple[int, int]:
    tier = result.prices[model_id].tiers[0]
    return tier.prompt_micro_per_m, tier.completion_micro_per_m


@pytest.mark.parametrize("order", [1, -1], ids=["base-listing-first", "base-listing-last"])
def test_listings_that_disagree_on_price_publish_no_price(
    feed: list[dict[str, Any]], order: int
) -> None:
    feed.extend([*OPUS_LISTINGS[::order], GEMMA])

    result = lightning.fetch()

    assert OPUS not in result.prices
    assert lightning._DISCOVERED_MANIFEST_ROWS[OPUS] == {
        "id": OPUS,
        "upstream_id": OPUS,
        "endpoints": ["chat/completions"],
        "context_length": 1_000_000,
    }
    assert f"{OPUS}: 3 listings of {OPUS} disagree on price; no price published" in result.notes
    # A model listed once is unaffected.
    assert _rates(result, "google/gemma-4-31b-it") == (140_000, 400_000)


def test_listings_that_disagree_only_on_completion_publish_no_price(
    feed: list[dict[str, Any]],
) -> None:
    feed.extend([_listing(OPUS, "a", 5e-06, 2.5e-05), _listing(OPUS, "b", 5e-06, 5e-05)])

    assert OPUS not in lightning.fetch().prices


def test_listings_that_agree_publish_the_price_but_no_listing_name(
    feed: list[dict[str, Any]],
) -> None:
    feed.extend(
        [
            _listing("openai/gpt-4-turbo-preview", "Lightning SDK expert", 1e-05, 3e-05),
            _listing("openai/gpt-4-turbo-preview", "LitLogger helper", 1e-05, 3e-05, context=128_000),
            GEMMA,
        ]
    )

    result = lightning.fetch()

    assert _rates(result, "openai/gpt-4-turbo-preview") == (10_000_000, 30_000_000)
    assert lightning._DISCOVERED_MANIFEST_ROWS["openai/gpt-4-turbo-preview"] == {
        "id": "openai/gpt-4-turbo-preview",
        "upstream_id": "openai/gpt-4-turbo-preview",
        "endpoints": ["chat/completions"],
        "context_length": 128_000,
    }
    # The only listing of a model still names it.
    gemma = lightning._DISCOVERED_MANIFEST_ROWS["google/gemma-4-31b-it"]
    assert gemma["display_name"] == "gemma-4-31B-it"
    assert gemma["context_length"] == 1_048_576


def test_a_listing_without_a_usable_price_still_makes_the_id_a_duplicate(
    feed: list[dict[str, Any]],
) -> None:
    feed.extend(
        [
            _listing(OPUS, "Third-party app", 5e-06, 2.5e-05, context=8192),
            _listing(OPUS, "Agent 3", None, None, context=1_000_000),
        ]
    )

    result = lightning.fetch()

    assert _rates(result, OPUS) == (5_000_000, 25_000_000)
    discovered = lightning._DISCOVERED_MANIFEST_ROWS[OPUS]
    assert "display_name" not in discovered
    assert discovered["context_length"] == 8192


def test_an_infinite_context_on_any_listing_is_ignored(feed: list[dict[str, Any]]) -> None:
    # JSON 1e309 parses to float("inf"); int() of it raises OverflowError.
    feed.extend(
        [
            _listing(OPUS, "Agent 3", 5e-06, 2.5e-05, context=1_000_000),
            _listing(OPUS, "Third-party app", 5e-06, 2.5e-05, context=float("inf")),  # type: ignore[arg-type]
        ]
    )

    result = lightning.fetch()

    assert _rates(result, OPUS) == (5_000_000, 25_000_000)
    assert lightning._DISCOVERED_MANIFEST_ROWS[OPUS]["context_length"] == 1_000_000


def test_distinct_native_ids_of_one_model_keep_the_last_priced_listing(
    feed: list[dict[str, Any]],
) -> None:
    # Two native ids that normalize to one model are two routes with their own
    # prices, not one ambiguous listing: the last one wins, as it always has.
    first, last = "qwen/Qwen3.8-27B", "qwen/qwen3.8-27b"
    model_id = mapped_or_canonical_model_id(first, lightning._NATIVE_TO_OR_ID)
    assert model_id == mapped_or_canonical_model_id(last, lightning._NATIVE_TO_OR_ID)
    feed.extend(
        [
            _listing(first, "Qwen3.8-27B", 4e-07, 3e-06),
            _listing(last, "qwen3.8-27b", 5e-07, 4e-06),
        ]
    )

    result = lightning.fetch()

    assert _rates(result, model_id) == (500_000, 4_000_000)
    assert lightning._DISCOVERED_MANIFEST_ROWS[model_id]["upstream_id"] == last
    assert lightning._DISCOVERED_MANIFEST_ROWS[model_id]["display_name"] == "qwen3.8-27b"
    assert not [note for note in result.notes if "disagree" in note]


def _write_manifest(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(
            {"provider": "lightning", "price_scale": "microdollars_per_million", "models": rows}
        ),
        encoding="utf-8",
    )


def _committed(opus: dict[str, Any]) -> list[dict[str, Any]]:
    # Pausing one of three routes stays under the writer's 50% mass-prune guard.
    return [
        {
            "id": OPUS,
            "upstream_id": OPUS,
            "display_name": "Claude Opus 4.8",
            "endpoints": ["chat/completions"],
            **opus,
        },
        {
            "id": "google/gemma-4-31b-it",
            "upstream_id": "lightning-ai/gemma-4-31B-it",
            "display_name": "gemma-4-31B-it",
            "endpoints": ["chat/completions"],
            "input_token_price_per_m": 140_000,
            "output_token_price_per_m": 400_000,
        },
        {
            "id": "openai/gpt-4.1",
            "upstream_id": "openai/gpt-4.1",
            "display_name": "GPT 4.1",
            "endpoints": ["chat/completions"],
            "input_token_price_per_m": 2_000_000,
            "output_token_price_per_m": 8_000_000,
        },
    ]


@pytest.fixture
def manifests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    before_dir, after_dir = tmp_path / "before", tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    monkeypatch.setattr(lightning, "MANIFEST_PATH", after_dir / "lightning.json")
    return before_dir, after_dir


def _refresh(
    manifests: tuple[Path, Path], committed: list[dict[str, Any]]
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    before_dir, after_dir = manifests
    _write_manifest(before_dir / "lightning.json", committed)
    _write_manifest(after_dir / "lightning.json", committed)
    notes = lightning.write_provider_manifest(lightning.fetch())
    rows = json.loads((after_dir / "lightning.json").read_text())["models"]
    return notes, {row["id"]: row for row in rows}


PRICE_FIELDS = (
    "input_token_price_per_m",
    "output_token_price_per_m",
    "cached_input_token_price_per_m",
    "price_tiers",
)


def test_a_price_conflict_pauses_the_route_without_tripping_the_spike_gate(
    feed: list[dict[str, Any]], manifests: tuple[Path, Path]
) -> None:
    feed.extend([*OPUS_LISTINGS, GEMMA, GPT41])

    notes, rows = _refresh(
        manifests,
        _committed(
            {
                "input_token_price_per_m": 5_000_000,
                "output_token_price_per_m": 25_000_000,
                "cached_input_token_price_per_m": 500_000,
                "price_tiers": [
                    {
                        "max_prompt_tokens": 200_000,
                        "input_token_price_per_m": 5_000_000,
                        "output_token_price_per_m": 25_000_000,
                    }
                ],
            }
        ),
    )

    assert notes == [
        "lightning: refreshed provider_models/lightning.json "
        "(2 priced rows, tombstoned 1 unavailable)"
    ]
    opus = rows[OPUS]
    assert opus["routable"] is False and opus["routable_reason"] == "price-unavailable"
    assert opus["display_name"] == "Claude Opus 4.8"
    assert not [field for field in PRICE_FIELDS if field in opus]
    failures, _changes, removed = check(*map(_load_provider_manifests, manifests))
    assert failures == []
    assert sorted(removed) == sorted(
        [OPUS_ROUTE, f"{OPUS_ROUTE} cached-input", f"{OPUS_ROUTE} tier[max=200000]"]
    )


@pytest.mark.parametrize(
    ("listings", "routable"),
    [(OPUS_LISTINGS, False), (OPUS_LISTINGS[:1], True)],
    ids=["conflicting-listings", "one-listing"],
)
def test_a_relisted_route_recovers_only_with_one_price(
    feed: list[dict[str, Any]],
    manifests: tuple[Path, Path],
    listings: list[dict[str, Any]],
    routable: bool,
) -> None:
    feed.extend([*listings, GEMMA, GPT41])

    _notes, rows = _refresh(
        manifests,
        _committed(
            {
                "routable": False,
                "routable_reason": "delisted-upstream",
                "missing_since": "2026-09-20",
            }
        ),
    )

    opus = rows[OPUS]
    if routable:
        assert opus.get("routable", True) is True and "routable_reason" not in opus
        assert (opus["input_token_price_per_m"], opus["output_token_price_per_m"]) == (
            5_000_000,
            25_000_000,
        )
    else:
        assert opus["routable"] is False and opus["routable_reason"] == "price-unavailable"
        assert "input_token_price_per_m" not in opus


def test_a_new_duplicated_model_gets_no_listing_name(
    feed: list[dict[str, Any]], manifests: tuple[Path, Path]
) -> None:
    feed.extend(
        [
            _listing("openai/gpt-4-turbo-preview", "Lightning SDK expert", 1e-05, 3e-05),
            _listing("openai/gpt-4-turbo-preview", "LitLogger helper", 1e-05, 3e-05, context=128_000),
            OPUS_LISTINGS[0],
            GEMMA,
            GPT41,
        ]
    )

    _notes, rows = _refresh(
        manifests,
        _committed({"input_token_price_per_m": 5_000_000, "output_token_price_per_m": 25_000_000}),
    )

    added = rows["openai/gpt-4-turbo-preview"]
    assert added["display_name"] == "openai/gpt-4-turbo-preview"
    assert added["context_length"] == 128_000
    assert (added["input_token_price_per_m"], added["output_token_price_per_m"]) == (
        10_000_000,
        30_000_000,
    )
