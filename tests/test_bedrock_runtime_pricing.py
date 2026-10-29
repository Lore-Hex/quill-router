import html
from decimal import ROUND_HALF_UP, Decimal

import pytest

from scripts.pricing.providers import amazon_bedrock_runtime as runtime

CARD = """<h1>Claude Haiku 5.5</h1><p>Prompts of 100,000 tokens or fewer are billed at the standard per-token rates.
Prompts that exceed 100,000 tokens are billed at higher rates for input, output, and cache read and write operations.</p>"""


def fixture():
    rows = []
    records = {}
    amounts = {
        "Claude Haiku 5.5": ("0.11", "0.55", "0.1375", "0.011"),
        runtime.LONG_CONTEXT_ROW: ("0.55", "2.75", "0.6875", "0.055"),
        "Claude Sonnet 5.5": ("2.2", "11", "2.75", "0.11"),
        "Claude Opus 5.5": ("4.4", "22", "5.5", "0.22"),
        "Claude Sonnet 5": ("2.2", "11", "2.75", "0.22"),
        "Claude Opus 4.8": ("5.5", "27.5", "6.875", "0.55"),
    }
    for i, (name, prices) in enumerate(amounts.items()):
        tokens = []
        for j, price in enumerate(prices):
            key = f"m{i}p{j}"
            records[key] = {"price": price, "RegionlessRateCode": key}
            tokens.append(f"{{priceOf!bedrockfoundationmodels/reviewed!{key}}}")
        cells = [name, tokens[0], tokens[1], "N/A", "N/A", tokens[2], "N/A", tokens[3]]
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    table = "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in runtime.HEADERS) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    markup = f"<h2>{runtime.SCOPE}</h2>" + table
    page = f'<div data-pricing-markup="{html.escape(markup)}"></div>'
    return page, {"reviewed": {"regions": {runtime.REGION: records}}}


def test_exact_us_tariffs_and_long_context_boundary():
    page, artifacts = fixture()
    prices = runtime.parse_prices(page, artifacts, CARD)
    assert set(prices) == set(runtime.MODELS.values())
    haiku = prices["anthropic/claude-haiku-5.5"]
    assert haiku.tiers[0].max_prompt_tokens == 100_000
    assert haiku.tiers[1].max_prompt_tokens is None
    assert (haiku.tiers[0].prompt_micro_per_m, haiku.tiers[1].prompt_micro_per_m) == (110_000, 550_000)
    assert (haiku.tiers[0].prompt_cached_micro_per_m, haiku.tiers[1].prompt_cached_micro_per_m) == (11_000, 55_000)
    assert prices["anthropic/claude-sonnet-5.5"].tiers[0].prompt_cached_micro_per_m == 110_000


def test_same_scope_for_other_provider_does_not_match():
    page, artifacts = fixture()
    other_provider = page.replace("Anthropic models", "Amazon models")
    prices = runtime.parse_prices(other_provider + page, artifacts, CARD)
    assert set(prices) == set(runtime.MODELS.values())


def test_same_provider_provisioned_capacity_does_not_match():
    page, artifacts = fixture()
    provisioned = page.replace("Price per 1M input tokens", "Price per hour per 1K Input TPM with 1-Month Commitment")
    prices = runtime.parse_prices(provisioned + page, artifacts, CARD)
    assert set(prices) == set(runtime.MODELS.values())


