"""Every announced provider retirement, through every generic path it must close.

One test per path, parametrized over provider_lifecycle._RETIREMENTS, so a new
retirement is covered the day it is recorded, with no hand-written test:

* the predicate: a route is retired from its record's instant, not a
  microsecond earlier, by canonical id or by upstream id alone, and only for
  that record's provider; a replacement the record names is not retired then;
* the runtime catalog: endpoints_for_model drops the provider's routes (prepaid
  and BYOK) at the instant without a catalog reload, keeps another host's, and
  never substitutes another model or upstream id;
* the hourly refresh: _index_provider_prices cannot index a retired route from
  a fresh provider result, and _keep_failed_snapshot_routes cannot revive one
  from the published snapshot of a provider whose refresh failed;
* manifest ingestion: _authoritative_provider_model_ids cannot admit a retired
  row that a stale provider manifest still lists;
* the catalog build: _filter_unserved_provider_endpoints drops a retired
  route even when it is registered explicitly.

Routes, prices and manifests here are fixtures built from the records, so the
tests do not move when a provider's feed does. Each path is checked against an
oracle read straight from the records (_records_retire), and each asserts the
routes are present just before the instant, so a path that drops everything
fails too. Provider-specific parsers (each fetch() and its feed format) keep
their own tests in the per-provider files.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from trusted_router import catalog, catalog_ingest, provider_lifecycle
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.provider_lifecycle import _RETIREMENTS, _Retirement

_TICK = timedelta(microseconds=1)
# A host no retirement names: it keeps serving every retired model.
_OTHER_HOST = "fixture-other-host"
# A model no retirement names: every path must keep it.
_SENTINEL = "fixture/never-retired-model"


def _retirement_id(retirement: _Retirement) -> str:
    index = _RETIREMENTS.index(retirement)
    return f"{index:03d}-{retirement.provider}-{retirement.effective_at:%Y%m%dT%H%M}"


_EACH_RETIREMENT = pytest.mark.parametrize("retirement", _RETIREMENTS, ids=_retirement_id)


def _records_retire(provider: str, model_id: str, upstream_id: str | None, at: datetime) -> bool:
    """Whether any record retires this route at `at`, read from the records alone."""
    return any(
        record.provider == provider
        and record.effective_at <= at
        and (model_id in record.model_ids or (upstream_id is not None and upstream_id in record.upstream_ids))
        for record in _RETIREMENTS
    )


def _routes(retirement: _Retirement) -> dict[str, str]:
    """model id -> upstream id for one fixture route per id the record names.

    A canonical id is served under a fixture upstream id, so only its canonical
    id can retire it; an upstream id is served under a fixture model id, so only
    its upstream id can. Replacements and the sentinel ride along as routes
    that must survive.
    """
    routes = {model_id: f"fixture-native/{model_id}" for model_id in retirement.model_ids}
    routes.update({f"fixture-model/{upstream_id}": upstream_id for upstream_id in retirement.upstream_ids})
    for model_id in (*retirement.replacement_model_ids, _SENTINEL):
        routes.setdefault(model_id, f"fixture-native/{model_id}")
    return routes


def _named(retirement: _Retirement) -> set[str]:
    """The fixture routes this record itself retires."""
    return {
        model_id for model_id, upstream_id in _routes(retirement).items()
        if model_id in retirement.model_ids or upstream_id in retirement.upstream_ids
    }


def _expected(retirement: _Retirement, at: datetime) -> set[str]:
    """The fixture routes the records leave in service at `at`."""
    return {
        model_id for model_id, upstream_id in _routes(retirement).items()
        if not _records_retire(retirement.provider, model_id, upstream_id, at)
    }


def _clock(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    monkeypatch.setattr(provider_lifecycle, "_utc_now", lambda: at)


@_EACH_RETIREMENT
def test_a_retirement_starts_at_its_instant_for_its_provider_only(retirement: _Retirement) -> None:
    retired = provider_lifecycle.provider_model_retired
    provider, cutover = retirement.provider, retirement.effective_at
    before = cutover - _TICK
    assert _named(retirement), f"{retirement.notice_id} names no route"
    # Every route this record names is still in service just before its
    # instant: no earlier record already retires it. Otherwise the record
    # does nothing, e.g. an extension added while the old record still
    # names the model, or a date mistyped later than an earlier record's.
    assert _named(retirement) <= _expected(retirement, before), retirement.notice_id
    for model_id in retirement.model_ids:
        assert retired(provider, model_id, at=cutover), model_id
        assert retired(provider, model_id, at=before) is _records_retire(provider, model_id, None, before), model_id
        assert not retired(_OTHER_HOST, model_id, at=cutover), model_id
    for upstream_id in retirement.upstream_ids:
        model_id = f"fixture-model/{upstream_id}"
        assert retired(provider, model_id, upstream_id, at=cutover), upstream_id
        assert retired(provider, model_id, upstream_id, at=before) is _records_retire(
            provider, model_id, upstream_id, before,
        ), upstream_id
        assert not retired(_OTHER_HOST, model_id, upstream_id, at=cutover), upstream_id
    assert not retired(provider, _SENTINEL, f"fixture-native/{_SENTINEL}", at=cutover)
    # A replacement the notice names is still served by that provider then.
    for replacement in retirement.replacement_model_ids:
        assert not retired(provider, replacement, at=cutover), replacement


@_EACH_RETIREMENT
def test_the_runtime_catalog_drops_only_the_retiring_hosts_routes_at_the_instant(
    monkeypatch: pytest.MonkeyPatch, retirement: _Retirement,
) -> None:
    endpoints: dict[str, ModelEndpoint] = {}
    for model_id, upstream_id in _routes(retirement).items():
        for host in (retirement.provider, _OTHER_HOST):
            for usage in ("Credits", "BYOK"):
                endpoint = ModelEndpoint(
                    id=f"{model_id}@{host}/{usage}", model_id=model_id,
                    provider=host, usage_type=usage, upstream_id=upstream_id,
                )
                endpoints[endpoint.id] = endpoint
    monkeypatch.setattr(catalog, "MODEL_ENDPOINTS", endpoints)

    def serving(model_id: str) -> list[tuple[str, str, str, str | None]]:
        return sorted(
            (e.provider, e.usage_type, e.model_id, e.upstream_id)
            for e in catalog.endpoints_for_model(model_id)
        )

    for at in (retirement.effective_at - _TICK, retirement.effective_at):
        _clock(monkeypatch, at)
        live = _expected(retirement, at)
        for model_id, upstream_id in _routes(retirement).items():
            # The surviving routes keep their own model and upstream ids: a
            # retirement never substitutes a replacement's weights.
            hosts = (retirement.provider, _OTHER_HOST) if model_id in live else (_OTHER_HOST,)
            expected = sorted(
                (host, usage, model_id, upstream_id) for host in hosts for usage in ("BYOK", "Credits")
            )
            assert serving(model_id) == expected, (at, model_id)
    assert not _named(retirement) & _expected(retirement, retirement.effective_at)


@_EACH_RETIREMENT
def test_the_hourly_refresh_cannot_index_a_retired_route(
    monkeypatch: pytest.MonkeyPatch, retirement: _Retirement,
) -> None:
    routes = _routes(retirement)
    provider = retirement.provider
    result_slug = refresh._result_slug_for_provider(provider)
    assert provider in refresh._PRICING_RESULT_PROVIDER_ALIASES.get(result_slug, (result_slug,))
    # The upstream ids come from the fixture routes, not a provider module's
    # map, so an upstream-only record is checked through the same lookup.
    monkeypatch.setattr(
        refresh, "_upstream_id_map_for", lambda slug: routes if slug == provider else {},
    )
    result = ProviderPricingResult(
        slug=result_slug, source="api",
        prices={model_id: ModelPrice(1_000_000, 2_000_000) for model_id in routes},
    )
    for at in (retirement.effective_at - _TICK, retirement.effective_at):
        _clock(monkeypatch, at)
        indexed = refresh._index_provider_prices({result_slug: result})
        assert {model_id for model_id, hosts in indexed.items() if provider in hosts} == _expected(retirement, at), at


@_EACH_RETIREMENT
def test_a_failed_refresh_cannot_revive_a_retired_route_from_the_published_snapshot(
    monkeypatch: pytest.MonkeyPatch, retirement: _Retirement,
) -> None:
    provider = retirement.provider
    pricing = {"prompt": "0.000001", "completion": "0.000002"}
    published = {"models": [
        {"id": model_id, "pricing": pricing, "endpoints": [
            {"tr_provider_slug": provider, "tag": "", "model_id": upstream_id, "pricing": pricing},
        ]}
        for model_id, upstream_id in sorted(_routes(retirement).items())
    ]}
    failed = {refresh._result_slug_for_provider(provider): []}
    for at in (retirement.effective_at - _TICK, retirement.effective_at):
        _clock(monkeypatch, at)
        merged: dict[str, object] = {"models": []}
        refresh._keep_failed_snapshot_routes(merged, json.loads(json.dumps(published)), failed)
        kept = {row["id"] for row in merged["models"]}  # type: ignore[union-attr]
        assert kept == _expected(retirement, at), at


@_EACH_RETIREMENT
def test_a_stale_manifest_cannot_admit_a_retired_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, retirement: _Retirement,
) -> None:
    provider = retirement.provider
    rows = [
        {"id": model_id, "upstream_id": upstream_id, "model_type": "chat",
         "endpoints": ["chat/completions"], "routable": True}
        for model_id, upstream_id in sorted(_routes(retirement).items())
    ]
    (tmp_path / f"{provider}.json").write_text(json.dumps({"provider": provider, "models": rows}))
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)
    # Operator holds are a separate gate; this checks the retirement alone.
    monkeypatch.setattr(catalog_ingest, "provider_model_operator_held", lambda *_args: False)
    for at in (retirement.effective_at - _TICK, retirement.effective_at):
        admitted = catalog_ingest._authoritative_provider_model_ids(provider, at=at)
        assert admitted & set(_routes(retirement)) == _expected(retirement, at), at


@_EACH_RETIREMENT
def test_the_catalog_build_drops_retired_routes_explicit_ones_included(
    monkeypatch: pytest.MonkeyPatch, retirement: _Retirement,
) -> None:
    # Every fixture route is registered explicitly, as an async media route is,
    # which skips the allowlists: the retirement must still end it, prepaid and
    # BYOK, and must leave another host's route alone.
    monkeypatch.setattr(catalog_ingest, "provider_model_operator_held", lambda *_args: False)
    monkeypatch.setattr(catalog_ingest, "PREPAID_PROVIDER_HOLD_REASONS", {})
    monkeypatch.setattr(catalog_ingest, "_provider_manifest_dark_model_ids", lambda: {})
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_SERVED_MODEL_ALLOWLIST", {})
    monkeypatch.setattr(catalog_ingest, "_AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS", frozenset())
    routes = _routes(retirement)
    endpoints = {
        f"{model_id}@{host}/{usage}": ModelEndpoint(
            id=f"{model_id}@{host}/{usage}", model_id=model_id,
            provider=host, usage_type=usage, upstream_id=upstream_id,
        )
        for model_id, upstream_id in routes.items()
        for host in (retirement.provider, _OTHER_HOST)
        for usage in ("Credits", "BYOK")
    }
    for at in (retirement.effective_at - _TICK, retirement.effective_at):
        kept = catalog_ingest._filter_unserved_provider_endpoints(
            endpoints, explicit_model_ids=frozenset(routes), at=at,
        )
        live = _expected(retirement, at)
        assert set(kept) == {
            endpoint_id for endpoint_id, endpoint in endpoints.items()
            if endpoint.provider == _OTHER_HOST or endpoint.model_id in live
        }, at
