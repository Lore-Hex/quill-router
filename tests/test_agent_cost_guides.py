"""The agent and cost guides use executable catalog and request contracts."""
from __future__ import annotations

import ast
import json
import re
import runpy
import sys
import warnings
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, ValidationError

from tests.test_catalog_count_claims import invalid_claims
from trusted_router import agent_cost_guides as guides
from trusted_router.agent_cost_guides import COST_GUIDE_ROUTES, DEEPSEEK_MODEL
from trusted_router.catalog import PROVIDERS, Model, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.dashboard import PUBLIC_PAGES, public_page_html
from trusted_router.gateway_processing import gateway_processing_claim
from trusted_router.pricing import (
    STANDARD_PRICE_MARKUP_BASIS_POINTS,
    PriceTier,
    resolve_request_rates,
)
from trusted_router.routes.inference import _validate_chat_messages
from trusted_router.routing import _requested_model_ids, provider_route_preferences
from trusted_router.schemas import CreateKeyRequest, GatewayAuthorizeRequest

PAGES = ("/docs/opencode", "/llm-gateway-pricing", "/docs/opencode-deepseek", "/docs/cut-llm-api-costs")
ROOT = Path(__file__).resolve().parents[1]
GATEWAY_FEE_SCOPE = (
    "These amounts compare gateway fees on fully spent credits; taxes, payment processing "
    "and optional add-ons are outside this comparison."
)


@pytest.mark.parametrize("path", PAGES)
def test_guide_contexts_rebuild_only_when_revision_changes(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch, fixed_catalog: None, path: str,
) -> None:
    revision = (1, "first")
    monkeypatch.setattr(guides.provider_lifecycle, "provider_catalog_revision", lambda: revision)
    caches = [guides._gateway_cost_context]
    if path in (PAGES[0], PAGES[2]):
        caches.append(guides._opencode_context)
    for cache in caches:
        assert cache.cache_info().misses == 0
    public_page_html(test_settings, path.lstrip("/"))
    for cache in caches:
        assert cache.cache_info().misses == 1
    public_page_html(test_settings, path.lstrip("/"))
    for cache in caches:
        assert cache.cache_info().misses == 1
        assert cache.cache_info().hits == 1
    revision = (2, "second")
    public_page_html(test_settings, path.lstrip("/"))
    for cache in caches:
        assert cache.cache_info().misses == 2


def test_rendering_preserves_cached_guide_contexts(
    test_settings: Settings, fixed_catalog: None,
) -> None:
    revision = guides._context_revision()
    contexts = (guides._opencode_context(revision), guides._gateway_cost_context(revision))
    before = deepcopy(contexts)
    for base_url in ("https://first.example/v1", "https://second.example/v1"):
        settings = test_settings.model_copy(update={"api_base_url": base_url})
        for path in PAGES:
            html = public_page_html(settings, path.lstrip("/"))
            if path == PAGES[0]:
                soup = BeautifulSoup(html, "html.parser")
                configs = [json.loads(block.get_text()) for block in soup.select("pre[data-config]")]
                v2 = [c["providers"]["trustedrouter"]["settings"]["baseURL"] for c in configs
                      if "settings" in c.get("providers", {}).get("trustedrouter", {})]
                v1 = [c["provider"]["trustedrouter"]["options"]["baseURL"] for c in configs
                      if "options" in c.get("provider", {}).get("trustedrouter", {})]
                assert v2 == [base_url]
                assert v1 == [base_url]
            assert contexts == before


def test_guide_contexts_expire_between_lifecycle_revisions(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch, fixed_catalog: None,
) -> None:
    deadline = datetime(2026, 11, 24, 12, 0, 1, tzinfo=UTC)
    now = deadline - timedelta(seconds=1)
    monkeypatch.setattr(guides.catalog_data, "_utc_now", lambda: now)
    monkeypatch.setattr(guides.provider_lifecycle, "_utc_now", lambda: now)
    revision = guides.provider_lifecycle.provider_catalog_revision()
    small_id = COST_GUIDE_ROUTES[1][0]
    endpoints = {
        e.id: replace(e, catalog_valid_until=deadline)
        if model_id in (small_id, DEEPSEEK_MODEL) else e
        for model_id in guides.MODELS for e in guides.endpoints_for_model(model_id)
    }
    monkeypatch.setattr(guides, "MODEL_ENDPOINTS", endpoints)
    monkeypatch.setattr(guides, "endpoints_for_model", lambda model_id: [
        e for e in endpoints.values() if e.model_id == model_id and e.catalog_is_current()
    ])
    for expired in (False, True):
        if expired:
            now = deadline
        assert guides.provider_lifecycle.provider_catalog_revision() == revision
        for path in PAGES:
            soup = BeautifulSoup(public_page_html(test_settings, path.lstrip("/")), "html.parser")
            if path in (PAGES[0], PAGES[2]):
                configs = [json.loads(block.get_text()) for block in soup.select("pre[data-config]")]
                assert any(DEEPSEEK_MODEL in config["model"] for config in configs) is not expired
            if path == PAGES[1]:
                assert bool(soup.select(f'tr[data-model="{small_id}"]')) is not expired
            if path != PAGES[0]:
                assert bool(soup.select("[data-route-count]")) is not expired
        for cache in (guides._opencode_context, guides._gateway_cost_context):
            assert cache.cache_info().misses == 1 + int(expired)
    assert guides._catalog_expiries.cache_info().misses == 1


