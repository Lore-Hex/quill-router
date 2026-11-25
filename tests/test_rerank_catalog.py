from __future__ import annotations

import json
import runpy
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from scripts.pricing.base import ModelPrice, ProviderPricingResult, normalize_parser_input
from scripts.pricing.parsers import cohere as cohere_parser
from scripts.pricing.parsers import voyage as voyage_parser
from scripts.pricing.providers import cohere, jina, voyage
from tests.fixture_routes import clear_catalog_caches
from tests.rerank_manifest_fixtures import MANIFEST_STATES, provider_manifest, rerank_rows
from trusted_router import catalog_data, catalog_ingest, catalog_registry, routing
from trusted_router.catalog import model_to_openrouter_shape
from trusted_router.catalog_data import Model
from trusted_router.choose_catalog import choose_catalog_payload
from trusted_router.internal_model_access import internal_model_ids, requests_internal_model
from trusted_router.model_changes import route_state
from trusted_router.pricing import (
    _PRICE_FLOOR_MICRODOLLARS_PER_M,
    STANDARD_PRICE_MARKUP_BASIS_POINTS,
    _customer_price,
)
from trusted_router.provider_manifest_policy import provider_manifest_row_price_is_valid
from trusted_router.routes.mcp import TrustedRouterMCP
from trusted_router.routing_snapshot import (
    CatalogInputs,
    Plane,
    canonical_snapshot_bytes,
    compile_routing_snapshot,
    snapshot_settings_from,
)
from trusted_router.seo_catalog import seo_catalog_evidence

FIXTURES = Path(__file__).parent / "fixtures/pricing"
RERANK_ID = "voyage/rerank-2"
ROW = {
    "id": RERANK_ID,
    "upstream_id": "rerank-2",
    "display_name": "Rerank 2",
    "model_type": "rerank",
    "endpoints": ["rerank"],
    "input_modalities": ["text"],
    "output_modalities": ["scores"],
    "input_token_price_per_m": 50_000,
    "output_token_price_per_m": 0,
}


def _manifest(path: Path, rows: list[dict]) -> None:
    (path / "voyage.json").write_text(json.dumps({
        "provider": "voyage", "price_scale": "microdollars_per_million", "models": rows,
    }))


def test_rerank_flags_default_off() -> None:
    assert all(not provider.supports_rerank for provider in catalog_data.PROVIDERS.values())
    assert not Model("example/model", "Model", "voyage", 8192).supports_rerank
    assert not any(model.supports_rerank for model in catalog_registry.MODELS.values())


def test_rerank_manifest_requires_provider_opt_in(tmp_path, monkeypatch) -> None:
    _manifest(tmp_path, [ROW])
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    assert catalog_ingest._rerank_models() == {}
    monkeypatch.setitem(catalog_data.PROVIDERS, "voyage",
                        replace(catalog_data.PROVIDERS["voyage"], supports_rerank=True))
    models = catalog_ingest._rerank_models()
    model = models[RERANK_ID]
    assert model.supports_rerank
    assert not model.supports_chat and not model.supports_embeddings and not model.supports_messages
    assert not model.internal_only
    assert model.prompt_price_microdollars_per_million_tokens == _customer_price(50_000)
    assert model.published_prompt_price_microdollars_per_million_tokens == _customer_price(50_000)
    assert model.completion_price_microdollars_per_million_tokens == 0
    assert model.price_tiers[0].prompt_price_microdollars_per_million_tokens == _customer_price(50_000)
    assert model.published_price_tiers == model.price_tiers
    assert model.input_modalities == ("text",) and model.output_modalities == ("scores",)
    assert model.prepaid_available and not model.byok_available
    endpoints = catalog_ingest._build_endpoints(models)
    assert len(endpoints) == 1
    endpoint = next(iter(endpoints.values()))
    assert endpoint.model_id == model.id and endpoint.provider == "voyage"
    assert endpoint.usage_type == "Credits"
    assert endpoint.prompt_price_microdollars_per_million_tokens == _customer_price(50_000)
    assert endpoint.completion_price_microdollars_per_million_tokens == 0
    assert catalog_ingest._build_endpoints({model.id: replace(model, byok_available=True)}) == endpoints
    monkeypatch.setitem(catalog_data.PROVIDERS, "voyage",
                        replace(catalog_data.PROVIDERS["voyage"], supports_rerank=False))
    assert catalog_ingest._build_endpoints(models) == {}



