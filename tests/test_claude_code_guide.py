"""Claude Code examples use controlled catalog rows, never moving manifests."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.fixture_routes import bypass_catalog_caches
from trusted_router import agent_cost_guides as guides
from trusted_router import catalog, routing
from trusted_router.catalog import Model, ModelEndpoint
from trusted_router.pricing import PriceTier

CHEAP = "anthropic/claude-haiku-9.1"
SONNET = "anthropic/claude-sonnet-9.3"
FABLE = "anthropic/claude-fable-9.4"
FABLE_VARIANT = "anthropic/claude-fable-9.9-aws"
OPUS = "anthropic/claude-opus-9.10"
OLDER = "anthropic/claude-opus-9.2"
HIDDEN = "anthropic/claude-hidden-fixture"
OTHER = "example/non-messages-fixture"
PRICES = {CHEAP: (1055000, 5275000), SONNET: (2110000, 10550000), OPUS: (4220000, 21100000),
          OLDER: (5275000, 26375000), FABLE: (8440000, 42200000), FABLE_VARIANT: (9000000, 45000000)}
PATH = "/docs/claude-code"


def native(model_id: str) -> str:
    """The id Anthropic's own API uses for a fixture model: no vendor prefix, no dots."""
    return model_id.split("/", 1)[1].replace(".", "-")


@pytest.fixture(autouse=True)
def controlled_claude_catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, ModelEndpoint]:
    bypass_catalog_caches(monkeypatch)
    models = {
        model_id: Model(model_id, model_id, "anthropic", 200000,
                        supports_messages=model_id != OTHER, internal_only=model_id == HIDDEN)
        for model_id in (*PRICES, HIDDEN, OTHER)
    }
    endpoints = {}
    for model_id in models:
        prompt, output = PRICES.get(model_id, (1000, 2000))
        endpoint = ModelEndpoint(
            id=model_id + "@anthropic/credits", model_id=model_id, provider="anthropic", usage_type="Credits",
            # Deliberately different headlines prove the request tier is used.
            prompt_price_microdollars_per_million_tokens=999000000,
            completion_price_microdollars_per_million_tokens=999000000,
            price_tiers=(PriceTier(50000, prompt, output), PriceTier(None, prompt * 2, output * 2)),
            max_completion_tokens=8192,
            # The haiku route carries a dated id, so it also answers to the undated form.
            upstream_id=native(model_id) + ("-20300101" if model_id == CHEAP else ""),
        )
        endpoints[endpoint.id] = endpoint
    alternative = replace(endpoints[CHEAP + "@anthropic/credits"], id=CHEAP + "@amazon-bedrock/credits",
                          provider="amazon-bedrock", price_tiers=(PriceTier(None, 3165000, 15825000),))
    endpoints[alternative.id] = alternative
    # A cheaper BYOK row for the same model must never set the listed price.
    byok = replace(endpoints[CHEAP + "@anthropic/credits"], id=CHEAP + "@anthropic/byok",
                   usage_type="BYOK", price_tiers=(PriceTier(None, 1000, 1000),))
    endpoints[byok.id] = byok
    monkeypatch.setattr(guides, "MODELS", models)
    monkeypatch.setattr(guides, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)
    # The router's own map of Anthropic ids is read from the same rows.
    monkeypatch.setattr(routing, "MODELS", models)
    monkeypatch.setattr(routing, "MODEL_ENDPOINTS", endpoints)
    return endpoints


def page(client: TestClient) -> BeautifulSoup:
    response = client.get(PATH)
    assert response.status_code == 200
    return BeautifulSoup(response.text, "html.parser")


