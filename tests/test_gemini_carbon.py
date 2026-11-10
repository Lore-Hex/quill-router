"""Carbon's native discovery, dated tariffs, admission and privacy contracts."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import base, vertex_prices
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.gemini_prices import current_price_source
from scripts.pricing.parsers import gemini as parser
from scripts.pricing.providers import gemini, google_vertex
from tests.test_gemini_markdoc_pricing import _tab, _table
from trusted_router.pricing_approvals import approved_dated_price_transition

MODEL = "google/gemini-4-carbon"
# Google documents that Gemini 4 ignores custom sampling values (Vertex model
# card: "Temperature: 1.0 (fixed; custom values ignored)"; Gemini API text
# generation guide: "Gemini 4 and Gemini 3.6+ models ignore custom values for
# temperature, top_p, and top_k"). Never advertise controls that do nothing.
IGNORED_SAMPLING = {"temperature", "top_p", "top_k"}


def test_carbon_has_both_direct_routes_and_correct_capabilities() -> None:
    from trusted_router.catalog import (
        MODEL_ENDPOINTS,
        MODELS,
        PRIVACY_TIER_CONFIDENTIAL,
        PRIVACY_TIER_ZERO_RETENTION,
        endpoint_meets_privacy_requirement,
        model_to_openrouter_shape,
    )

    model = MODELS[MODEL]
    shape = model_to_openrouter_shape(model)
    assert model.context_length == 1_048_576
    assert {"tools", "tool_choice", "reasoning_effort", "response_format"} <= set(
        shape["supported_parameters"]
    )
    assert "temperature" not in shape["supported_parameters"]
    routes = [ep for ep in MODEL_ENDPOINTS.values() if ep.model_id == MODEL]
    assert {ep.provider for ep in routes} == {"google-ai-studio", "google-vertex"}
    assert {ep.upstream_id for ep in routes} == {"gemini-4-carbon"}
    for ep in routes:
        assert not endpoint_meets_privacy_requirement(ep, PRIVACY_TIER_ZERO_RETENTION)
        assert not endpoint_meets_privacy_requirement(ep, PRIVACY_TIER_CONFIDENTIAL)


def _page() -> str:
    # Official Standard Markdoc cells, captured 2026-11-10. Batch/Contributor
    # are deliberately cheaper and must never become the ordinary tariff.
    return "## Gemini 4 Carbon {% id=\"gemini-4-carbon\" %}\n" + (
        _tab("Contributor", _table("$1", "$5", "$0.05"))
        + _tab("Standard", _table(
            "$2.00 through November 30, 2026.\n $4.00 starting December 1, 2026.",
            "$10.00 through November 30, 2026.\n $20.00 starting December 1, 2026.",
            "$0.10 through November 30, 2026.\n $0.20 starting December 1, 2026.\n"
            "Implicit caching only (explicit caching is not supported).",
        ))
        + _tab("Batch", _table("$1", "$5", "$0.05"))
    )


@pytest.mark.parametrize("day,factor", [
    (date(2026, 11, 10), 1), (date(2026, 11, 30), 1),
    (date(2026, 12, 1), 2), (date(2027, 1, 1), 2),
])
def test_standard_prices_follow_utc_cutover(day: date, factor: int) -> None:
    result = parser.parse(current_price_source(_page(), today=day))
    assert result[MODEL] == {
        "prompt_micro_per_m": factor * 2_000_000,
        "completion_micro_per_m": factor * 10_000_000,
        "prompt_cached_micro_per_m": factor * 100_000,
    }


@pytest.mark.parametrize("source", [
    _page().replace("December 1, 2026", "December 2, 2026"),
    _page().replace("December 1, 2026", "November 30, 2026"),
    _page().replace("starting December 1, 2026.", "starting next month."),
    _page().replace("$4.00 starting December 1, 2026.", ""),
])
def test_ambiguous_schedule_fails_closed(source: str) -> None:
    with pytest.raises(ValueError, match="gemini:"):
        parser.parse(current_price_source(source, today=date(2026, 11, 10)))


def test_date_transform_is_used_in_real_fetch_without_widening_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(base, "fetch_html", lambda *_args, **_kwargs: _page())
    result = base.fetch_provider(
        slug="gemini", url=gemini.URL, expected_models=[MODEL],
        require_runtime_models=False,
        source_transform=lambda source: current_price_source(source, today=date(2026, 12, 1)),
    )
    assert result.prices[MODEL].prompt_micro_per_m == 4_000_000
    assert base.ast_whitelist_check(Path(parser.__file__).read_text()) == []


def test_native_carbon_discovery_requires_its_own_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini, "_known_manifest_model_ids", lambda: set())
    assert gemini._new_required_price_ids({MODEL: {}}) == {MODEL}
    assert google_vertex._CHAT_ID.fullmatch("gemini-4-carbon")
    assert not google_vertex._CHAT_ID.fullmatch("gemini-4-carbon-cyber-permissive")


def test_dated_storage_rent_is_not_cached_token_price() -> None:
    source = "## Gemini 3.8 Flash\n" + _tab("Standard", _table(
        "$0.75", "$3.75", "$0.075\n"
        "$0.50 / 1,000,000 tokens per hour (storage price) through December 31, 2026.\n"
        "$1.00 / 1,000,000 tokens per hour (storage price) starting January 1, 2027.",
    ))
    assert parser.parse(current_price_source(source))["google/gemini-3.8-flash"][
        "prompt_cached_micro_per_m"
    ] == 75_000


@pytest.mark.parametrize("day,expected", [
    (date(2026, 11, 30), 2_000_000), (date(2026, 12, 1), 4_000_000),
])
def test_vertex_carbon_independent_standard_tariff(day: date, expected: int) -> None:
    # Carbon uses the same independently published table shape as Argon.
    source = (Path(__file__).parent / "fixtures/pricing/vertex_argon.html").read_text()
    result = vertex_prices.parse(source.replace("Argon", "Carbon"), as_of=day)
    assert result[MODEL].prompt_micro_per_m == expected
    assert result[MODEL].completion_micro_per_m == expected * 5
    assert result[MODEL].tiers[0].prompt_cached_micro_per_m == expected // 20
    assert MODEL + "-cyber-permissive" not in result


@pytest.mark.parametrize("provider", ["google-ai-studio", "google-vertex"])
@pytest.mark.parametrize("model", ["argon", "carbon"])
@pytest.mark.parametrize("suffix,before,after", [
    ("", (2_000_000, 10_000_000), (4_000_000, 20_000_000)),
    (" cached-input", (100_000, 0), (200_000, 0)),
])
def test_only_reviewed_complete_price_transition_is_approved(
    provider: str, model: str, suffix: str, before: tuple[int, int], after: tuple[int, int],
) -> None:
    route = f"google/gemini-4-{model} [{provider}:{provider}:gemini-4-{model}]{suffix}"
    assert not approved_dated_price_transition(route, before, after, today=date(2026, 11, 30))
    assert approved_dated_price_transition(route, before, after, today=date(2026, 12, 1))
    assert not approved_dated_price_transition(
        route, before, (after[0] + 1, after[1]), today=date(2026, 12, 1),
    )
    assert not approved_dated_price_transition(
        route + "-unreviewed", before, after, today=date(2026, 12, 1),
    )


def _refreshed_snapshot_entry() -> dict[str, Any]:
    # Shape of OpenRouter's Carbon row as ingested by refresh run 42913910810.
    # Its Google endpoints list sampling controls Google says Carbon ignores.
    sampling = ["reasoning", "include_reasoning", "max_tokens", "temperature", "top_p",
                "seed", "response_format", "tools", "tool_choice", "structured_outputs",
                "reasoning_effort"]
    price = {"prompt": "0.000002", "completion": "0.00001", "input_cache_read": "0.0000001"}
    return {
        "id": MODEL, "name": "Google: Gemini 4 Carbon", "context_length": 1_048_576,
        "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        "pricing": price, "top_provider": {"context_length": 1_048_576},
        "endpoints": [
            {"tr_provider_slug": "google-ai-studio", "context_length": 1_048_576,
             "max_completion_tokens": 32_768, "pricing": price,
             "pricing_source": "provider_direct", "supported_parameters": sampling},
            {"tr_provider_slug": "google-vertex", "context_length": 1_048_576,
             "max_completion_tokens": 32_768, "pricing": price,
             "pricing_source": "provider_direct",
             "supported_parameters": [*sampling, "stop"]},
        ],
    }


def test_refreshed_snapshot_sampling_controls_do_not_leak_into_carbon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import catalog_ingest

    snapshot = tmp_path / "openrouter_snapshot.json"
    snapshot.write_text(json.dumps({"models": [_refreshed_snapshot_entry()]}))
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)
    monkeypatch.setattr(catalog_ingest, "_api_reported_context_windows", lambda: {})
    models, endpoints = catalog_ingest._ingested_models_and_endpoints()
    routes = [ep for ep in endpoints.values() if ep.model_id == MODEL]
    assert {ep.provider for ep in routes} == {"google-ai-studio", "google-vertex"}
    for params in [models[MODEL].supported_parameters, *(ep.supported_parameters for ep in routes)]:
        assert not IGNORED_SAMPLING & set(params)
        assert {"tools", "tool_choice", "reasoning_effort", "response_format"} <= set(params)


def _committed_carbon_row(path: Path) -> dict[str, Any]:
    rows = json.loads(path.read_text())["models"]
    return next(row for row in rows if row["id"] == MODEL)


def _assert_parameter_contract(row: dict[str, Any]) -> None:
    params = row.get("supported_parameters")
    assert isinstance(params, list) and params, "exhaustive array lets the snapshot widen it"
    assert not IGNORED_SAMPLING & set(params)
    assert {"tools", "tool_choice", "reasoning_effort", "response_format"} <= set(params)


@pytest.mark.parametrize("module,slug,provider", [
    (gemini, "gemini", "google-ai-studio"),
    (google_vertex, "google_vertex", "google-vertex"),
])
def test_refresh_writers_preserve_carbon_parameter_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Any, slug: str, provider: str,
) -> None:
    committed = _committed_carbon_row(module.MANIFEST_PATH)
    _assert_parameter_contract(committed)
    manifest = tmp_path / module.MANIFEST_PATH.name
    manifest.write_text(json.dumps({"provider": provider, "models": [committed]}))
    monkeypatch.setattr(module, "MANIFEST_PATH", manifest)
    # Live discovery reports only identity and limits, never parameters.
    discovered = {
        "id": MODEL, "upstream_id": "gemini-4-carbon",
        "context_length": 1_048_576, "max_output_tokens": 32_768,
    }
    monkeypatch.setattr(module, "_DISCOVERED_MANIFEST_ROWS", {MODEL: discovered})
    result = ProviderPricingResult(slug=slug, prices={
        MODEL: ModelPrice(2_000_000, 10_000_000, prompt_cached_micro_per_m=100_000),
    }, source="api")
    module.write_provider_manifest(result)
    _assert_parameter_contract(_committed_carbon_row(manifest))