@pytest.mark.parametrize("source_price", [50_000, 1], ids=["markup", "floor"])
def test_rerank_customer_prices(source_price, tmp_path, monkeypatch) -> None:
    _manifest(tmp_path, [{**ROW, "input_token_price_per_m": source_price}])
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    monkeypatch.setitem(catalog_data.PROVIDERS, "voyage",
                        replace(catalog_data.PROVIDERS["voyage"], supports_rerank=True))

    expected = _customer_price(source_price)
    assert expected != source_price
    if source_price == 50_000:
        # 50,000 is divisible by 10,000, so the fee needs no rounding here.
        assert expected == 50_000 + 5 * STANDARD_PRICE_MARKUP_BASIS_POINTS
    else:
        assert source_price < _PRICE_FLOOR_MICRODOLLARS_PER_M
        assert expected == _PRICE_FLOOR_MICRODOLLARS_PER_M

    model = catalog_ingest._rerank_models()[RERANK_ID]
    assert model.prompt_price_microdollars_per_million_tokens == expected
    assert model.published_prompt_price_microdollars_per_million_tokens == expected
    assert model.completion_price_microdollars_per_million_tokens == 0
    assert model.published_completion_price_microdollars_per_million_tokens == 0
    for tiers in (model.price_tiers, model.published_price_tiers):
        assert len(tiers) == 1
        tier = tiers[0]
        assert tier.max_prompt_tokens is None
        assert tier.prompt_price_microdollars_per_million_tokens == expected
        assert tier.completion_price_microdollars_per_million_tokens == 0
        assert tier.prompt_cached_price_microdollars_per_million_tokens is None


@pytest.mark.parametrize("fields", [
    {"input_token_price_per_m": None}, {"input_token_price_per_m": 0},
    {"input_token_price_per_m": -1}, {"input_token_price_per_m": True},
    {"input_token_price_per_m": "50000"}, {"output_token_price_per_m": 1},
    {"output_token_price_per_m": None}, {"endpoints": ["chat/completions"]},
    {"endpoints": ["rerank", "embeddings"]}, {"billing_unit": "request"},
    {"billing_unit": "search"}, {"cached_input_token_price_per_m": 1}, {"price_tiers": []},
])
def test_rerank_invalid_price_never_loads(fields, tmp_path, monkeypatch) -> None:
    row = {**ROW, **fields}
    row = {key: value for key, value in row.items() if value is not None}
    assert not provider_manifest_row_price_is_valid(row)
    _manifest(tmp_path, [row])
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    monkeypatch.setitem(catalog_data.PROVIDERS, "voyage",
                        replace(catalog_data.PROVIDERS["voyage"], supports_rerank=True))
    assert catalog_ingest._rerank_models() == {}


