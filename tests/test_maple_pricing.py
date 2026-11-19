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
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
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
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
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
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
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
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
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
    pages = {maple.PRICING_URL: PRICES, maple.MODEL_DOCS_URL: CAPABILITIES}
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
    prices = '<p>USD per 1 million tokens.</p><table><thead><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Output</th></tr></thead><tbody>'
    capabilities = '<table><thead><tr><th>Model</th><th>Context tokens</th><th>Thinking</th><th>Image input</th><th>Tool calls</th></tr></thead><tbody>'
    for native, _, name, context, prompt, cached, output in EXPANDED_MODELS:
        cache = "$" + cached if cached is not None else "\u2013Standard input price applies"
        prices += f'<tr><th>{name}</th><td><s>$99</s>${prompt}</td><td>{cache}</td><td>${output}</td></tr>'
        thinking = "No" if native == "llama3-3-70b" else "Yes"
        image = "Yes" if native in {"glm-5-3-flash", "kimi-k3-1-pro", "deepseek-v4-1-flash", maple.NATIVE_ID, "gemma4-31b"} else "No"
        tools = "No" if native == "gpt-oss-safeguard-120b" else "Yes"
        capabilities += f'<tr><td>{name}<code>{native}</code></td><td>{context}</td><td>{thinking}</td><td>{image}</td><td>{tools}</td></tr>'
    return {
        maple.PRICING_URL: prices + "</tbody></table>",
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
PER_MILLION_FOOTER = "<footer>Legacy pricing: USD per 1 million tokens.</footer>"


@pytest.mark.parametrize("swaps", [
    # The table's lead and its caption both move to per-thousand units.
    [("USD per 1 million tokens.", "USD per 1 thousand tokens."),
     ("in US dollars per one million tokens", "in US dollars per one thousand tokens")],
    [("in US dollars per one million tokens", "in US dollars per one thousand tokens")],
    [("USD per 1 million tokens.", "USD per 1 thousand tokens.")],
    [("USD per 1 million tokens.", "USD per 1 million tokens. Cached input is USD per 1 thousand tokens.")],
    # No unit stated by the table at all; only an unrelated footer states one.
    [("USD per 1 million tokens.", "Prices by model."),
     ("Model prices on every plan, in US dollars per one million tokens", "Model prices on every plan")],
])
def test_unit_must_be_stated_by_the_price_table_not_elsewhere_on_the_page(swaps):
    page = live_pages_2026_11_19()[maple.PRICING_URL]
    assert maple.parse_prices(page, "Inkling 2 Small") == INKLING_PRICE
    for before, after in swaps:
        assert before in page
        page = page.replace(before, after)
    with pytest.raises(RuntimeError, match="unit changed"):
        maple.parse_prices(page + PER_MILLION_FOOTER, "Inkling 2 Small")


ABOVE_128K_TABLE = (
    "<table><caption>Above 128K context: USD per 1 million tokens</caption>"
    "<thead><tr><th>Model</th><th>Input</th><th>Output</th></tr></thead>"
    '<tbody><tr><th scope="row">Inkling 2 Small</th><td>$0.90</td><td>$3.00</td></tr></tbody></table>'
)


@pytest.mark.parametrize("extra", [
    ABOVE_128K_TABLE,
    # Another token table without any condition wording.
    ABOVE_128K_TABLE.replace("Above 128K context: USD per 1 million tokens", "Batch prices"),
    # Another table that prices a published model under other column names.
    "<table><thead><tr><th>Model</th><th>Price</th></tr></thead>"
    "<tbody><tr><td>Inkling 2 Small</td><td>$0.90</td></tr></tbody></table>",
    # A section outside any table that prices a published model.
    "<p><strong>Inkling 2 Small</strong> batch rate: $0.20 in, $0.75 out.</p>",
    # A tier or context condition with no table at all.
    "<p>Prompts over 128K tokens are billed at twice the listed rate.</p>",
    "<p>Long-context requests use separate prices.</p>",
])
def test_other_price_tables_sections_or_conditions_are_refused(extra):
    page = live_pages_2026_11_19()[maple.PRICING_URL]
    with pytest.raises(RuntimeError, match="maple: "):
        maple.parse_prices(page + extra, "Inkling 2 Small")


def test_extra_tier_table_fails_the_whole_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(maple, "MANIFEST_PATH", tmp_path / "maple.json")
    monkeypatch.setattr(maple, "_DISCOVERED", {})
    pages = live_pages_2026_11_19()
    pages[maple.PRICING_URL] += ABOVE_128K_TABLE
    monkeypatch.setattr(maple, "fetch_html", pages.__getitem__)
    with pytest.raises(RuntimeError):
        maple.fetch()
    assert maple._DISCOVERED == {}


def test_unpriced_mentions_and_plan_prices_elsewhere_are_not_model_prices():
    # Live FAQ and plan wording: model names without a price, prices without
    # a model name. Neither is a second token price.
    page = live_pages_2026_11_19()[maple.PRICING_URL] + (
        "<details><summary>Did prices go down on every plan?</summary><p>Yes. "
        "DeepSeek V4.1 Flash, DeepSeek V4.1 Flash Uncensored and the Kimi models' "
        "output prices stay the same.</p></details>"
        "<div><p>Max $100</p><p>Max+ $200 /mo</p><p>Inkling 2 Small is new.</p></div>"
    )
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
    pages[maple.PRICING_URL] = pages[maple.PRICING_URL].replace("Kimi K3.1 Pro", "Kimi K3")
    with pytest.raises(RuntimeError):
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
