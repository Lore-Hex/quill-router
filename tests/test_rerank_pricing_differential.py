"""Pricing compatibility against the reviewer's saved origin/main parsers.

The frozen functions below retain their original bodies. Only parse function
names changed; both share the original Decimal conversion helper. Do not update
these functions when updating production parsers.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.pricing import base, refresh
from scripts.pricing.parsers import cohere, voyage
from scripts.pricing.providers import cohere as cohere_provider

FIXTURES = Path(__file__).parent / "fixtures/pricing"


def _microdollars_per_million(value: str) -> int:
    return int((Decimal(value) * Decimal(1_000_000)).to_integral_value())


def _origin_main_cohere_parse(html: str) -> dict[str, dict[str, int]]:
    # Next.js serializes the Sanity pricing payload into script text with
    # escaped quotes. Normalize only that representation, then scope the
    # price lookup to the Embed 4 record so Model Vault instance rates and
    # image prices cannot be mistaken for text-token pricing.
    text = html.replace(r'\"', '"')
    record = re.search(
        r'"modelName":"Embed 4","per":"1M tokens"'
        r'.{0,8000}?"pricings":\[(.*?)\]',
        text,
        re.DOTALL,
    )
    if record is None:
        priced = re.findall(r'"modelName":"([^"]+)","per":"1M tokens"', text)
        if priced and "Embed 4" not in priced:
            # Since 2026-11 cohere.com/pricing prices only Embed 5 Fast and
            # Embed 5 Pro; Embed 4 keeps a model card with no rate, while
            # docs.cohere.com still lists embed-v4.0 as a served model. No
            # rewrite of this parser can recover a rate the page does not
            # publish, so say so instead of asking self-heal to drop it.
            raise ValueError(
                "cohere: cohere.com/pricing no longer publishes an Embed 4 rate "
                f"(priced per 1M tokens: {sorted(set(priced))[:6]})"
            )
        return {}
    price = re.search(
        r'"inputLabel":"Cost","inputPrice":([0-9]+(?:\.[0-9]+)?)',
        record.group(1),
    )
    if price is None:
        return {}
    return {
        "cohere/embed-v4.0": {
            "prompt_micro_per_m": _microdollars_per_million(price.group(1)),
            "completion_micro_per_m": 0,
        }
    }


def _embedding_sections(markdown: str) -> list[str]:
    sections: list[str] = []
    for start, end in (
        (r"^# Text Embeddings\s*$", r"^# Multimodal Embeddings\s*$"),
        (r"^## Older models\s*$", r"^# Fine-tuned models\s*$"),
    ):
        match = re.search(start + r"(.*?)" + end, markdown, re.MULTILINE | re.DOTALL)
        if match is not None:
            sections.append(match.group(1))
    return sections


def _origin_main_voyage_parse(markdown: str) -> dict[str, dict[str, int]]:
    prices: dict[str, dict[str, int]] = {}
    for section in _embedding_sections(markdown):
        for line in section.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            million_price = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)", cells[2])
            if million_price is None:
                continue
            for native_id in re.findall(r"`([^`]+)`", cells[0]):
                if not native_id.startswith("voyage-"):
                    continue
                prices[f"voyage/{native_id}"] = {
                    "prompt_micro_per_m": _microdollars_per_million(
                        million_price.group(1)
                    ),
                    "completion_micro_per_m": 0,
                }
    return prices


@pytest.mark.parametrize("fixture", sorted(
    path for slug in ("cohere", "voyage") for path in FIXTURES.glob(f"{slug}*")
    if path.suffix in {".html", ".md"}
), ids=lambda path: path.name)
def test_every_fixture_preserves_origin_main_prices(fixture: Path) -> None:
    page = base.normalize_parser_input(fixture.read_text(encoding="utf-8"))
    if fixture.name.startswith("cohere"):
        try:
            old_prices = _origin_main_cohere_parse(page)
        except ValueError as old_error:
            assert "no longer publishes an Embed 4 rate" in str(old_error)
            with pytest.raises(ValueError) as new_error:
                cohere.parse(page)
            assert str(new_error.value) == str(old_error)
            old_prices = {}
            new_prices = cohere.parse_rerank(page)
        else:
            new_prices = cohere.parse(page)
        prefix = "cohere/rerank-"
    else:
        old_prices = _origin_main_voyage_parse(page)
        new_prices = voyage.parse(page)
        prefix = "voyage/rerank-"
    assert {model_id: new_prices[model_id] for model_id in old_prices} == old_prices
    assert all(model_id.startswith(prefix) for model_id in new_prices.keys() - old_prices.keys())


COHERE_FIXTURES = ["cohere_pricing_2026-11-07.html", "cohere_pricing_2026-11-24.html"]
RERANK_PRICES = {
    "cohere/rerank-5-fast": {"prompt_micro_per_m": 20_000, "completion_micro_per_m": 0},
    "cohere/rerank-5-pro": {"prompt_micro_per_m": 50_000, "completion_micro_per_m": 0},
}


@pytest.mark.parametrize("fixture", COHERE_FIXTURES)
def test_cohere_rerank_prices_with_other_record_members(fixture: str) -> None:
    page = base.normalize_parser_input((FIXTURES / fixture).read_text())
    assert cohere.parse_rerank(page) == RERANK_PRICES
    # The combined parser still returns both model types when Embed 4 is priced.
    embed_page = base.normalize_parser_input((FIXTURES / "cohere.html").read_text())
    assert cohere.parse(page + embed_page) == {
        **RERANK_PRICES,
        "cohere/embed-v4.0": {"prompt_micro_per_m": 120_000, "completion_micro_per_m": 0},
    }


@pytest.mark.parametrize("fixture", COHERE_FIXTURES)
@pytest.mark.parametrize("name", ["Rerank 5 Fast", "Rerank 5 Pro"])
@pytest.mark.parametrize("change", [
    "missing_pricings", "empty_pricings", "missing_cost", "truncated_record",
    "search_unit", "request_unit", "row_override", "record_override",
])
def test_cohere_rerank_cannot_take_a_neighbour_price(fixture: str, name: str, change: str) -> None:
    page = base.normalize_parser_input((FIXTURES / fixture).read_text()).replace(r'\"', '"')
    start = page.index(f'"modelName":"{name}"')
    record, length = json.JSONDecoder().raw_decode("{" + page[start:])
    end = start + length - 1
    assert record["pricings"][0]["inputLabel"] == "Cost"
    if change in {"missing_pricings", "truncated_record"}:
        del record["pricings"]
    elif change == "empty_pricings":
        record["pricings"] = []
    elif change == "missing_cost":
        record["pricings"][0]["inputLabel"] = "Other"
    elif change in {"search_unit", "request_unit"}:
        record["per"] = "1K searches" if change == "search_unit" else "1K requests"
    elif change == "row_override":
        record["pricings"][0]["overridePer"] = "1K searches"
    else:
        record["overridePer"] = "1K requests"
    replacement = json.dumps(record, separators=(",", ":"))[1:]
    if change == "truncated_record":
        replacement = replacement[:-1]
    changed = page[:start] + replacement + page[end:]
    model_id = "cohere/" + name.lower().replace(" ", "-")
    assert cohere.parse_rerank(changed) == {
        key: value for key, value in RERANK_PRICES.items() if key != model_id
    }


def test_cohere_rerank_stops_at_its_own_closing_brace() -> None:
    page = ('{"modelName":"Rerank Example","per":"1M tokens"},'
            '{"pricings":[{"inputLabel":"Cost","inputPrice":0.05}]}')
    assert cohere.parse_rerank(page) == {}


@pytest.mark.parametrize("fixture", COHERE_FIXTURES)
def test_missing_embed_rate_stops_before_self_heal(fixture: str, monkeypatch) -> None:
    page = (FIXTURES / fixture).read_text()
    monkeypatch.setattr(base, "fetch_html", lambda *_a, **_kw: page)

    def no_self_heal(**_kwargs):
        pytest.fail("an unpublished Embed 4 rate must not request a parser rewrite")

    monkeypatch.setattr(base, "self_heal_parser", no_self_heal)
    with pytest.raises(ValueError, match="no longer publishes an Embed 4 rate"):
        base.fetch_provider(slug="cohere", url=cohere_provider.URL,
                            expected_models=cohere_provider.EXPECTED_MODELS)
    # Rerank-only evidence does not qualify for the provider's Embed pin.
    page = page.replace("Embed 5 Fast", "Other Fast").replace("Embed 5 Pro", "Other Pro")
    monkeypatch.setattr(cohere_provider, "fetch_html", lambda *_a, **_kw: page)
    assert not cohere_provider.embed4_rate_unpublished(page)
    slug, result, error = refresh._fetch_one("cohere")
    assert slug == "cohere" and result is None
    assert error is not None and "no longer publishes an Embed 4 rate" in error


def test_unrecognized_cohere_page_still_reaches_self_heal(monkeypatch) -> None:
    page = "<html><body>Pricing</body></html>"
    monkeypatch.setattr(base, "fetch_html", lambda *_a, **_kw: page)
    monkeypatch.setattr(cohere_provider, "fetch_html", lambda *_a, **_kw: page)
    calls = []

    def record_self_heal(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("test stops before any parser write")

    monkeypatch.setattr(base, "self_heal_parser", record_self_heal)
    with pytest.raises(RuntimeError, match="test stops before any parser write"):
        cohere_provider.fetch()
    assert len(calls) == 1
    assert calls[0]["slug"] == "cohere"
    assert "empty pricing dict" in calls[0]["errors"]