@pytest.mark.parametrize("manifest_state", MANIFEST_STATES)
@pytest.mark.parametrize("provider,fixture,expected", [
    (voyage, "voyage.html", {"voyage/rerank-2": 50_000}),
    (voyage, "voyage_pricing_2026-11-24.md", {
        "voyage/rerank-3": 50_000, "voyage/rerank-3-lite": 20_000,
        "voyage/rerank-2.5": 50_000, "voyage/rerank-2.5-lite": 20_000,
        "voyage/rerank-2": 50_000, "voyage/rerank-2-lite": 20_000,
        "voyage/rerank-1": 50_000, "voyage/rerank-lite-1": 20_000,
    }),
    (cohere, "cohere_pricing_2026-11-07.html",
     {"cohere/rerank-5-fast": 20_000, "cohere/rerank-5-pro": 50_000}),
    (cohere, "cohere_pricing_2026-11-24.html",
     {"cohere/rerank-5-fast": 20_000, "cohere/rerank-5-pro": 50_000}),
])
def test_rerank_parser_to_manifest(provider, fixture, expected, manifest_state, tmp_path, monkeypatch) -> None:
    import scripts.pricing.base as base
    import scripts.pricing.manifest as manifest_writer

    page = (FIXTURES / fixture).read_text()
    monkeypatch.setattr(base, "fetch_html", lambda *_a, **_kw: page)
    if provider is cohere:
        monkeypatch.setattr(cohere, "fetch_html", lambda *_a, **_kw: page)
    path = tmp_path / f"{provider.SLUG}.json"
    initial = provider_manifest(provider.SLUG, manifest_state)
    path.write_text(json.dumps(initial))
    monkeypatch.setattr(provider, "MANIFEST_PATH", path)
    result = provider.fetch()
    assert not set(expected) & result.price_index_model_ids
    instant = datetime(2026, 11, 24, 12, tzinfo=UTC)
    monkeypatch.setattr(manifest_writer, "datetime", SimpleNamespace(now=lambda _tz: instant))
    provider.write_provider_manifest(result)
    raw = json.loads(path.read_text())
    rows = {row["id"]: row for row in raw["models"] if row["model_type"] == "rerank"}
    initial_rows = {row["id"]: row for row in initial["models"] if row["model_type"] == "rerank"}
    assert set(rows) == set(expected) | set(initial_rows)
    assert {model_id for model_id, row in rows.items() if row.get("routable") is not False} == set(expected)
    for model_id in initial_rows.keys() - expected.keys():
        assert rows[model_id] == {
            **initial_rows[model_id], "routable": False, "routable_reason": "price-unavailable",
        }
    assert raw["model_count"] == len(raw["models"])
    for model_id, cost in expected.items():
        row = rows[model_id]
        assert row["input_token_price_per_m"] == cost
        assert row["output_token_price_per_m"] == 0
        assert row["endpoints"] == ["rerank"]
        assert row["upstream_id"] == model_id.split("/", 1)[1]
        assert row["pricing_source"] == provider.URL
        assert provider_manifest_row_price_is_valid(row)
    assert not catalog_data.PROVIDERS[provider.SLUG].supports_rerank
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    assert catalog_ingest._rerank_models() == {}

    # The next hourly write may change only the root generated_at field.
    assert raw["generated_at"] == "2026-11-24T12:00:00Z"
    instant += timedelta(hours=1)
    provider.write_provider_manifest(result)
    second = json.loads(path.read_text())
    assert second == {**raw, "generated_at": "2026-11-24T13:00:00Z"}


@pytest.mark.parametrize("unit", ["search", "request"])
def test_rerank_non_token_prices_are_skipped(unit, monkeypatch) -> None:
    import scripts.pricing.base as base

    page = (FIXTURES / "cohere_pricing_2026-11-07.html").read_text()
    page = page.replace('Rerank 5 Fast\\",\\"per\\":\\"1M tokens',
                        f'Rerank 5 Fast\\",\\"per\\":\\"1K {unit}s')
    assert "cohere/rerank-5-fast" not in cohere_parser.parse_rerank(normalize_parser_input(page))
    monkeypatch.setattr(cohere, "fetch_html", lambda *_a, **_kw: page)
    result = cohere.fetch()
    assert any("Rerank 5 Fast" in note and unit in note for note in result.notes)
    source = (FIXTURES / "voyage.html").read_text()
    table = source[source.index("## Older models"):source.index("# Fine-tuned models")]
    table = table.replace("## Older models", "# Rerank").replace("per million tokens", f"per million {unit}s")
    page = source.replace("| `rerank-2`               | $0.00005                  | $0.05                    |", "") + table
    assert "voyage/rerank-2" not in voyage_parser.parse(page)
    monkeypatch.setattr(base, "fetch_html", lambda *_a, **_kw: page)
    result = voyage.fetch()
    assert any("rerank-2" in note and "per-token" in note for note in result.notes)


def test_jina_scores_without_token_price_are_reported(monkeypatch) -> None:
    rows = [{"id": model_id, "output_modalities": ["embeddings"],
             "pricing": {"prompt": "0.00000002"}} for model_id in jina.EXPECTED_MODELS]
    rows.append({"id": "jina-ai/jina-reranker-v3", "output_modalities": ["scores"],
                 "pricing": {"request": "0.00000002"}})
    monkeypatch.setenv("JINA_API_KEY", "test-key")
    monkeypatch.setattr(jina, "fetch_json", lambda *_a, **_kw: {"data": rows})
    monkeypatch.setattr(jina, "_probe", lambda *_a: True)
    result = jina.fetch()
    assert "jina-ai/jina-reranker-v3" not in result.prices
    assert any("jina-reranker-v3" in note and "per-token" in note for note in result.notes)


