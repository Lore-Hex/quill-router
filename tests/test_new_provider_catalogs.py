from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.pricing import unserved_route_holds
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import chutes, cloudflare_workers_ai, digitalocean
from tests import catalog_vehicles
from tests.provider_holds import row_taken_down_by_refresh
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
)
from trusted_router.provider_contracts import INPUT_ONLY_PROVIDER_MODELS
from trusted_router.provider_manifest_policy import UNSERVED_ROUTE_HOLD_REASON


def test_digitalocean_official_pricing_uses_integer_microdollars() -> None:
    markdown = """
| DeepSeek | [DeepSeek V4 Flash](https://example.test) | Input/output tokens | $0.112 per 1M tokens<br>$0.224 per 1M tokens<br>Prompt caching $0.028 per 1M tokens |
| Z.AI | [GLM-5.2](https://example.test) | Input/output tokens | $1.40 per 1M tokens<br>$4.40 per 1M tokens<br>Prompt caching $0.21 per 1M tokens |
"""

    prices = digitalocean._official_prices(markdown)

    assert prices["deepseek/deepseek-v4-flash"] == ModelPrice(
        prompt_micro_per_m=112_000,
        completion_micro_per_m=224_000,
        prompt_cached_micro_per_m=28_000,
    )
    assert prices["z-ai/glm-5.2"] == ModelPrice(
        prompt_micro_per_m=1_400_000,
        completion_micro_per_m=4_400_000,
        prompt_cached_micro_per_m=210_000,
    )


def test_cloudflare_uses_public_model_name_not_opaque_catalog_uuid() -> None:
    assert (
        cloudflare_workers_ai._canonical_model_id("@cf/moonshotai/kimi-k2.7-code")
        == "moonshotai/kimi-k2.7-code"
    )
    assert (
        cloudflare_workers_ai._canonical_model_id("@cf/meta/llama-4-scout")
        == "meta-llama/llama-4-scout"
    )
    assert (
        cloudflare_workers_ai._canonical_model_id("@cf/zai-org/glm-5.2")
        == "z-ai/glm-5.2"
    )


def test_cloudflare_pricing_parses_input_cache_and_output_units() -> None:
    price = cloudflare_workers_ai._model_price(
        {
            "price": [
                {"unit": "per M input tokens", "price": "0.95"},
                {"unit": "per M cached input tokens", "price": "0.19"},
                {"unit": "per M output tokens", "price": "4.00"},
            ]
        }
    )

    assert price == ModelPrice(
        prompt_micro_per_m=950_000,
        completion_micro_per_m=4_000_000,
        prompt_cached_micro_per_m=190_000,
    )


def _cloudflare_row_state_is_accepted(row: dict[str, object]) -> bool:
    # A row is routable, awaiting a price, held unroutable after the refresh's
    # live canary failed (it recovers on a passing canary), held by the
    # refresh because Cloudflare refuses every probe while serving its other
    # models (provider-route-unserved, re-probed after the hold), or
    # tombstoned by the refresh after the provider delisted it.
    reason = row.get("routable_reason")
    return bool(
        row.get("routable") is True and reason is None
        or row.get("routable") is False and (
            reason in {"awaiting-price", "account-unfunded", "provider-canary-failed"}
            or row_taken_down_by_refresh(row)
            or reason == "delisted-upstream" and row.get("missing_since")
        )
    )


