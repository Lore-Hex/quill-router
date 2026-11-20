import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import maple
from trusted_router.provider_manifest_policy import (
    EXPIRED_PROVIDER_MANIFEST,
    provider_manifest_valid_until,
)

PRICES = """<p>USD equivalent per 1 million tokens.</p><table>
<thead><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Output</th></tr></thead><tbody>
<tr><th scope="row">DeepSeek V4.1 Flash</th><td>$0.01</td><td>$0.001</td><td>$0.02</td></tr>
<tr><th scope="row">DeepSeek V4.1 Flash Uncensored</th><td>$0.65</td><td>$0.13</td><td>$1.45</td></tr>
</tbody></table>"""
CAPABILITIES = """<table><thead><tr><th>Model</th><th>Context tokens</th><th>Image input</th></tr></thead>
<tbody>
<tr><td>DeepSeek V4.1 Flash Uncensored <code>deepseek-v4-1-flash-1017-uncensored</code></td>
<td>1,048,576</td><td>Yes</td></tr></tbody></table>"""


def test_exact_uncensored_prices_never_use_stock_or_plan_prices():
    price = maple.parse_prices(PRICES)
    assert price.prompt_micro_per_m == 650_000
    assert price.tiers[0].prompt_cached_micro_per_m == 130_000
    assert price.completion_micro_per_m == 1_450_000


@pytest.mark.parametrize(("before", "after"), [
    ("USD equivalent per 1 million tokens.", "EUR equivalent per 1 million tokens."),
    ("$0.65", "$0"), ("$1.45", "$0"), ("$0.13", "$0.66"),
    ("$0.13", "$-0.13"), ("$0.13", "$0.1234567"), ("$0.13", "$0.13 per token"),
    ("DeepSeek V4.1 Flash Uncensored", "DeepSeek V4.2 Flash Uncensored"),
])
def test_bad_units_or_rates_fail_closed(before, after):
    with pytest.raises(RuntimeError):
        maple.parse_prices(PRICES.replace(before, after))


# Shape of maple.ai/pricing after the 2026-11-12 redesign: new unit wording and
# struck-through superseded rates beside the current ones.
REDESIGNED_PRICES = """<p class="pr-lead">USD per 1 million tokens. The same in Chat, Maple Work and the API.</p>
<table><thead><tr><th scope="col">Model</th><th scope="col">Input</th>
<th scope="col">Cached<span class="pr-wide"> input</span></th><th scope="col">Output</th></tr></thead><tbody>
<tr> <th scope="row">DeepSeek V4.1 Flash Uncensored</th>
<td><span class="pr-cell"><span>$0.65</span></span></td><td><span class="pr-cell"><span>$0.13</span></span></td>
<td><span class="pr-cell"><span>$1.45</span></span></td> </tr>
<tr> <th scope="row">GLM-5.3 Flash</th>
<td><span class="pr-cell"><s class="pr-was"><span class="sr-only">Previously </span>$0.80</s><span>$0.34</span></span></td>
<td><span class="pr-cell"><s class="pr-was"><span class="sr-only">Previously </span>$0.20</s><span>$0.08</span></span></td>
<td><span class="pr-cell"><s class="pr-was"><span class="sr-only">Previously </span>$2.50</s><span>$1.09</span></span></td> </tr>
</tbody></table>"""


def test_redesigned_page_parses_the_same_exact_rates():
    price = maple.parse_prices(REDESIGNED_PRICES)
    assert price.prompt_micro_per_m == 650_000
    assert price.tiers[0].prompt_cached_micro_per_m == 130_000
    assert price.completion_micro_per_m == 1_450_000


def test_struck_through_previous_rate_is_ignored_and_current_rate_is_read():
    lowered = REDESIGNED_PRICES.replace(
        "<span>$0.65</span>",
        '<s class="pr-was"><span class="sr-only">Previously </span>$0.65</s><span>$0.50</span>',
        1,
    )
    assert maple.parse_prices(lowered).prompt_micro_per_m == 500_000


@pytest.mark.parametrize(("before", "after"), [
    ("USD per 1 million tokens.", "EUR per 1 million tokens."),
    ("USD per 1 million tokens.", "USD per 1 thousand tokens."),
    ("<span>$0.13</span>", "<span>$0.13</span><span>$0.12</span>"),
])
def test_redesigned_page_bad_units_or_ambiguous_rates_fail_closed(before, after):
    with pytest.raises(RuntimeError):
        maple.parse_prices(REDESIGNED_PRICES.replace(before, after, 1))