def test_models_prices_and_session_arithmetic(client: TestClient) -> None:
    soup = page(client)
    rows = soup.select("#models tr[data-model]")
    assert {row["data-model"] for row in rows} == set(PRICES)
    for row in rows:
        prompt, output = PRICES[row["data-model"]]
        cells = [td.get_text(" ", strip=True) for td in row.select("td")]
        assert Decimal(cells[1].split("$")[1]) == Decimal(prompt) / 1000000
        assert Decimal(cells[2].split("$")[1]) == Decimal(output) / 1000000
        assert "200,000" in cells[3]
        assert cells[4].endswith("2" if row["data-model"] == CHEAP else "1")
    assert HIDDEN not in str(soup) and OTHER not in str(soup)
    examples = soup.select("[data-session-model]")
    assert [row["data-session-model"] for row in examples] == [CHEAP, OPUS]
    assert "50 requests" in soup.get_text() and "20,000" in soup.get_text() and "1,000" in soup.get_text()
    for example in examples:
        prompt, output = PRICES[example["data-session-model"]]
        # Literal workload and separate unit conversion, independent of guide constants/helpers.
        micro = sum((Decimal(tokens * rate) / 1000000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                    for tokens, rate in ((20000, prompt), (1000, output)))
        expected = Decimal(50) * max(micro, Decimal(1)) / 1000000
        assert example["data-session-cost"] == f"{expected.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
        assert "(1.00 × $" in example.get_text() and "(0.05 × $" in example.get_text()


def test_exact_exports_and_messages_check(client: TestClient) -> None:
    soup = page(client)
    assert soup.select_one('pre[data-example="exports"]').get_text() == (
        'export ANTHROPIC_BASE_URL="https://api.trustedrouter.com"\n'
        'export ANTHROPIC_AUTH_TOKEN="$TRUSTEDROUTER_API_KEY"'
    )
    command = shlex.split(soup.select_one('pre[data-example="curl"]').get_text())
    assert command[:2] == ["curl", "https://api.trustedrouter.com/v1/messages"]
    assert "Authorization: Bearer $TRUSTEDROUTER_API_KEY" in command
    assert "anthropic-version: 2023-06-01" in command
    assert json.loads(command[command.index("-d") + 1]) == {
        "model": CHEAP, "messages": [{"role": "user", "content": "Reply with PONG only."}], "max_tokens": 16,
    }
    assert soup.select_one('a[href="https://code.claude.com/docs/en/llm-gateway-connect"]')


def test_model_aliases_are_pinned_to_listed_ids(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                                controlled_claude_catalog: dict[str, ModelEndpoint]) -> None:
    soup = page(client)
    # Newest numbered model of each family: 9.10 is newer than 9.2.
    assert soup.select_one('pre[data-example="pins"]').get_text() == (
        f'export ANTHROPIC_DEFAULT_OPUS_MODEL="{OPUS}"\n'
        f'export ANTHROPIC_DEFAULT_SONNET_MODEL="{SONNET}"\n'
        f'export ANTHROPIC_DEFAULT_HAIKU_MODEL="{CHEAP}"\n'
        # A suffixed variant is never the pin, even with a higher number.
        f'export ANTHROPIC_DEFAULT_FABLE_MODEL="{FABLE}"'
    )
    setup = soup.select_one("#setup").get_text(" ", strip=True)
    assert "set the alias variables to listed IDs" in setup and "are not accepted" not in setup
    assert "ANTHROPIC_MODEL" in setup and "claude --model" in setup
    assert "/status" in setup and "~/.claude/settings.json" in setup
    assert "has no /v1" in setup
    for key, endpoint in list(controlled_claude_catalog.items()):
        if endpoint.model_id == SONNET:
            del controlled_claude_catalog[key]
    guides._claude_code_context.cache_clear()
    assert "SONNET_MODEL" not in page(client).select_one('pre[data-example="pins"]').get_text()
    guides._claude_code_context.cache_clear()
    monkeypatch.setattr(guides, "MODELS", {})
    empty = page(client)
    assert not empty.select('pre[data-example="pins"]')
    assert "ANTHROPIC_MODEL" in empty.select_one("#setup").get_text()


def test_default_ids_are_shown_only_while_the_router_resolves_them(
        client: TestClient, monkeypatch: pytest.MonkeyPatch,
        controlled_claude_catalog: dict[str, ModelEndpoint]) -> None:
    def note() -> str | None:
        guides._claude_code_context.cache_clear()
        found = page(client).select_one("#setup p[data-native-ids]")
        return found.get_text(" ", strip=True) if found else None

    # The example is the newest Opus, by the id the router itself resolves.
    text = note()
    assert text is not None and text.startswith("That is the whole setup.")
    assert f"such as {native(OPUS)} ," in text and f"here {OPUS} ." in text
    assert "ANTHROPIC_MODEL and claude --model take either form" in text
    assert "and a listed ID" not in page(client).select_one("#setup").get_text(" ", strip=True)
    assert routing.resolve_model_alias(native(OPUS)) == OPUS
    assert routing.resolve_model_alias(native(CHEAP)) == CHEAP
    # A model with a dated and an undated id is shown by the shorter one.
    assert guides._native_id_example([CHEAP]) == {"native_id": native(CHEAP), "model_id": CHEAP}
    assert guides._native_id_example([OTHER, HIDDEN]) is None

    # Opus served only by another host has no Anthropic id; the next alias is shown.
    key = OPUS + "@anthropic/credits"
    direct = controlled_claude_catalog[key]
    controlled_claude_catalog[key] = replace(direct, provider="amazon-bedrock")
    text = note()
    assert text is not None and f"such as {native(SONNET)} ," in text and native(OPUS) not in text
    controlled_claude_catalog[key] = direct

    # Two models claiming one id: the router resolves neither, so neither is shown.
    older = OLDER + "@anthropic/credits"
    controlled_claude_catalog[older] = replace(controlled_claude_catalog[older], upstream_id=native(OPUS))
    text = note()
    assert routing.resolve_model_alias(native(OPUS)) == native(OPUS)
    assert text is not None and f"such as {native(SONNET)} ," in text and native(OPUS) not in text

    # No resolving id at all: the page goes back to requiring the pins.
    monkeypatch.setattr(guides, "native_model_id_targets", lambda: {})
    guides._claude_code_context.cache_clear()
    setup = page(client).select_one("#setup")
    assert not setup.select("p[data-native-ids]")
    words = setup.get_text(" ", strip=True)
    assert "Then pin the models to listed IDs, which carry the anthropic/ prefix" in words
    assert "whole setup" not in words and "either form" not in words
    assert "or start with claude --model , and a listed ID." in words
    assert setup.select_one('pre[data-example="pins"]')


@pytest.mark.parametrize("missing", ["all", "price", "opus", "all-prices", "all-opus"])
def test_sections_degrade(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                          controlled_claude_catalog: dict[str, ModelEndpoint], missing: str) -> None:
    if missing == "all":
        monkeypatch.setattr(guides, "MODELS", {})
    else:
        target = CHEAP if missing == "price" else OPUS
        for key, endpoint in list(controlled_claude_catalog.items()):
            if (endpoint.model_id == target or missing == "all-prices"
                    or (missing == "all-opus" and endpoint.model_id in {OPUS, OLDER})):
                del controlled_claude_catalog[key]
    soup = page(client)
    assert soup.select_one("#setup") and soup.select_one("#limits")
    assert "None" not in soup.get_text()
    if missing in {"all", "all-prices"}:
        assert not soup.select("#models table, [data-session-model], pre[data-example='curl']")
    else:
        assert not soup.select(f'tr[data-model="{target}"]')
        assert soup.select("#models table") and soup.select("[data-session-model]")


def test_published_limits_are_used(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guides, "published_context_length", lambda model: 123456)
    monkeypatch.setattr(guides, "published_max_completion_tokens", lambda model, endpoints: 8)
    soup = page(client)
    assert all("123,456" in row.get_text() for row in soup.select("#models tr[data-model]"))
    assert '"max_tokens":8' in soup.select_one('pre[data-example="curl"]').get_text()


def test_context_reuses_revision_and_expires(client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                            controlled_claude_catalog: dict[str, ModelEndpoint]) -> None:
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
    now = deadline - timedelta(seconds=1)
    revision = (1, "fixture")
    monkeypatch.setattr(guides.provider_lifecycle, "provider_catalog_revision", lambda: revision)
    monkeypatch.setattr(guides.catalog_data, "_utc_now", lambda: now)
    for key, endpoint in list(controlled_claude_catalog.items()):
        controlled_claude_catalog[key] = replace(endpoint, catalog_valid_until=deadline)
    assert page(client).select("#models table")
    assert page(client).select("#models table")
    assert guides._claude_code_context.cache_info().misses == 1
    assert guides._claude_code_context.cache_info().hits == 1
    revision = (2, "updated")
    page(client)
    assert guides._claude_code_context.cache_info().misses == 2
    now = deadline
    assert not page(client).select("#models table, [data-session-model], pre[data-example='curl']")
    assert guides._claude_code_context.cache_info().misses == 3


def test_copy_and_links(client: TestClient) -> None:
    soup = page(client)
    text = soup.get_text(" ", strip=True).lower()
    for phrase in ("card fee", "card processing", "payment processing", "payment-processing",
                   "staff cannot read", "repository", "render time", "manifest"):
        assert phrase not in text
    assert not re.search(r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|tokens/s)\b", text)
    assert "5.5% fee" in text
    assert soup.h1.get_text() == "Use Claude Code through TrustedRouter"
    for link in ("/docs/opencode", "/docs/spend-controls", "/docs/prompt-caching", "/who-can-see-your-data"):
        assert soup.select_one(f'.content-document a[href="{link}"]')
    assert [h.get_text() for h in soup.select(".content-document h2")] == [
        "Set it up", "Which Claude models", "What a session costs", "Limits",
    ]
    limits = soup.select_one("#limits").get_text(" ", strip=True)
    assert "web search, are not passed through on this path yet" in limits
    assert "api.trustedrouter.com" in limits
    assert "budget_strict: true" in limits and "budget_alert_only: false" in limits
    assert "cache_control" in limits and "/v1/messages" in limits
    assert f'href="{PATH}"' in client.get("/docs").text
    setup_page = BeautifulSoup(client.get("/docs/agent-setup").text, "html.parser")
    note = next(p for p in setup_page.select("p") if "default model IDs" in p.get_text())
    assert "are accepted as sent" in note.get_text() and "pinned" not in note.get_text()
    assert "anthropic/" in note.get_text() and note.select_one(f'a[href="{PATH}"]')
    for path in ("/docs/agent-setup", "/pricing", "/docs/opencode", "/docs/cut-llm-api-costs"):
        other = BeautifulSoup(client.get(path).text, "html.parser")
        for footer in other.select("footer"):
            footer.decompose()
        assert other.select_one(f'a[href="{PATH}"]'), path
    assert f"<loc>https://trustedrouter.com{PATH}</loc>" in client.get("/sitemap-core.xml").text
    for path in ("/llms.txt", "/llms-full.txt", "/docs/llms.txt", "/docs/llms-full.txt"):
        assert PATH in client.get(path).text
    graphs = [json.loads(script.string) for script in soup.select('script[type="application/ld+json"]')]
    faq = next(node for graph in graphs for node in graph["@graph"] if node["@type"] == "FAQPage")
    assert len(faq["mainEntity"]) == 4
    answers = [item["acceptedAnswer"]["text"] for item in faq["mainEntity"]]
    assert all("ANTHROPIC_" in answer for answer in answers)
    assert "ANTHROPIC_BASE_URL=https://api.trustedrouter.com without /v1" in answers[1]
    assert all("anthropic/ prefix" in answer and "ANTHROPIC_MODEL" in answer for answer in answers[:2])
    assert all(variable in answers[0] for _, variable in guides.CLAUDE_CODE_ALIAS_VARIABLES)
    assert "default model IDs are accepted as sent" in answers[0] and "pin the" not in answers[0]
    assert "No model variable is required" in answers[1] and "pin the" not in answers[1]
    assert [variable for _, variable in guides.CLAUDE_CODE_ALIAS_VARIABLES] == [
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
    ]
    assert not any("trustedrouter.com/v1" in answer for answer in answers)
