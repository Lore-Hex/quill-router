from dataclasses import replace

import pytest

from trusted_router.catalog_aliases import canonical_listing_id, consolidate_model_routes
from trusted_router.catalog_data import Model, ModelEndpoint, model_origin_for_model_id


def test_stepfun_native_namespace_shares_reviewed_model_origin() -> None:
    origin = model_origin_for_model_id("stepfun/step-3.5-flash")
    assert origin is not None and origin.country == "CN"
    assert model_origin_for_model_id("stepfun-ai/step-3.5-flash") is origin
    assert model_origin_for_model_id("stepfun-ai/step-5-preview") is origin
    assert model_origin_for_model_id("unreviewed-stepfun/step-5-preview") is None


def test_discovered_qwen_listing_keeps_native_routes_and_existing_tariffs() -> None:
    native = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    canonical = "qwen/qwen3-30b-a3b-instruct-2507"
    models = {name: Model(name, name, "nebius", 1000) for name in (native, canonical)}
    old = ModelEndpoint(
        f"{native}@nebius/prepaid", native, "nebius", "Credits",
        upstream_id=native, prompt_price_microdollars_per_million_tokens=123,
        supported_parameters=("tools",),
    )
    existing = ModelEndpoint(
        f"{canonical}@featherless/prepaid", canonical, "featherless", "Credits",
        upstream_id=native, prompt_price_microdollars_per_million_tokens=456,
    )
    endpoints = {old.id: old, existing.id: existing}
    assert canonical_listing_id(native, models) == canonical
    assert canonical_listing_id(native, {native: models[native]}) == native
    assert canonical_listing_id(native + "-FP8", models) == native + "-FP8"
    consolidate_model_routes(models, endpoints)
    copied_id = f"{canonical}@nebius/prepaid"
    assert endpoints[copied_id] == replace(old, id=copied_id, model_id=canonical)
    assert endpoints[old.id] is old
    assert endpoints[existing.id] is existing
    before = dict(endpoints)
    consolidate_model_routes(models, endpoints)
    assert endpoints == before


@pytest.mark.parametrize("target_present", [False, True])
def test_qwen_public_listing_survives_until_its_target_exists(monkeypatch, target_present) -> None:
    from trusted_router.routes import catalog

    native = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    canonical = "qwen/qwen3-30b-a3b-instruct-2507"
    models = {native: Model(native, native, "nebius", 1000)}
    if target_present:
        models[canonical] = Model(canonical, canonical, "featherless", 1000)
    monkeypatch.setattr(catalog, "MODELS", models)
    monkeypatch.setattr(catalog, "model_to_openrouter_shape", lambda model: {
        "id": model.id, "pricing": {"prompt": "0", "completion": "0"}, "trustedrouter": {},
    })
    rows = catalog._public_catalog_payload.__wrapped__((0, "target-presence-regression")).shapes
    assert [row["id"] for row in rows] == [canonical if target_present else native]
