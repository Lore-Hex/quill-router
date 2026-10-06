from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.providers import byteplus

# Exact cells from BytePlus's standard and video tables (2026-10-03).
# Deliberately include a cheaper Flex section so it cannot become the tariff.
MD = """
## Online inference (standard)
|**Model ID**|**Pricing tiers**|**Input**|**Audio**|**Storage**|**Cache**|**Cache audio**|**Output**|
|---|---|---|---|---|---|---|---|
|dola-seed-2-1-turbo|Prompt length [0, 256]|0.5|-|0.0083|0.1|-|2.5|
|seed-2-0-pro-260328|Prompt length [0, 128]|0.50|-|0.0083|0.10|-|3.00|
||Prompt length (128, 256]|1.00|-|0.0083|0.20|-|6.00|
|deepseek-v4-1-flash-260910|Off-peak hours|0.15|-|0.0083|0.003|-|0.60|
||Peak hours|0.30|-|0.0083|0.006|-|1.20|
## Online inference (Flex)
|dola-seed-2-1-turbo|Prompt length [0, 256]|0.25|-|0.1|-|1.25|
# Video generation models
## Pricing
|**Model ID**|**Online inference**|**Offline inference**|
|---|---|---|
|dreamina-seedance-2-5-260628<br><br>> Details|* For 480p and 720p outputs:<br><br> * Input without video: 10.70<br><br> * Input with video: 6.40<br><br>* For 1080p outputs:<br><br> * Input without video: 11.7|Not supported yet|
|dreamina-seedance-2-0-260128|* For 480p and 720p outputs:<br><br> * Input without video: 7.0<br><br> * Input with video: 4.3|Not supported yet|
|dreamina-seedance-2-0-fast-260128|* For 480p and 720p outputs:<br><br> * Input without video: (Original) 5.6 `Time limited 25% off`<br><br> * Input with video: (Original) 3.3|Not supported yet|
## Price examples
"""


def catalog():
    return {"data": [
        {"id": "dola-seed-2-1-turbo-260628", "domain": "VLM", "token_limits": {"context_window": 262144, "max_output_token_length": 262144}},
        {"id": "seed-2-0-pro-260328", "domain": "LLM"},
        {"id": "deepseek-v4-1-flash-260910", "domain": "LLM"},
        {"id": "unpriced-new-model", "domain": "LLM"},
        {"id": "seedream-new", "domain": "ImageGeneration"},
        *[{"id": key, "domain": "VideoGeneration"} for key in byteplus.VIDEO_MODELS],
    ]}


def html(md=MD):
    data = {"loaderData": {"(lang)/docs/(libcode)/(doccode$)/page": {"curDoc": {"MDContent": md}}}}
    return '<script>window._ROUTER_DATA=' + json.dumps(data) + ';</script>'


def test_exact_standard_context_cache_and_video_tariffs():
    prices = byteplus.parse_prices(byteplus.pricing_markdown(html()))
    assert prices["dola-seed-2-1-turbo"].prompt_micro_per_m == 500_000
    tiers = prices["seed-2-0-pro-260328"].tiers
    assert [(t.max_prompt_tokens, t.prompt_micro_per_m, t.completion_micro_per_m, t.prompt_cached_micro_per_m) for t in tiers] == [
        (128_000, 500_000, 3_000_000, 100_000), (None, 1_000_000, 6_000_000, 200_000),
    ]
    for native, output in zip(byteplus.VIDEO_MODELS, [10_700_000, 7_000_000, 5_600_000], strict=True):
        assert prices[native].prompt_micro_per_m == 0
        assert prices[native].completion_micro_per_m == output
    assert "deepseek-v4-1-flash-260910" not in prices


@pytest.mark.parametrize("bad", [
    MD.replace("10.70", "USD unknown"), MD.replace("## Online inference (standard)", "## changed"),
    MD.replace("480p and 720p", "720p"), MD.replace("0.50|-|", "-1|-|"),
    MD.replace("|1.00|-|", "|NaN|-|"),
])
def test_price_shape_change_is_not_silently_accepted(bad):
    with pytest.raises((RuntimeError, ValueError)):
        byteplus.parse_prices(bad)


def test_discovery_live_statuses_unknown_prices_and_rollout_gate(monkeypatch):
    monkeypatch.setattr(byteplus, "NATIVE_ROUTES_DEPLOYED", False)
    payload = catalog()
    payload["data"][1]["status"] = "Shutdown"
    prices, rows = byteplus.discover(payload, MD)
    assert "bytedance/seed-2-0-pro-260328" not in prices
    assert rows["bytedance/seed-2-0-pro-260328"]["routable_reason"] == "provider-retired"
    assert rows["bytedance/unpriced-new-model"]["routable_reason"] == "awaiting-price"
    assert rows["bytedance/seedream-new"]["routable_reason"] == "unsupported-api"
    assert rows["deepseek/deepseek-v4.1-flash"]["routable_reason"] == "time-dependent-pricing-unsupported"
    for model in byteplus.VIDEO_MODELS.values():
        assert rows[model]["billing_unit"] == "output_tokens"
        assert rows[model]["routable"] is False


