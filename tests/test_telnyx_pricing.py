from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import telnyx


def test_complete_native_prices_do_not_depend_on_secondary_sources(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    payload = _live_payload()
    for row in payload["data"]:
        row["pricing"] = {
            "input": "0.135",
            "output": "0.450",
            "cached_prompt": "0.027",
            "currency": "USD",
            "unit": "1M_tokens",
        }
    payload["data"].append(
        _live_model(
            "zai-org/GLM-5.3-Flash",
            pricing=payload["data"][0]["pricing"],
        )
    )

    def fetch_native_only(url: str, **_kwargs) -> dict:  # noqa: ANN003
        assert url == telnyx.MODELS_URL, (
            "complete native prices need no secondary network dependency"
        )
        return payload

    monkeypatch.setattr(telnyx, "fetch_json", fetch_native_only)
    result = telnyx.fetch()
    assert result.prices["z-ai/glm-5.3-flash"] == ModelPrice(
        135_000,
        450_000,
        prompt_cached_micro_per_m=27_000,
    )
    assert "openai/gpt-5.5" not in result.prices


def test_telnyx_rejects_foreign_currency_and_excludes_priority_only() -> None:
    price = {"input": "1", "output": "4", "currency": "EUR", "unit": "1M_tokens"}
    with pytest.raises(RuntimeError, match="unsupported pricing currency"):
        telnyx._live_catalog({"data": [_live_model("zai-org/GLM-5.3", pricing=price)]})  # noqa: SLF001
    priority = _live_model("zai-org/GLM-5.3")
    priority["service_tiers"] = ["priority"]
    discovered, _ = telnyx._live_catalog(
        {
            "data": [  # noqa: SLF001
                priority,
                _live_model("moonshotai/Kimi-K3"),
                _live_model("Groq/gpt-oss-120b", owned_by="Groq"),
                _live_model("anthropic/claude-haiku-4-5", owned_by="anthropic"),
                _live_model("google/gemini-3.7-flash", owned_by="google"),
            ]
        }
    )
    assert set(discovered) == {"moonshotai/kimi-k3"}


def test_telnyx_currency_and_tier_contract_variants() -> None:
    row = _live_model(
        "zai-org/GLM-5.3",
        pricing={
            "input": "1",
            "output": "4",
            "currency": "usd",
            "unit": "1M_tokens",
        },
    )
    row["service_tiers"] = ["Default"]
    _, prices = telnyx._live_catalog({"data": [row]})  # noqa: SLF001
    assert prices["z-ai/glm-5.3"] == ModelPrice(1_000_000, 4_000_000)
    del row["pricing"]["currency"]
    discovered, prices = telnyx._live_catalog({"data": [row]})  # noqa: SLF001
    assert "z-ai/glm-5.3" in discovered
    assert not prices
    for missing in (None, "", " "):
        row["pricing"]["currency"] = missing
        _, prices = telnyx._live_catalog({"data": [row]})  # noqa: SLF001
        assert not prices
    for invalid in ({"default": True}, [], [None], [" "]):
        row["service_tiers"] = invalid
        with pytest.raises(RuntimeError, match="invalid service_tiers"):
            telnyx._live_catalog({"data": [row]})  # noqa: SLF001


def _live_model(
    native_id: str,
    *,
    context_length: int = 100_000,
    vision: bool = False,
    pricing: dict[str, str] | None = None,
    owned_by: str = "Telnyx",
) -> dict:
    return {
        "id": native_id,
        "owned_by": owned_by,
        "task": "text-generation",
        "context_length": context_length,
        "max_completion_tokens": 64_000 if native_id.endswith("Kimi-K3") else None,
        "is_vision_supported": vision,
        "regions": ["us-east-1"],
        "pricing": {
            "currency": "USD",
            **(
                pricing
                or {
                    "input": "0.000000",
                    "output": "0.000000",
                    "cached_prompt": "0.000000",
                    "unit": "1M_tokens",
                }
            ),
        },
    }


def _live_payload() -> dict:
    rows = [
        _live_model("google/gemma-2b-it", context_length=8192),
        _live_model("meta-llama/Llama-3.3-70B-Instruct", context_length=99_000),
        _live_model("meta-llama/Meta-Llama-3.1-70B-Instruct", context_length=99_000),
        _live_model("meta-llama/Meta-Llama-3.1-8B-Instruct", context_length=131_072),
        _live_model("MiniMaxAI/MiniMax-M2.7", context_length=200_000),
        _live_model("MiniMaxAI/MiniMax-M3-MXFP8", context_length=1_000_000),
        _live_model("moonshotai/Kimi-K2.5", context_length=256_000, vision=True),
        _live_model("moonshotai/Kimi-K2.6", context_length=262_144, vision=True),
        _live_model(
            "moonshotai/Kimi-K3",
            context_length=1_000_000,
            vision=True,
            pricing={
                "input": "2.700000",
                "output": "13.500000",
                "cached_prompt": "0.270000",
                "unit": "1M_tokens",
            },
        ),
        _live_model("Qwen/Qwen3-235B-A22B", context_length=32_768),
        _live_model("zai-org/GLM-5.1-FP8", context_length=202_752),
        _live_model("zai-org/GLM-5.2", context_length=1_000_000),
        _live_model("openai/gpt-5.5", owned_by="openai"),
    ]
    return {"object": "list", "data": rows}


def _product_row(name: str, tier: str = "standard") -> dict:
    return {
        "model": name,
        "service_tier": tier,
        "rates": {
            "currency": "USD",
            "unit": "per_1k_tokens",
            "values": {
                field: [{"min": 100000, "max": None, "rate": rate}]
                for field, rate in (
                    ("input", "0.000135"),
                    ("output", "0.000450"),
                    ("cached_input", "0.000027"),
                )
            },
        },
        "free_allowance": {"unit": "tokens", "values": {"input": 100000}},
    }


def test_telnyx_fetch_uses_standard_json_prices_and_live_membership(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    payload = {
        "data": [
            _live_model("zai-org/GLM-5.3-Flash"),
            _live_model("Qwen/Future-Unpriced"),
            _live_payload()["data"][8],
        ]
    }
    product_rows = [
        _product_row("glm-5.3-flash", "priority"),
        _product_row("glm-5.3-flash"),
        _product_row("gpt-5"),
        _product_row("kimi-k3"),
    ]

    def fake_fetch_json(url: str, **kwargs) -> dict:  # noqa: ANN003
        assert kwargs["extra_headers"] == {"Authorization": "Bearer test-key"}
        if url == telnyx.MODELS_URL:
            return payload
        if url == telnyx.PRODUCT_PRICING_URL:
            return {"data": product_rows}
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(telnyx, "fetch_json", fake_fetch_json)
    result = telnyx.fetch()
    assert set(result.prices) == {"z-ai/glm-5.3-flash", "moonshotai/kimi-k3"}
    assert result.prices["z-ai/glm-5.3-flash"] == ModelPrice(
        135_000, 450_000, prompt_cached_micro_per_m=27_000
    )
    assert result.prices["moonshotai/kimi-k3"].prompt_micro_per_m == 2_700_000
    rows = telnyx._DISCOVERED_MANIFEST_ROWS  # noqa: SLF001
    assert rows["z-ai/glm-5.3-flash"]["pricing_source"] == telnyx.PRODUCT_PRICING_URL
    assert rows["moonshotai/kimi-k3"]["pricing_source"] == telnyx.MODELS_URL
    path = tmp_path / "telnyx.json"
    monkeypatch.setattr(telnyx, "MANIFEST_PATH", path)
    telnyx.write_provider_manifest(result)
    written = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert written["qwen/future-unpriced"]["routable"] is False
    assert written["qwen/future-unpriced"]["routable_reason"] == "awaiting-price"
    assert "input_token_price_per_m" not in written["qwen/future-unpriced"]


@pytest.mark.parametrize("tier", ["priority", "flex", "unknown", None])
def test_product_prices_never_publish_nonstandard_tiers(tier: str | None) -> None:
    discovered, _ = telnyx._live_catalog({"data": [_live_model("zai-org/GLM-5.3")]})  # noqa: SLF001
    row = _product_row("glm-5.3")
    row["service_tier"] = tier
    assert not telnyx._product_prices({"data": [row]}, discovered)  # noqa: SLF001


@pytest.mark.parametrize("field,value", [("currency", "EUR"), ("unit", "per_token")])
def test_product_pricing_rejects_currency_or_unit_changes(field: str, value: str) -> None:
    discovered, _ = telnyx._live_catalog({"data": [_live_model("zai-org/GLM-5.3")]})  # noqa: SLF001
    row = _product_row("glm-5.3")
    row["rates"][field] = value
    with pytest.raises(RuntimeError, match="unsupported product pricing units"):
        telnyx._product_prices({"data": [row]}, discovered)  # noqa: SLF001


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-1", "broken", "0.0009"])
def test_product_pricing_rejects_invalid_or_variable_volume_rates(rate: str) -> None:
    discovered, _ = telnyx._live_catalog({"data": [_live_model("zai-org/GLM-5.3")]})  # noqa: SLF001
    row = _product_row("glm-5.3")
    row["rates"]["values"]["input"].append({"min": 1000000, "max": None, "rate": rate})
    assert not telnyx._product_prices({"data": [row]}, discovered)  # noqa: SLF001


def test_catalog_rejects_duplicate_identity() -> None:
    row = _live_model("zai-org/GLM-5.3")
    with pytest.raises(RuntimeError, match="duplicate canonical model"):
        telnyx._live_catalog({"data": [row, row]})  # noqa: SLF001


def test_product_pricing_rejects_duplicate_standard_rates() -> None:
    discovered, _ = telnyx._live_catalog({"data": [_live_model("zai-org/GLM-5.3")]})  # noqa: SLF001
    row = _product_row("glm-5.3")
    with pytest.raises(RuntimeError, match="duplicate standard product price"):
        telnyx._product_prices({"data": [row, row]}, discovered)  # noqa: SLF001


def test_free_cached_input_is_preserved() -> None:
    row = _live_model(
        "zai-org/GLM-5.3",
        pricing={
            "input": "1",
            "output": "4",
            "cached_prompt": "0",
            "unit": "1M_tokens",
        },
    )
    _, prices = telnyx._live_catalog({"data": [row]})  # noqa: SLF001
    assert prices["z-ai/glm-5.3"].tiers[0].prompt_cached_micro_per_m == 0


@pytest.mark.parametrize("cached", ["-1", "NaN", "broken"])
def test_bad_cached_rate_does_not_publish_undiscounted_price(cached: str) -> None:
    row = _live_model(
        "zai-org/GLM-5.3",
        pricing={
            "input": "1",
            "output": "4",
            "cached_prompt": cached,
            "unit": "1M_tokens",
        },
    )
    _, prices = telnyx._live_catalog({"data": [row]})  # noqa: SLF001
    assert not prices


def test_telnyx_zero_catalog_prices_are_not_interpreted_as_free() -> None:
    discovered, direct_prices = telnyx._live_catalog(_live_payload())  # noqa: SLF001

    assert set(discovered) == set(telnyx.EXPECTED_MODELS)
    assert set(direct_prices) == {"moonshotai/kimi-k3"}
    assert direct_prices["moonshotai/kimi-k3"].prompt_micro_per_m == 2_700_000


def test_telnyx_future_owned_priced_model_is_discovered_without_a_hand_map() -> None:
    payload = {
        "data": [
            _live_model(
                "Qwen/Qwen4-Next",
                pricing={
                    "input": "0.123456",
                    "output": "1.234567",
                    "cached_prompt": "0.012345",
                    "unit": "1M_tokens",
                },
            )
        ]
    }

    discovered, prices = telnyx._live_catalog(payload)  # noqa: SLF001

    assert discovered["qwen/qwen4-next"]["upstream_id"] == "Qwen/Qwen4-Next"
    assert prices["qwen/qwen4-next"] == ModelPrice(
        123_456,
        1_234_567,
        prompt_cached_micro_per_m=12_345,
    )
    assert telnyx.UPSTREAM_ID_MAP["qwen/qwen4-next"] == "Qwen/Qwen4-Next"


def test_telnyx_manifest_keeps_context_vision_and_exact_native_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "telnyx.json"
    manifest_path.write_text(
        json.dumps({"provider": "telnyx", "models": []}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(telnyx, "MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    payload = _live_payload()
    for row in payload["data"]:
        row["pricing"].update(input="0.5", output="1", cached_prompt="0.1")
        row["service_tiers"] = ["default", "priority"]
        row["license"] = "mit"
    monkeypatch.setattr(
        telnyx,
        "fetch_json",
        lambda url, **_kwargs: payload,
    )

    result = telnyx.fetch()
    notes = telnyx.write_provider_manifest(result)

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in raw["models"]}
    assert raw["model_count"] == 12
    assert "12 priced rows" in notes[0]
    assert rows["moonshotai/kimi-k3"]["upstream_id"] == "moonshotai/Kimi-K3"
    assert rows["moonshotai/kimi-k3"]["context_length"] == 1_000_000
    assert rows["moonshotai/kimi-k3"]["max_output_tokens"] == 64_000
    assert rows["moonshotai/kimi-k3"]["input_modalities"] == ["text", "image"]
    assert rows["z-ai/glm-5.2"]["input_modalities"] == ["text"]
    assert rows["z-ai/glm-5.2"]["provider_regions"] == ["us-east-1"]
    assert rows["z-ai/glm-5.2"]["provider_service_tiers"] == ["default", "priority"]
    assert rows["z-ai/glm-5.2"]["license"] == "mit"
    assert rows["z-ai/glm-5.2"]["pricing_source"] == telnyx.MODELS_URL

    # A cleared upstream limit must not leave last week's limit in metadata.
    payload["data"][8]["max_completion_tokens"] = None
    telnyx.write_provider_manifest(telnyx.fetch())
    rows = {row["id"]: row for row in json.loads(manifest_path.read_text())["models"]}
    assert rows["moonshotai/kimi-k3"]["max_output_tokens"] is None


def test_telnyx_regions_follow_default_tier_and_clear_removed_declarations(
    tmp_path: Path, monkeypatch,
) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "telnyx.json"
    manifest_path.write_text(json.dumps({"provider": "telnyx", "models": []}))
    monkeypatch.setattr(telnyx, "MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("TELNYX_API_KEY", "test-key")
    payload = _live_payload()
    for row in payload["data"]:
        row["pricing"].update(input="0.5", output="1", cached_prompt="0.1")
        row["regions"] = ["USA", "EU"]
        row["regions_by_service_tier"] = {"default": ["USA"], "priority": ["EU"]}
    monkeypatch.setattr(telnyx, "fetch_json", lambda url, **kwargs: payload)
    telnyx.write_provider_manifest(telnyx.fetch())
    assert all(row["provider_regions"] == ["USA"] for row in json.loads(manifest_path.read_text())["models"])
    for row in payload["data"]:
        row["regions_by_service_tier"] = {"priority": ["EU"]}
    telnyx.write_provider_manifest(telnyx.fetch())
    assert all(row["provider_regions"] == [] for row in json.loads(manifest_path.read_text())["models"])


def test_telnyx_manifest_is_loaded_as_prepaid_and_byok_catalog_routes() -> None:
    from trusted_router.catalog import MODEL_ENDPOINTS

    manifest = json.loads(telnyx.MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_model_ids = {str(row["id"]) for row in manifest["models"]}
    endpoints = [endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "telnyx"]
    assert set(telnyx.EXPECTED_MODELS) <= manifest_model_ids
    assert {(endpoint.model_id, endpoint.usage_type) for endpoint in endpoints} == {
        (model_id, usage_type)
        for model_id in manifest_model_ids
        for usage_type in ("Credits", "BYOK")
    }
    assert MODEL_ENDPOINTS["moonshotai/kimi-k3@telnyx/prepaid"].upstream_id == "moonshotai/Kimi-K3"
