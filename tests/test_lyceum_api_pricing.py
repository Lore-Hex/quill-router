from copy import deepcopy

import pytest

from scripts.pricing.providers import lyceum


def model_row(model="z-ai/glm-5.3-flash", *, kind="chat"):
    def meter(name, value, currency="USD"):
        return {"meter": "serverless_inference_" + name + "_tokens",
                "unit_price": value, "currency": currency}

    return {
        "id": model, "model_id": model, "kind": kind,
        "publication_status": "published", "display_name": model,
        "context_length": 1048576, "max_output_tokens": 65536,
        "input_modalities": [{"type": "text", "pricing": [
            meter("prompt", "0.00000018", "EUR"),
            meter("prompt", "0.0000002"), meter("cached_prompt", "0.00000005"),
        ]}],
        "output_modalities": [{"type": "text", "pricing": [
            meter("completion", "0.0000005"),
        ]}],
    }


def test_exact_api_prices_and_binary_context_without_scraping_html():
    row = model_row()
    row["input_modalities"].append({"type": "image"})
    prices, rows = lyceum.discover_api({"data": [row]})
    price = prices[row["id"]]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (200000, 500000)
    assert price.tiers[0].prompt_cached_micro_per_m == 50000
    assert rows[row["id"]]["context_length"] == 1048576
    assert rows[row["id"]]["input_modalities"] == ["text", "image"]


def test_api_alias_cannot_silently_replace_requested_weights():
    row = model_row("moonshotai/kimi-k2.6")
    row["model_id"] = "moonshotai/kimi-k3"
    prices, rows = lyceum.discover_api({"data": [model_row(), row]})
    assert row["id"] not in prices
    assert rows[row["id"]]["routable"] is False
    assert rows[row["id"]]["routable_reason"] == "upstream-model-alias"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.1", "0", True, "0.0000000000001", "1e10000"])
def test_invalid_api_price_fails_closed(value):
    row = model_row()
    row["input_modalities"][0]["pricing"][1]["unit_price"] = value
    with pytest.raises(RuntimeError):
        lyceum.discover_api({"data": [row]})


def test_conflicting_modality_prices_and_missing_usd_fail_closed():
    row = model_row()
    image = deepcopy(row["input_modalities"][0])
    image["type"] = "image"
    image["pricing"][1]["unit_price"] = "0.0000003"
    row["input_modalities"].append(image)
    with pytest.raises(RuntimeError):
        lyceum.discover_api({"data": [row]})
    row = model_row()
    row["input_modalities"][0]["pricing"] = row["input_modalities"][0]["pricing"][:1]
    with pytest.raises(RuntimeError):
        lyceum.discover_api({"data": [row]})


def test_embedding_api_price_has_no_output_charge():
    row = model_row("qwen/qwen3-embedding-8b", kind="embedding")
    row["max_output_tokens"] = None
    row["input_modalities"][0]["pricing"] = [
        {"meter": "serverless_inference_prompt_tokens", "unit_price": "0.00000002", "currency": "USD"},
        {"meter": "serverless_inference_cached_prompt_tokens", "unit_price": "0.00", "currency": "USD"},
    ]
    row["output_modalities"] = [{"type": "embedding"}]
    prices, rows = lyceum.discover_api({"data": [row]})
    assert prices[row["id"]].completion_micro_per_m == 0
    assert rows[row["id"]]["endpoints"] == ["embeddings"]
    assert prices[row["id"]].tiers[0].prompt_cached_micro_per_m is None


def test_unpublished_and_duplicate_catalog_rows_fail_closed():
    row = model_row()
    row["publication_status"] = "retired"
    prices, rows = lyceum.discover_api({"data": [model_row("z-ai/glm-5.3"), row]})
    assert row["id"] not in prices
    assert rows[row["id"]]["routable"] is False
    with pytest.raises(RuntimeError):
        lyceum.discover_api({"data": [model_row(), model_row()]})