@pytest.mark.parametrize("uncached,read,write", [
    (100_000, 0, 0), (100_001, 0, 0), (100, 99_900, 0),
    (100, 99_901, 0), (100, 0, 99_900), (100, 0, 99_901),
])
def test_haiku_tier_counts_cache_reads_and_writes(uncached, read, write):
    from trusted_router.authorize_candidates import endpoint_cost_microdollars
    from trusted_router.catalog_data import ModelEndpoint, PriceTier

    page, artifacts = fixture()
    price = runtime.parse_prices(page, artifacts, CARD)["anthropic/claude-haiku-5.5"]
    endpoint = ModelEndpoint(
        id="test-bedrock-haiku", model_id="anthropic/claude-haiku-5.5",
        provider="amazon-bedrock", usage_type="Credits",
        price_tiers=tuple(PriceTier(t.max_prompt_tokens, t.prompt_micro_per_m,
                                   t.completion_micro_per_m, t.prompt_cached_micro_per_m)
                          for t in price.tiers),
    )
    # Independent decimal oracle at the public USD/M tariff and the ledger's
    # half-up-per-meter rounding. No production price selector is reused.
    rates = (110_000, 550_000, 11_000, 137_500) if uncached + read + write <= 100_000 else (550_000, 2_750_000, 55_000, 687_500)
    expected = sum(int((Decimal(count) * Decimal(rate) / 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                   for count, rate in zip((uncached, 5, read, write), rates, strict=True))
    assert endpoint_cost_microdollars(endpoint, uncached, 5, cache_read_tokens=read, cache_creation_tokens=write) == expected


def test_runtime_limits_require_exact_programmatic_profile():
    model = "anthropic/claude-haiku-5.5"
    card = '<ul><li>Context window: 1M tokens</li><li>Max output tokens: 128K</li></ul><table><tr><td>bedrock-runtime</td><td>us.anthropic.claude-haiku-5-5</td></tr></table>'
    row = runtime.model_metadata(card, model)
    assert row["context_length"] == 1_000_000
    assert row["max_output_tokens"] == 128_000
    assert row["routable"] is False
    for invalid in (
        card.replace("bedrock-runtime", "bedrock-mantle"),
        card.replace("us.anthropic", "global.anthropic"),
        card.replace("128K", "2M"),
        card.replace("1M tokens", "unknown"),
    ):
        with pytest.raises(RuntimeError):
            runtime.model_metadata(invalid, model)


@pytest.mark.parametrize("mode", ["inherit", "default", "None", None])
def test_runtime_discovery_requires_all_retention_destinations(monkeypatch, mode):
    checked = []

    def policy(url, **kwargs):
        checked.append(url)
        assert kwargs["extra_headers"] == {"Authorization": "Bearer fake"}
        return {"mode": mode if "us-east-2" in url else "none"}

    monkeypatch.setattr(runtime, "fetch_json", policy)
    monkeypatch.setattr(runtime, "fetch_html", lambda _: pytest.fail("card must not load before policy checks"))
    with pytest.raises(RuntimeError, match="retention none"):
        runtime.fetch_runtime_rows("fake")
    assert len(checked) == 2


@pytest.mark.parametrize("failure", [None, "zero", "boolean", "missing", "stop", "content", "status", "oversize", "json"])
def test_runtime_canary_requires_usage_and_actual_content(httpx_mock, failure):
    response = {"stop_reason": "end_turn", "usage": {"input_tokens": 18, "output_tokens": 5}, "content": [{"type": "text", "text": "PONG"}]}
    if failure in {"zero", "boolean", "missing"}:
        response["usage"]["input_tokens"] = {"zero": 0, "boolean": True, "missing": None}[failure]
    if failure == "stop":
        response["stop_reason"] = "max_tokens"
    if failure == "content":
        response["content"][0]["text"] = "Something else"
    options = {"json": response}
    if failure == "oversize":
        options = {"content": b" " * 65537}
    if failure == "json":
        options = {"content": b"not json"}
    httpx_mock.add_response(
        method="POST", url="https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-haiku-5-5/invoke",
        status_code=403 if failure == "status" else 200, **options,
    )
    assert runtime.probe_runtime("fake", "anthropic/claude-haiku-5.5") is (failure is None)
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("change", ["global", "unit", "duplicate", "model", "boundary", "missing", "write", "nan", "fraction", "region", "identity"])
def test_changed_contract_fails_closed(change):
    page, artifacts = fixture()
    card = CARD
    records = artifacts["reviewed"]["regions"][runtime.REGION]
    if change == "global":
        page = page.replace(runtime.SCOPE, "Global Cross-region Inference")
    elif change == "unit":
        page = page.replace("per 1M", "per 1K")
    elif change == "duplicate":
        page += page
    elif change == "model":
        page = page.replace("Claude Sonnet 5.5", "Claude Sonnet 5.6")
    elif change == "boundary":
        card = card.replace("100,000", "200,000")
    elif change == "missing":
        del records["m0p0"]
    elif change == "write":
        records["m0p2"]["price"] = "0.22"
    elif change == "nan":
        records["m0p0"]["price"] = "NaN"
    elif change == "fraction":
        records["m0p0"]["price"] = "0.1100001"
    elif change == "region":
        artifacts["reviewed"]["regions"]["Global"] = artifacts["reviewed"]["regions"].pop(runtime.REGION)
    elif change == "identity":
        records["m0p0"]["RegionlessRateCode"] = "other"
    with pytest.raises(RuntimeError):
        runtime.parse_prices(page, artifacts, card)
