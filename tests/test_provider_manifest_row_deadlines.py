"""A row's own published price deadline ends only that model's routes.

Decision Models publishes an existing-customer discount for s1-pro and
s1-pro-2 that ends at a fixed instant. Those rows must fail closed then (the
discounted price is never charged after it ends), while every other route of
the provider keeps the provider's ordinary manifest deadline.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from scripts.pricing.providers import _system1models
from scripts.pricing.providers._system1models import System1Catalog
from tests.test_system1models_provider import catalog
from trusted_router import catalog_ingest
from trusted_router.catalog_data import ModelEndpoint

GENERATED = datetime(2026, 11, 11, 9, 42, 34, tzinfo=UTC)
CUTOVER = datetime(2026, 11, 12, tzinfo=UTC)
PROVIDER_DEADLINE = GENERATED + timedelta(days=14)
DISCOUNTED = ("s1-pro", "s1-pro-2")
LIST_PRICE = ("s1-fast", "s1-pro-3")


def _write_manifest(monkeypatch, tmp_path, tier: str) -> System1Catalog:
    """Run the real discovery writer on the day before the discount ends."""
    adapter = System1Catalog(tier)
    adapter.manifest_path = tmp_path / f"{adapter.slug}.json"
    payload = catalog(*LIST_PRICE, *DISCOUNTED)
    for item in payload["data"]:
        if item["id"] in DISCOUNTED:
            item["prices"]["USD"][tier] = {"eu": "0.084", "global": "0.080"}[tier]
    original = adapter.discover
    monkeypatch.setattr(adapter, "discover", lambda value: original(value, today=date(2026, 11, 11)))
    monkeypatch.setattr(_system1models, "fetch_json", lambda *_a: payload)
    monkeypatch.setattr(adapter, "probe", lambda *_a: True)
    monkeypatch.setenv(adapter.key_env, "test")
    adapter.write_provider_manifest(adapter.fetch())
    raw = json.loads(adapter.manifest_path.read_text())
    raw["generated_at"] = GENERATED.isoformat().replace("+00:00", "Z")
    adapter.manifest_path.write_text(json.dumps(raw))
    return adapter


def _edit_manifest(adapter: System1Catalog, edit) -> None:
    raw = json.loads(adapter.manifest_path.read_text())
    edit(raw)
    adapter.manifest_path.write_text(json.dumps(raw))


def _endpoints(monkeypatch, tmp_path) -> dict[str, ModelEndpoint]:
    """Every endpoint kind for the manifests in tmp_path, after the final expiry pass."""
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    _, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    assert endpoints and all(e.usage_type == "Credits" for e in endpoints.values())
    # Decision Models has no BYOK route today; a BYOK route of the same model
    # (or one installed statically later) must expire with its Credits route.
    byok = {
        e.id.replace("/prepaid", "/byok"): replace(
            e, id=e.id.replace("/prepaid", "/byok"), usage_type="BYOK", catalog_valid_until=None,
        )
        for e in endpoints.values()
    }
    unrelated = ModelEndpoint("openai/gpt-x@openai/prepaid", "openai/gpt-x", "openai", "Credits")
    return catalog_ingest._apply_provider_manifest_expiry(
        {**endpoints, **byok, unrelated.id: unrelated}
    )


def _routes(endpoints: dict[str, ModelEndpoint], slug: str, model: str) -> list[ModelEndpoint]:
    routes = [e for e in endpoints.values() if e.model_id == f"{slug}/{model}"]
    assert sorted(e.usage_type for e in routes) == ["BYOK", "Credits"], (slug, model)
    return routes


@pytest.mark.parametrize("tier", ["global", "eu"])
def test_only_the_discounted_models_stop_routing_at_the_cutover(monkeypatch, tmp_path, tier):
    adapter = _write_manifest(monkeypatch, tmp_path, tier)
    endpoints = _endpoints(monkeypatch, tmp_path)
    slug = adapter.slug
    before, at = CUTOVER - timedelta(seconds=1), CUTOVER
    for model in DISCOUNTED:
        for route in _routes(endpoints, slug, model):
            assert route.catalog_valid_until == CUTOVER
            assert route.catalog_is_current(at=before)
            assert not route.catalog_is_current(at=at), route.id
    for model in LIST_PRICE:
        for route in _routes(endpoints, slug, model):
            assert route.catalog_valid_until == PROVIDER_DEADLINE
            assert route.catalog_is_current(at=at), route.id
            assert not route.catalog_is_current(at=PROVIDER_DEADLINE), route.id
    assert endpoints["openai/gpt-x@openai/prepaid"].catalog_valid_until is None


@pytest.mark.parametrize(
    "bad", ["2026-11-12T00:00:00", "tomorrow", 1_762_905_600, True, {"at": "2026-11-12"}]
)
def test_a_malformed_row_deadline_fails_only_that_row_closed(monkeypatch, tmp_path, bad):
    from trusted_router.provider_manifest_policy import EXPIRED_PROVIDER_MANIFEST

    adapter = _write_manifest(monkeypatch, tmp_path, "global")

    def corrupt(raw):
        for row in raw["models"]:
            if row["id"].endswith("/s1-pro-3"):
                row["pricing_valid_until"] = bad

    _edit_manifest(adapter, corrupt)
    endpoints = _endpoints(monkeypatch, tmp_path)
    for route in _routes(endpoints, adapter.slug, "s1-pro-3"):
        assert route.catalog_valid_until == EXPIRED_PROVIDER_MANIFEST
        assert not route.catalog_is_current(at=GENERATED)
    for route in _routes(endpoints, adapter.slug, "s1-fast"):
        assert route.catalog_valid_until == PROVIDER_DEADLINE


def test_a_manifest_level_deadline_still_ends_every_route_of_its_provider(monkeypatch, tmp_path):
    # hixtube and upstage publish one provider-wide price deadline. It still
    # caps every route of that provider, and the earlier row deadline wins.
    adapter = _write_manifest(monkeypatch, tmp_path, "eu")
    provider_wide = datetime(2026, 11, 11, 18, 30, tzinfo=UTC)
    _edit_manifest(adapter, lambda raw: raw.update(pricing_valid_until="2026-11-11T18:30:00Z"))
    endpoints = _endpoints(monkeypatch, tmp_path)
    for model in (*DISCOUNTED, *LIST_PRICE):
        for route in _routes(endpoints, adapter.slug, model):
            assert route.catalog_valid_until == provider_wide
    later = "2026-11-20T00:00:00Z"
    _edit_manifest(adapter, lambda raw: raw.update(pricing_valid_until=later))
    endpoints = _endpoints(monkeypatch, tmp_path)
    for route in _routes(endpoints, adapter.slug, "s1-pro"):
        assert route.catalog_valid_until == CUTOVER
    for route in _routes(endpoints, adapter.slug, "s1-fast"):
        assert route.catalog_valid_until == datetime(2026, 11, 20, tzinfo=UTC)


def test_the_routing_snapshot_honours_a_row_deadline(monkeypatch, tmp_path):
    from trusted_router.routing_snapshot import _endpoint_entry, publication_window_as_of

    adapter = _write_manifest(monkeypatch, tmp_path, "global")
    # Off the hour, so only the row deadline itself can start a window.
    row_deadline = datetime(2026, 11, 11, 23, 30, tzinfo=UTC)

    def move(raw):
        for row in raw["models"]:
            if row.get("pricing_valid_until"):
                row["pricing_valid_until"] = row_deadline.isoformat()

    _edit_manifest(adapter, move)
    endpoints = _endpoints(monkeypatch, tmp_path)
    seconds = int(row_deadline.timestamp())
    hour = seconds - seconds % 3_600
    # The publisher's as-of windows end at the row deadline.
    assert publication_window_as_of(seconds - 1) == (hour, seconds)
    assert publication_window_as_of(seconds) == (seconds, hour + 3_600)
    # Each published entry carries its own deadline: the discounted routes
    # stop at it, the provider's other routes keep serving.
    starts = [hour]
    for model in DISCOUNTED:
        for route in _routes(endpoints, adapter.slug, model):
            assert _endpoint_entry(route, starts).catalog_valid_until == seconds
    for route in _routes(endpoints, adapter.slug, "s1-fast"):
        entry = _endpoint_entry(route, starts)
        assert entry.catalog_valid_until == int(PROVIDER_DEADLINE.timestamp())
