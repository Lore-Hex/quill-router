"""Featherless's API omits cache rates published in its official price tables."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from scripts.pricing.openai_catalog import openai_model_price
from scripts.pricing.providers import featherless

NO_TABLE_PRICES = (
    Path(__file__).parent / "fixtures/pricing/featherless_request_pricing_2026-10-11.html"
).read_text(encoding="utf-8")

PRICES = """
<table><tr><th>Family</th><th>Model Class</th><th>Input</th>
<th>Cached input / 1M tokens</th><th>Output / 1M tokens</th></tr>
<tr><td>GLM 5.2</td><td>glm52-753b</td><td>$1.4 / 1M tok</td>
<td>$0.15</td><td>$4.4</td></tr>
<tr><td>GLM 5.3</td><td>glm-moe-dsa-753b3</td><td>$1 / 1M tok</td>
<td>-</td><td>$4</td></tr>
<tr><td>GLM Flash</td><td>glm5-next-321b3</td><td>$0.15 / 1M tok</td>
<td>$0.03</td><td>$0.5</td></tr>
<tr><td>Deepseek 4</td><td>deepseek4-284b</td><td>$0.1385 / 1M tok</td>
<td>$0.03</td><td>$0.279</td></tr>
<tr><td>Deepseek 4.1</td><td>deepseek4-763b</td><td>$0.3 / 1M tok</td>
<td>$0.03</td><td>$1.2</td></tr>
<tr><td>Music</td><td>music</td><td>$22 / 1M chars</td><td>-</td><td>n/a</td></tr>
</table>
<table><tr><th>Model</th><th>Family</th><th>Model Class</th><th>Input</th>
<th>Cached input / 1M tokens</th><th>Output / 1M tokens</th></tr>
<tr><td>zai-org/GLM-5.3</td><td>GLM</td><td>glm-moe-dsa-753b3</td>
<td>$1.4 / 1M tok</td><td>$0.26</td><td>$4.4</td></tr>
<tr><td>deepseek-ai/DeepSeek-V4-Flash-0731</td><td>Deepseek 4</td>
<td>deepseek4-284b</td><td>$0.14 / 1M tok</td><td>$0.03</td><td>$0.28</td></tr>
</table>
"""


def _row(
    model: str = "zai-org/GLM-5.2",
    model_class: str = "glm52-753b",
    prompt: str = "0.0000014",
    completion: str = "0.0000044",
) -> dict[str, Any]:
    return {
        "id": model,
        "model_class": model_class,
        "pricing": {"prompt": prompt, "completion": completion, "input": 1.4, "output": 4.4},
    }


def _normalize(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]], html: str = PRICES):
    monkeypatch.setattr(featherless, "fetch_html", lambda _url: html)
    return featherless._normalize_rows(rows)


@pytest.mark.parametrize(
    ("row", "cached"),
    [
        (_row(), 150_000),
        (_row("deepseek-ai/DeepSeek-V4.1-Flash", "deepseek4-763b", "0.0000003", "0.0000012"), 30_000),
        (_row("zai-org/GLM-5.3", "glm-moe-dsa-753b3"), 260_000),
        (_row("zai-org/GLM-5.3-Flash", "glm5-next-321b3", "0.00000015", "0.0000005"), 30_000),
        (
            _row(
                "deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek4-284b", "0.00000014", "0.00000028"
            ),
            30_000,
        ),
    ],
)
def test_cache_prices_join_exact_model_before_class(monkeypatch, row, cached):
    before = deepcopy(row)
    normalized = _normalize(monkeypatch, [row])[0]
    price = openai_model_price(normalized)
    original = openai_model_price(row)
    assert price is not None and original is not None
    assert price.tiers[0].prompt_cached_micro_per_m == cached
    assert price.prompt_micro_per_m == original.prompt_micro_per_m
    assert price.completion_micro_per_m == original.completion_micro_per_m
    assert row == before
    assert featherless.CATALOG.spec.normalize_rows is featherless._normalize_rows


def test_explicit_api_cache_price_wins_including_zero(monkeypatch):
    for value in ("0.00000012", 0):
        row = _row()
        row["pricing"]["input_cache_read"] = value
        normalized = _normalize(monkeypatch, [row])[0]
        price = openai_model_price(normalized)
        assert price is not None
        assert price.tiers[0].prompt_cached_micro_per_m == (120_000 if value else 0)


@pytest.mark.parametrize("tasks", [None, [], ["text-generation"], ["image-text-to-text"]])
def test_task_metadata_controls_vision_without_changing_limits_or_source(monkeypatch, tasks):
    row = _row("deepseek-ai/DeepSeek-V4.1-Flash", "deepseek4-763b", "0.0000003", "0.0000012")
    row.update(
        tasks=tasks,
        vision_supported=False,
        input_modalities=["text"],
        context_length=262144,
        max_completion_tokens=32768,
    )
    before = deepcopy(row)
    normalized = _normalize(monkeypatch, [row])[0]
    expected = ["text", "image"] if tasks == ["image-text-to-text"] else ["text"]
    assert normalized["input_modalities"] == expected
    assert normalized["context_length"] == 262144
    assert normalized["max_completion_tokens"] == 32768
    assert row == before


@pytest.mark.parametrize("bad", ["invalid", "-0.1", "NaN", "Infinity", "0.1"])
def test_malformed_api_cache_price_does_not_fall_back_to_public_discount(monkeypatch, bad):
    row = _row()
    row["pricing"]["input_cache_read"] = bad
    with pytest.raises(RuntimeError, match="invalid API cache price"):
        _normalize(monkeypatch, [row])


def test_reordered_columns_keep_input_output_and_cache_separate(monkeypatch):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(PRICES, "html.parser")
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        tr.insert(0, cells[-1].extract())
    row = _normalize(monkeypatch, [_row()], str(soup))[0]
    price = openai_model_price(row)
    assert price is not None
    assert price.prompt_micro_per_m == 1_400_000
    assert price.completion_micro_per_m == 4_400_000
    assert price.tiers[0].prompt_cached_micro_per_m == 150_000


def test_unknown_model_and_explicit_no_cache_do_not_inherit_discount(monkeypatch):
    rows = [_row("future/model", "future"), _row("zai-org/GLM-5.3", "glm52-753b")]
    normalized = _normalize(monkeypatch, rows, PRICES.replace("$0.26", "-"))
    for row in normalized:
        price = openai_model_price(row)
        assert price is not None
        assert price.tiers[0].prompt_cached_micro_per_m is None


@pytest.mark.parametrize("bad", ["$oops", "$-1", "$NaN", "$Infinity", "$5"])
def test_invalid_or_excessive_cache_price_fails_closed(monkeypatch, bad):
    with pytest.raises(RuntimeError, match="featherless"):
        _normalize(monkeypatch, [_row()], PRICES.replace("$0.15</td>", f"{bad}</td>"))


def test_different_authenticated_plan_price_does_not_get_class_discount(monkeypatch):
    with pytest.raises(RuntimeError, match="price mismatch"):
        _normalize(monkeypatch, [_row(prompt="0.000002")])


@pytest.mark.parametrize("missing", ["prompt", "completion"])
def test_per_million_api_fields_never_become_per_token_prices(monkeypatch, missing):
    row = _row("future/model", "future")
    del row["pricing"][missing]
    with pytest.raises(RuntimeError, match="missing per-token API prices"):
        _normalize(monkeypatch, [row])


def test_missing_cache_column_does_not_become_output_price(monkeypatch):
    with pytest.raises(RuntimeError, match="cache.*table"):
        _normalize(monkeypatch, [_row()], PRICES.replace("Cached input / 1M tokens", "Other"))


def test_conflicting_duplicate_price_fails_closed(monkeypatch):
    with pytest.raises(RuntimeError, match="conflicting"):
        _normalize(monkeypatch, [_row()], PRICES + PRICES.replace("$0.15</td>", "$0.12</td>"))


def test_table_free_fetch_uses_api_prices_and_removes_committed_cache_discount(
    monkeypatch, tmp_path, capsys,
):
    from scripts.pricing import refresh
    from trusted_router.pricing import cache_token_prices_microdollars

    # Multiple rows exercise one warning per fetch, not one per model. Use an
    # authenticated rate different from the former class price on purpose.
    rows = [
        {
            **_row(native, prompt="0.0000007", completion="0.0000018"),
            "available_on_current_plan": True,
        }
        for native in featherless.CURATED_NATIVE_MODELS
    ]
    for row in rows:
        row["pricing"].update(input=0.7, output=1.8, image=0, request=0)
    original = deepcopy(rows)
    by_native = {row["id"]: row for row in rows}

    def api(url, *, extra_headers):
        assert extra_headers == {"Authorization": "Bearer test-key"}
        if url.startswith(featherless.URL + "?"):
            return {"data": rows}
        return by_native[unquote(url.removeprefix(featherless.URL + "/"))]

    old_rows = [
        {
            "id": featherless.CATALOG.model_id(row["id"]),
            "upstream_id": row["id"],
            "input_token_price_per_m": 1_400_000,
            "output_token_price_per_m": 4_400_000,
            "cached_input_token_price_per_m": 150_000,
        }
        for row in rows
    ]
    manifest = tmp_path / "featherless.json"
    manifest.write_text(json.dumps({"provider": "featherless", "models": old_rows}))
    summary = tmp_path / "summary.txt"
    monkeypatch.setenv("FEATHERLESS_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(featherless, "fetch_json", api)
    monkeypatch.setattr(featherless, "fetch_html", lambda _url: NO_TABLE_PRICES)
    monkeypatch.setattr(featherless.CATALOG, "manifest_path", manifest)
    monkeypatch.setattr(featherless.CATALOG, "upstream_id_map", {})
    monkeypatch.setattr(featherless.CATALOG, "discovered_rows", {})
    monkeypatch.setattr(featherless.CATALOG, "_fetched", False)

    result = featherless.fetch()

    assert set(result.prices) == set(featherless.CATALOG.spec.expected_models)
    for price in result.prices.values():
        assert price.prompt_micro_per_m == 700_000
        assert price.completion_micro_per_m == 1_800_000
        assert price.tiers[0].prompt_cached_micro_per_m is None
    assert rows == original
    assert capsys.readouterr().err.count("no published cache prices") == 1
    assert summary.read_text().count("no published cache prices") == 1

    featherless.write_provider_manifest(result)
    refreshed_rows = json.loads(manifest.read_text())["models"]
    assert len(refreshed_rows) == len(old_rows)
    assert all("cached_input_token_price_per_m" not in row for row in refreshed_rows)
    assert all(row["input_token_price_per_m"] == 700_000 for row in refreshed_rows)

    model_id = old_rows[0]["id"]
    old_pricing = {
        "prompt": "0.0000014", "completion": "0.0000044", "input_cache_read": "0.00000015",
    }
    snapshot = {"models": [{
        "id": model_id,
        "pricing": old_pricing,
        "endpoints": [{"tr_provider_slug": "featherless", "pricing": old_pricing}],
    }]}
    merged = refresh._merge_snapshot(
        snapshot, {model_id: {"featherless": result.prices[model_id]}}, set()
    )["models"][0]
    assert "input_cache_read" not in merged["pricing"]
    assert "input_cache_read" not in merged["endpoints"][0]["pricing"]
    # The existing billing fallback charges the full input rate.
    assert cache_token_prices_microdollars("featherless", 700_000)[0] == 700_000


@pytest.mark.parametrize("remaining_kind", ["model", "model class"])
def test_table_free_page_does_not_mask_subsequent_partial_tables(remaining_kind):
    from bs4 import BeautifulSoup

    assert featherless._published_prices(NO_TABLE_PRICES) == ({}, {})
    tables = BeautifulSoup(PRICES, "html.parser").find_all("table")
    partial = str(tables[1 if remaining_kind == "model" else 0])
    with pytest.raises(RuntimeError, match="missing class/model cache price tables"):
        featherless._published_prices(partial)