@pytest.mark.parametrize("path", (*PAGES, "/docs/claude-code"))
def test_guides_render_and_are_discoverable(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert client.get(path + "/").status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.find("link", rel="canonical")["href"] == f"https://trustedrouter.com{path}"
    assert len(soup.find_all("h1")) == 1
    assert not invalid_claims(response.text)
    document = soup.select_one(".content-document").get_text(" ").lower()
    for term in ("rerank", "repository", "render time", "packaged catalog"):
        assert term not in document
    assert f"<loc>https://trustedrouter.com{path}</loc>" in client.get("/sitemap-core.xml").text
    for index in ("/llms.txt", "/llms-full.txt", "/docs/llms.txt", "/docs/llms-full.txt"):
        assert path in client.get(index).text
    if path != "/docs/claude-code":
        assert soup.find("time", datetime="2026-11-24")
    graphs = [json.loads(script.string) for script in soup.select('script[type="application/ld+json"]')]
    faqs = [node for graph in graphs for node in graph["@graph"] if node["@type"] == "FAQPage"]
    assert faqs and len(faqs[0]["mainEntity"]) > 1
    assert f'href="{path}"' in client.get("/docs/agent-setup").text
    assert f'href="{path}"' in client.get("/pricing").text
    for table in soup.select(".content-document table"):
        assert len(table.select("thead th")) <= 5
        assert "reference-mobile-table" in table["class"]
        assert "reference-column-table" in table["class"]
        assert all(td.select_one(".reference-cell-label") for td in table.select("tbody td"))
    prose = BeautifulSoup(str(soup.select_one(".content-document")), "html.parser")
    for code in prose.select("code"):
        code.decompose()
    assert not re.search(r"\b(?:openai|deepinfra|engy|io-net)\b", prose.get_text())
    assert not re.search(r"\b(?:Anthropic|OpenAI|DeepSeek): ", prose.get_text())


def _validate_chat(body: dict[str, Any]) -> None:
    _validate_chat_messages(body)
    metadata = {key: value for key, value in body.items() if key != "messages"}
    assert metadata.keys() <= GatewayAuthorizeRequest.model_fields.keys()
    GatewayAuthorizeRequest.model_validate({**metadata, "api_key_hash": "guide-test-key"})
    provider_route_preferences(body)
    for model_id in [body["model"], *body.get("models", [])]:
        assert model_id in guides.MODELS


@pytest.mark.parametrize("path", [PAGES[0], PAGES[2]])
def test_opencode_configs_and_catalog_limits(client: TestClient, path: str) -> None:
    soup = BeautifulSoup(client.get(path).text, "html.parser")
    catalog = {model["id"]: model for model in client.get("/v1/models").json()["data"]}
    schema = json.loads((ROOT / "tests/fixtures/opencode-provider-config.schema.json").read_text())
    validator = Draft202012Validator(schema)
    configs = soup.select("pre[data-config]")
    assert len([pre for pre in soup.select("pre") if pre.get_text().lstrip().startswith("{")]) == len(configs)
    for block in configs:
        config = json.loads(block.get_text())
        validator.validate(config)
        assert config.keys() <= {"$schema", "model", "providers", "provider"}
        assert config["$schema"] == "https://opencode.ai/config.json"
        assert re.fullmatch(r"[^/#]+/[^#]+(?:#[^#]+)?", config["model"])
        provider_id, model_id = config["model"].split("/", 1)
        assert provider_id in {"trustedrouter", "openrouter"}
        assert model_id in guides.MODELS
        key = "provider" if block["data-config"] == "v1" else "providers"
        assert ("providers" if key == "provider" else "provider") not in config
        label = block.find_previous("h3").get_text()
        assert ("1.x" if key == "provider" else "V2") in label
        if key not in config:
            assert model_id == DEEPSEEK_MODEL
            continue
        provider = config[key][provider_id]
        if "package" in provider:
            assert key == "providers"
            assert provider.keys() == {"name", "env", "package", "settings", "models"}
            assert provider["env"] == ["TRUSTEDROUTER_API_KEY"]
            assert provider["package"] == "@opencode/ai/providers/openai-compatible"
            assert provider["settings"].keys() == {"baseURL"}
        elif "npm" in provider:
            assert key == "provider"
            assert provider.keys() == {"name", "npm", "options", "models"}
            assert provider["npm"] == "@ai-sdk/openai-compatible"
            assert provider["options"].keys() == {"baseURL", "apiKey"}
            assert provider["options"]["apiKey"] == "{env:TRUSTEDROUTER_API_KEY}"
        else:
            assert provider.keys() == {"models"}
        for model_id, entry in provider["models"].items():
            assert model_id in guides.MODELS
            if model_id.startswith("trustedrouter/"):
                assert entry.keys() == {"limit"}
                assert sum(f'"{model_id}"' in line for line in block.get_text().splitlines()) == 1
                line = next(line for line in block.get_text().splitlines() if f'"{model_id}"' in line)
                assert json.loads("{" + line.strip().rstrip(",") + "}") == {model_id: entry}
            else:
                assert entry.keys() == {"name", "limit"}
                assert entry["name"] == guides.MODELS[model_id].name.split(": ", 1)[-1]
            served = catalog[model_id]
            expected = {"context": served["context_length"]}
            if served["top_provider"]["max_completion_tokens"] is not None:
                expected["output"] = served["top_provider"]["max_completion_tokens"]
            if provider_id == "openrouter":
                expected = {"context": 1048576, "output": 393216}
            assert entry["limit"] == expected
            assert f'{expected["context"]:,}' in soup.get_text()
    if path == PAGES[0]:
        aliases = soup.select("tr[data-alias]")
        for row in aliases:
            assert f'{catalog[row["data-alias"]]["context_length"]:,}' in row.get_text()
        for smoke in soup.select('pre[data-example="curl"]'):
            match = re.search(r"-d '(.*?)'", smoke.get_text(), re.S)
            assert match
            _validate_chat(json.loads(match.group(1)))


@pytest.mark.parametrize("path", PAGES)
def test_guide_descriptions_are_complete(client: TestClient, path: str) -> None:
    expected = PUBLIC_PAGES[path.lstrip("/")].description
    assert len(expected) <= 150
    assert expected.endswith(".") and not expected.endswith(("...", "…"))
    if path == PAGES[1]:
        assert expected == (
            "OpenRouter and Cloudflare charge 5% to 8% on credits; Vercel has no gateway fee; "
            "TrustedRouter adds 5.5% to tokens with no monthly or seat fee."
        )
    soup = BeautifulSoup(client.get(path).text, "html.parser")
    descriptions = [soup.select_one(selector)["content"] for selector in (
        'meta[name="description"]', 'meta[property="og:description"]', 'meta[name="twitter:description"]',
    )]
    graphs = [json.loads(script.string) for script in soup.select('script[type="application/ld+json"]')]
    page_descriptions = [node["description"] for graph in graphs for node in graph["@graph"]
                         if node["@type"] == "WebPage"]
    if path == PAGES[1]:
        assert page_descriptions == [expected]
    for description in descriptions + page_descriptions:
        assert description == expected  # Detect truncation even without a trailing ellipsis.
        assert not description.endswith(("...", "…"))


@pytest.mark.parametrize("removed", [(), ("zdr",), ("fast",), ("fast", "synth"),
                                    ("fast", "synth", "synth-code"), tuple(guides.ALIAS_POLICIES)])
def test_opencode_alias_copy_follows_available_lists(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fixed_catalog: None, removed: tuple[str, ...],
) -> None:
    monkeypatch.setattr(guides, "MODELS", {key: value for key, value in guides.MODELS.items()
                                         if key not in {f"trustedrouter/{alias}" for alias in removed}})
    soup = BeautifulSoup(client.get(PAGES[0]).text, "html.parser")
    step = soup.select("#quick-start ol li")[2]
    if "zdr" in removed:
        assert step.get_text() == "Run /models and choose a model from the catalog."
        assert step.select_one('a[href="/models"]')
    else:
        assert step.get_text() == "Run /models and choose trustedrouter/trustedrouter/zdr."
        assert [code.get_text() for code in step.select("code")] == ["/models", "trustedrouter/trustedrouter/zdr"]
    section = soup.select_one("#models")
    assert section.select_one("p").get_text() == (
        "TrustedRouter appears in OpenCode's catalog with seven built-in aliases; "
        "any other catalog model is added with a few lines of config."
    )
    expected = [alias for alias in ("fast", "synth", "synth-code") if alias not in removed]
    paragraphs = [p for p in section.select("p") if "lower served limits" in p.get_text()]
    assert len(paragraphs) == bool(expected)
    if expected:
        paragraph = paragraphs[0]
        names = expected[0] if len(expected) == 1 else ", ".join(expected[:-1]) + " and " + expected[-1]
        verbs = "has lower served limits and needs" if len(expected) == 1 else "have lower served limits and need"
        assert f"{names} {verbs} the override below; the others are at or above the catalog figure and need none." in paragraph.get_text()
        assert [code.get_text() for code in paragraph.select("code")][:len(expected)] == expected
        config = json.loads(section.select_one("pre[data-config]").get_text())
        overrides = config["providers"]["trustedrouter"]["models"]
        assert list(overrides) == [f"trustedrouter/{alias}" for alias in expected]
        assert set(overrides) <= {row["data-alias"] for row in section.select("tr[data-alias]")}
    else:
        assert not section.select("pre[data-config]")


@pytest.mark.parametrize("missing_model", [False, True])
def test_deepseek_openrouter_selector_is_independent_of_our_catalog(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fixed_catalog: None, missing_model: bool,
) -> None:
    if missing_model:
        monkeypatch.setattr(guides, "MODELS", {})
    soup = BeautifulSoup(client.get(PAGES[2]).text, "html.parser")
    paragraph = soup.select_one("#openrouter p")
    assert paragraph.get_text() == (
        "Run /connect, choose OpenRouter, and enter an OpenRouter key; "
        "then select openrouter/deepseek/deepseek-v4.1-flash with /models."
    )
    assert [code.get_text() for code in paragraph.select("code")] == [
        "/connect", "openrouter/deepseek/deepseek-v4.1-flash", "/models",
    ]
    assert "Inference base URL" not in soup.select_one(".content-document > p").get_text()


@pytest.mark.parametrize("path", PAGES)
def test_guides_use_configured_api_base(test_settings: Settings, path: str) -> None:
    settings = test_settings.model_copy(update={"api_base_url": "https://regional.example/v1"})
    soup = BeautifulSoup(public_page_html(settings, path.lstrip("/")), "html.parser")
    document = soup.select_one(".content-document").get_text(" ")
    assert settings.api_base_url in document
    assert "https://api.trustedrouter.com" not in document
    if path == PAGES[2]:
        route = next(row for row in soup.select(".content-document > .content-reference tbody tr")
                     if "TRUSTEDROUTER_API_KEY" in row.get_text())
        assert settings.api_base_url in [code.get_text() for code in route.select("td:last-child code")]
        assert settings.api_base_url not in soup.select_one(".content-document > p").get_text()
    for pre in soup.select("pre[data-config]"):
        config = json.loads(pre.get_text())
        for group, options in (("providers", "settings"), ("provider", "options")):
            for provider in config.get(group, {}).values():
                if options in provider:
                    assert provider[options]["baseURL"] == settings.api_base_url
    for smoke in soup.select('pre[data-example="curl"]'):
        assert f"curl {settings.api_base_url}/chat/completions" in smoke.get_text()


def test_cost_requests_use_production_contracts(client: TestClient, test_settings: Settings) -> None:
    soup = BeautifulSoup(client.get(PAGES[3]).text, "html.parser")
    examples = soup.select("pre[data-request]")
    assert not BeautifulSoup(client.get(PAGES[1]).text, "html.parser").select("pre[data-request]")
    for pre in examples:
        body = json.loads(pre.get_text())
        if pre["data-request"] == "key":
            assert body.keys() <= CreateKeyRequest.model_fields.keys()
            key = CreateKeyRequest.model_validate(body)
            assert key.budget_strict and not key.budget_alert_only
        else:
            if pre["data-request"] == "batch-item":
                body = {"model": pre["data-model"], **body}
            _validate_chat(body)
            if "models" in body:
                candidates, _ = _requested_model_ids(body, test_settings)
                assert candidates == [body["model"], *body["models"]]
    assert "ordinary fallback at normal prices" in soup.get_text()
    assert "not an absolute final-charge ceiling" in soup.get_text()


def test_python_samples_compile_and_use_catalog_models(client: TestClient) -> None:
    soup = BeautifulSoup(client.get(PAGES[3]).text, "html.parser")
    samples = soup.select("pre[data-python]")
    parsed = {pre["data-python"]: ast.parse(pre.get_text()) for pre in samples}
    for name in parsed.keys() & {"before", "after"}:
        calls = [node for node in ast.walk(parsed[name]) if isinstance(node, ast.Call)]
        client_call = next(node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "OpenAI")
        client_args = {arg.arg: arg.value for arg in client_call.keywords}
        assert set(client_args) == ({"api_key"} if name == "before" else {"base_url", "api_key"})
        assert ast.unparse(client_args["api_key"]) == (
            "os.environ['OPENAI_API_KEY']" if name == "before" else "os.environ['TRUSTEDROUTER_API_KEY']"
        )
        model = next(arg.value for call in calls for arg in call.keywords if arg.arg == "model")
        assert ast.literal_eval(model) == "gpt-5.4-mini"
    migration = soup.select_one("#migration")
    if soup.select_one('pre[data-python="split"]'):
        assert "Second, change" in migration.get_text()
        assert "openai/gpt-6-luna" in [code.get_text() for code in migration.select("p code")]
    for pre in samples:
        source = pre.get_text()
        compile(source, pre["data-python"], "exec")
        tree = ast.parse(source)
        constants = [node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        model_ids = ["openai/" + value if value == "gpt-5.4-mini" else value for value in constants
                     if value == "gpt-5.4-mini" or value.startswith(("openai/", "anthropic/"))]
        assert model_ids and all(model_id in guides.MODELS for model_id in model_ids)
        messages = next(ast.literal_eval(node) for node in ast.walk(tree)
                        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Dict))
        for model_id in model_ids:
            _validate_chat({"model": model_id, "messages": messages})


def _decimal(row: Any, field: str) -> Decimal:
    if row.has_attr(f"data-{field}"):
        return Decimal(row[f"data-{field}"])
    node = BeautifulSoup(str(row.select_one(f'[data-value="{field}"]')), "html.parser")
    for label in node.select(".reference-cell-label"):
        label.decompose()
    return Decimal(node.get_text().strip(" $").replace(",", ""))


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _shown_price_date(times: list[Any], stamp: str, stamps: set[str]) -> Any:
    """The one ``<time>`` among ``times`` that shows a manifest stamp.

    The page groups the stamps by the minute it displays and gives a group
    its earliest stamp. One refresh writes the provider manifests a second or
    so apart, so a manifest's own stamp is the element's ``datetime`` only
    when no other manifest in ``stamps`` was written earlier in that minute.
    """
    def label(value: str) -> str:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%B %-d, %Y, %H:%M UTC")

    assert stamp in stamps
    shown = [node for node in times if node.get_text() == label(stamp)]
    assert len(shown) == 1
    assert shown[0]["datetime"] == min(other for other in stamps if label(other) == label(stamp))
    return shown[0]


def _shown(soup: Any, name: str) -> Decimal:
    node = soup.select_one(f"[data-{name}]")
    if name in {
        "halving-percent", "break-even-percent", "saving-percent", "escalation-percent",
        "route-saving-percent", "cache-saving-percent",
    }:
        exact = Decimal(node[f"data-{name}"])
        places = {"halving-percent": 0, "route-saving-percent": 0, "break-even-percent": 1}.get(name, 2)
        display = format(exact, f".{places}f")
        if "." in display:
            display = display.rstrip("0").rstrip(".")
        assert node.get_text() == display + "%"
        return exact
    return Decimal(node.get(f"data-{name}") or node.get_text().strip("$% ").replace(",", ""))


def _rates(endpoint: Any, inputs: int = 4000) -> Any:
    return resolve_request_rates(
        endpoint.price_tiers,
        headline_prompt_micro_per_m=endpoint.prompt_price_microdollars_per_million_tokens,
        headline_completion_micro_per_m=endpoint.completion_price_microdollars_per_million_tokens,
        total_prompt_tokens=inputs,
    )


def test_worked_costs_match_catalog_and_rounding(client: TestClient) -> None:
    soup = BeautifulSoup(client.get(PAGES[1]).text, "html.parser")
    savings = BeautifulSoup(client.get(PAGES[3]).text, "html.parser")
    workload = soup.select_one("#workload")
    count, inputs, outputs = (int(workload[f"data-{key}"]) for key in ("requests", "input", "output"))
    assert (count, inputs, outputs) == (10_000, 4_000, 1_000)
    fee = _shown(soup, "fee-percent") / 100
    assert fee == Decimal(STANDARD_PRICE_MARKUP_BASIS_POINTS) / 10_000
    rows = soup.select("#cost-example tbody tr")
    assert [(row["data-model"], row["data-provider"]) for row in rows] == [
        pair for pair in COST_GUIDE_ROUTES if any(row["data-model"] == pair[0] for row in rows)
    ]
    totals = {}
    provider_totals = {}
    stamps = {guides._manifest(row["data-provider"])["generated_at"] for row in rows}
    for row in rows:
        model_id, provider = row["data-model"], row["data-provider"]
        manifest = guides._manifest(provider)
        source = next(model for model in manifest["models"] if model["id"] == model_id)
        source = next((tier for tier in source.get("price_tiers", [])
                       if tier["max_prompt_tokens"] is None or inputs <= tier["max_prompt_tokens"]), source)
        assert row["data-as-of"] == manifest["generated_at"]
        _shown_price_date(soup.select("[data-price-dates] time"), manifest["generated_at"], stamps)
        assert row.select_one("a").get_text() == guides.MODELS[model_id].name.split(": ", 1)[-1]
        assert f"on {PROVIDERS[provider].name}" in row.get_text()
        assert _decimal(row, "provider-input") == Decimal(source["input_token_price_per_m"]) / 1_000_000
        assert _decimal(row, "provider-output") == Decimal(source["output_token_price_per_m"]) / 1_000_000
        endpoint = next(e for e in guides.endpoints_for_model(model_id)
                        if e.provider == provider and e.usage_type == "Credits")
        assert endpoint.request_price_microdollars == 0
        assert guides.MODELS[model_id].minimum_charge_microdollars == 0
        rates = _rates(endpoint, inputs)
        assert _decimal(row, "retail-input") == Decimal(rates.prompt_price_microdollars_per_million_tokens) / 1_000_000
        assert _decimal(row, "retail-output") == Decimal(rates.completion_price_microdollars_per_million_tokens) / 1_000_000
        provider_cost = count * (inputs * _decimal(row, "provider-input")
                                 + outputs * _decimal(row, "provider-output")) / 1_000_000
        fee_cost = provider_cost * fee
        per_request = sum((tokens * _decimal(row, field)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                          for tokens, field in ((inputs, "retail-input"), (outputs, "retail-output")))
        total = count * max(per_request, Decimal(1)) / 1_000_000
        assert _decimal(row, "provider-cost") == _cents(provider_cost)
        assert _decimal(row, "fee") == _cents(fee_cost)
        assert _decimal(row, "rounding") == _cents(total - provider_cost - fee_cost)
        assert _decimal(row, "total") == _cents(total)
        totals[model_id] = total
        provider_totals[model_id] = provider_cost
    for page, name in ((savings, "mixed-total"), (savings, "baseline"), (savings, "saving"),
                       (savings, "saving-percent"), (savings, "halving-percent"),
                       (soup, "break-even-percent"), (savings, "escalation-percent"),
                       (savings, "escalation-total")):
        for _ in page.select(f"[data-{name}]"):
            small, mid = (totals[COST_GUIDE_ROUTES[i][0]] for i in (1, 2))
            mixed = Decimal("0.8") * small + Decimal("0.2") * mid
            expected = {
                "mixed-total": _cents(mixed), "baseline": _cents(mid),
                "saving": _cents(mid - mixed), "escalation-percent": Decimal(20),
                "escalation-total": _cents(small + Decimal("0.2") * mid),
            }
            if name == "saving-percent":
                expected[name] = (mid - mixed) / mid * 100
            elif name == "halving-percent":
                expected[name] = mid / (2 * (mid - small)) * 100
            elif name == "break-even-percent":
                expected[name] = (mid - provider_totals[COST_GUIDE_ROUTES[2][0]]) / (mid - small) * 100
            assert _shown(page, name) == expected[name]
    assert _shown(soup, "fee-per-hundred") == _cents(100 * fee)
    assert _shown(soup, "hundred-total") == _cents(100 * (1 + fee))
    for number in ("10,000", "4,000", "1,000"):
        assert number in workload.get_text()
    for caption in soup.select("[data-price-dates]"):
        assert len(caption.select("a")) == 1
    scale_rows = soup.select("#fee-scale tbody tr")
    assert [_decimal(row, "inference") for row in scale_rows] == [1000, 10000, 100000]
    for row in scale_rows:
        inference = _decimal(row, "inference")
        assert _decimal(row, "fee") == _cents(inference * fee)
        assert _decimal(row, "total") == _cents(inference * (1 + fee))
        for field in ("inference", "fee", "total"):
            assert f"${_decimal(row, field):,.2f}" in row.select_one(f'[data-value="{field}"]').get_text()


def test_route_prices_and_cache_savings_follow_catalog(client: TestClient) -> None:
    endpoints = [e for e in guides.endpoints_for_model(DEEPSEEK_MODEL)
                 if e.usage_type == "Credits" and e.provider in PROVIDERS
                 and guides._source(guides._manifest(e.provider), DEEPSEEK_MODEL) is not None]
    amounts = []
    for endpoint in endpoints:
        rates = _rates(endpoint)
        i = Decimal(rates.prompt_price_microdollars_per_million_tokens) / 1_000_000
        o = Decimal(rates.completion_price_microdollars_per_million_tokens) / 1_000_000
        amounts.append((i + o, endpoint.provider, i, o))
    amounts.sort()
    for path in PAGES[1:]:
        soup = BeautifulSoup(client.get(path).text, "html.parser")
        for block in soup.select("[data-route-count]"):
            low, high = amounts[0], amounts[-1]
            stamps = {guides._manifest(route[1])["generated_at"] for route in (low, high)}
            assert int(block["data-route-count"]) == len(endpoints)
            for which, route in (("low", low), ("high", high)):
                assert _shown(soup, f"route-{which}-input") == route[2]
                assert _shown(soup, f"route-{which}-output") == route[3]
                assert block[f"data-{which}-provider"] == route[1]
                assert PROVIDERS[route[1]].name in block.get_text()
                _shown_price_date(block.select("time"), guides._manifest(route[1])["generated_at"], stamps)
    savings = BeautifulSoup(client.get(PAGES[3]).text, "html.parser")
    for percent in savings.select("[data-route-saving-percent]"):
        lever = percent.find_parent("tr")
        assert "Up to " + percent.get_text() in lever.get_text()
        assert "between the highest and lowest priced route for this model today" in lever.get_text()
        assert "Your saving is against the route you use now" in lever.get_text()
        low, high = amounts[0], amounts[-1]
        weighted_low = 4000 * low[2] + 1000 * low[3]
        weighted_high = 4000 * high[2] + 1000 * high[3]
        assert _shown(savings, "route-saving-percent") == (1 - weighted_low / weighted_high) * 100
    for _ in savings.select("[data-cache-saving-percent]"):
        model, provider = COST_GUIDE_ROUTES[1]
        endpoint = next(e for e in guides.endpoints_for_model(model) if e.provider == provider and e.usage_type == "Credits")
        tier = next(t for t in endpoint.price_tiers if t.max_prompt_tokens is None or 4000 <= t.max_prompt_tokens)
        read = Decimal(tier.prompt_cached_price_microdollars_per_million_tokens) / 1_000_000
        prompt = Decimal(_rates(endpoint).prompt_price_microdollars_per_million_tokens) / 1_000_000
        assert _shown(savings, "cache-read") == read
        assert _shown(savings, "cache-input") == prompt
        assert _shown(savings, "cache-saving-percent") == (1 - read / prompt) * 100


def test_vendor_comparisons_have_sources_and_date(client: TestClient) -> None:
    soup = BeautifulSoup(client.get(PAGES[1]).text, "html.parser")
    assert soup.find("time", datetime="2026-11-24").get_text() == "November 24, 2026"
    for table_id in ("gateway-fees", "gateway-cost-visibility"):
        rows = soup.select(f"#{table_id} tbody tr")
        assert len(rows) == 6
        for row in rows:
            assert row.select_one('td:last-child a[href]')
    assert "markup" in soup.select_one("#gateway-fees").get_text()
    lede = soup.select_one(".content-document > p").get_text()
    assert "The hosted gateways compared here add no markup to token prices" in lede
    assert "OpenRouter and Cloudflare charge 5% to 8% when you buy credits" in lede
    assert "Vercel charges no gateway fee" in lede
    assert "TrustedRouter instead includes a 5.5% markup in each token price and has no monthly or seat fee" in lede
    hundred = soup.select_one("#hundred")
    assert hundred.select_one("h2").get_text() == "What $100 of inference costs through each gateway"
    items = hundred.select("li")
    assert items[0].get_text() == "TrustedRouter: $105.50."
    assert items[1].get_text().startswith("OpenRouter Standard: $105.50")
    assert items[3].get_text() == "Vercel AI Gateway: $100.00."
    assert (
        "TrustedRouter and OpenRouter Standard both come to $105.50 in gateway fee terms; "
        "OpenRouter takes its 5.5% when credits are bought (with a $0.80 minimum), "
        "while TrustedRouter includes its 5.5% in each token price."
    ) in hundred.get_text()
    fee_rows = soup.select("#gateway-fees tbody tr")
    assert "No monthly or seat fee." in fee_rows[0].get_text()
    assert "0%; no platform fee on tokens" in fee_rows[2].get_text()
    assert "Add-ons metered separately." in fee_rows[2].get_text()
    cheapest = soup.select_one("#cheapest").get_text()
    graphs = [json.loads(script.string) for script in soup.select('script[type="application/ld+json"]')]
    faq = next(node for graph in graphs for node in graph["@graph"] if node["@type"] == "FAQPage")
    answer = next(item["acceptedAnswer"]["text"] for item in faq["mainEntity"]
                  if item["name"] == "What is the cheapest OpenAI-compatible LLM gateway?")
    assert (
        "Among the hosted options with published totals, $100.00 of inference costs "
        "$100.00 through Vercel AI Gateway, $105.00 through Cloudflare Unified Billing "
        "or OpenRouter crypto, $105.50 through TrustedRouter or OpenRouter Standard, "
        "and $108.00 through OpenRouter Business."
    ) in cheapest
    assert (
        "For $100.00 of inference in gateway fee terms on fully spent credits, Vercel "
        "costs $100.00, Cloudflare Unified Billing and OpenRouter crypto $105.00, "
        "TrustedRouter and OpenRouter Standard $105.50, and OpenRouter Business $108.00."
    ) in answer
    for text in (cheapest, answer):
        assert "Self-hosted LiteLLM has no gateway fee" in text
    usage = json.loads(soup.select_one("pre[data-usage]").get_text())["usage"]
    assert usage["cost_microdollars"] == usage["total_cost_microdollars"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_price_dates_show_only_different_providers(
    monkeypatch: pytest.MonkeyPatch, test_settings: Settings, fixed_catalog: None,
) -> None:
    from trusted_router import agent_cost_guides

    original_manifest = agent_cost_guides._manifest
    high_provider = "io-net"  # Highest price in fixed_catalog; do not build before patching.
    common_stamp = "2026-11-24T17:28:45Z"
    different_stamp = "2026-11-23T16:15:30Z"

    def dated_manifest(provider: str) -> dict[str, Any]:
        manifest = original_manifest(provider)
        manifest["generated_at"] = (
            different_stamp if provider in {"deepinfra", high_provider} else common_stamp
        )
        return manifest

    monkeypatch.setattr(agent_cost_guides, "_manifest", dated_manifest)
    soup = BeautifulSoup(public_page_html(test_settings, PAGES[1].lstrip("/")), "html.parser")
    for selector, provider in (("[data-price-dates]", "deepinfra"), ("[data-route-count]", high_provider)):
        block = soup.select_one(selector)
        assert [node["datetime"] for node in block.select("time")] == [common_stamp, different_stamp]
        assert "Prices collected November 24, 2026, 17:28 UTC" in block.get_text()
        assert f"; {PROVIDERS[provider].name} prices collected November 23, 2026, 16:15 UTC" in block.get_text()
        assert len(block.select("a")) == 1
    caption = soup.select_one("[data-price-dates]").get_text()
    assert "OpenAI" not in caption
    assert "Anthropic" not in caption


def test_price_dates_group_by_the_minute_shown() -> None:
    """One refresh writes provider manifests a second or so apart (2026-11-25:
    11:28:23 and 11:28:24). Their labels agree, so the caption names no
    provider; only a different shown minute earns its own clause."""
    from trusted_router.agent_cost_guides import _price_dates

    rows = [
        {"as_of": "2026-11-25T11:28:24Z", "provider_name": "OpenAI"},
        {"as_of": "2026-11-25T11:28:23Z", "provider_name": "DeepInfra"},
        {"as_of": "2026-11-25T11:28:23Z", "provider_name": "Anthropic"},
    ]
    assert _price_dates(rows) == [{
        "timestamp": "2026-11-25T11:28:23Z",
        "label": "November 25, 2026, 11:28 UTC",
        "providers": ["OpenAI", "DeepInfra", "Anthropic"],
    }]
    rows[0]["as_of"] = "2026-11-25T11:29:01Z"
    dates = _price_dates(rows)
    assert [date["label"] for date in dates] == ["November 25, 2026, 11:28 UTC", "November 25, 2026, 11:29 UTC"]
    assert dates[1]["providers"] == ["OpenAI"]


@pytest.mark.parametrize("seconds_later", [1, 90])
def test_a_late_openai_manifest_never_reads_as_a_model_name(
    monkeypatch: pytest.MonkeyPatch, test_settings: Settings, fixed_catalog: None, seconds_later: int,
) -> None:
    """The caption's clause for a provider collected at another time must not
    read "OpenAI: ", which the guide's prose check reserves for model names.
    One second later shares the shown minute and adds no clause; 90 seconds
    later adds "OpenAI prices collected ..."."""
    from trusted_router import agent_cost_guides

    original_manifest = agent_cost_guides._manifest
    base = datetime(2026, 11, 25, 11, 28, 23, tzinfo=UTC)
    late = (base + timedelta(seconds=seconds_later)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def dated_manifest(provider: str) -> dict[str, Any]:
        manifest = original_manifest(provider)
        manifest["generated_at"] = late if provider == "openai" else base.strftime("%Y-%m-%dT%H:%M:%SZ")
        return manifest

    monkeypatch.setattr(agent_cost_guides, "_manifest", dated_manifest)
    soup = BeautifulSoup(public_page_html(test_settings, PAGES[1].lstrip("/")), "html.parser")
    caption = soup.select_one("[data-price-dates]").get_text()
    assert not re.search(r"\b(?:Anthropic|OpenAI|DeepSeek): ", caption)
    assert ("OpenAI prices collected" in caption) == (seconds_later >= 60)
    # What the catalog-following tests read: each manifest's stamp is shown
    # once, by its own element only when it does not share an earlier one's minute.
    times = soup.select("[data-price-dates] time")
    stamps = {base.strftime("%Y-%m-%dT%H:%M:%SZ"), late}
    assert len(times) == 1 + (seconds_later >= 60)
    for stamp in stamps:
        _shown_price_date(times, stamp, stamps)
    assert bool(soup.select(f'[data-price-dates] time[datetime="{late}"]')) == (seconds_later >= 60)


@pytest.mark.parametrize("path", [PAGES[0], PAGES[2]])
def test_rendered_limits_equal_models_response(client: TestClient, path: str) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 200
    catalog = {model["id"]: model for model in response.json()["data"]}
    soup = BeautifulSoup(client.get(path).text, "html.parser")
    for row in soup.select("tr[data-alias]"):
        context = row.select("td")[-1].get_text().split("tokens")[-2].strip()
        assert int(context.replace(",", "")) == catalog[row["data-alias"]]["context_length"]
    seen = set()
    for block in soup.select("pre[data-config]"):
        config = json.loads(block.get_text())
        for group in ("provider", "providers"):
            models = config.get(group, {}).get("trustedrouter", {}).get("models", {})
            for model_id, model in models.items():
                seen.add(model_id)
                served = catalog[model_id]
                assert model["limit"]["context"] == served["context_length"]
                output = served["top_provider"]["max_completion_tokens"]
                assert model["limit"].get("output") == output
                if model_id == DEEPSEEK_MODEL:
                    document = soup.select_one(".content-document").get_text()
                    assert f'{served["context_length"]:,} context tokens' in document
                    if output is not None:
                        assert f'{output:,} output tokens' in document
    if path == PAGES[0]:
        aliases = {row["data-alias"] for row in soup.select("tr[data-alias]")}
        assert aliases == {f"trustedrouter/{name}" for name in guides.ALIAS_POLICIES
                           if f"trustedrouter/{name}" in guides.MODELS}
        overrides = seen - {DEEPSEEK_MODEL}
        assert overrides == {model_id for model_id in aliases
                             if catalog[model_id]["context_length"] < 1_000_000}


def test_opencode_schema_requires_output_for_v1_limits() -> None:
    schema = json.loads((ROOT / "tests/fixtures/opencode-provider-config.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    model = {"models": {"trustedrouter/fast": {"limit": {"context": 131072}}}}
    validator.validate({"providers": {"trustedrouter": model}})
    with pytest.raises(ValidationError):
        validator.validate({"provider": {"trustedrouter": model}})


def test_python_allocation_and_ticket_validation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    soup = BeautifulSoup(client.get(PAGES[3]).text, "html.parser")
    called_models: list[str] = []
    replies: list[str | None] = []

    def create(*, model: str, messages: list[dict[str, str]]) -> SimpleNamespace:
        _validate_chat({"model": model, "messages": messages})
        called_models.append(model)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=replies.pop(0)),
        )])

    def fake_client(**kwargs: str) -> SimpleNamespace:
        assert kwargs["api_key"] == "guide-test-key"
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    openai = ModuleType("openai")
    monkeypatch.setattr(openai, "OpenAI", fake_client, raising=False)
    monkeypatch.setitem(sys.modules, "openai", openai)
    monkeypatch.setenv("TRUSTEDROUTER_API_KEY", "guide-test-key")
    sample = tmp_path / "sample.py"
    for block in soup.select('pre[data-python="split"]'):
        sample.write_text(block.get_text())
        replies.extend(["Summary"] * 10)
        runpy.run_path(str(sample))
        assert called_models == ["openai/gpt-6-luna"] * 8 + ["anthropic/claude-sonnet-5.5"] * 2
    for block in soup.select('pre[data-python="quality"]'):
        sample.write_text(block.get_text())
        good = '{"issue":"Checkout returns 500","priority":"urgent"}'
        for reply in (good, None, "not JSON", "[]", '{}', '{"issue":"Checkout returns 500"}',
                      '{"issue":"","priority":"urgent"}', '{"issue":"Checkout","priority":[]}'):
            called_models.clear()
            replies[:] = [reply, good] if reply != good else [good]
            runpy.run_path(str(sample))
            expected = ["openai/gpt-6-luna"]
            if reply != good:
                expected.append("anthropic/claude-sonnet-5.5")
            assert called_models == expected
            assert not replies


@pytest.mark.parametrize("path", PAGES)
def test_removed_guide_phrases_stay_absent(client: TestClient, path: str) -> None:
    soup = BeautifulSoup(client.get(path).text, "html.parser")
    text = soup.get_text(" ").lower()
    scope = GATEWAY_FEE_SCOPE.lower()
    if path == PAGES[1]:
        assert text.count(scope) == 2
        assert soup.select_one("#hundred > p").get_text().count(GATEWAY_FEE_SCOPE) == 1
        assert soup.select_one("#cheapest > p").get_text().count(GATEWAY_FEE_SCOPE) == 1
        text = text.replace(scope, "")
    else:
        assert scope not in text
    # The shared gateway processing statement is the one approved use of that word.
    text = text.replace(gateway_processing_claim().lower(), "")
    for node in soup.select('meta[content], script[type="application/ld+json"]'):
        text += " " + (node.get("content") or node.get_text()).lower()
    for phrase in (
        "what this page does not cover", "for 1.x change", "fallback only",
        "opencode needs no key of its own", "console keys authenticate",
        "synth uses multiple calls; evaluate its extra cost", "lifetime key limits",
        "this describes documented behavior, not a live integration test",
        "cited page does not establish invoice reconciliation", "respect scan limits and delayed arrival",
        "for management and activity; inference uses", "do not add it twice",
        "a gateway usually pays for itself", "these are example budget choices",
        "the short example itself does not establish a cache hit", "it does not itself prove a saving",
        "return only the integer 42", "most hosted llm gateways", "processing", "at checkout",
    ):
        assert phrase not in text


@pytest.fixture
def fixed_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Literal inputs keep complete-page coverage independent of price refreshes."""
    models = {}
    endpoints: dict[str, list[ModelEndpoint]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    prices = (
        (DEEPSEEK_MODEL, "deepinfra", "DeepSeek V4.1 Flash", 200000, 600000, 211000, 633000),
        ("openai/gpt-6-luna", "openai", "GPT-6 Luna", 100000, 500000, 105500, 527500),
        ("anthropic/claude-sonnet-5.5", "anthropic", "Claude Sonnet 5.5", 2000000, 10000000, 2110000, 10550000),
        ("openai/gpt-6.2-sol", "openai", "GPT-6.2 Sol", 2000000, 10000000, 2110000, 10550000),
        ("anthropic/claude-opus-5.5", "anthropic", "Claude Opus 5.5", 4000000, 20000000, 4220000, 21100000),
        (DEEPSEEK_MODEL, "engy", "DeepSeek V4.1 Flash", 30000, 120000, 31650, 126600),
        (DEEPSEEK_MODEL, "io-net", "DeepSeek V4.1 Flash", 820200, 3353400, 865311, 3537837),
    )
    for model_id, provider, name, source_input, source_output, retail_input, retail_output in prices:
        models[model_id] = Model(model_id, name, provider, 1048576, max_completion_tokens=393216)
        endpoints.setdefault(model_id, []).append(ModelEndpoint(
            id=f"{provider}/{model_id}", model_id=model_id, provider=provider, usage_type="Credits",
            prompt_price_microdollars_per_million_tokens=retail_input,
            completion_price_microdollars_per_million_tokens=retail_output,
            price_tiers=(PriceTier(None, retail_input, retail_output, 10550),),
            max_completion_tokens=393216,
        ))
        manifest = manifests.setdefault(provider, {
            "generated_at": "2026-11-24T17:28:45Z", "source": "https://example.com/prices", "models": [],
        })
        manifest["models"].append({"id": model_id, "input_token_price_per_m": source_input,
                                   "output_token_price_per_m": source_output})
    for alias in guides.ALIAS_POLICIES:
        model_id = "trustedrouter/" + alias
        models[model_id] = Model(model_id, alias, "trustedrouter",
                                131072 if alias in {"fast", "synth", "synth-code"} else 1048576)
    sdk_id = "openai/gpt-5.4-mini"
    models[sdk_id] = Model(sdk_id, "GPT-5.4 Mini", "openai", 1048576)
    monkeypatch.setattr(guides, "MODELS", models)
    monkeypatch.setattr(guides, "endpoints_for_model", lambda model_id: endpoints.get(model_id, []))
    monkeypatch.setattr(guides, "_manifest", lambda provider: manifests.get(provider))
    monkeypatch.setattr(guides, "published_context_length", lambda model: model.context_length)
    monkeypatch.setattr(guides, "published_max_completion_tokens", lambda model, endpoints: model.max_completion_tokens)


def test_full_fixed_catalog(client: TestClient, fixed_catalog: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    test_worked_costs_match_catalog_and_rounding(client)
    test_route_prices_and_cache_savings_follow_catalog(client)
    test_python_samples_compile_and_use_catalog_models(client)
    test_python_allocation_and_ticket_validation(client, monkeypatch, tmp_path)
    soup = BeautifulSoup(client.get(PAGES[1]).text, "html.parser")
    savings = BeautifulSoup(client.get(PAGES[3]).text, "html.parser")
    assert [(row["data-model"], row["data-provider"]) for row in soup.select("#cost-example tbody tr")] == list(COST_GUIDE_ROUTES)
    assert len(savings.select("pre[data-request]")) == 7
    assert len(savings.select("pre[data-python]")) == 4
    assert savings.select_one("[data-route-saving-percent]").get_text() == "96%"
    assert "Up to 96%" in savings.get_text()
    for key, expected in (("halving", "53%"), ("saving", "76%"), ("escalation", "20%"), ("cache-saving", "90%")):
        assert savings.select_one(f"[data-{key}-percent]").get_text() == expected
    assert "Move about 53%" in savings.get_text()
    assert "The 5.5% fee is repaid once about 5.5% of Claude Sonnet 5.5 traffic moves to GPT-6 Luna" in soup.get_text()
    assert soup.select_one("[data-break-even-percent]").get_text() == "5.5%"
    schema = json.loads((ROOT / "tests/fixtures/opencode-provider-config.schema.json").read_text())
    validator = Draft202012Validator(schema)
    for path, count in ((PAGES[0], 7), (PAGES[2], 3)):
        page = BeautifulSoup(client.get(path).text, "html.parser")
        assert len(page.select("pre[data-config]")) == count
        seen = set()
        for block in page.select("pre[data-config]"):
            config = json.loads(block.get_text())
            validator.validate(config)
            for group in ("provider", "providers"):
                for provider in config.get(group, {}).values():
                    for model_id, model in provider["models"].items():
                        seen.add(model_id)
                        source = guides.MODELS[model_id]
                        expected = {"context": source.context_length}
                        if source.max_completion_tokens is not None:
                            expected["output"] = source.max_completion_tokens
                        assert model["limit"] == expected
                        assert f'{source.context_length:,}' in page.get_text()
        assert DEEPSEEK_MODEL in seen
        document = page.select_one(".content-document").get_text(" ")
        for selector in re.findall(r"(?<!@)(?:trustedrouter|openrouter)/[a-z0-9./-]+", document):
            if selector.startswith(("trustedrouter/trustedrouter/", "trustedrouter/deepseek/", "openrouter/")):
                selector = selector.split("/", 1)[1]
            assert selector.rstrip(".") in guides.MODELS
        if path == PAGES[0]:
            aliases = page.select("tr[data-alias]")
            assert len(aliases) == 7
            assert {row["data-alias"] for row in aliases} == {
                f"trustedrouter/{name}" for name in ("auto", "cheap", "e2e", "fast", "synth", "synth-code", "zdr")
            }
            assert seen - {DEEPSEEK_MODEL} == {"trustedrouter/fast", "trustedrouter/synth", "trustedrouter/synth-code"}


def test_live_catalog_missing_inputs_warn(client: TestClient) -> None:
    soup = BeautifulSoup(client.get(PAGES[1]).text, "html.parser")
    savings = BeautifulSoup(client.get(PAGES[3]).text, "html.parser")
    present = {row["data-model"] for row in soup.select("#cost-example tbody tr")}
    for model_id, provider in COST_GUIDE_ROUTES:
        if model_id not in present:
            missing = []
            if model_id not in guides.MODELS:
                missing.append("model")
            if guides._source(guides._manifest(provider), model_id) is None:
                missing.append("manifest row or price tier")
            if not any(e.provider == provider and e.usage_type == "Credits" for e in guides.endpoints_for_model(model_id)):
                missing.append("current prepaid endpoint")
            warnings.warn(f"Worked example missing {model_id} on {provider}: {', '.join(missing)}", stacklevel=2)
    pair = f"{COST_GUIDE_ROUTES[1][0]} and {COST_GUIDE_ROUTES[2][0]}"
    for page, figure, inputs in (
        (savings, "mixed-total", f"{pair} model rows"),
        (savings, "halving-percent", f"{pair} prices with a positive difference"),
        (soup, "break-even-percent", f"{pair} rows and provider price"),
        (savings, "escalation-total", f"{pair} model rows"),
        (savings, "cache-saving-percent", f"{COST_GUIDE_ROUTES[1][0]} cache-read tier and input price"),
        (savings, "saving-percent", f"{pair} model rows and nonzero premium price"),
        (savings, "route-saving-percent", f"{DEEPSEEK_MODEL} routes and nonzero high route price"),
        (savings, "route-count", f"{DEEPSEEK_MODEL} model, prepaid routes and manifests"),
    ):
        if not page.select(f"[data-{figure}]"):
            warnings.warn(f"Figure {figure} missing input: {inputs}", stacklevel=2)


@pytest.mark.parametrize("missing", ["small", "premium", "deepseek", "manifest", "all",
                                     "model", "alias", "aliases", "models", "cache-tier", "cache-rate", "manifest-file"])
def test_catalog_removals_degrade_sections(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fixed_catalog: None, missing: str,
) -> None:
    original_endpoints = guides.endpoints_for_model
    original_manifest = guides._manifest
    small, premium = COST_GUIDE_ROUTES[1][0], COST_GUIDE_ROUTES[2][0]
    targets = {"small": small, "premium": premium, "deepseek": DEEPSEEK_MODEL}
    if missing in targets or missing == "all":
        monkeypatch.setattr(guides, "endpoints_for_model", lambda model_id: (
            [] if missing == "all" or model_id == targets[missing] else original_endpoints(model_id)
        ))
    elif missing in {"manifest", "manifest-file"}:
        def manifest(provider: str) -> dict[str, Any] | None:
            result = original_manifest(provider)
            if missing == "manifest-file" and provider == "openai":
                return None
            return {**result, "models": [row for row in result["models"] if row["id"] != small]}
        monkeypatch.setattr(guides, "_manifest", manifest)
    elif missing in {"model", "alias", "aliases", "models"}:
        removed = {"model": {DEEPSEEK_MODEL}, "alias": {"trustedrouter/fast"},
                   "aliases": {"trustedrouter/" + alias for alias in guides.ALIAS_POLICIES},
                   "models": set(guides.MODELS)}[missing]
        monkeypatch.setattr(guides, "MODELS", {key: value for key, value in guides.MODELS.items() if key not in removed})
    else:
        def without_cache(model_id: str) -> list[ModelEndpoint]:
            endpoints = original_endpoints(model_id)
            return [replace(e, price_tiers=() if missing == "cache-tier" else tuple(
                replace(t, prompt_cached_price_microdollars_per_million_tokens=None) for t in e.price_tiers
            )) for e in endpoints] if model_id == small else endpoints
        monkeypatch.setattr(guides, "endpoints_for_model", without_cache)
    pages = {}
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200
        assert "None" not in response.text
        assert not re.search(r"\$\s+(?!\d)\S", BeautifulSoup(response.text, "html.parser").get_text(" "))
        assert not any(marker in response.text for marker in ("{{", "{%", "{#"))
        pages[path] = BeautifulSoup(response.text, "html.parser")
    gateway, savings = pages[PAGES[1]], pages[PAGES[3]]
    assert len(gateway.select("#gateway-fees tbody tr")) == 6
    assert len(gateway.select("#gateway-cost-visibility tbody tr")) == 6
    assert len(gateway.select("#fee-scale tbody tr")) == 3
    assert len(pages[PAGES[0]].select("#quick-start ol li")) == 3
    assert len(pages[PAGES[2]].select("#direct ol li")) == 3
    assert pages[PAGES[0]].select_one("#api-key")
    assert pages[PAGES[0]].select_one("#troubleshooting")
    assert savings.select_one("#batch") and savings.select_one("#cap")
    lost_small = missing in {"small", "all", "manifest", "models", "manifest-file"}
    lost_premium = missing in {"premium", "all", "models"}
    lost_deepseek = missing in {"deepseek", "all", "model", "models"}
    for model_id, lost in ((small, lost_small), (premium, lost_premium), (DEEPSEEK_MODEL, lost_deepseek)):
        assert bool(gateway.select(f'tr[data-model="{model_id}"]')) is not lost
    for name in ("mixed-total", "halving-percent", "escalation-total", "saving-percent"):
        assert bool(savings.select(f"[data-{name}]")) is not (lost_small or lost_premium)
    assert bool(gateway.select("[data-break-even-percent]")) is not (lost_small or lost_premium)
    assert bool(savings.select("#fallback")) is not (lost_small or lost_premium)
    if lost_small or lost_premium:
        assert "Move about" not in savings.select_one(".content-document > p").get_text()
        assert "Batch" in savings.select_one(".content-document > p").get_text()
    assert bool(savings.select("[data-cache-saving-percent]")) is not (lost_small or missing in {"cache-tier", "cache-rate"})
    assert bool(savings.select("#cache")) is not (lost_small or missing in {"cache-tier", "cache-rate"})
    for path in PAGES[1:]:
        assert bool(pages[path].select("[data-route-count]")) is not lost_deepseek
    assert bool(savings.select("[data-route-saving-percent]")) is not lost_deepseek
    for path in (PAGES[0], PAGES[2]):
        configs = [json.loads(block.get_text()) for block in pages[path].select("pre[data-config]")]
        assert any(DEEPSEEK_MODEL in config["model"] for config in configs) is not lost_deepseek
        if lost_deepseek:
            assert pages[path].select_one('a[href="/models"]')
    if lost_deepseek:
        assert not pages[PAGES[0]].select("#custom-model, #custom-provider")
        assert not pages[PAGES[0]].select('pre[data-example="curl"]')
    if missing in {"alias", "aliases", "models"}:
        assert not pages[PAGES[0]].select('tr[data-alias="trustedrouter/fast"]')
        assert not any('"trustedrouter/fast"' in pre.get_text() for pre in pages[PAGES[0]].select("pre[data-config]"))
    if missing in {"aliases", "models"}:
        assert not pages[PAGES[0]].select("tr[data-alias]")
        assert not any("Alias context limit overrides" in h.get_text() for h in pages[PAGES[0]].select("h3"))
    if missing in {"all", "models"}:
        assert not gateway.select("#cost-example")
        section = gateway.select_one("#worked-example")
        assert "Multiply input millions" in section.get_text()
        assert section.select_one('a[href="/models"]')
    test_worked_costs_match_catalog_and_rounding(client)
    test_route_prices_and_cache_savings_follow_catalog(client)
