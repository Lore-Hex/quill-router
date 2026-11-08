from __future__ import annotations

import json

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers.thinkingmachines import parse
from scripts.pricing.providers import thinkingmachines
from tests.pinned_manifests import THINKINGMACHINES_GLM_5_3, serve_manifest_rows
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS

SERVERLESS = [
    {"tinker_id": "thinkingmachines/Inkling-2-Small:peft:262144:sampling-nvfp4",
     "input": "$0.30", "cached_input": "$0.03", "output": "$1.00"},
    {"tinker_id": "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4",
     "input": "$0.30", "cached_input": "$0.06", "output": "$1.20"},
    {"tinker_id": "thinkingmachines/Inkling:peft:262144:sampling-nvfp4",
     "input": "$1.00", "cached_input": "$0.17", "output": "$4.05"},
]
SAMPLER = [
    {"tinker_id": "zai-org/GLM-5.3:peft:262144", "prefill": "$4.86",
     "cached_prefill": "$0.972", "sample": "$12.15", "train": "$14.58",
     "active_params": 40000000000, "original_sample": "$99.99"},
]


def test_fetch_uses_named_json_prices_not_html_column_positions(monkeypatch):
    feeds = {thinkingmachines.SERVERLESS_URL: SERVERLESS, thinkingmachines.SAMPLER_URL: SAMPLER}
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    result = thinkingmachines.fetch()
    assert result.source == "api"
    assert result.prices["z-ai/glm-5.3"] == ModelPrice(4_860_000, 12_150_000, prompt_cached_micro_per_m=972_000)
    assert result.prices["thinkingmachines/inkling"] == ModelPrice(1_000_000, 4_050_000, prompt_cached_micro_per_m=170_000)
    assert result.prices["thinkingmachines/inkling-2-small"] == ModelPrice(300_000, 1_000_000, prompt_cached_micro_per_m=30_000)
    assert set(result.prices) == set(thinkingmachines.EXPECTED_MODELS)


@pytest.mark.parametrize("bad", [None, "$NaN", "$-1", "4.86", "$0", "$1.1234567"])
def test_json_rejects_missing_or_invalid_prices(bad):
    row = {**SAMPLER[0], "prefill": bad}
    with pytest.raises(RuntimeError, match="thinkingmachines"):
        thinkingmachines._json_prices([row], {row["tinker_id"]: "z-ai/glm-5.3"}, ("prefill", "cached_prefill", "sample"))


def test_json_rejects_conflicts_missing_models_and_excessive_cache():
    ids = {SAMPLER[0]["tinker_id"]: "z-ai/glm-5.3"}
    for rows in [[], {}, [SAMPLER[0], {**SAMPLER[0], "sample": "$99"}], [{**SAMPLER[0], "cached_prefill": "$5"}]]:
        with pytest.raises(RuntimeError, match="thinkingmachines"):
            thinkingmachines._json_prices(rows, ids, ("prefill", "cached_prefill", "sample"))


def _pricing_html(*, active: str = "old") -> str:
    return f"""
    <div id="pricing-toggle">
      <button class="{"active" if active == "old" else ""}" data-mode="old">Old</button>
      <button class="{"active" if active == "new" else ""}" data-mode="new">New</button>
    </div>
    <table><tbody id="model-tbody"><tr>
      <td>Inkling (256K)</td>
      <td class="tinker-id">thinkingmachines/Inkling:peft:262144</td>
      <td>Hybrid</td><td>MoE</td><td>Large</td><td>256K</td>
      <td class="price">
        <span class="price-old">$3.74 <span class="price-cached">$0.748 (cached)</span></span>
        <span class="price-new">$4.00 <span class="price-cached">$0.800 (cached)</span></span>
      </td>
      <td class="price"><span class="price-old">$9.36</span><span class="price-new">$10.00</span></td>
    </tr></tbody></table>
    """