def test_duplicate_model_price_is_ambiguous_even_when_equal():
    with pytest.raises(RuntimeError):
        maple.parse_prices(PRICES + PRICES)


def test_exact_native_capability_and_context():
    assert maple.parse_context(CAPABILITIES) == 1_048_576
    for changed in [
        CAPABILITIES.replace(maple.NATIVE_ID, "deepseek-v4-1-flash"),
        CAPABILITIES.replace("1,048,576", "2,097,152"),
        CAPABILITIES.replace("1,048,576", "unlimited"),
        CAPABILITIES + CAPABILITIES,
    ]:
        with pytest.raises(RuntimeError):
            maple.parse_context(changed)


def test_fetch_writes_held_text_only_route_without_inventing_fees(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "_PENDING_ENCLAVE_MODELS", frozenset({maple.MODEL_ID}))
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = {maple.PRICING_URL: live_pricing_page(), maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    result = maple.fetch()
    maple.write_provider_manifest(result)
    manifest = json.loads(maple.MANIFEST_PATH.read_text())
    assert len(manifest["models"]) == 1
    row = manifest["models"][0]
    assert row["id"] == maple.MODEL_ID
    assert row["upstream_id"] == maple.NATIVE_ID
    assert row["routable"] is False
    assert row["routable_reason"] == "attested-enclave-rollout-pending"
    assert row["input_modalities"] == ["text"]
    assert row["supports_tools"] is False
    assert row["cached_input_token_price_per_m"] == 130_000
    assert not any("fixed" in key or "minimum" in key or "rounding" in key for key in row)


def test_failed_refresh_cannot_reuse_prior_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = {maple.PRICING_URL: live_pricing_page(), maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    result = maple.fetch()
    pages[maple.MODEL_DOCS_URL] = "temporarily unavailable"
    with pytest.raises(RuntimeError):
        maple.fetch()
    with pytest.raises(RuntimeError, match="fresh reviewed"):
        maple.write_provider_manifest(result)
    assert not maple.MANIFEST_PATH.exists()


def test_price_refresh_does_not_clear_failed_encrypted_canary(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    monkeypatch.setattr(maple, "_PENDING_ENCLAVE_MODELS", frozenset())
    pages = {maple.PRICING_URL: live_pricing_page(), maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    maple.MANIFEST_PATH.write_text(json.dumps({"provider": "maple", "models": [{
        "id": maple.MODEL_ID, "routable": False, "routable_reason": "provider-canary-failed",
    }]}))
    maple.write_provider_manifest(maple.fetch())
    row = json.loads(maple.MANIFEST_PATH.read_text())["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "provider-canary-failed"


def test_maple_manifest_prices_expire_and_pending_routes_stay_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "_PENDING_ENCLAVE_MODELS", frozenset({maple.MODEL_ID}))
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = {maple.PRICING_URL: live_pricing_page(), maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    maple.write_provider_manifest(maple.fetch())
    manifest = json.loads(maple.MANIFEST_PATH.read_text())
    assert provider_manifest_valid_until("maple", manifest) == EXPIRED_PROVIDER_MANIFEST
    # Even after a future reviewed promotion, the last good prices have a deadline.
    manifest["models"][0]["routable"] = True
    generated = datetime(2026, 10, 28, tzinfo=UTC)
    manifest["generated_at"] = generated.isoformat()
    assert provider_manifest_valid_until("maple", manifest) == generated + timedelta(days=14)
    manifest["generated_at"] = "invalid"
    assert provider_manifest_valid_until("maple", manifest) == EXPIRED_PROVIDER_MANIFEST


def test_reviewed_maple_refresh_publishes_only_exact_text_workload(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = {maple.PRICING_URL: live_pricing_page(), maple.MODEL_DOCS_URL: CAPABILITIES}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    maple.write_provider_manifest(maple.fetch())
    row, = json.loads(maple.MANIFEST_PATH.read_text())["models"]
    assert row["id"] == maple.MODEL_ID and row["upstream_id"] == maple.NATIVE_ID
    assert row["routable"] is True and "routable_reason" not in row
    assert row["input_modalities"] == ["text"] and row["endpoints"] == ["chat/completions"]
    assert not row["supports_tools"] and not row["supports_reasoning"]
    assert row["cached_input_token_price_per_m"] == 130_000


EXPANDED_MODELS = [
    ("glm-5-3-flash", "z-ai/glm-5.3-flash", "GLM-5.3 Flash", "1,048,576", "0.34", "0.08", "1.09"),
    ("glm-5-3", "z-ai/glm-5.3", "GLM 5.3", "262,144", "2.61", "0.25", "9.66"),
    ("kimi-k3-1-pro", "moonshotai/kimi-k3.1-pro", "Kimi K3.1 Pro", "262,144", "6.00", "0.60", "25.00"),
    ("deepseek-v4-1-flash", "deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash", "1,048,576", "0.65", "0.13", "1.45"),
    ("gpt-oss-120b", "openai/gpt-oss-120b", "OpenAI GPT-OSS 120B", "131,072", "0.23", None, "0.90"),
    ("gemma4-31b", "google/gemma-4-31b-it", "Gemma 4 31B", "262,144", "0.60", None, "1.50"),
    ("llama3-3-70b", "meta-llama/llama-3.3-70b-instruct", "Llama 3.3 70B", "131,072", "2.63", None, "4.13"),
    ("gpt-oss-safeguard-120b", "openai/gpt-oss-safeguard-120b", "OpenAI GPT-OSS Safeguard 120B API only", "131,072", "0.23", None, "0.90"),
    (maple.NATIVE_ID, maple.MODEL_ID, maple.DISPLAY_NAME, "1,048,576", "0.65", "0.13", "1.45"),
]


def expanded_pages():
    # fetch() pins the reviewed pricing section, so prices come from the live
    # page capture; its rates are the ones listed in EXPANDED_MODELS.
    capabilities = '<table><thead><tr><th>Model</th><th>Context tokens</th><th>Thinking</th><th>Image input</th><th>Tool calls</th></tr></thead><tbody>'
    for native, _, name, context, _prompt, _cached, _output in EXPANDED_MODELS:
        thinking = "No" if native == "llama3-3-70b" else "Yes"
        image = "Yes" if native in {"glm-5-3-flash", "kimi-k3-1-pro", "deepseek-v4-1-flash", maple.NATIVE_ID, "gemma4-31b"} else "No"
        tools = "No" if native == "gpt-oss-safeguard-120b" else "Yes"
        capabilities += f'<tr><td>{name}<code>{native}</code></td><td>{context}</td><td>{thinking}</td><td>{image}</td><td>{tools}</td></tr>'
    return {
        maple.PRICING_URL: live_pricing_page(),
        maple.MODEL_DOCS_URL: capabilities + "</tbody></table><p>Embeddings: <code>nomic-embed-text</code></p>",
    }


def test_all_published_models_discovered_without_promoting_unqualified_routes(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    monkeypatch.setattr(maple, "fetch_html", expanded_pages().__getitem__)
    result = maple.fetch()
    assert len(result.prices) == 9
    maple.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(maple.MANIFEST_PATH.read_text())["models"]}
    assert len(rows) == 10
    assert {key for key, row in rows.items() if row["routable"]} == {maple.MODEL_ID}
    for native, canonical, _, context, prompt, cached, output in EXPANDED_MODELS:
        row = rows[canonical]
        assert row["upstream_id"] == native
        assert row["context_length"] == int(context.replace(",", ""))
        assert row["input_token_price_per_m"] == int(Decimal(prompt) * 1_000_000)
        assert row["cached_input_token_price_per_m"] == int(Decimal(cached or prompt) * 1_000_000)
        assert row["output_token_price_per_m"] == int(Decimal(output) * 1_000_000)
        if canonical != maple.MODEL_ID:
            assert row["routable_reason"] == "attested-enclave-qualification-pending"
    assert rows["moonshotai/kimi-k3.1-pro"]["input_modalities"] == ["text", "image"]
    assert rows["moonshotai/kimi-k3.1-pro"]["supports_reasoning"] is True
    assert rows["openai/gpt-oss-safeguard-120b"]["supports_tools"] is False
    assert rows["meta-llama/llama-3.3-70b-instruct"]["supports_reasoning"] is False
    embedding = rows["maple/nomic-embed-text"]
    assert embedding["model_type"] == "embedding" and embedding["endpoints"] == ["embeddings"]
    assert "input_token_price_per_m" not in embedding
    assert embedding["routable_reason"] == "price-and-encrypted-embedding-qualification-pending"


@pytest.mark.parametrize("before,after", [
    ("kimi-k3-1-pro", "kimi-k3-2-pro"),
    ("kimi-k3-1-pro", "auto:powerful"),
    ("<td>Yes</td>", "<td>Maybe</td>"),
    ("262,144", "2,097,152"),
])
def test_new_identity_or_ambiguous_capabilities_fail_closed(before, after):
    html = expanded_pages()[maple.MODEL_DOCS_URL]
    with pytest.raises(RuntimeError):
        maple.parse_catalog(html.replace(before, after, 1))


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pricing"


def live_pages_2026_11_19():
    # Trimmed captures of maple.ai/docs/proxy/models and maple.ai/pricing after
    # Maple added Inkling 2 Small (model list reviewed 16 November 2026).
    return {
        maple.MODEL_DOCS_URL: (FIXTURE_DIR / "maple_proxy_models_2026-11-19.html").read_text(encoding="utf-8"),
        maple.PRICING_URL: (FIXTURE_DIR / "maple_pricing_2026-11-19.html").read_text(encoding="utf-8"),
    }


def live_pricing_page():
    return live_pages_2026_11_19()[maple.PRICING_URL]


def test_live_catalog_inventories_inkling_2_small_as_a_held_row(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    monkeypatch.setattr(maple, "fetch_html", live_pages_2026_11_19().__getitem__)
    result = maple.fetch()
    assert len(result.prices) == 10
    assert result.prices["thinkingmachines/inkling-2-small"] == ModelPrice(
        450_000, 1_500_000, prompt_cached_micro_per_m=50_000
    )
    assert result.prices[maple.MODEL_ID] == ModelPrice(650_000, 1_450_000, prompt_cached_micro_per_m=130_000)

    maple.write_provider_manifest(result)
    rows = {row["id"]: row for row in json.loads(maple.MANIFEST_PATH.read_text())["models"]}
    assert len(rows) == 11
    # Inventory only: the qualified uncensored workload stays the sole route.
    assert {key for key, row in rows.items() if row["routable"]} == {maple.MODEL_ID}
    inkling = rows["thinkingmachines/inkling-2-small"]
    assert inkling["upstream_id"] == "inkling-2-small"
    assert inkling["display_name"] == "Inkling 2 Small"
    assert inkling["context_length"] == 524_288
    assert inkling["input_modalities"] == ["text", "image"]
    assert inkling["supports_tools"] is True and inkling["supports_reasoning"] is True
    assert inkling["routable_reason"] == "attested-enclave-qualification-pending"
    assert (
        inkling["input_token_price_per_m"],
        inkling["cached_input_token_price_per_m"],
        inkling["output_token_price_per_m"],
    ) == (450_000, 50_000, 1_500_000)


INKLING_PRICE = ModelPrice(450_000, 1_500_000, prompt_cached_micro_per_m=50_000)
LEAD = "USD per 1 million tokens."
CAPTION = "in US dollars per one million tokens"
FOOTNOTE = "Automatic model aliases are billed at the rate of the model selected for the request."
INKLING_ROW = '<th scope="row">Inkling 2 Small</th>'
DASH_CELL = (
    '<span class="pr-dash"><span aria-hidden="true">\u2013</span>'
    '<span class="sr-only">Standard input price applies</span></span>'
)
PIN_REFUSAL = "maple: pricing section text changed; review"


def _edited(*swaps, page=None):
    page = live_pricing_page() if page is None else page
    for before, after in swaps:
        assert page.count(before) == 1, before
        page = page.replace(before, after)
    return page


def _edit_row(page, model, before, after):
    start = page.index(f'<th scope="row">{model}')
    end = page.index("</tr>", start)
    row = page[start:end]
    assert row.count(before) == 1, before
    return page[:start] + row.replace(before, after) + page[end:]


def _table(headers, *rows):
    head = "".join(f"<th>{header}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def test_reviewed_pricing_section_is_the_live_text_and_states_the_unit():
    maple.verify_pricing_page(live_pricing_page())
    for text in (LEAD, CAPTION, "Model Input Cached input Output", FOOTNOTE,
                 "Inkling 2 Small {price} {price} {price}", "OpenAI GPT-OSS Safeguard 120B API only"):
        assert text in maple._REVIEWED_PRICING_SECTION


def test_a_price_value_change_alone_still_parses(monkeypatch, tmp_path):
    # The pin never blocks a price change: a current rate, a struck previous
    # rate, and a standard-input dash that becomes a cached rate.
    page = _edited(("<span>$0.45</span>", "<span>$0.40</span>"))
    page = _edit_row(page, "Gemma 4 31B", "$0.80</s>", "$0.70</s>")
    page = _edit_row(page, "Gemma 4 31B", DASH_CELL, '<span class="pr-cell"><span>$0.30</span></span>')
    maple.verify_pricing_page(page)
    assert maple.parse_prices(page, "Inkling 2 Small") == ModelPrice(400_000, 1_500_000, prompt_cached_micro_per_m=50_000)
    assert maple.parse_prices(page, "Gemma 4 31B") == ModelPrice(600_000, 1_500_000, prompt_cached_micro_per_m=300_000)
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = {**live_pages_2026_11_19(), maple.PRICING_URL: page}
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    assert maple.fetch().prices["thinkingmachines/inkling-2-small"].prompt_micro_per_m == 400_000


FULL_HEADER_ALTERNATE = (
    "<table><caption>Above 128K context</caption><thead><tr><th>Model</th><th>Input</th>"
    "<th>Cached input</th><th>Output</th></tr></thead><tbody><tr><th>Inkling 2 Small (above 128K)</th>"
    "<td>$0.90</td><td>$0.10</td><td>$3.00</td></tr></tbody></table>"
)


@pytest.mark.parametrize("swaps", [
    # Unit changes in the lead, caption and footnote, including the spellings
    # the round-3 review found the unit parser skipped.
    [(LEAD, "USD per 1 thousand tokens.")],
    [(CAPTION, "in US dollars per one thousand tokens")],
    [(LEAD, "USD per two million tokens.")],
    [(FOOTNOTE, FOOTNOTE + " Cached input is billed per 1,000 cached-input tokens.")],
    [(FOOTNOTE, FOOTNOTE + " Batch jobs are USD per 1 million requests.")],
    # A footnote hidden after an <hr/>, or outside the table's wrapper div.
    [("</table>", "</table><hr/><p>Cached input is billed per 1K tokens.</p>")],
    [("</section>", "<p>Prompts above 128K tokens bill at twice these rates.</p></section>")],
    # Qualified, renamed and added rows.
    [(INKLING_ROW, '<th scope="row">Inkling 2 Small (up to 128K)</th>')],
    [(">API only</span>", ">(API only)</span>")],
    [("</tbody>", '<tr><th scope="row">Inkling 2 Small (above 128K)</th><td>$0.90</td>'
                  "<td>$0.10</td><td>$3.00</td></tr></tbody>")],
])
def test_any_other_text_change_in_the_pricing_section_is_refused(swaps):
    with pytest.raises(RuntimeError, match=PIN_REFUSAL):
        maple.verify_pricing_page(_edited(*swaps))


@pytest.mark.parametrize("where", ["inside", "outside"])
def test_a_full_header_alternate_table_is_refused(where):
    page = live_pricing_page()
    page = _edited(("</table>", "</table>" + FULL_HEADER_ALTERNATE)) if where == "inside" else page + FULL_HEADER_ALTERNATE
    with pytest.raises(RuntimeError, match="maple: expected exactly one token price table; review"):
        maple.verify_pricing_page(page)


ABOVE_128K_TABLE = (
    "<table><caption>Above 128K context: USD per 1 million tokens</caption>"
    "<thead><tr><th>Model</th><th>Input</th><th>Output</th></tr></thead>"
    '<tbody><tr><th scope="row">Inkling 2 Small</th><td>$0.90</td><td>$3.00</td></tr></tbody></table>'
)


@pytest.mark.parametrize("extra", [
    ABOVE_128K_TABLE,
    _table(["Plan", "Price"], ["Pro", "$20/month"]),
    _table(["Model", "Batch"], ["Inkling&nbsp;2&nbsp;Small", "$0.20"]),
    _table(["Model", "Batch"], ["OpenAI GPT-OSS Safeguard 120B (API only)", "0.12 USD"]),
    _table(["Model", "Batch"], ["GLM 5.3", "0.12/M"]),
    _table(["Model", "Batch"], ["GLM 5.3", "0.00012 /1K"]),
    _table(["Model", "Batch"], ["GLM 5.3", "\u20ac0.11"]),
    _table(["Model", "Batch"], ["GLM 5.3", "12\u00a2"]),
    # Currency codes glued to the amount (round-4 review).
    "<table><caption>DeepSeek V4.1 Flash Uncensored above 128K tokens</caption>"
    "<thead><tr><th>Input</th><th>Output</th></tr></thead>"
    "<tbody><tr><td>USD1.30</td><td>USD2.90</td></tr></tbody></table>",
    _table(["Model", "Batch"], ["GLM 5.3", "EUR1.20"]),
    _table(["Model", "Batch"], ["GLM 5.3", "1.10 GBP"]),
    _table(["Model", "Batch"], ["GLM 5.3", "jpy180"]),
    # No currency marker at all (round-5 review): plain figures under a
    # unit caption, and a context multiplier.
    "<table><caption>DeepSeek V4.1 Flash Uncensored above 128K tokens, in US dollars per one million tokens"
    "</caption><thead><tr><th>Input</th><th>Output</th></tr></thead>"
    "<tbody><tr><td>1.30</td><td>2.90</td></tr></tbody></table>",
    _table(["Context", "Price multiplier"], ["Up to 128K tokens", "1x"], ["Above 128K tokens", "2x"]),
    # No figures at all (round-6 review): a written-out tier, and cached-input
    # rates in words per thousand tokens.
    _table(["Context", "Rates"], ["Above one hundred thousand tokens", "Double the listed input and output rates"]),
    "<table><caption>Cached input, in US dollars per thousand tokens</caption>"
    "<thead><tr><th>Model family</th><th>Cached input</th></tr></thead>"
    "<tbody><tr><td>Inkling</td><td>Five hundredths of a cent</td></tr></tbody></table>",
    # Trade-off: any new table holds Maple for review, even an unpriced
    # capability or modality table, with or without model names.
    _table(["Model", "Image support"], ["Inkling 2 Small", "Yes"], ["GLM 5.3", "No"]),
    _table(["Model", "Input (modalities)", "Output (modalities)"],
           ["Inkling 2 Small", "Text, image", "Text"], ["GLM 5.3", "Text", "Text"]),
    _table(["Feature", "Chat", "API"], ["Image support", "Yes", "Yes"], ["Tool calls", "Yes", "No"]),
    _table(["Model family", "Input (modalities)", "Output (modalities)"],
           ["Inkling", "Text, image", "Text"], ["Kimi", "Text, image", "Text"]),
])
def test_any_other_table_on_the_pricing_page_is_refused(extra):
    with pytest.raises(RuntimeError, match="maple: unexpected table on the pricing page; review"):
        maple.verify_pricing_page(live_pricing_page() + extra)


def test_an_extra_table_inside_the_pricing_section_is_refused():
    page = _edited(("</table>", "</table><table><tr><td>Rates double for long prompts</td></tr></table>"))
    with pytest.raises(RuntimeError, match="maple: unexpected table on the pricing page; review"):
        maple.verify_pricing_page(page)


@pytest.mark.parametrize("extra", [
    # Prose outside the parsed structures is out of scope: ordinary copy,
    # even with a price, never refuses the page.
    "<h3>What is a context window?</h3>",
    "<p>Ask us about the enterprise tier.</p>",
    "<p>Context-aware answers.</p>",
    "<p>Over 1M downloads.</p>",
    "<p>Try Inkling 2 Small with Pro for $20/month.</p>",
])
def test_ordinary_copy_outside_a_table_passes(extra):
    page = live_pricing_page() + extra
    maple.verify_pricing_page(page)
    assert maple.parse_prices(page, "Inkling 2 Small") == INKLING_PRICE


def test_extra_tier_table_fails_the_whole_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = live_pages_2026_11_19()
    pages[maple.PRICING_URL] += ABOVE_128K_TABLE
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    with pytest.raises(RuntimeError, match="unexpected table on the pricing page"):
        maple.fetch()
    assert maple._DISCOVERED == {}


def test_unpriced_mentions_and_plan_prices_elsewhere_are_not_model_prices():
    # Live FAQ and plan wording: model names without a price, prices without
    # a model name. Neither is a second token price.
    page = live_pricing_page() + (
        "<details><summary>Did prices go down on every plan?</summary><p>Yes. "
        "DeepSeek V4.1 Flash, DeepSeek V4.1 Flash Uncensored and the Kimi models' "
        "output prices stay the same.</p></details>"
        "<div><p>Max $100</p><p>Max+ $200 /mo</p><p>Inkling 2 Small is new.</p></div>"
    )
    maple.verify_pricing_page(page)
    assert maple.parse_prices(page, "Inkling 2 Small") == INKLING_PRICE
    assert maple.parse_prices(page, maple.DISPLAY_NAME) == ModelPrice(
        650_000, 1_450_000, prompt_cached_micro_per_m=130_000
    )


@pytest.mark.parametrize("after", ["inkling-2-medium", "auto:quick"])
def test_live_catalog_still_fails_closed_on_an_unreviewed_identity(after):
    html = live_pages_2026_11_19()[maple.MODEL_DOCS_URL]
    assert maple.parse_catalog(html)["thinkingmachines/inkling-2-small"]["upstream_id"] == "inkling-2-small"
    with pytest.raises(RuntimeError, match="new or ambiguous model identity"):
        maple.parse_catalog(html.replace("<code>inkling-2-small</code>", f"<code>{after}</code>", 1))


def test_expanded_discovery_does_not_reuse_state_after_a_price_failure(monkeypatch):
    pages = expanded_pages()
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    maple.fetch()
    # A $0 rate keeps the pinned section text (prices are placeholders) and
    # fails only price parsing, after discovery has already run.
    assert pages[maple.PRICING_URL].count("<span>$6.00</span>") == 1
    pages[maple.PRICING_URL] = pages[maple.PRICING_URL].replace("<span>$6.00</span>", "<span>$0</span>")
    maple.verify_pricing_page(pages[maple.PRICING_URL])
    with pytest.raises(RuntimeError, match="maple: invalid token prices"):
        maple.fetch()
    assert maple._DISCOVERED == {}


@pytest.mark.parametrize("uncached,cached,output", [(36, 0, 4), (81, 12_288, 19), (0, 1_000_000, 100_000)])
def test_published_maple_route_uses_standard_retail_markup_without_request_minimum(uncached, cached, output):
    from trusted_router.authorize_candidates import endpoint_cost_microdollars
    from trusted_router.catalog import MODEL_ENDPOINTS

    endpoint, = [row for row in MODEL_ENDPOINTS.values() if row.provider == "maple"]
    assert endpoint.model_id == maple.MODEL_ID and endpoint.upstream_id == maple.NATIVE_ID
    expected = sum(int((Decimal(count) * Decimal(rate) / 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                   for count, rate in zip((uncached, cached, output), (685_750, 137_150, 1_529_750), strict=True))
    assert endpoint_cost_microdollars(endpoint, uncached, output, cache_read_tokens=cached) == expected


@pytest.mark.parametrize("uncached,cached,output", [
    (0, 0, 0), (1, 0, 1), (36, 0, 4), (81, 12_288, 19),
    (1_000_000, 0, 100_000), (0, 1_000_000, 100_000),
])
def test_maple_bills_actual_cache_meters_without_provider_credit_rounding(uncached, cached, output):
    from trusted_router.authorize_candidates import endpoint_cost_microdollars
    from trusted_router.catalog_data import ModelEndpoint, PriceTier

    price = maple.parse_prices(PRICES)
    endpoint = ModelEndpoint(
        id="maple-cache-oracle", model_id=maple.MODEL_ID,
        upstream_id=maple.NATIVE_ID, provider="maple", usage_type="Credits",
        price_tiers=tuple(PriceTier(t.max_prompt_tokens, t.prompt_micro_per_m,
                                   t.completion_micro_per_m, t.prompt_cached_micro_per_m)
                          for t in price.tiers),
    )
    # Independent public-tariff oracle; cache reads replace, never add to,
    # uncached prompt tokens. Preserve the ledger's microdollar rounding.
    expected = sum(int((Decimal(count) * Decimal(rate) / 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                   for count, rate in zip((uncached, cached, output), (650_000, 130_000, 1_450_000), strict=True))
    assert endpoint_cost_microdollars(endpoint, uncached, output, cache_read_tokens=cached) == expected
