"""Mistral provider-pricing source contracts."""

from __future__ import annotations

from scripts.pricing.parsers.mistral import parse
from scripts.pricing.providers import mistral


def test_mistral_uses_dedicated_api_pricing_page() -> None:
    """The general plans page does not contain the API model-price records."""
    assert mistral.URL == "https://mistral.ai/pricing/api/"


def test_mistral_parser_reads_server_rendered_api_price_cards() -> None:
    html = """
    <nav>
      <p>Mistral Medium 3.5</p>
      <p>Mistral Small 4</p>
    </nav>
    <article>
      <p class="text-h5 font-mistral">Mistral Medium 3.5</p>
      <div>
        <p>Input (/M tokens)</p>
        <mistral-atom-text-price><span>$1.5</span></mistral-atom-text-price>
      </div>
      <div>
        <p>Output (/M tokens)</p>
        <mistral-atom-text-price><span>$7.5</span></mistral-atom-text-price>
      </div>
      <code>mistral-medium-latest</code>
    </article>
    <article>
      <p class="text-h5 font-mistral">Mistral Small 4</p>
      <div>
        <p>Input (/M tokens)</p>
        <mistral-atom-text-price><span>$0.15</span></mistral-atom-text-price>
      </div>
      <div>
        <p>Output (/M tokens)</p>
        <mistral-atom-text-price><span>$0.6</span></mistral-atom-text-price>
      </div>
      <code>mistral-small-latest</code>
    </article>
    """

    assert parse(html) == {
        "mistralai/mistral-medium-3-5": {
            "prompt_micro_per_m": 1_500_000,
            "completion_micro_per_m": 7_500_000,
        },
        "mistralai/mistral-small-2603": {
            "prompt_micro_per_m": 150_000,
            "completion_micro_per_m": 600_000,
        },
    }