def test_refresh_canaries_manifest_and_safety_holds(monkeypatch, tmp_path):
    monkeypatch.setattr(byteplus, "NATIVE_ROUTES_DEPLOYED", False)
    path = tmp_path / "byteplus.json"
    monkeypatch.setattr(byteplus, "MANIFEST_PATH", path)
    monkeypatch.setenv("BYTEPLUS_API_KEY", "test-key")
    monkeypatch.setattr(byteplus, "fetch_json", lambda *_a, **_kw: catalog())
    monkeypatch.setattr(byteplus, "fetch_html", lambda *_a, **_kw: html())
    probed = []
    def probe(**kw):
        probed.append(kw["model"])
        return kw["model"] == "dola-seed-2-1-turbo-260628"
    monkeypatch.setattr(byteplus, "probe_openai_chat", probe)
    result = byteplus.fetch()
    byteplus.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert len(probed) == 2
    assert rows["bytedance/dola-seed-2-1-turbo-260628"]["routable_reason"] == "gateway-upgrade-required"
    assert rows["bytedance/seed-2-0-pro-260328"]["routable_reason"] == "provider-canary-failed"
    for model in byteplus.VIDEO_MODELS.values():
        assert rows[model]["routable_reason"] == "gateway-upgrade-required"
        assert model not in result.price_index_model_ids
    monkeypatch.setattr(byteplus, "NATIVE_ROUTES_DEPLOYED", True)
    result = byteplus.fetch()
    byteplus.write_provider_manifest(result)
    live = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    # Discovery cannot lift an operator hold. Activation is a separate reviewed
    # manifest edit, and must target only the rollout hold, not canary failures.
    assert live["bytedance/dola-seed-2-1-turbo-260628"]["routable"] is False
    from scripts.pricing.manifest import set_manifest_model_canary_states
    approved = {"bytedance/dola-seed-2-1-turbo-260628", *byteplus.VIDEO_MODELS.values()}
    set_manifest_model_canary_states(path, checked_model_ids=approved,
                                    healthy_model_ids=approved,
                                    failure_reason="gateway-upgrade-required")
    byteplus.write_provider_manifest(result)
    live = {row["id"]: row for row in json.loads(path.read_text())["models"]}
    assert live["bytedance/dola-seed-2-1-turbo-260628"]["routable"]
    assert live["bytedance/seed-2-0-pro-260328"]["routable_reason"] == "provider-canary-failed"
    assert all(live[model]["routable"] for model in byteplus.VIDEO_MODELS.values())
    monkeypatch.delenv("BYTEPLUS_API_KEY")
    with pytest.raises(RuntimeError):
        byteplus.fetch()
    with pytest.raises(RuntimeError):
        byteplus.write_provider_manifest(result)


def test_deployed_native_video_catalog_uses_exact_token_tariffs():
    from trusted_router import catalog_ingest
    from trusted_router.pricing import _customer_price

    assert byteplus.NATIVE_ROUTES_DEPLOYED
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    rows = {row["id"]: row for row in json.loads(byteplus.MANIFEST_PATH.read_text())["models"]}
    for native, model in byteplus.VIDEO_MODELS.items():
        row = rows[model]
        assert row.get("routable", True)
        assert row["upstream_id"] == native
        assert row["billing_unit"] == "output_tokens"
        rate = row["output_token_price_per_m"]
        assert type(rate) is int and rate > 0
        endpoint = endpoints[model + "@byteplus/prepaid"]
        assert models[model].supports_video
        assert endpoint.provider == "byteplus"
        assert endpoint.prompt_price_microdollars_per_million_tokens == 0
        assert endpoint.completion_price_microdollars_per_million_tokens == _customer_price(rate)


def test_registration_and_native_token_video_price(monkeypatch, tmp_path):
    from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
    from trusted_router import catalog_ingest
    from trusted_router.pricing import _customer_price
    from trusted_router.provider_manifest_policy import _provider_manifest_row_price_is_valid
    from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS
    assert OPENAI_COMPATIBLE_PROVIDERS["byteplus"] == (("BYTEPLUS_API_KEY",), byteplus.BASE_URL)
    assert any(row[0] == "byteplus" for row in _DISCOVERABLE_MANIFEST_PROVIDERS)
    assert "BYTEPLUS_API_KEY:trustedrouter-byteplus-api-key" in (Path(__file__).resolve().parents[1] / ".github/workflows/refresh-prices.yml").read_text()
    row = {"id": "bytedance/seedance-2.5", "upstream_id": "dreamina-seedance-2-5-260628",
           "model_type": "video", "billing_unit": "output_tokens", "endpoints": ["videos"],
           "input_token_price_per_m": 0, "output_token_price_per_m": 10_700_000}
    assert _provider_manifest_row_price_is_valid(row)
    (tmp_path / "byteplus.json").write_text(json.dumps({"models": [row], "price_scale": "microdollars_per_million"}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert models[row["id"]].supports_video
    ep = endpoints[row["id"] + "@byteplus/prepaid"]
    assert ep.prompt_price_microdollars_per_million_tokens == 0
    assert ep.completion_price_microdollars_per_million_tokens == _customer_price(10_700_000)
    assert ep.price_tiers[0].prompt_price_microdollars_per_million_tokens == 0