def _serverless_pricing_html() -> str:
    return """
    <table><tbody id="model-tbody">
      <tr>
        <td>GLM-5.3 (256K)</td>
        <td class="tinker-id">zai-org/GLM-5.3:peft:262144</td>
        <td>256K</td><td>Large</td><td>MoE</td><td>Reasoning</td>
        <td class="price">$4.86<span class="price-cached">$0.972 (cached)</span></td>
        <td class="price">$12.15</td>
        <td class="price">$14.58</td>
      </tr>
    </tbody></table>
    <table><tbody id="serverless-tbody">
      <tr>
        <td>Inkling-Small</td>
        <td class="tinker-id">thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4</td>
        <td>256K</td>
        <td class="price">$0.30<span class="price-cached">$0.06 (cached)</span></td>
        <td class="price">$1.20</td>
      </tr>
      <tr>
        <td>Inkling</td>
        <td class="tinker-id">thinkingmachines/Inkling:peft:262144:sampling-nvfp4</td>
        <td>256K</td>
        <td class="price">$1.00<span class="price-cached">$0.17 (cached)</span></td>
        <td class="price">$4.05</td>
      </tr>
    </tbody></table>
    """


def test_parser_merges_serverless_and_selected_sampler_models() -> None:
    assert parse(_serverless_pricing_html()) == {
        "thinkingmachines/inkling-small": {
            "prompt_micro_per_m": 300_000,
            "completion_micro_per_m": 1_200_000,
            "prompt_cached_micro_per_m": 60_000,
        },
        "thinkingmachines/inkling": {
            "prompt_micro_per_m": 1_000_000,
            "completion_micro_per_m": 4_050_000,
            "prompt_cached_micro_per_m": 170_000,
        },
        "z-ai/glm-5.3": {
            "prompt_micro_per_m": 4_860_000,
            "completion_micro_per_m": 12_150_000,
            "prompt_cached_micro_per_m": 972_000,
        },
    }


def test_parser_uses_currently_active_pricing_version() -> None:
    assert parse(_pricing_html()) == {
        "thinkingmachines/inkling": {
            "prompt_micro_per_m": 3_740_000,
            "completion_micro_per_m": 9_360_000,
            "prompt_cached_micro_per_m": 748_000,
        }
    }
    assert parse(_pricing_html(active="new")) == {
        "thinkingmachines/inkling": {
            "prompt_micro_per_m": 4_000_000,
            "completion_micro_per_m": 10_000_000,
            "prompt_cached_micro_per_m": 800_000,
        }
    }


def test_parser_reads_current_direct_price_cells() -> None:
    html = """
    <table><tbody id="model-tbody"><tr>
      <td>Inkling (256K)</td>
      <td class="tinker-id">thinkingmachines/Inkling:peft:262144</td>
      <td>Hybrid</td><td>MoE</td><td>Large</td><td>256K</td>
      <td class="price">$3.74<span class="price-cached">$0.748 (cached)</span></td>
      <td class="price">$9.36</td>
    </tr></tbody></table>
    """

    assert parse(html) == {
        "thinkingmachines/inkling": {
            "prompt_micro_per_m": 3_740_000,
            "completion_micro_per_m": 9_360_000,
            "prompt_cached_micro_per_m": 748_000,
        }
    }


