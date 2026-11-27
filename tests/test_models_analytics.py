from __future__ import annotations

import json
import logging
import queue
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.axiom_growth import model
from trusted_router.axiom_config import _DroppingQueueHandler
from trusted_router.main import _ApplicationConsoleFormatter

EVENTS = [
    ("models.search_used", {}),
    ("models.search_zero_results", {}),
    ("models.popular_pick_clicked", {"pick": "deepseek"}),
    ("models.filter_changed", {"filter": "zdr"}),
    ("models.sort_changed", {"sort": "cached"}),
    ("models.model_clicked", {"model_id": "trustedrouter/auto"}),
    ("models.provider_clicked", {"model_id": "trustedrouter/auto", "provider_id": "openai"}),
    ("models.comparison_expanded", {"model_id": "trustedrouter/auto"}),
]


@pytest.mark.parametrize(("event", "catalog"), EVENTS)
def test_catalog_event_survives_attribution_scrubbing_and_export(client, caplog, event, catalog):
    client.get("/models?utm_source=catalog-test&q=private-query-canary")
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    response = client.post("/analytics/events", json={"event": event, "catalog": catalog})
    assert response.status_code == 204
    record = next(r for r in caplog.records if r.getMessage() == f"acquisition.{event}")
    prepared = _DroppingQueueHandler(queue.Queue()).prepare(record)
    cloud = json.loads(_ApplicationConsoleFormatter().format(prepared))
    projected = model.project_event({**cloud, "_time": "2026-11-27T12:00:00Z"}, source="cloud_logging")
    assert projected["anonymous_fingerprint"] == record.anonymous_fingerprint
    assert projected["page_path"] == "/models"
    for key, value in catalog.items():
        assert projected[f"catalog_{key}"] == value
    assert "private-query-canary" not in json.dumps(prepared.__dict__)
    assert "private-query-canary" not in json.dumps(projected)


@pytest.mark.parametrize("body", [
    {"event": "models.search_used", "query": "private-query"},
    {"event": "models.search_used", "catalog": {"query": "private-query"}},
    {"event": "models.search_used", "catalog": {"model_id": "trustedrouter/auto"}},
    {"event": "models.model_clicked", "catalog": {"model_id": "private-query"}},
    {"event": "models.provider_clicked", "catalog": {"model_id": "trustedrouter/auto", "provider_id": "private-query"}},
    {"event": "models.filter_changed", "catalog": {"filter": "private-query"}},
    {"event": "models.sort_changed", "catalog": {"sort": "private-query"}},
    {"event": "models.popular_pick_clicked", "catalog": {"pick": "private-query"}},
    {"event": "models.comparison_expanded"},
    {"event": "models.search_used", "elapsed_ms": 5},
    {"event": "home.catalog_filter", "catalog": {"filter": "zdr"}},
])
def test_catalog_rejects_unapproved_metadata(client: TestClient, body):
    assert client.post("/analytics/events", json=body).status_code == 400


@pytest.mark.parametrize("headers", [{"sec-gpc": "1"}, {"dnt": "1"}])
def test_catalog_privacy_signals(client, caplog, headers):
    client.get("/models")
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    for event, catalog in EVENTS:
        assert client.post("/analytics/events", json={"event": event, "catalog": catalog}, headers=headers).status_code == 204
    assert "acquisition.models." not in caplog.text


def test_catalog_no_cookie_no_new_identity(client, caplog):
    client.cookies.clear()
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    response = client.post("/analytics/events", json={"event": "models.search_used"})
    assert response.status_code == 204
    assert not response.cookies
    assert "acquisition.models." not in caplog.text


def test_catalog_names_all_exported():
    source = (Path(__file__).parents[1] / "src/trusted_router/static/models.js").read_text()
    sent = set(re.findall(r'track\("(models\.[a-z_]+)"', source))
    assert sent == {event for event, _ in EVENTS}
    assert {f"acquisition.{event}" for event in sent} <= model.BROWSER_EVENTS


def test_catalog_fingerprint_joins_existing_signup_and_activation(client, caplog):
    from trusted_router.acquisition import record_successful_api_call

    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    client.get("/models?utm_source=catalog-test")
    client.post("/analytics/events", json={"event": "models.search_used"})
    signup = client.post("/v1/signup", json={"email": "catalog-activation@example.com"})
    assert signup.status_code == 201
    record_successful_api_call(signup.json()["data"]["workspace_id"], model="test/model", provider="test")
    stages = {"acquisition.models.search_used", "acquisition.signup_completed", "acquisition.first_successful_api_call"}
    records = [r for r in caplog.records if r.getMessage() in stages]
    assert {r.getMessage() for r in records} == stages
    assert len({r.anonymous_fingerprint for r in records}) == 1


def test_catalog_controls_match_accepted_metadata(client):
    from typing import get_args
    from urllib.parse import parse_qs, urlsplit

    from bs4 import BeautifulSoup

    from trusted_router.routes.acquisition import CatalogEventDetails, MarketingEventRequest

    page = BeautifulSoup(client.get("/models").text, "html.parser")
    controls = {
        "pick": ("[data-model-query]", "data-model-query", "models.popular_pick_clicked"),
        "filter": (".model-filter-row a", "href", "models.filter_changed"),
        "sort": ("[data-model-sort] option", "value", "models.sort_changed"),
    }
    for field, (selector, attribute, event) in controls.items():
        values = {control[attribute] for control in page.select(selector)}
        if field == "filter":
            values = {parse_qs(urlsplit(value).query).get("filter", ["all"])[0] for value in values}
        annotation = CatalogEventDetails.model_fields[field].annotation
        literal = next(arg for arg in get_args(annotation) if arg is not type(None))
        assert values == set(get_args(literal))
        for value in values:
            MarketingEventRequest.model_validate({"event": event, "catalog": {field: value}})


@pytest.mark.parametrize("kind", ["model", "provider"])
def test_catalog_rejects_internal_identifiers(client, monkeypatch, kind):
    from dataclasses import replace

    from trusted_router.catalog import MODELS, PROVIDERS

    registry = MODELS if kind == "model" else PROVIDERS
    identifier = "trustedrouter/auto" if kind == "model" else "openai"
    monkeypatch.setitem(registry, identifier, replace(registry[identifier], internal_only=True))
    assert client.post("/analytics/events", json={
        "event": "models.provider_clicked",
        "catalog": {"model_id": "trustedrouter/auto", "provider_id": "openai"},
    }).status_code == 400
