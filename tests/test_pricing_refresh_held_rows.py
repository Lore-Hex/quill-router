"""Held provider-manifest rows never become snapshot endpoints.

November 19: once Tinker's price fetch recovered (#2760), the fresh price for
its held Inkling 2 Small row made `_merge_snapshot` synthesize a Tinker
endpoint OpenRouter does not list. The runtime held-row filter drops only
prepaid routes, so the snapshot published it as a BYOK route and every hourly
refresh failed CI. Stale fallback already skipped held rows; the fresh path
did not.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult

HELD = "acme/held"
SIBLING = "acme/sibling"
ONLY_HELD = "acme/only-held"


def _manifest_row(model_id: str, price: int, reason: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": model_id,
        "upstream_id": model_id,
        "input_token_price_per_m": price,
        "output_token_price_per_m": price,
    }
    if reason is not None:
        row["routable"] = False
        row["routable_reason"] = reason
    return row


@pytest.fixture
def refresh_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Two providers: `tinker` holds HELD and ONLY_HELD; `other` serves HELD.

    OpenRouter lists HELD (on `other` only), SIBLING (no keyed endpoint) and
    ONLY_HELD (no keyed endpoint). The held provider is the cheapest, so a
    leaked price would also take the headline.
    """
    manifests = tmp_path / "provider_models"
    parsers = tmp_path / "parsers"
    manifests.mkdir()
    parsers.mkdir()
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"models": []}))
    slugs = ("tinker", "other")
    state: dict[str, Any] = {"reason": "provider-access-unavailable"}

    prices = {
        "tinker": {HELD: 1_000_000, SIBLING: 2_000_000, ONLY_HELD: 1_000_000},
        "other": {HELD: 5_000_000},
    }

    def manifest(slug: str) -> dict[str, Any]:
        held_ids = {HELD, ONLY_HELD} if slug == "tinker" else set()
        return {
            "provider": slug,
            "models": [
                _manifest_row(model_id, price, state["reason"] if model_id in held_ids else None)
                for model_id, price in prices[slug].items()
            ],
        }

    modules = {}
    for slug in slugs:
        path = manifests / f"{slug}.json"
        path.write_text(json.dumps(manifest(slug)))
        (parsers / f"{slug}.py").write_text("# parser\n")

        def write(result: ProviderPricingResult, slug: str = slug, path: Path = path) -> list[str]:
            # Like the real writers: refresh prices, keep the row's hold.
            path.write_text(json.dumps(manifest(slug)))
            return [f"{slug}: refreshed"]

        modules[slug] = SimpleNamespace(
            MANIFEST_PATH=path, MANIFEST_STALE_FALLBACK=True, write_provider_manifest=write,
        )

    def fetch() -> tuple[dict[str, ProviderPricingResult], list[tuple[str, str]]]:
        return {
            slug: ProviderPricingResult(
                slug=slug,
                source="api",
                prices={model_id: ModelPrice(price, price) for model_id, price in prices[slug].items()},
            )
            for slug in slugs
        }, []

    def or_model(model_id: str, endpoints: list[str]) -> dict[str, Any]:
        return {
            "id": model_id,
            "name": model_id,
            "context_length": 8192,
            "pricing": {"prompt": "0.000009", "completion": "0.000009"},
            "endpoints": [
                {
                    "tr_provider_slug": slug,
                    "model_id": model_id,
                    "pricing": {"prompt": "0.000009", "completion": "0.000009"},
                }
                for slug in endpoints
            ],
        }

    feed = {
        "models": [
            or_model(HELD, ["other"]),
            or_model(SIBLING, []),
            or_model(ONLY_HELD, []),
        ]
    }
    monkeypatch.setattr(refresh, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(refresh, "PROVIDER_MANIFEST_DIR", manifests)
    monkeypatch.setattr(refresh, "PARSERS_DIR", parsers)
    monkeypatch.setattr(refresh, "parser_path", lambda slug: parsers / f"{slug}.py")
    monkeypatch.setattr(refresh, "PROVIDER_SLUGS", slugs)
    monkeypatch.setattr(refresh, "_import_provider", modules.__getitem__)
    monkeypatch.setattr(refresh, "_fetch_all_providers", fetch)
    monkeypatch.setattr(refresh, "build_openrouter_snapshot", lambda: feed)
    monkeypatch.setattr(refresh, "_new_parser_requirements", lambda *_args: {})
    monkeypatch.setattr(refresh, "configure_runtime_required_models", lambda *_args: None)
    return state


def _published() -> dict[str, dict[str, Any]]:
    snapshot = json.loads(refresh.SNAPSHOT_PATH.read_text())
    return {model["id"]: model for model in snapshot["models"]}


def _routes(model: dict[str, Any]) -> set[str]:
    return {endpoint["tr_provider_slug"] for endpoint in model["endpoints"]}


@pytest.mark.parametrize(
    "reason", ["provider-access-unavailable", "operator-credit-exhausted", "provider-security-incident"],
)
def test_fresh_held_row_openrouter_does_not_list_never_becomes_a_snapshot_endpoint(
    refresh_run: dict[str, Any], reason: str,
) -> None:
    refresh_run["reason"] = reason

    assert refresh.main([]) == 0

    models = _published()
    # The held provider's fresh price neither synthesizes an endpoint nor
    # sets the headline; the provider OpenRouter lists keeps serving.
    assert _routes(models[HELD]) == {"other"}
    assert models[HELD]["pricing"]["prompt"] == "0.000005"
    # A model priced only by held rows is left out, not sent to the
    # OpenRouter fallback (which would publish OpenRouter's endpoints).
    assert ONLY_HELD not in models
    # The hold is per row: the provider's routable sibling still publishes
    # through a synthesized endpoint.
    assert _routes(models[SIBLING]) == {"tinker"}
    # The manifest keeps the fresh price and the hold.
    rows = {
        row["id"]: row
        for row in json.loads((refresh.PROVIDER_MANIFEST_DIR / "tinker.json").read_text())["models"]
    }
    assert rows[HELD]["routable_reason"] == reason
    assert rows[HELD]["input_token_price_per_m"] == 1_000_000


@pytest.mark.parametrize("reason", ["provider-canary-failed", "awaiting-price"])
def test_discovery_managed_unroutable_rows_keep_their_snapshot_routes(
    refresh_run: dict[str, Any], reason: str,
) -> None:
    # Narrow on purpose: these states already publish snapshot routes for
    # customer keys today. Whether they should is BYOK policy, not this fix.
    refresh_run["reason"] = reason

    assert refresh.main([]) == 0

    models = _published()
    assert _routes(models[HELD]) == {"other", "tinker"}
    assert _routes(models[ONLY_HELD]) == {"tinker"}


def test_lifted_hold_publishes_the_route_again(refresh_run: dict[str, Any]) -> None:
    # The fresh manifest decides: once a canary lifts the hold, the route
    # returns on the same run.
    refresh_run["reason"] = None

    assert refresh.main([]) == 0

    models = _published()
    assert _routes(models[HELD]) == {"other", "tinker"}
    assert models[HELD]["pricing"]["prompt"] == "0.000001"
    assert _routes(models[ONLY_HELD]) == {"tinker"}


def test_merge_snapshot_drops_held_rows_for_listed_and_synthesized_endpoints() -> None:
    feed = {
        "models": [
            {
                "id": HELD,
                "name": HELD,
                "pricing": {"prompt": "0.000009", "completion": "0.000009"},
                "endpoints": [
                    {"tr_provider_slug": "tinker", "model_id": HELD},
                    {"tr_provider_slug": "other", "model_id": HELD},
                ],
            },
            {"id": SIBLING, "name": SIBLING, "endpoints": []},
        ]
    }
    index = {
        HELD: {"tinker": ModelPrice(1, 1), "other": ModelPrice(5, 5), "maple": ModelPrice(2, 2)},
        SIBLING: {"tinker": ModelPrice(2, 2)},
    }
    held = {"tinker": frozenset({HELD}), "maple": frozenset({HELD})}

    merged = refresh._merge_snapshot(feed, index, set(), held)

    models = {model["id"]: model for model in merged["models"]}
    assert _routes(models[HELD]) == {"other"}
    assert _routes(models[SIBLING]) == {"tinker"}
    # Without holds the same inputs publish every priced route.
    unheld = {m["id"]: m for m in refresh._merge_snapshot(feed, index, set())["models"]}
    assert _routes(unheld[HELD]) == {"tinker", "other", "maple"}


def test_real_tinker_inkling_2_small_hold_is_excluded_from_snapshot_routes() -> None:
    # The committed Tinker manifest holds Inkling 2 Small for account access.
    held = refresh._manifest_held_routes()
    assert "thinkingmachines/inkling-2-small" in held["thinkingmachines"]
    # Its routable siblings are not held.
    assert "thinkingmachines/inkling-small" not in held["thinkingmachines"]
    # Pending enclave qualification is not an account hold (Maple routes
    # are prepaid-only and the runtime filter already drops them).
    assert "thinkingmachines/inkling-2-small" not in held.get("maple", frozenset())


def test_chutes_route_unavailable_holds_are_excluded_from_snapshot_routes() -> None:
    # Chutes still appears on OpenRouter for these models; without the
    # exclusion the hourly refresh would re-publish the dead TEE routes (as
    # BYOK routes) and fail its own snapshot checks.
    held = refresh._manifest_held_routes()
    for model_id in ("qwen/qwen3.5-397b-a17b", "qwen/qwen3.5-27b", "mistralai/mistral-nemo"):
        assert model_id in held["chutes"]
    assert "z-ai/glm-5.2" not in held["chutes"]