def test_manifest_writer_updates_integer_rates(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    manifest = tmp_path / "thinkingmachines.json"
    manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "thinkingmachines/inkling",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "thinkingmachines/inkling-small",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "thinkingmachines/inkling-2-small",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                    {
                        "id": "z-ai/glm-5.3",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(thinkingmachines, "MANIFEST_PATH", manifest)
    result = ProviderPricingResult(
        slug="thinkingmachines",
        prices={
            "thinkingmachines/inkling": ModelPrice(
                prompt_micro_per_m=1_000_000,
                completion_micro_per_m=4_050_000,
                prompt_cached_micro_per_m=170_000,
            ),
            "thinkingmachines/inkling-small": ModelPrice(
                prompt_micro_per_m=300_000,
                completion_micro_per_m=1_200_000,
                prompt_cached_micro_per_m=60_000,
            ),
            "thinkingmachines/inkling-2-small": ModelPrice(
                prompt_micro_per_m=300_000,
                completion_micro_per_m=1_000_000,
                prompt_cached_micro_per_m=30_000,
            ),
            "z-ai/glm-5.3": ModelPrice(
                prompt_micro_per_m=4_860_000,
                completion_micro_per_m=12_150_000,
                prompt_cached_micro_per_m=972_000,
            ),
        },
        source="deterministic",
    )

    thinkingmachines.write_provider_manifest(result)

    rows = {
        row["id"]: row
        for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]
    }
    assert rows["thinkingmachines/inkling"]["input_token_price_per_m"] == 1_000_000
    assert rows["thinkingmachines/inkling"]["output_token_price_per_m"] == 4_050_000
    assert rows["thinkingmachines/inkling"]["cached_input_token_price_per_m"] == 170_000
    assert rows["thinkingmachines/inkling-small"]["input_token_price_per_m"] == 300_000
    assert rows["thinkingmachines/inkling-small"]["output_token_price_per_m"] == 1_200_000
    assert (
        rows["thinkingmachines/inkling-small"]["cached_input_token_price_per_m"]
        == 60_000
    )
    assert rows["thinkingmachines/inkling-2-small"]["input_token_price_per_m"] == 300_000
    assert rows["thinkingmachines/inkling-2-small"]["output_token_price_per_m"] == 1_000_000
    assert rows["thinkingmachines/inkling-2-small"]["cached_input_token_price_per_m"] == 30_000
    assert rows["z-ai/glm-5.3"]["input_token_price_per_m"] == 4_860_000
    assert rows["z-ai/glm-5.3"]["output_token_price_per_m"] == 12_150_000
    assert rows["z-ai/glm-5.3"]["cached_input_token_price_per_m"] == 972_000


@pytest.mark.provider_health
def test_tinker_still_serves_glm_5_3() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    assert "z-ai/glm-5.3@thinkingmachines/prepaid" in MODEL_ENDPOINTS


def test_tinker_glm_5_3_sampler_route_is_published_with_verified_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # The route built from its pinned manifest row: how the catalog publishes
    # the verified sampler contract holds whatever Thinking Machines lists today.
    serve_manifest_rows(monkeypatch, tmp_path, "thinkingmachines", [THINKINGMACHINES_GLM_5_3])
    model = MODELS["z-ai/glm-5.3"]
    endpoint = MODEL_ENDPOINTS["z-ai/glm-5.3@thinkingmachines/prepaid"]

    assert model.context_length >= 262_144
    assert endpoint.provider == "thinkingmachines"
    assert endpoint.usage_type == "Credits"
    assert endpoint.upstream_id == "zai-org/GLM-5.3:peft:262144"
    assert endpoint.prompt_price_microdollars_per_million_tokens == 5_127_300
    assert endpoint.completion_price_microdollars_per_million_tokens == 12_818_250


INKLING_2_SMALL_ROW = {
    "id": "thinkingmachines/inkling-2-small",
    "upstream_id": "thinkingmachines/Inkling-2-Small:peft:262144:sampling-nvfp4",
    "display_name": "Thinking Machines Inkling 2 Small 256K",
    "context_length": 262144,
    "input_modalities": ["text", "image"],
    "output_modalities": ["text"],
    "endpoints": ["chat/completions"],
    "input_token_price_per_m": 300000,
    "output_token_price_per_m": 1000000,
    "cached_input_token_price_per_m": 30000,
}


def test_inkling_2_small_would_publish_without_an_open_weights_badge(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # Once the access hold lifts, the route publishes as text+image and still
    # carries no open-weights badge: its weights are unpublished.
    from trusted_router.catalog import endpoints_for_model, model_open_weights

    serve_manifest_rows(monkeypatch, tmp_path, "thinkingmachines", [INKLING_2_SMALL_ROW])
    model = MODELS["thinkingmachines/inkling-2-small"]
    assert model.context_length == 262_144
    assert model.input_modalities == ("text", "image")
    assert not model_open_weights(model)
    assert model_open_weights(MODELS["thinkingmachines/inkling-small"])
    assert {
        (endpoint.provider, endpoint.upstream_id)
        for endpoint in endpoints_for_model(model.id)
    } == {("thinkingmachines", "thinkingmachines/Inkling-2-Small:peft:262144:sampling-nvfp4")}


def test_inkling_2_small_is_held_out_of_routing_and_probes() -> None:
    # QUILL-ROUTER-FD: Tinker rejects TR's key for this model, so customer
    # requests failed. The route is held; its siblings keep serving.
    from trusted_router.catalog import endpoints_for_model
    from trusted_router.synthetic.probes import rotation_candidates

    rows = {
        row["id"]: row
        for row in json.loads(thinkingmachines.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    }
    for model_id, reason in thinkingmachines.OPERATOR_HOLDS.items():
        assert rows[model_id]["routable"] is False
        assert rows[model_id]["routable_reason"] == reason == "provider-access-unavailable"
        assert endpoints_for_model(model_id) == []
    assert thinkingmachines.OPERATOR_HOLDS.keys() == {"thinkingmachines/inkling-2-small"}

    probed = rotation_candidates().get("thinkingmachines", [])
    assert "thinkingmachines/inkling-2-small" not in probed
    for sibling in ("thinkingmachines/inkling", "thinkingmachines/inkling-small"):
        assert "routable" not in rows[sibling]
        assert sibling in probed
        assert any(e.provider == "thinkingmachines" for e in endpoints_for_model(sibling))


def _held_manifest(tmp_path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    manifest = tmp_path / "thinkingmachines.json"
    held = {**INKLING_2_SMALL_ROW, "routable": False, "routable_reason": "provider-access-unavailable"}
    sibling = {
        "id": "thinkingmachines/inkling-small",
        "upstream_id": "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4",
        "input_token_price_per_m": 1,
        "output_token_price_per_m": 1,
    }
    others = [
        {"id": model_id, "upstream_id": model_id, "input_token_price_per_m": 1, "output_token_price_per_m": 1}
        for model_id in ("thinkingmachines/inkling", "z-ai/glm-5.3")
    ]
    manifest.write_text(json.dumps({"models": [held, sibling, *others]}), encoding="utf-8")
    monkeypatch.setattr(thinkingmachines, "MANIFEST_PATH", manifest)
    feeds = {thinkingmachines.SERVERLESS_URL: SERVERLESS, thinkingmachines.SAMPLER_URL: SAMPLER}
    monkeypatch.setattr(thinkingmachines, "fetch_json", feeds.__getitem__)
    for name in thinkingmachines.API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    return manifest


def _refresh_rows(manifest) -> dict[str, dict]:  # noqa: ANN001
    thinkingmachines.write_provider_manifest(thinkingmachines.fetch())
    return {row["id"]: row for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]}


def test_access_hold_survives_refreshes_without_a_tinker_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _held_manifest(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(thinkingmachines, "probe_openai_chat", lambda **kw: calls.append(kw["model"]) or True)

    rows = _refresh_rows(manifest)

    assert calls == []
    held = rows["thinkingmachines/inkling-2-small"]
    assert (held["routable"], held["routable_reason"]) == (False, "provider-access-unavailable")
    assert held["output_token_price_per_m"] == 1_000_000
    assert "routable" not in rows["thinkingmachines/inkling-small"]


@pytest.mark.parametrize("healthy", [False, True])
def test_access_canary_rechecks_only_held_routes_and_lifts_on_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch, healthy: bool,
) -> None:
    manifest = _held_manifest(tmp_path, monkeypatch)
    monkeypatch.setenv("THINKING_MACHINES_API_KEY", "test-key")
    calls: list[dict] = []
    monkeypatch.setattr(thinkingmachines, "probe_openai_chat", lambda **kw: calls.append(kw) or healthy)

    rows = _refresh_rows(manifest)

    assert [(c["base_url"], c["model"]) for c in calls] == [
        (thinkingmachines.BASE_URL, "thinkingmachines/Inkling-2-Small:peft:262144:sampling-nvfp4")
    ]
    held = rows["thinkingmachines/inkling-2-small"]
    if healthy:
        assert "routable" not in held and "routable_reason" not in held
    else:
        assert (held["routable"], held["routable_reason"]) == (False, "provider-access-unavailable")
    assert "routable" not in rows["thinkingmachines/inkling-small"]

    # Once lifted, later refreshes neither probe nor re-hold the route.
    calls.clear()
    rows = _refresh_rows(manifest)
    assert bool(calls) is not healthy
    assert ("routable" not in rows["thinkingmachines/inkling-2-small"]) is healthy