@pytest.mark.parametrize("manifest_state", MANIFEST_STATES)
def test_rerank_catalog_public_and_routing_differential(
    tmp_path, monkeypatch, client, test_settings, manifest_state,
) -> None:
    shutil.copytree(catalog_ingest._PROVIDER_MODELS_DIR, tmp_path / "manifests")
    directory = tmp_path / "manifests"
    # Exercise both sides of the scheduled refresh, regardless of today's files.
    for slug in ("voyage", "cohere"):
        path = directory / f"{slug}.json"
        raw = json.loads(path.read_text())
        raw["models"] = [row for row in raw["models"] if row.get("model_type") != "rerank"]
        raw["models"].extend(rerank_rows(slug, manifest_state))
        raw["model_count"] = len(raw["models"])
        path.write_text(json.dumps(raw))
    # Every copied provider must have a rerank-free baseline before adding ROW.
    for path in directory.glob("*.json"):
        raw = json.loads(path.read_text())
        raw["models"] = [row for row in raw["models"] if row.get("model_type") != "rerank"]
        raw["model_count"] = len(raw["models"])
        path.write_text(json.dumps(raw))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", directory)
    monkeypatch.setitem(catalog_data.PROVIDERS, "voyage",
                        replace(catalog_data.PROVIDERS["voyage"], supports_rerank=True))
    from trusted_router import middleware

    monkeypatch.setattr(middleware, "secrets", SimpleNamespace(token_urlsafe=lambda _: "fixed-test-nonce"))
    instant = catalog_registry.CATALOG_RESOLVED_AT
    monkeypatch.setattr(catalog_data, "_utc_now", lambda: instant)
    for module_name, module in list(sys.modules.items()):
        if module is not None and module_name.startswith("trusted_router") and "utcnow" in vars(module):
            monkeypatch.setattr(module, "utcnow", lambda: instant)
    path = directory / "voyage.json"
    raw = json.loads(path.read_text())
    baseline = runpy.run_path(catalog_registry.__file__)
    raw["models"].append(ROW)
    raw["model_count"] = len(raw["models"])
    path.write_text(json.dumps(raw))
    extended = runpy.run_path(catalog_registry.__file__)
    assert set(extended["MODELS"]) - set(baseline["MODELS"]) == {RERANK_ID}
    assert extended["MODELS"][RERANK_ID].supports_rerank
    assert {key: value for key, value in extended["MODELS"].items() if key != RERANK_ID} == baseline["MODELS"]
    added = {key: value for key, value in extended["MODEL_ENDPOINTS"].items()
             if value.model_id == RERANK_ID}
    assert len(added) == 1
    assert {key: value for key, value in extended["MODEL_ENDPOINTS"].items()
            if key not in added} == baseline["MODEL_ENDPOINTS"]

    def observe(registry):
        with pytest.MonkeyPatch.context() as patch:
            for module_name, module in list(sys.modules.items()):
                if module is not None and module_name.startswith("trusted_router"):
                    for name in ("MODELS", "MODEL_ENDPOINTS"):
                        if name in vars(module):
                            patch.setattr(module, name, registry[name])
            clear_catalog_caches()
            if RERANK_ID in registry["MODELS"]:
                candidates = routing.rerank_route_endpoint_candidates({"model": RERANK_ID}, test_settings)
                assert [(model.id, endpoint.id) for model, endpoint in candidates] == [
                    (RERANK_ID, next(iter(added)))
                ]
            outputs = {}
            paths = (
                "/v1/models", "/v1/models?include=endpoints", "/v1/models/picker",
                "/v1/models/count", "/v1/providers", "/v1/embeddings/models", "/v1/images/models",
                "/v1/endpoints/zdr", "/v1/regions", "/", "/models", "/providers",
                "/providers/voyage", "/compare/models",
                "/sitemap.xml", "/sitemap-models.xml", "/sitemap-providers.xml",
                "/sitemap-core.xml", "/sitemap-comparisons.xml",
                f"/models/{RERANK_ID}", f"/models/{RERANK_ID}/pricing",
                f"/v1/models/{RERANK_ID}/endpoints", "/models/voyage/voyage-3-large",
            )
            for url in paths:
                response = client.get(url)
                assert response.status_code == (404 if url.startswith(f"/models/{RERANK_ID}") else 200), url
                outputs[url] = (response.status_code, response.content, response.headers.get("etag"))
            providers_html = client.get("/providers", headers={"accept": "text/html"})
            assert providers_html.status_code == 200
            outputs["providers_html"] = providers_html.content
            mcp = TrustedRouterMCP(test_settings)
            outputs["mcp_models"] = mcp._models_projection()
            outputs["mcp_providers"] = mcp._providers_projection()
            outputs["route_history"] = route_state(
                model_to_openrouter_shape(model) for model in registry["MODELS"].values()
            )
            outputs["internal_models"] = internal_model_ids()
            outputs["internal_key"] = requests_internal_model({"model": RERANK_ID})
            assert not outputs["internal_key"]
            outputs["choose"] = choose_catalog_payload(test_mode=True)
            outputs["counts"] = seo_catalog_evidence("openrouter", test_mode=True)
            for route in (routing.chat_route_endpoint_candidates, routing.embeddings_route_endpoint_candidates):
                for model_id in (RERANK_ID, "trustedrouter/auto", "voyage/voyage-3-large"):
                    try:
                        candidates = route({"model": model_id}, test_settings)
                        value = [(model.id, endpoint.id) for model, endpoint in candidates]
                    except HTTPException as error:
                        value = (error.status_code, error.detail)
                    outputs[route.__name__, model_id] = value
            inputs = CatalogInputs.live(test_settings)
            snapshot = compile_routing_snapshot(
                at=int(instant.timestamp()), horizon=3600,
                settings=snapshot_settings_from(test_settings), source_revision="rerank-test",
                plane=Plane(cloud="gcp", region="us-central1"), inputs=inputs,
            )
            outputs["snapshot"] = canonical_snapshot_bytes(snapshot)
            assert RERANK_ID.encode() not in outputs["snapshot"]
            response = client.post("/v1/rerank", json={"model": RERANK_ID, "query": "q", "documents": ["d"]})
            assert response.status_code == 501
            outputs["rerank"] = response.json()
            return outputs

    try:
        before, after = observe(baseline), observe(extended)
        for key in before:
            assert before[key] == after[key], key
    finally:
        clear_catalog_caches()


