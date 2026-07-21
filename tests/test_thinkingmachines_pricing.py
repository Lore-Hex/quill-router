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
                    }
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
                prompt_micro_per_m=3_740_000,
                completion_micro_per_m=9_360_000,
                prompt_cached_micro_per_m=748_000,
            )
        },
        source="deterministic",
    )

    thinkingmachines.write_provider_manifest(result)

    row = json.loads(manifest.read_text(encoding="utf-8"))["models"][0]
    assert row["input_token_price_per_m"] == 3_740_000
    assert row["output_token_price_per_m"] == 9_360_000
    assert row["cached_input_token_price_per_m"] == 748_000
