"""Price scrapers for the hosted decision model (TypeSafe AI's Jev).

Both hosts bill input only. Each parser reads ITS host's published rate and
must fail loudly -- never publish zero, never guess -- when the published shape
changes, because the number it writes is the number customers are billed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing.providers import typesafe, vercel_ai_gateway

JEV = "typesafe-ai/jev"

# The pricing block of https://docs.typesafe.ai/models.md as served 2026-09-19,
# escapes included: the page is markdown, so dollar signs arrive backslashed.
TYPESAFE_PAGE = r"""
| Model                       | Identifier                                                                                |
| --------------------------- | ----------------------------------------------------------------------------------------- |
| Jev 1.13                    | `jev-1.13.0`                                                                              |
| Price (per Btok / per Mtok) | \$42 / \$0.042                                                                            |
| Rate limits                 | 250,000 tokens per second / 1,200 requests per minute                                     |

* **Price:** Charged per input token. Output tokens are free. A Btok is a billion tokens and an Mtok is a million tokens.
"""


def _only_price(prices: dict[str, Any]) -> tuple[int, int]:
    assert list(prices) == [JEV]
    price = prices[JEV]
    return price.prompt_micro_per_m, price.completion_micro_per_m


def test_typesafe_reads_the_published_rate_as_input_only() -> None:
    assert _only_price(typesafe.parse(TYPESAFE_PAGE)) == (42_000, 0)


def test_typesafe_follows_a_real_price_change() -> None:
    page = TYPESAFE_PAGE.replace(r"\$42 / \$0.042", r"\$1,050 / \$1.05")
    assert _only_price(typesafe.parse(page)) == (1_050_000, 0)


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("output becomes metered", "Output tokens are free.", "Output tokens cost extra."),
        ("the two figures disagree", r"\$42 / \$0.042", r"\$42 / \$0.052"),
        ("the row is renamed", "Price (per Btok / per Mtok)", "Pricing"),
        ("the price is zero", r"\$42 / \$0.042", r"\$0 / \$0"),
        ("the rate is below a microdollar", r"\$42 / \$0.042", r"\$0.0000004 / \$0.0000000004"),
    ],
)
def test_typesafe_refuses_to_guess(label: str, old: str, new: str) -> None:
    assert old in TYPESAFE_PAGE, label
    with pytest.raises(RuntimeError):
        typesafe.parse(TYPESAFE_PAGE.replace(old, new))


def test_typesafe_refuses_a_page_that_prices_two_models() -> None:
    second = "\n| Price (per Btok / per Mtok) | \\$99 / \\$0.099 |\n"
    with pytest.raises(RuntimeError, match="exactly one price row, found 2"):
        typesafe.parse(TYPESAFE_PAGE + second)


def test_typesafe_refuses_a_second_model_arriving_as_a_column() -> None:
    # Reading only the first value cell published 42,000 for a page on which
    # `jev-latest` had moved to the model in the SECOND column, at twice that.
    page = TYPESAFE_PAGE.replace(
        "| Jev 1.13                    | `jev-1.13.0`", "| Jev 1.13 | Jev 1.14 | `jev-1.13.0`"
    ).replace(
        r"| \$42 / \$0.042                                                                            |",
        r"| \$42 / \$0.042 | \$84 / \$0.084 |",
    )
    assert r"\$84 / \$0.084" in page, "fixture: the second column did not land"
    with pytest.raises(RuntimeError, match="more than one model"):
        typesafe.parse(page)


@pytest.mark.parametrize(
    "cell",
    [
        r"\$42 / \$0.042 (\$84 / \$0.084 from October)",  # two pairs in one cell
        r"\$42 / \$0.042 plus \$1 per request",  # a fee beside the rate
        "contact sales",
    ],
)
def test_typesafe_refuses_a_price_cell_that_says_more_than_one_rate(cell: str) -> None:
    page = TYPESAFE_PAGE.replace(r"\$42 / \$0.042", cell, 1)
    with pytest.raises(RuntimeError):
        typesafe.parse(page)


def test_typesafe_refuses_a_non_text_page() -> None:
    with pytest.raises(RuntimeError):
        typesafe.parse(None)


def _vercel_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": JEV,
        "type": "evaluation",
        "pricing": {"input": "0.000000042", "output": "0"},
    }
    row.update(overrides)
    return {"data": [{"id": "openai/gpt-5", "type": "language"}, row]}


def test_vercel_reads_the_per_token_rate_as_input_only() -> None:
    assert _only_price(vercel_ai_gateway.parse(_vercel_row())) == (42_000, 0)


def test_vercel_treats_a_missing_output_price_as_free() -> None:
    assert _only_price(vercel_ai_gateway.parse(_vercel_row(pricing={"input": "0.000000042"}))) == (
        42_000,
        0,
    )


@pytest.mark.parametrize(
    "payload",
    [
        _vercel_row(pricing={"input": "0.000000042", "output": "0.0000001"}),  # metered output
        _vercel_row(pricing={"input": "0", "output": "0"}),  # a zero price is not a price
        _vercel_row(pricing={}),  # no input price at all
        _vercel_row(pricing={"input": "cheap"}),
        _vercel_row(pricing={"input": "0.0000000000004"}),  # below a microdollar per million
        _vercel_row(type="language"),  # no longer a decision model
        # Vercel's other rows carry these. On this row any of them means the
        # flat `input` rate is no longer what a request costs.
        _vercel_row(
            pricing={
                "input": "0.000000042",
                "output": "0",
                "input_tiers": [{"cost": "0.000000084", "min": 10000}],
            }
        ),
        _vercel_row(pricing={"input": "0.000000042", "output": "0", "regional": {"eu": {}}}),
        _vercel_row(pricing={"input": "0.000000042", "output": "0", "peak_pricing": {}}),
        _vercel_row(pricing={"input": "0.000000042", "output": "0", "service_tiers": {}}),
        _vercel_row(pricing=None),
        {"data": [{"id": "openai/gpt-5", "type": "language"}]},  # delisted
        {"data": "nope"},
        None,
    ],
)
def test_vercel_refuses_to_guess(payload: object) -> None:
    with pytest.raises(RuntimeError):
        vercel_ai_gateway.parse(payload)


@pytest.mark.parametrize("module", [typesafe, vercel_ai_gateway])
def test_each_manifest_is_a_decision_row_the_catalog_will_read(module: Any) -> None:
    raw = json.loads(Path(module.MANIFEST_PATH).read_text(encoding="utf-8"))
    assert raw["provider"] == module.SLUG
    rows = {row["id"]: row for row in raw["models"]}
    assert set(rows) == set(module.EXPECTED_MODELS)
    row = rows[JEV]
    # catalog_ingest._input_only_manifest_cost ignores the row unless BOTH hold,
    # and would then silently bill the checked-in fallback price instead.
    assert row["model_type"] == "decision"
    assert "decide" in row["endpoints"]
    assert row["input_token_price_per_m"] > 0
    assert row["output_token_price_per_m"] == 0