@pytest.mark.parametrize("heading", ["Rerank", "Rerankers", "Reranking"])
def test_voyage_rerank_section_uses_the_token_column(heading) -> None:
    page = (FIXTURES / "voyage.html").read_text()
    page = page.replace("## Older models", f"# {heading}")
    assert voyage_parser.parse(page)[RERANK_ID] == {
        "prompt_micro_per_m": 50_000, "completion_micro_per_m": 0,
    }


def test_rerank_manifest_respects_explicit_route_hold(tmp_path, monkeypatch) -> None:
    _manifest(tmp_path, [{**ROW, "routable": False}])
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    monkeypatch.setitem(catalog_data.PROVIDERS, "voyage",
                        replace(catalog_data.PROVIDERS["voyage"], supports_rerank=True))
    assert catalog_ingest._rerank_models() == {}


@pytest.mark.parametrize("provider", [voyage, cohere], ids=lambda provider: provider.SLUG)
@pytest.mark.parametrize("manifest_state", MANIFEST_STATES)
def test_rerank_writer_holds_a_row_when_its_token_price_disappears(
    tmp_path, monkeypatch, provider, manifest_state,
) -> None:
    path = tmp_path / f"{provider.SLUG}.json"
    initial = provider_manifest(provider.SLUG, manifest_state)
    path.write_text(json.dumps(initial))
    monkeypatch.setattr(provider, "MANIFEST_PATH", path)
    embedding = initial["models"][0]
    priced_rerank = rerank_rows(provider.SLUG, "post-refresh")
    result = ProviderPricingResult(provider.SLUG, {
        row["id"]: ModelPrice(row["input_token_price_per_m"], 0)
        for row in [embedding, *priced_rerank]
    }, "deterministic", fetched_url=provider.URL)
    provider.write_provider_manifest(result)
    before = json.loads(path.read_text())
    missing_id = priced_rerank[0]["id"]
    del result.prices[missing_id]
    provider.write_provider_manifest(result)
    after = json.loads(path.read_text())
    expected_rows = [
        {**row, "routable": False, "routable_reason": "price-unavailable"}
        if row["id"] == missing_id else row
        for row in before["models"]
    ]
    assert after["models"] == expected_rows
    assert after["model_count"] == before["model_count"] == len(expected_rows)
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    monkeypatch.setitem(catalog_data.PROVIDERS, provider.SLUG,
                        replace(catalog_data.PROVIDERS[provider.SLUG], supports_rerank=True))
    assert set(catalog_ingest._rerank_models()) == {row["id"] for row in priced_rerank} - {missing_id}
