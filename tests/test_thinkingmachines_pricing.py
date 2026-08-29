from __future__ import annotations

import json

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers.thinkingmachines import parse
from scripts.pricing.providers import thinkingmachines


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


def _glm53_pricing_html() -> str:
    return """
    <table><tbody id="model-tbody"><tr>
      <td>GLM-5.3 (256K)</td>
      <td class="tinker-id">zai-org/GLM-5.3:peft:262144</td>
      <td>256K</td><td>Large</td><td>MoE</td><td>Reasoning</td>
      <td class="price">$4.86<span class="price-cached">$0.972 (cached)</span></td>
      <td class="price">$12.15</td>
    </tr></tbody></table>
    """


def test_parser_reads_all_serverless_inference_models() -> None:
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
    }


def test_parser_reads_glm53_base_model_pricing() -> None:
    assert parse(_glm53_pricing_html()) == {
        "z-ai/glm-5.3": {
            "prompt_micro_per_m": 4_860_000,
            "completion_micro_per_m": 12_150_000,
            "prompt_cached_micro_per_m": 972_000,
        }
    }


def test_parser_reads_glm53_serverless_variant() -> None:
    html = """
    <table><tbody id="serverless-tbody"><tr>
      <td>GLM-5.3</td>
      <td class="tinker-id">zai-org/GLM-5.3:peft:262144:sampling-nvfp4</td>
      <td>256K</td>
      <td class="price">$4.86<span class="price-cached">$0.972 (cached)</span></td>
      <td class="price">$12.15</td>
    </tr></tbody></table>
    """

    assert parse(html) == {
        "z-ai/glm-5.3": {
            "prompt_micro_per_m": 4_860_000,
            "completion_micro_per_m": 12_150_000,
            "prompt_cached_micro_per_m": 972_000,
        }
    }


def test_parser_merges_serverless_and_base_model_tables() -> None:
    parsed = parse(_serverless_pricing_html() + _glm53_pricing_html())

    assert set(parsed) == {
        "thinkingmachines/inkling",
        "thinkingmachines/inkling-small",
        "z-ai/glm-5.3",
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
            "z-ai/glm-5.3": ModelPrice(
                prompt_micro_per_m=4_860_000,
                completion_micro_per_m=12_150_000,
                prompt_cached_micro_per_m=972_000,
            ),
        },
        source="deterministic",
    )

    thinkingmachines.write_provider_manifest(result)

    rows = {row["id"]: row for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]}
    assert rows["thinkingmachines/inkling"]["input_token_price_per_m"] == 1_000_000
    assert rows["thinkingmachines/inkling"]["output_token_price_per_m"] == 4_050_000
    assert rows["thinkingmachines/inkling"]["cached_input_token_price_per_m"] == 170_000
    assert rows["thinkingmachines/inkling-small"]["input_token_price_per_m"] == 300_000
    assert rows["thinkingmachines/inkling-small"]["output_token_price_per_m"] == 1_200_000
    assert rows["thinkingmachines/inkling-small"]["cached_input_token_price_per_m"] == 60_000
    assert rows["z-ai/glm-5.3"]["input_token_price_per_m"] == 4_860_000
    assert rows["z-ai/glm-5.3"]["output_token_price_per_m"] == 12_150_000
    assert rows["z-ai/glm-5.3"]["cached_input_token_price_per_m"] == 972_000
