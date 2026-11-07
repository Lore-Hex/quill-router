"""Lyceum's live /models discovery: one unexpected row is held, never the provider."""
from __future__ import annotations

import copy

import pytest

from scripts.pricing.providers import lyceum


def tariff(meter: str, price: str) -> dict[str, str]:
    return {"currency": "USD", "meter": f"serverless_inference_{meter}_tokens", "unit_price": price}


def api_row(native: str = "z-ai/glm-5.3-flash", **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": native, "model_id": native, "publication_status": "published", "kind": "chat",
        "display_name": native, "context_length": 131072, "max_output_tokens": 8192,
        "input_modalities": [{"type": "text", "pricing": [
            tariff("prompt", "0.0000002"), tariff("cached_prompt", "0.00000005")]}],
        "output_modalities": [{"type": "text", "pricing": [tariff("completion", "0.0000005")]}],
    }
    row.update(changes)
    return row


def test_valid_rows_publish_exact_prices() -> None:
    prices, rows = lyceum.discover_api({"data": [api_row()]})
    price = prices["z-ai/glm-5.3-flash"]
    assert (price.prompt_micro_per_m, price.completion_micro_per_m) == (200_000, 500_000)
    assert price.tiers[0].prompt_cached_micro_per_m == 50_000
    assert rows["z-ai/glm-5.3-flash"].get("routable") is not False
    assert lyceum.ROW_ERRORS == []


def free_row() -> dict[str, object]:
    return api_row("meta-llama/Llama-3.3-70B-Instruct", output_modalities=[
        {"type": "text", "pricing": [tariff("completion", "0")]}])


def tiered_row() -> dict[str, object]:
    row = api_row("meta-llama/Llama-3.3-70B-Instruct")
    row["input_modalities"] = [{"type": "text", "pricing": [tariff("reasoning", "0.0000001")]}]
    return row


@pytest.mark.parametrize(("row", "reason"), [
    (api_row("meta-llama/Llama-3.3-70B-Instruct", max_output_tokens=None), "invalid token limits"),
    (api_row("meta-llama/Llama-3.3-70B-Instruct", max_output_tokens=10**9), "invalid token limits"),
    (free_row(), "missing or nonpositive USD token tariff"),
    (tiered_row(), "unknown or duplicate USD token tariff"),
    (api_row("meta-llama/Llama-3.3-70B-Instruct", input_modalities=None), "missing input_modalities"),
    (api_row("meta-llama/Llama-3.3-70B-Instruct", output_modalities=[{"type": "image", "pricing": [
        tariff("completion", "0.0000005")]}]), "unsupported modalities"),
    (api_row("meta-llama/Llama-3.3-70B-Instruct", input_modalities=["text"]), "invalid modality tariffs"),
])
def test_one_unexpected_row_is_held_and_the_rest_publish(row: dict[str, object], reason: str) -> None:
    # A newly listed meta-llama/llama-3.3-70b-instruct row froze every Lyceum
    # route from 2026-10-26 to the 14-day manifest deadline (QUILL-ROUTER-B9).
    prices, rows = lyceum.discover_api({"data": [api_row(), row]})
    assert set(prices) == {"z-ai/glm-5.3-flash"}
    held = rows["meta-llama/llama-3.3-70b-instruct"]
    assert (held["routable"], held["routable_reason"]) == (False, lyceum.ROW_INVALID)
    assert held["upstream_id"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert lyceum.ROW_ERRORS == [f"meta-llama/llama-3.3-70b-instruct: lyceum: {reason}"]


def test_duplicate_mapping_holds_the_model_instead_of_choosing_one() -> None:
    first = api_row("meta-llama/Llama-3.3-70B-Instruct")
    second = api_row("meta-llama/llama-3.3-70b-instruct")
    prices, rows = lyceum.discover_api({"data": [api_row(), first, second]})
    assert set(prices) == {"z-ai/glm-5.3-flash"}
    held = rows["meta-llama/llama-3.3-70b-instruct"]
    assert (held["routable"], held["routable_reason"]) == (False, lyceum.ROW_INVALID)
    assert lyceum.ROW_ERRORS == ["meta-llama/llama-3.3-70b-instruct: duplicate catalog ID"]


def test_unmapped_id_is_reported_and_skipped() -> None:
    prices, rows = lyceum.discover_api({"data": [api_row(), api_row("Llama 3.3 70B?")]})
    assert set(prices) == set(rows) == {"z-ai/glm-5.3-flash"}
    assert lyceum.ROW_ERRORS == ["unmapped catalog ID 'Llama 3.3 70B?'"]


def test_rows_that_hold_never_keep_a_price_from_an_earlier_row() -> None:
    bad = api_row("z-ai/glm-5.3-flash", max_output_tokens=0)
    prices, rows = lyceum.discover_api({"data": [api_row("z-ai/glm-5.3"), api_row(), bad]})
    assert set(prices) == {"z-ai/glm-5.3"}
    assert rows["z-ai/glm-5.3-flash"]["routable"] is False


@pytest.mark.parametrize("payload", [
    {},
    {"data": []},
    {"data": [api_row(max_output_tokens=None)]},
    {"data": [api_row(max_output_tokens=None), free_row()]},
])
def test_catalog_with_nothing_valid_still_fails_closed(payload: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="no data list|no valid priced models"):
        lyceum.discover_api(copy.deepcopy(payload))


def test_held_rows_are_named_in_the_refresh_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYCEUM_API_KEY", "fixture-key")
    payload = {"data": [api_row(), api_row("meta-llama/Llama-3.3-70B-Instruct", max_output_tokens=None)]}
    monkeypatch.setattr(lyceum, "fetch_json", lambda url, extra_headers: payload)
    monkeypatch.setattr(lyceum, "models_requiring_canary", lambda path, ids: [])
    result = lyceum.fetch()
    assert "held meta-llama/llama-3.3-70b-instruct: lyceum: invalid token limits" in result.notes
    assert set(result.prices) == {"z-ai/glm-5.3-flash"}
