"""Ingest refuses a token-billed manifest route that lacks a token price.

A missing or malformed token price parsed as zero, so the route billed at the
customer floor of $0.01 per million tokens (zero for pass-through retail
prices) while TR paid the provider's real price. #1374 fixed the writer bug
that could publish such a row; this guard keeps a future writer bug from
becoming a nearly free route.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from trusted_router import catalog_ingest, main
from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.image_generation import OPENAI_IMAGE_MODEL_IDS

MODEL_ID = "acme/unpriced-chat"
PRICES = {"input_token_price_per_m": 100_000, "output_token_price_per_m": 400_000}
REFUSAL = f"catalog.unpriced_manifest_row_refused provider=novita model={MODEL_ID}"

Ingest = Callable[..., tuple[dict[str, Model], dict[str, ModelEndpoint]]]


def _chat_row(**fields: Any) -> dict[str, Any]:
    return {
        "id": MODEL_ID,
        "upstream_id": "unpriced-chat",
        "model_type": "chat",
        "endpoints": ["chat/completions"],
        "context_length": 131_072,
        "routable": True,
        **fields,
    }


@pytest.fixture
def ingest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ingest:
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    monkeypatch.setattr(catalog_ingest, "_REFUSED_UNPRICED_ROWS", set())
    monkeypatch.setattr(catalog_ingest, "_REPORTED_UNPRICED_ROWS", set())

    def run(
        provider: str, *rows: dict[str, Any]
    ) -> tuple[dict[str, Model], dict[str, ModelEndpoint]]:
        manifest = {
            "provider": provider,
            "price_scale": "microdollars_per_million",
            "models": list(rows),
        }
        (tmp_path / f"{provider}.json").write_text(json.dumps(manifest), encoding="utf-8")
        return catalog_ingest._supplemental_provider_models_and_endpoints()

    return run


def test_a_priced_chat_row_is_billed_at_its_marked_up_prices(ingest: Ingest) -> None:
    models, endpoints = ingest("novita", _chat_row(**PRICES))

    assert set(models) == {MODEL_ID}
    assert set(endpoints) == {f"{MODEL_ID}@novita/prepaid", f"{MODEL_ID}@novita/byok"}
    prepaid = endpoints[f"{MODEL_ID}@novita/prepaid"]
    assert prepaid.prompt_price_microdollars_per_million_tokens == 105_500
    assert prepaid.completion_price_microdollars_per_million_tokens == 422_000


@pytest.mark.parametrize(
    "prices",
    [
        {},
        {"output_token_price_per_m": 400_000},
        {"input_token_price_per_m": 100_000},
        {**PRICES, "input_token_price_per_m": None},
        {**PRICES, "input_token_price_per_m": "unknown"},
        {**PRICES, "input_token_price_per_m": True},
        {**PRICES, "output_token_price_per_m": -1},
        {**PRICES, "output_token_price_per_m": 1.5},
    ],
    ids=[
        "both-missing",
        "input-missing",
        "output-missing",
        "input-null",
        "input-text",
        "input-bool",
        "output-negative",
        "output-fractional",
    ],
)
def test_a_chat_row_without_both_token_prices_gets_no_route(
    ingest: Ingest, caplog: pytest.LogCaptureFixture, prices: dict[str, Any]
) -> None:
    with caplog.at_level(logging.WARNING, logger=catalog_ingest.__name__):
        models, endpoints = ingest("novita", _chat_row(**prices))
        # Ingestion runs before Sentry starts, so it records; create_app reports.
        assert caplog.messages == []
        catalog_ingest.report_refused_manifest_rows()

    assert models == {}
    assert endpoints == {}
    assert caplog.messages == [REFUSAL]


def test_each_refused_row_is_reported_once(
    ingest: Ingest, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=catalog_ingest.__name__):
        ingest("novita", _chat_row())
        catalog_ingest.report_refused_manifest_rows()
        ingest("novita", _chat_row())
        catalog_ingest.report_refused_manifest_rows()

    assert caplog.messages == [REFUSAL]


def test_explicit_zero_token_prices_are_prices(ingest: Ingest) -> None:
    # Committed on 2026-09-28: openrouter stealth/union-alpha and together
    # prism-ml/ternary-bonsai-27b list 0/0, and bill at the customer floor.
    _models, endpoints = ingest(
        "novita", _chat_row(input_token_price_per_m=0, output_token_price_per_m=0)
    )

    prepaid = endpoints[f"{MODEL_ID}@novita/prepaid"]
    assert prepaid.prompt_price_microdollars_per_million_tokens == 10_000
    assert prepaid.completion_price_microdollars_per_million_tokens == 10_000


def test_a_per_image_row_needs_no_token_prices(ingest: Ingest) -> None:
    row = {
        "id": "acme/image",
        "upstream_id": "image",
        "model_type": "image",
        "endpoints": ["images"],
        "routable": True,
        "fixed_output_price_microdollars": 40_000,
    }

    models, endpoints = ingest("novita", row)

    assert set(models) == {"acme/image"}
    assert endpoints
    # The enclave bills a fixed hold per image, not tokens.
    prompt_prices = {e.prompt_price_microdollars_per_million_tokens for e in endpoints.values()}
    assert prompt_prices == {0}


def test_a_token_billed_openai_image_row_needs_both_token_prices(ingest: Ingest) -> None:
    row = {
        "id": sorted(OPENAI_IMAGE_MODEL_IDS)[0],
        "model_type": "image",
        "endpoints": ["images"],
        "routable": True,
    }

    assert ingest("openai", row) == ({}, {})
    _models, endpoints = ingest("openai", {**row, **PRICES})
    assert endpoints


def test_the_refusal_changes_no_priced_route_in_the_committed_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Differential: the committed manifests ingested with and without the
    # guard. It may only remove routes of rows it reported, and every route it
    # keeps must be billed exactly as before.
    monkeypatch.setattr(catalog_ingest, "_REFUSED_UNPRICED_ROWS", set())
    _models, guarded = catalog_ingest._supplemental_provider_models_and_endpoints()
    refused = set(catalog_ingest._REFUSED_UNPRICED_ROWS)
    monkeypatch.setattr(catalog_ingest, "_has_token_prices", lambda _row: True)
    _models, unguarded = catalog_ingest._supplemental_provider_models_and_endpoints()

    assert guarded
    assert guarded.keys() <= unguarded.keys()
    assert {key: unguarded[key] for key in guarded} == guarded
    removed = {(e.provider, e.model_id) for key, e in unguarded.items() if key not in guarded}
    assert removed <= refused


def test_create_app_reports_refused_rows_after_observability_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The catalog is ingested when trusted_router.main is imported, before
    # create_app starts Sentry, so a warning logged during ingestion never
    # reached Sentry Logs.
    events: list[str] = []
    monkeypatch.setattr(main, "init_sentry", lambda _settings: events.append("sentry"))
    monkeypatch.setattr(main, "init_axiom", lambda _settings: events.append("axiom"))
    monkeypatch.setattr(main, "report_refused_manifest_rows", lambda: events.append("report"))

    main.create_app(Settings(environment="test"), init_observability=True)

    assert events == ["sentry", "axiom", "report"]