def test_cloudflare_committed_manifest_exposes_only_funded_priced_routes() -> None:
    manifest = json.loads(cloudflare_workers_ai.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = manifest["models"]

    assert rows
    for row in rows:
        assert _cloudflare_row_state_is_accepted(row), (row["id"], row.get("routable"), row.get("routable_reason"))
    assert all(
        row["upstream_id"].startswith("@cf/")
        or row["upstream_id"] == "moonshotai/kimi-k3"
        for row in rows
    )


@pytest.mark.parametrize(("reason", "priced", "age_hours", "source", "prior", "expected"), [
    (None, True, 25, "api", None, None),
    (None, True, 25, "api", False, "provider-canary-failed"),
    ("account-unfunded", True, 25, "api", True, "account-unfunded"),
    ("provider-canary-failed", True, 25, "api", None, "provider-canary-failed"),
    ("awaiting-price", False, 25, "api", None, "awaiting-price"),
    (None, False, 25, "api", None, "awaiting-price"),
    (None, True, 1, "api", None, "provider-route-unserved"),
    (None, True, 25, "cache", None, "provider-route-unserved"),
])
def test_cloudflare_unserved_recovery_needs_fresh_provider_disposition(
    tmp_path, monkeypatch, reason, priced, age_hours, source, prior, expected,
):
    from scripts.pricing.unserved_route_holds import apply_unserved_route_holds

    now = datetime.now(UTC)
    model = "meta-llama/llama-3.2-11b-vision-instruct"
    manifest = tmp_path / "cloudflare-workers-ai.json"
    held = {"id": model, "model_type": "chat", "upstream_id": "@cf/meta/llama-3.2-11b-vision-instruct",
            "routable": False, "routable_reason": "provider-route-unserved", "unserved_hold_count": 1,
            "unserved_held_at": (now - timedelta(hours=age_hours)).isoformat(),
            "input_token_price_per_m": 48500, "output_token_price_per_m": 676000}
    if prior is not None:
        held["unserved_prior_routable"] = prior
        if not prior:
            held["unserved_prior_routable_reason"] = "provider-canary-failed"
    manifest.write_text(json.dumps({"models": [held]}))
    if prior is not False:
        apply_unserved_route_holds(set(), manifest_dir=tmp_path, now=now)
        assert json.loads(manifest.read_text())["models"] == [held]  # A timer is not a funding check.

    observed = {"id": model, "upstream_id": held["upstream_id"], "routable": reason is None}
    if reason:
        observed["routable_reason"] = reason
    monkeypatch.setattr(cloudflare_workers_ai, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(cloudflare_workers_ai, "_DISCOVERED_MANIFEST_ROWS", {model: observed})
    result = ProviderPricingResult(
        slug=cloudflare_workers_ai.SLUG, source=source, fetched_url="https://example.test/prices",
        prices={model: ModelPrice(prompt_micro_per_m=48500, completion_micro_per_m=676000)} if priced else {},
    )
    cloudflare_workers_ai.write_provider_manifest(result)
    apply_unserved_route_holds(set(), manifest_dir=tmp_path, now=datetime.now(UTC))
    row, = json.loads(manifest.read_text())["models"]
    assert row["routable"] is (expected is None)
    assert row.get("routable_reason") == expected
    assert ("unserved_lifted_at" in row) is (expected != "provider-route-unserved")
    assert row["unserved_hold_count"] == 1


def test_cloudflare_refuses_ambiguous_candidate_before_writing(tmp_path, monkeypatch):
    model = "example/model"
    path = tmp_path / "cloudflare-workers-ai.json"
    path.write_text(json.dumps({"models": [
        {"id": model, "upstream_id": "@cf/example/model", "model_type": "chat", "routable": True,
         "input_token_price_per_m": 1, "output_token_price_per_m": 2},
    ]}))
    original = path.read_bytes()
    monkeypatch.setattr(cloudflare_workers_ai, "MANIFEST_PATH", path)
    monkeypatch.setattr(cloudflare_workers_ai, "_DISCOVERED_MANIFEST_ROWS", {model: {"id": model, "routable": None}})
    with pytest.raises(ValueError, match="routing disposition for 'example/model'"):
        cloudflare_workers_ai.write_provider_manifest(ProviderPricingResult(
            slug=cloudflare_workers_ai.SLUG, source="api", fetched_url="https://example.test/prices",
            prices={model: ModelPrice(prompt_micro_per_m=1, completion_micro_per_m=2)},
        ))
    assert path.read_bytes() == original


def test_a_lifted_unserved_hold_leaves_a_cloudflare_row_in_an_accepted_state(tmp_path: Path, monkeypatch) -> None:
    # 2026-11-28 00:24Z refresh: the expired hold on this row was lifted by
    # popping `routable`, which left a row that is neither routable=true nor
    # any accepted unroutable state, and the snapshot failed the test above.
    # The row is exactly as main held it (a hold placed before holds recorded
    # the row's prior routability); its siblings are funded, priced routes.
    def priced(model_id: str, **extra: object) -> dict[str, object]:
        return {
            "id": model_id, "upstream_id": f"@cf/{model_id}", "model_type": "chat",
            "endpoints": ["chat/completions"], "status": 1, "context_length": 128000,
            "input_token_price_per_m": 48500, "output_token_price_per_m": 676000, **extra,
        }

    held = priced(
        "meta-llama/llama-3.2-11b-vision-instruct", routable=False,
        routable_reason=UNSERVED_ROUTE_HOLD_REASON,
        unserved_held_at="2026-11-26T23:28:44Z", unserved_hold_count=1,
    )
    siblings = [priced(f"meta/sibling-{index}", routable=True) for index in range(3)]
    path = tmp_path / f"{cloudflare_workers_ai.MANIFEST_PATH.stem}.json"
    path.write_text(json.dumps({"provider": "cloudflare-workers-ai", "models": [held, *siblings]}) + "\n",
                    encoding="utf-8")

    # The expiry may restore the funded route, but the timer cannot attest to
    # funding. Use the normal provider writer first, as the workflow does.
    now = datetime(2026, 11, 28, 0, 28, 36, tzinfo=UTC)
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(cloudflare_workers_ai, "datetime", clock)
    fresh = {str(r["id"]): {"id": r["id"], "upstream_id": r["upstream_id"], "routable": True}
             for r in [held, *siblings]}
    monkeypatch.setattr(cloudflare_workers_ai, "MANIFEST_PATH", path)
    monkeypatch.setattr(cloudflare_workers_ai, "_DISCOVERED_MANIFEST_ROWS", fresh)
    cloudflare_workers_ai.write_provider_manifest(ProviderPricingResult(
        slug=cloudflare_workers_ai.SLUG, source="api",
        prices=dict.fromkeys(fresh, ModelPrice(prompt_micro_per_m=48500, completion_micro_per_m=676000)),
    ))
    unserved_route_holds.apply_unserved_route_holds(
        set(), manifest_dir=tmp_path, now=now,
    )

    rows = {row["id"]: row for row in json.loads(path.read_text(encoding="utf-8"))["models"]}
    lifted = rows["meta-llama/llama-3.2-11b-vision-instruct"]
    assert lifted["routable"] is True
    assert "routable_reason" not in lifted
    assert all(_cloudflare_row_state_is_accepted(row) for row in rows.values())


@pytest.mark.provider_health
def test_cloudflare_lists_kimi_k3_at_its_native_id_with_a_1m_window() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    rows = json.loads(cloudflare_workers_ai.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    kimi_k3 = next(row for row in rows if row["id"] == "moonshotai/kimi-k3")
    assert kimi_k3["upstream_id"] == "moonshotai/kimi-k3"
    assert kimi_k3["context_length"] == 1_048_576


def test_discovered_writer_never_routes_a_new_unpriced_model(tmp_path: Path) -> None:
    manifest_path = tmp_path / "provider.json"
    manifest_path.write_text(
        json.dumps({"provider": "example", "models": []}) + "\n",
        encoding="utf-8",
    )
    result = ProviderPricingResult(
        slug="example",
        prices={},
        source="api",
        fetched_url="https://example.test/models",
    )

    from scripts.pricing.manifest import write_discovered_chat_manifest

    write_discovered_chat_manifest(
        result,
        manifest_path=manifest_path,
        discovered_rows={
            "example/model": {
                "id": "example/model",
                "upstream_id": "native-model",
                "display_name": "Example Model",
            }
        },
        source_url="https://example.test/models",
    )

    row = json.loads(manifest_path.read_text(encoding="utf-8"))["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "awaiting-price"


def test_new_provider_privacy_and_gateway_registration() -> None:
    assert {"chutes", "digitalocean", "cloudflare-workers-ai"}.issubset(
        GATEWAY_PREPAID_PROVIDER_SLUGS
    )
    assert PROVIDERS["chutes"].provider_zero_data_retention is True
    assert PROVIDERS["chutes"].provider_confidential_compute is True
    assert PROVIDERS["chutes"].provider_e2ee is True
    assert "Verification fails closed" in PROVIDERS["chutes"].provider_policy
    assert PROVIDERS["cloudflare-workers-ai"].supports_byok is False


def test_new_provider_manifests_create_only_eligible_routes() -> None:
    endpoints = list(catalog_vehicles.registry_endpoints().values())

    # A provider routes only its manifest's routable rows: none for a row the
    # refresh tombstoned or one still awaiting a price.
    for provider in (chutes, digitalocean, cloudflare_workers_ai):
        manifest = json.loads(provider.MANIFEST_PATH.read_text(encoding="utf-8"))
        routable = {row["id"] for row in manifest["models"] if row.get("routable") is not False}
        routed = {endpoint.model_id for endpoint in endpoints if endpoint.provider == provider.SLUG}
        # The refresh's own unserved-route hold ends the route on TrustedRouter's
        # credentials. The snapshot that refresh wrote before the hold still
        # lists the route, so a customer's own key keeps it (BYOK), as for any
        # Credits-only manifest hold.
        held_unserved = {row["id"] for row in manifest["models"] if row_taken_down_by_refresh(row)}
        on_credits = {
            endpoint.model_id for endpoint in endpoints
            if endpoint.provider == provider.SLUG and endpoint.usage_type == "Credits"
        }
        assert on_credits <= routable, provider.SLUG
        assert routed <= routable | held_unserved, provider.SLUG
    cloudflare = [
        endpoint
        for endpoint in endpoints
        if endpoint.provider == "cloudflare-workers-ai"
    ]
    assert not any(
        endpoint.model_id == "meta-llama/llama-guard-3-8b"
        for endpoint in cloudflare
    )
    assert all(
        endpoint.prompt_price_microdollars_per_million_tokens > 0
        and (
            endpoint.completion_price_microdollars_per_million_tokens == 0
            if (endpoint.provider, endpoint.model_id) in INPUT_ONLY_PROVIDER_MODELS
            else endpoint.completion_price_microdollars_per_million_tokens > 0
        )
        for endpoint in cloudflare
    )


@pytest.mark.provider_health
def test_new_providers_serve_routes_and_cloudflare_serves_kimi_k3() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    endpoints = list(MODEL_ENDPOINTS.values())
    assert any(endpoint.provider == "chutes" for endpoint in endpoints)
    assert any(endpoint.provider == "digitalocean" for endpoint in endpoints)
    assert any(
        endpoint.provider == "cloudflare-workers-ai"
        and endpoint.model_id == "moonshotai/kimi-k3"
        and endpoint.upstream_id == "moonshotai/kimi-k3"
        for endpoint in endpoints
    )


def test_chutes_manifest_contains_only_confidential_compute_rows() -> None:
    manifest = json.loads(chutes.MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["models"]
    assert all(row["confidential_compute"] is True for row in manifest["models"])
    assert any(row["id"] == "z-ai/glm-5.2" for row in manifest["models"])


def test_digitalocean_manifest_preserves_exact_upstream_ids() -> None:
    manifest = json.loads(digitalocean.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in manifest["models"]}

    assert rows["deepseek/deepseek-v4-flash"]["upstream_id"] == "deepseek-4-flash"
    assert rows["z-ai/glm-5.2"]["upstream_id"] == "glm-5.2"


@pytest.mark.provider_health
def test_digitalocean_prices_glm_52_with_a_paid_discounted_cache_read() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    manifest = json.loads(digitalocean.MANIFEST_PATH.read_text(encoding="utf-8"))
    glm = next(row for row in manifest["models"] if row["id"] == "z-ai/glm-5.2")
    assert glm["input_token_price_per_m"] > 0
    assert glm["output_token_price_per_m"] > 0
    assert 0 < glm["cached_input_token_price_per_m"] < glm["input_token_price_per_m"]
