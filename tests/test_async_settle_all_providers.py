"""Async settlement takes every provider's Credits endpoint, and what it leaves out.

The billing snapshot once accepted only a direct OpenAI or Anthropic endpoint
(a provider list, and a model id under its provider), and authorize marked
every other requested model name ``partner``. Those two rules also stood in for
exclusions nothing else stated. With every provider accepted, each is stated
on its own and held here:

* a custom or user-provided model: retired at authorize's door (410, before any
  hold), and excluded by the authorization's own creator ids for any row that
  carries them;
* a TrustedRouter program, meta-router or private alias, and the partner
  model: excluded by the requested name (``names_program_model``);
* an endpoint whose provider reports its own price tier basis: excluded by its
  provider and model (``private_tier_basis``).
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException

from tests.test_async_settle_admission import request
from tests.test_async_settle_ticket import authorization, endpoint, runtime, settings
from tests.test_gateway_authorize_spanner_operations import _body, _seed_typed_gateway_store
from tests.test_spanner_batch_dml import _state
from trusted_router import billing_snapshot as billing
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, ModelEndpoint
from trusted_router.catalog_data import (
    META_MODEL_IDS,
    PARASAIL_LIBERTY_2_0_MODEL_ID,
    PRIVATE_PROXY_MODEL_TARGETS,
    Model,
)
from trusted_router.pricing import PriceTier
from trusted_router.provider_contracts import (
    PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS,
    SAKANA_FUGU_MODEL_ID,
)
from trusted_router.retired_models import RETIRED_MODEL_PREFIXES
from trusted_router.routes.internal import gateway
from trusted_router.routing import ROUTING_MODEL_ALIAS_TARGETS, canonical_model_id
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.async_settle import (
    PROGRAM_MODEL_PREFIXES,
    authorize_additions,
    names_program_model,
)

NAMESPACE = "trustedrouter/"
FIXTURE_ALIAS = NAMESPACE + "fixture-alias"

FALSE_ONLY = {"async_eligible": False}


def _route(model_id: str, provider: str, **changes: Any) -> ModelEndpoint:
    return endpoint(id=f"{model_id}@{provider}/prepaid", model_id=model_id, provider=provider, **changes)


def _additions(routes: list[ModelEndpoint], *, model: str | None = None, auth: Any = None,
               **body_changes: Any) -> dict[str, Any]:
    """authorize_additions as the route calls it, admission on for the workspace."""
    authorize_body = GatewayAuthorizeRequest(
        api_key_hash="key-v1", model=model or routes[0].model_id, route_type="chat.completions", **body_changes)
    return authorize_additions(
        request=request(runtime()), body=authorize_body, authorization=auth or authorization(),
        endpoints=routes, settings=settings(), typed=True, federated=False, replay=False, effective_at=None)


# --------------------------------------------------- every provider is taken --

@pytest.mark.parametrize(("model_id", "provider", "convention"), [
    ("openai/billing-v1", "openai", "includes_cache"),
    ("anthropic/billing-v1", "anthropic", "excludes_cache"),
    # A reseller of another publisher's model: the convention is the rule's
    # over the provider slug and the model id. amazon-bedrock and
    # google-vertex relay Anthropic-native usage for an anthropic/ model.
    ("openai/billing-v1", "azure", "includes_cache"),
    ("anthropic/billing-v1", "amazon-bedrock", "excludes_cache"),
    ("anthropic/billing-v1", "google-vertex", "excludes_cache"),
    ("openai/billing-v1", "amazon-bedrock", "includes_cache"),
    ("anthropic/billing-v1", "together", "includes_cache"),
    ("deepseek/billing-v1", "venice", "includes_cache"),
    # A publisher serving its own model, neither OpenAI nor Anthropic.
    ("deepseek/billing-v1", "deepseek", "includes_cache"),
    ("google/billing-v1", "google-ai-studio", "includes_cache"),
])
def test_an_ordinary_model_of_any_publisher_on_any_provider_is_eligible(
    model_id: str, provider: str, convention: str,
) -> None:
    result = _additions([_route(model_id, provider)])
    assert list(result) == ["async_eligible", "billing_snapshot", "billing_snapshot_hash", "settlement_ticket",
                            "settlement_status_url"]
    assert result["async_eligible"] is True
    (candidate,) = result["billing_snapshot"]["candidates"]
    assert (candidate["endpoint_id"], candidate["provider"], candidate["model_id"]) == (
        f"{model_id}@{provider}/prepaid", provider, model_id)
    assert candidate["prompt_convention"] == convention
    snapshot = billing.parse_snapshot(billing.canonical_bytes(billing.BillingSnapshot.model_validate(
        result["billing_snapshot"])))
    assert billing.canonical_hash(snapshot) == result["billing_snapshot_hash"]


def test_candidates_of_several_providers_freeze_into_one_snapshot() -> None:
    tiers = (PriceTier(200_000, 3_000_000, 15_000_000, 300_000), PriceTier(None, 6_000_000, 22_500_000, 600_000))
    routes = [
        _route("openai/billing-v1", "azure"), _route("anthropic/billing-v1", "anthropic"),
        _route("anthropic/billing-v1", "amazon-bedrock", price_tiers=tiers,
               prompt_price_microdollars_per_million_tokens=3_000_000,
               completion_price_microdollars_per_million_tokens=15_000_000),
    ]
    result = _additions(routes, model="anthropic/billing-v1", models=["openai/billing-v1"])
    assert result["async_eligible"] is True
    # Sorted by endpoint id, each with the rule's convention for its provider
    # and model (amazon-bedrock serving the anthropic/ model is exclusive).
    assert [(c["provider"], c["prompt_convention"], len(c["tiers"])) for c in
            result["billing_snapshot"]["candidates"]] == [
        ("amazon-bedrock", "excludes_cache", 2), ("anthropic", "excludes_cache", 0), ("azure", "includes_cache", 0)]
    bedrock = result["billing_snapshot"]["candidates"][0]
    # The explicit cached rates are frozen as given, not the provider default.
    assert [t["rates"]["cached_input_micro_per_million"] for t in bedrock["tiers"]] == [300_000, 600_000]


@pytest.mark.parametrize("change", [
    dict(usage_type="BYOK"), dict(request_price_microdollars=1), dict(provider="not a slug"),
    dict(prompt_price_microdollars_per_million_tokens=0.5),
])
def test_one_refused_candidate_still_refuses_the_whole_snapshot(change: dict[str, Any]) -> None:
    good = _route("deepseek/billing-v1", "venice")
    assert _additions([good])["async_eligible"] is True
    assert _additions([good, replace(_route("deepseek/billing-v1", "deepinfra"), **change)]) == FALSE_ONLY


# ------------------------------------------------- programs and aliases stay --

PROGRAM_NAMES = [
    "trustedrouter/auto", "trustedrouter/auto:nitro", "trustedrouter/auto-routing", "trustedrouter/cheap",
    "trustedrouter/zdr", "trustedrouter/liberty-4.0", "trustedrouter/prometheus", "trustedrouter/fusion",
    "trustedrouter/polyphemus-1.0", "trustedrouter/trev-1.0", "trustedrouter/monitor",
    "trustedrouter/not-in-the-catalog", "nyte/auto", "nyte/auto-routing:floor", "nyte/liberty-4.0",
    PARASAIL_LIBERTY_2_0_MODEL_ID, PARASAIL_LIBERTY_2_0_MODEL_ID + ":nitro",
    PARASAIL_LIBERTY_2_0_MODEL_ID + "-2026-01-01", "TrustedRouter/auto", "NYTE/Auto", " trustedrouter/auto ",
    "Parasail/Liberty-2.0",
]
ORDINARY_NAMES = [
    "openai/gpt-5.5", "openai/gpt-5.5:nitro", "openai/gpt-5.5-2026-04-23", "anthropic/claude-haiku-4.5",
    "anthropic/claude-haiku-4.5:floor", "deepseek/deepseek-v4-flash", "z-ai/glm-5.3:nitro",
    "google/gemini-2.5-flash", "meta-llama/llama-3.1-8b-instruct", "Qwen/a-model", "gpt-5.5",
    "system1models/a-model", "parasail/another-model", "trusted/router", "trustedrouterx/auto",
]


@pytest.mark.parametrize("name", PROGRAM_NAMES)
def test_a_program_alias_or_partner_name_keeps_the_synchronous_settle(name: str) -> None:
    route = _route("deepseek/billing-v1", "venice")
    assert names_program_model(name)
    # Named as the model, or among its fallbacks.
    assert _additions([route], model=name) == FALSE_ONLY
    assert _additions([route], models=[name]) == FALSE_ONLY
    assert _additions([route], models=["openai/billing-v1", name]) == FALSE_ONLY


@pytest.mark.parametrize("name", ORDINARY_NAMES)
def test_an_ordinary_name_is_not_a_program(name: str) -> None:
    route = _route("deepseek/billing-v1", "venice")
    assert not names_program_model(name)
    assert _additions([route], model=name)["async_eligible"] is True
    assert _additions([route], models=[name])["async_eligible"] is True


def test_every_name_the_retired_rule_admitted_is_still_admitted() -> None:
    """The retired rule admitted exactly the names under openai/ and
    anthropic/. None of them is a program name, so no request loses async
    settlement to this rule."""
    admitted = [name for name in (*MODELS, *ORDINARY_NAMES, *PROGRAM_NAMES)
                if name.startswith(("openai/", "anthropic/"))]
    assert len(admitted) >= 20
    for name in admitted:
        for spelling in (name, name + ":nitro", name + "-2026-01-01"):
            assert not names_program_model(spelling), spelling


def test_the_program_namespace_holds_every_program_the_catalog_knows() -> None:
    """The rule reads a name; the catalog says which models are programs. Every
    meta-router, orchestration program, private alias and internal model the
    catalog lists is under the namespace, save the partner model the rule
    names; no ordinary catalog model is. A program added outside the
    namespace fails here, not silently in production."""
    assert PROGRAM_MODEL_PREFIXES == (NAMESPACE, PARASAIL_LIBERTY_2_0_MODEL_ID)
    programs = (
        set(META_MODEL_IDS) | set(PRIVATE_PROXY_MODEL_TARGETS)
        | {model.id for model in MODELS.values() if model.internal_only}
        | {model_id for model_id in MODELS if model_id.startswith(NAMESPACE)}
    )
    assert len(programs) >= 80
    assert {name for name in programs if not name.startswith(NAMESPACE)} == {PARASAIL_LIBERTY_2_0_MODEL_ID}
    for name in programs:
        spellings = [name, name + ":nitro", name + ":floor", name + "-2026-01-01"]
        if name.startswith(NAMESPACE):
            spellings.append("nyte/" + name.removeprefix(NAMESPACE))
        for spelling in spellings:
            assert names_program_model(spelling), spelling
    ordinary = set(MODELS) - programs
    assert len(ordinary) >= 300
    for name in ordinary:
        assert not names_program_model(name), name
    # Whatever routing resolves a spelling to: if that is a program, the
    # spelling itself is one. Aliases, variant suffixes, dated and bare names.
    spellings = {*MODELS, *ROUTING_MODEL_ALIAS_TARGETS, *ROUTING_MODEL_ALIAS_TARGETS.values(),
                 *PROGRAM_NAMES, *ORDINARY_NAMES}
    spellings |= {name.removeprefix("openai/") for name in MODELS if name.startswith("openai/")}
    spellings |= {name + suffix for name in tuple(spellings) for suffix in (":nitro", ":floor", "-2026-01-01")}
    resolved_programs = 0
    for spelling in spellings:
        if canonical_model_id(spelling) in programs:
            resolved_programs += 1
            assert names_program_model(spelling), spelling
    assert resolved_programs >= len(programs)


# ------------------------------------------ custom and user-provided models --

@pytest.mark.parametrize("creator", [
    dict(custom_model_id="cm-1"), dict(user_provided_model_id="um-1"),
    dict(custom_model_id="cm-1", custom_model_markup_basis_points=500),
    dict(custom_model_markup_basis_points=500),
])
@pytest.mark.parametrize(("model_id", "provider"), [
    ("openai/billing-v1", "openai"), ("deepseek/billing-v1", "venice"), ("anthropic/billing-v1", "amazon-bedrock"),
])
def test_an_authorization_carrying_a_creator_identity_is_never_eligible(
    creator: dict[str, Any], model_id: str, provider: str,
) -> None:
    """The facts come from the authorization row, whatever the provider: a
    row with a custom or user-provided model id (one written before those
    were retired) answers false only."""
    route = _route(model_id, provider)
    assert _additions([route])["async_eligible"] is True
    auth = authorization()
    for name, value in creator.items():
        setattr(auth, name, value)
    assert _additions([route], auth=auth) == FALSE_ONLY


def _authorize(seeded: Any, model: str, **fields: Any) -> dict[str, Any]:
    """The authorize route's own function, async-v1 requested, with these
    body members (declared or not, as an older enclave sent them)."""
    _store, _db, key = seeded
    authorize_body = GatewayAuthorizeRequest.model_validate({
        **_body(key.hash).model_dump(exclude_none=True), "model": model, "route_type": "chat.completions",
        "invocation_nonce": "nonce-all-providers", **fields})
    data: dict[str, Any] = gateway._authorize_gateway_sync(
        request(runtime()), authorize_body, settings())["data"]
    return data


@pytest.fixture
def seeded() -> Any:
    return _seed_typed_gateway_store()


@pytest.fixture
def resold_catalog(monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[str]]:
    """One ordinary model of another publisher on two resellers, fixture-owned
    so that no catalog refresh, hold or retirement moves the candidates."""
    model = "fixture-lab/all-providers-model"
    assert model not in MODELS and not names_program_model(model)
    monkeypatch.setitem(MODELS, model, Model(
        id=model, name="All providers fixture", provider="fixture-lab", context_length=1_000_000,
        supported_parameters=("max_tokens",), prepaid_available=True))
    ids = []
    for provider, prompt in (("deepinfra", 1_000_000), ("together", 1_100_000)):
        route = ModelEndpoint(
            id=f"{model}@{provider}/prepaid", model_id=model, provider=provider, usage_type="Credits",
            upstream_id="all-providers-fixture", supported_parameters=("max_tokens",),
            prompt_price_microdollars_per_million_tokens=prompt,
            completion_price_microdollars_per_million_tokens=3_000_000,
        )
        monkeypatch.setitem(MODEL_ENDPOINTS, route.id, route)
        ids.append(route.id)
    # A program-namespace alias of that model, pinned too: the same candidates
    # as the ordinary name, by a name the rule leaves out.
    monkeypatch.setitem(ROUTING_MODEL_ALIAS_TARGETS, FIXTURE_ALIAS, model)
    return model, ids


def test_authorize_freezes_another_publishers_model_on_its_resellers(
    seeded: Any, resold_catalog: tuple[str, list[str]],
) -> None:
    """Through the authorize route itself: the snapshot lists every candidate
    the authorization holds, and the stored row carries no creator identity."""
    store, _db, _key = seeded
    model, endpoint_ids = resold_catalog
    data = _authorize(seeded, model)
    assert data["async_eligible"] is True and data["invocation_nonce"] == "nonce-all-providers"
    candidates = data["billing_snapshot"]["candidates"]
    assert [c["endpoint_id"] for c in candidates] == sorted(endpoint_ids)
    assert {c["provider"] for c in candidates} == {"deepinfra", "together"}
    assert {(c["model_id"], c["prompt_convention"]) for c in candidates} == {(model, "includes_cache")}
    row = store.get_gateway_authorization(data["authorization_id"])
    assert row is not None and sorted(row.candidate_endpoint_ids) == sorted(endpoint_ids)
    assert row.custom_model_id is None and row.user_provided_model_id is None
    assert row.custom_model_markup_basis_points == 0


@pytest.mark.parametrize("fields", [
    dict(custom_model_id="cm-1"), dict(custom_model_revision=1), dict(user_provided_model_id="um-1"),
    dict(user_provided_model_revision=1),
])
def test_a_creator_identity_is_refused_before_any_hold(
    seeded: Any, resold_catalog: tuple[str, list[str]], fields: dict[str, Any],
) -> None:
    _store, db, _key = seeded
    model, _ids = resold_catalog
    before = copy.deepcopy(_state(db))
    with pytest.raises(HTTPException) as refused:
        _authorize(seeded, model, **fields)
    assert refused.value.status_code == 410
    assert _state(db) == before and not db.gateway_authorizations


@pytest.mark.parametrize("prefix", RETIRED_MODEL_PREFIXES)
@pytest.mark.parametrize("position", ["model", "fallback"])
def test_a_retired_creator_model_name_is_refused_before_any_hold(
    seeded: Any, resold_catalog: tuple[str, list[str]], prefix: str, position: str,
) -> None:
    _store, db, _key = seeded
    model, _ids = resold_catalog
    before = copy.deepcopy(_state(db))
    with pytest.raises(HTTPException) as refused:
        if position == "model":
            _authorize(seeded, prefix + "x")
        else:
            _authorize(seeded, model, models=[prefix + "x"])
    assert refused.value.status_code == 410
    assert _state(db) == before and not db.gateway_authorizations


def test_a_replay_of_a_creator_authorization_is_refused_not_refrozen(
    seeded: Any, resold_catalog: tuple[str, list[str]],
) -> None:
    """A row written while creator models existed carries its ids; replaying
    it answers 410, and an ordinary replay answers with no frozen prices."""
    store, db, _key = seeded
    model, _ids = resold_catalog
    first = _authorize(seeded, model)
    assert first["async_eligible"] is True
    replay = _authorize(seeded, model)
    assert replay["idempotent_replay"] is True and replay["authorization_id"] == first["authorization_id"]
    assert replay["async_eligible"] is False and "billing_snapshot" not in replay
    row = db.gateway_authorizations[first["authorization_id"]]
    row["payload"] = json.dumps({**json.loads(row["payload"]), "custom_model_id": "cm-legacy"},
                                separators=(",", ":"))
    assert store.get_gateway_authorization(first["authorization_id"]).custom_model_id == "cm-legacy"
    with pytest.raises(HTTPException) as refused:
        _authorize(seeded, model)
    assert refused.value.status_code == 410


@pytest.mark.parametrize("name", [
    "TrustedRouter/fixture-alias", "TRUSTEDROUTER/FIXTURE-ALIAS", "NYTE/fixture-alias", "Nyte/fixture-alias",
    "TrustedRouter/auto", "Parasail/Liberty-2.0", "PARASAIL/LIBERTY-2.0",
])
@pytest.mark.parametrize("position", ["model", "fallback"])
def test_a_program_name_in_another_case_is_one_and_does_not_authorize(
    seeded: Any, resold_catalog: tuple[str, list[str]], name: str, position: str,
) -> None:
    """The rule folds letter case, as the native Batch rule and the partner
    classifier it shares its predicates with do. Routing does not: such a
    spelling resolves to no model, as the model or as a fallback, so there is
    no authorization for an exact comparison to have missed either."""
    _store, db, _key = seeded
    model, _ids = resold_catalog
    assert names_program_model(name)
    with pytest.raises(HTTPException) as refused:
        if position == "model":
            _authorize(seeded, name)
        else:
            _authorize(seeded, model, models=[name])
    assert refused.value.status_code == 400
    assert not db.gateway_authorizations


@pytest.mark.parametrize("name", [FIXTURE_ALIAS, "nyte/fixture-alias", FIXTURE_ALIAS + ":nitro"])
@pytest.mark.parametrize("position", ["model", "fallback"])
def test_authorize_answers_a_program_name_false_only(
    seeded: Any, resold_catalog: tuple[str, list[str]], name: str, position: str,
) -> None:
    """Through the authorize route: an alias in the program namespace
    authorizes the very candidates the ordinary name does, and its async-v1
    answer is false only, named as the model or as a fallback."""
    store, db, _key = seeded
    model, endpoint_ids = resold_catalog
    assert names_program_model(name)
    data = _authorize(seeded, name) if position == "model" else _authorize(seeded, model, models=[name])
    assert len(db.gateway_authorizations) == 1
    row = store.get_gateway_authorization(data["authorization_id"])
    assert row is not None and sorted(row.candidate_endpoint_ids) == sorted(endpoint_ids)
    assert data["async_eligible"] is False
    assert "billing_snapshot" not in data and "settlement_ticket" not in data


def test_the_partner_model_does_not_authorize_on_an_async_route(
    seeded: Any, resold_catalog: tuple[str, list[str]],
) -> None:
    _store, db, _key = seeded
    with pytest.raises(HTTPException) as refused:
        _authorize(seeded, PARASAIL_LIBERTY_2_0_MODEL_ID)
    assert refused.value.status_code == 400 and not db.gateway_authorizations


# ---------------------------------------------------- a private tier basis --

def test_an_endpoint_with_a_private_tier_basis_keeps_the_synchronous_settle() -> None:
    assert PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS == {("sakana", SAKANA_FUGU_MODEL_ID)}
    tiers = (PriceTier(272_000, 5_000_000, 30_000_000, 500_000), PriceTier(None, 10_000_000, 45_000_000, 1_000_000))
    fugu = _route(SAKANA_FUGU_MODEL_ID, "sakana", price_tiers=tiers)
    ordinary = _route("deepseek/billing-v1", "venice")
    # The snapshot itself would build: only the fact keeps it out.
    assert billing.build_snapshot([fugu], billing.Eligibility()).candidates[0].provider == "sakana"
    assert _additions([fugu]) == FALSE_ONLY
    # One such candidate among others excludes the request.
    assert _additions([ordinary, fugu], model="deepseek/billing-v1") == FALSE_ONLY
    assert _additions([ordinary], model="deepseek/billing-v1")["async_eligible"] is True
    # The pair, not the provider or the model alone.
    assert _additions([_route("sakana-ai/another-model", "sakana")])["async_eligible"] is True
    assert _additions([_route(SAKANA_FUGU_MODEL_ID, "venice")])["async_eligible"] is True
