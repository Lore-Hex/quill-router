"""Async settlement never takes a request the settle path prices specially.

The synchronous settle (routes/internal/gateway.py,
_settle_gateway_authorization) prices an ordinary Credits request by the plain
endpoint formula, which a frozen billing snapshot reproduces
(tests/test_billing_snapshot_catalog.py). Every other way it prices a request
is a branch of that function, keyed on the authorization, the selected
endpoint or the settle body. Authorize answers an async-v1 request from the
requested facts (services/async_settle.requested_facts), each read from the
same field or predicate the branch reads.

Three things are held here:

* the settle function's pricing statements, and every authorization and
  endpoint attribute it reads, are enumerated from its source and each is
  assigned the requested fact that keeps it out of async settlement. A new
  branch or a new input fails until it is assigned one;
* each of those facts is true exactly where the settle path's own predicate
  is, and makes authorize answer false only;
* over the catalog: every model string a request can name, as its model or
  as a fallback, and every Credits endpoint an authorization can hold, is
  answered false only whenever the settle path's predicates or routing's own
  resolution say it is not an ordinary model on an ordinary endpoint.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fastapi import HTTPException

from tests.test_async_settle_admission import request
from tests.test_async_settle_ticket import authorization, endpoint, runtime, settings
from trusted_router import billing_snapshot as billing
from trusted_router import routing
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, ModelEndpoint
from trusted_router.catalog_data import (
    META_MODEL_IDS,
    PARASAIL_LIBERTY_2_0_MODEL_ID,
    PRIVATE_PROXY_MODEL_TARGETS,
)
from trusted_router.model_aliases import ROUTER_MODEL_NAMESPACE, names_router_model
from trusted_router.partner_billing import (
    PARASAIL_LIBERTY_2_0_IDEMPOTENCY_PREFIX,
    PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX,
    PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE,
)
from trusted_router.polyphemus import MODEL_ID as POLYPHEMUS_MODEL_ID
from trusted_router.pricing import PriceTier
from trusted_router.provider_contracts import PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.async_settle import (
    PROGRAM_MODEL_PREFIXES,
    authorize_additions,
    names_program_model,
    requested_facts,
)
from trusted_router.speech import SPEECH_MODELS
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.transcription import TRANSCRIPTION_MODELS
from trusted_router.types import UsageType
from trusted_router.web_fetch import FETCH_MODEL_ID

FALSE_ONLY = {"async_eligible": False}
ASYNC_ROUTES = ("chat.completions", "responses")

# How a pricing statement or an input is covered, when not by a requested fact.
PLAIN = "the plain endpoint formula, or an input every ordinary request has"
REFUND = "a refund is free in both paths"
IDENTITY = "identity or bookkeeping, never a price"

# The facts a request's exclusion reads (billing_snapshot.exclusion).
FACTS = frozenset(billing.Eligibility.model_fields)
# exclusion() names a fact as its reason, but for these two.
REASONS = {"usage_type": "non_credits", "authority": "settlement_authority"}


# ------------------------------------- the settle path's branches, from source --

class Statement(NamedTuple):
    names: tuple[str, ...]
    guards: tuple[str, ...]
    value: str


# The names the settle function computes its charge from.
PRICING_NAMES = frozenset({
    "actual_cost", "selected_endpoint", "price_tier_input_tokens", "service_tier", "video_cost",
    "user_model_pair", "partner_mode", "billing_snapshot", "additional_cost", "uncached_input",
    "total_input", "cache_read", "cache_creation", "output_tokens",
})


def _settle_function() -> ast.FunctionDef:
    assert gateway.__file__ is not None
    tree = ast.parse(Path(gateway.__file__).read_text())
    (function,) = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == "_settle_gateway_authorization"]
    return function


def _assigned(node: ast.stmt) -> tuple[str, ...]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        targets = [node.target]
    names: list[str] = []
    for target in targets:
        for element in target.elts if isinstance(target, ast.Tuple) else [target]:
            if isinstance(element, ast.Name):
                names.append(element.id)
    return tuple(names)


def _alternatives(value: ast.expr) -> list[tuple[tuple[str, ...], str]]:
    """A conditional expression as its alternatives, each under its test."""
    out: list[tuple[tuple[str, ...], str]] = []
    while isinstance(value, ast.IfExp):
        out.append(((ast.unparse(value.test),), ast.unparse(value.body)))
        value = value.orelse
    out.append((("otherwise",) if out else (), ast.unparse(value)))
    return out


def settle_pricing_statements() -> list[Statement]:
    """Every assignment to a pricing name in the settle function, in source
    order, under the conditions it runs in."""
    found: list[Statement] = []

    def walk(statements: list[ast.stmt], guards: tuple[str, ...]) -> None:
        for node in statements:
            if isinstance(node, ast.If):
                test = ast.unparse(node.test)
                walk(node.body, (*guards, test))
                walk(node.orelse, (*guards, f"not ({test})"))
            elif isinstance(node, ast.Try):
                walk(node.body, guards)
                for handler in node.handlers:
                    walk(handler.body, (*guards, "except"))
                walk(node.orelse, guards)
                walk(node.finalbody, guards)
            elif isinstance(node, ast.With | ast.For | ast.While):
                walk(node.body, guards)
            else:
                names = tuple(name for name in _assigned(node) if name in PRICING_NAMES)
                if not names:
                    continue
                assert isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign) and node.value is not None
                prefix = "+= " if isinstance(node, ast.AugAssign) else ""
                for tests, value in _alternatives(node.value):
                    found.append(Statement(names, (*guards, *tests), prefix + value))

    walk(_settle_function().body, ())
    return found


# Each pricing statement of the settle function, and what keeps an async-v1
# authorization away from it: the requested fact that reads the same field or
# predicate, or PLAIN where it is the ordinary path itself. Matched by the
# assigned names, the conditions, and the start of the assigned expression.
COVERED: list[tuple[Statement, str]] = [
    (Statement(("user_model_pair",), (), "_authorized_user_model_pair(authorization)"), "user_model"),
    (Statement(("selected_endpoint",), (), "_select_authorized_endpoint(authorization, body)"), PLAIN),
    (Statement(("selected_endpoint",),
               ("selected_endpoint.model_id == POLYPHEMUS_MODEL_ID and body.input_count == 0 and "
                "(body.output_count == 0)",), "replace(selected_endpoint, "), "polyphemus"),
    (Statement(("output_tokens",), (), "body.output_count"), PLAIN),
    (Statement(("service_tier",), ("user_model_pair is not None",), "None"), "user_model"),
    (Statement(("service_tier",), ("otherwise",), "_actual_service_tier_or_error(body.service_tier)"),
     "service_tier"),
    (Statement(("uncached_input", "total_input", "cache_read", "cache_creation"), (),
               "normalized_prompt_accounting(selected_endpoint.provider, body, model_id=selected_endpoint.model_id)"), PLAIN),
    (Statement(("uncached_input",), ("user_model_pair is not None",), "total_input"), "user_model"),
    (Statement(("partner_mode",), (),
               "_partner_billing_mode_or_error(requested_model_id=authorization.requested_model_id, "
               "route_type=body.route_type, idempotency_key=authorization.idempotency_key)"), "partner"),
    (Statement(("price_tier_input_tokens",), (),
               "_provider_price_tier_input_tokens(selected_endpoint, body.price_tier_input_tokens)"),
     "private_tier_basis"),
    (Statement(("video_cost",), (), "None"), PLAIN),
    (Statement(("video_cost",), ("success and authorization.video_pricing_snapshot is not None",),
               "video_cost_microdollars(authorization.video_pricing_snapshot, "), "video_cost"),
    (Statement(("billing_snapshot",), (), "billing_pricing_snapshot(authorization)"), PLAIN),
    (Statement(("actual_cost",), ("video_cost is not None",), "video_cost"), "video_cost"),
    (Statement(("actual_cost",), ("user_model_pair is not None",), "custom_model_cost_microdollars("),
     "user_model"),
    (Statement(("actual_cost",), ("partner_mode is not None",), "partner_cost_microdollars(partner_mode, "),
     "partner"),
    # The Stage D document and the live catalog are the same formula over
    # prices of the same instant; its two private inputs are covered above.
    (Statement(("actual_cost",), ("billing_snapshot is not None",),
               "_endpoint_cost_microdollars_from_document(billing_snapshot, selected_endpoint.id, "
               "uncached_input, output_tokens, cache_read_tokens=cache_read, "
               "cache_creation_tokens=cache_creation, price_tier_input_tokens=price_tier_input_tokens)"),
     PLAIN),
    (Statement(("actual_cost",), ("otherwise",),
               "_endpoint_cost_microdollars(selected_endpoint, uncached_input, output_tokens, "
               "cache_read_tokens=cache_read, cache_creation_tokens=cache_creation, "
               "price_tier_input_tokens=price_tier_input_tokens, effective_at=authorization.created_at, "
               "service_tier=service_tier)"), PLAIN),
    (Statement(("actual_cost",), ("not success",), "0"), REFUND),
    (Statement(("actual_cost",), ("not (not success)", "user_model_pair is not None",
                                  "actual_cost > authorization.estimated_microdollars"),
               "authorization.estimated_microdollars"), "user_model"),
    # The identity for a route that is not a native Batch one, which no async route is.
    (Statement(("actual_cost",), ("not (not success)", "not (user_model_pair is not None)"),
               "_native_batch_cost_or_error(actual_cost, route_type=body.route_type, "), "native_batch"),
    (Statement(("actual_cost",),
               ("success and authorization.receipt_fee_basis_points > 0 and "
                "(UsageType.for_endpoint(selected_endpoint) == UsageType.CREDITS) and "
                "(user_model_pair is None) and (partner_mode is None)",),
               "signed_receipt_price_microdollars(actual_cost, authorization.receipt_fee_basis_points)"),
     "receipt_fee"),
    (Statement(("actual_cost",), ("success and authorization.custom_model_markup_basis_points > 0",),
               "+= custom_model_markup_micro"), "custom_markup"),
    (Statement(("additional_cost",), (), "body.additional_cost_microdollars"), "tool_cost"),
    (Statement(("actual_cost",), ("authorization.model_id in TRANSCRIPTION_MODELS",
                                  "not transcription_spec.token_billed"), "0"), "tool_cost"),
    (Statement(("actual_cost",), ("authorization.model_id in SPEECH_MODELS", "not speech_spec.token_billed"),
               "0"), "tool_cost"),
    (Statement(("actual_cost",), ("additional_cost",), "+= additional_cost"), "tool_cost"),
    (Statement(("actual_cost",), ("success and authorization.app_markup_basis_points > 0",),
               "+= proposed_app_markup_micro"), "app_markup"),
    (Statement(("actual_cost",), ("success and selected_endpoint.model_id == POLYPHEMUS_MODEL_ID",),
               "min(actual_cost, authorization.estimated_microdollars)"), "polyphemus"),
]

# Every attribute of the authorization the settle function reads, anywhere,
# and the requested fact that reads the same one (or why it is not a price).
AUTHORIZATION_READS: dict[str, str] = {
    "video_pricing_snapshot": "video_cost",
    "user_provided_model_id": "user_model",
    "user_model_prompt_price_microdollars_per_m": "user_model",
    "user_model_completion_price_microdollars_per_m": "user_model",
    "user_model_owner_user_id": "user_model",
    "custom_model_id": "custom_model",
    "custom_model_markup_basis_points": "custom_markup",
    "custom_model_owner_user_id": "custom_markup",
    "requested_model_id": "partner",
    # The partner classifier's key marker, and the native Batch route's.
    "idempotency_key": "partner",
    "native_batch_eligible": "native_batch",
    "receipt_fee_basis_points": "receipt_fee",
    "additional_cost_reservation_microdollars": "tool_cost",
    # Fetch, transcription and speech are keyed on the authorized model.
    "model_id": "tool_cost",
    "app_markup_basis_points": "app_markup",
    "app_id": "app_markup",
    "app_owner_user_id": "app_markup",
    "usage_type": "usage_type",
    "settlement": "authority",
    # The plain formula's price instant, which the snapshot freezes at.
    "created_at": PLAIN,
    # A ceiling for the user-model, video, speech, transcription and
    # selector branches above, and a log line; no ordinary charge reads it.
    "estimated_microdollars": PLAIN,
    "id": IDENTITY, "workspace_id": IDENTITY, "key_hash": IDENTITY, "region": IDENTITY, "tags": IDENTITY,
    "credit_reservation_id": IDENTITY, "settled": IDENTITY, "fastpath_envelope": IDENTITY,
}
# Likewise for the selected endpoint: its id and provider price it by the
# plain formula; its model is read for the selector alone.
ENDPOINT_READS: dict[str, str] = {
    "id": PLAIN, "provider": PLAIN, "model_id": "polyphemus", "price_tiers": "polyphemus",
}


def _reads(base: str) -> set[str]:
    return {node.attr for node in ast.walk(_settle_function())
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == base}


def test_every_pricing_statement_of_the_settle_path_is_assigned_a_fact() -> None:
    """A change to how the settle path prices a request changes this list.
    Assign the new statement the requested fact that keeps it out of async
    settlement (and populate that fact in services/async_settle.requested_facts
    from what the statement reads), then record it in COVERED."""
    found = settle_pricing_statements()
    expected = [statement for statement, _fact in COVERED]
    assert len(found) == len(expected), [s for s in found if not any(
        s.names == e.names and s.guards == e.guards and s.value.startswith(e.value) for e in expected)]
    for seen, (want, covered) in zip(found, COVERED, strict=True):
        assert (seen.names, seen.guards) == (want.names, want.guards), (seen, want)
        assert seen.value.startswith(want.value), (seen, want)
        assert covered in FACTS or covered in (PLAIN, REFUND), covered
    # The two plain-formula calls take exactly these keywords: a new pricing
    # input needs a fact of its own.
    keywords = {
        ast.unparse(node.func): sorted(keyword.arg or "**" for keyword in node.keywords)
        for node in ast.walk(_settle_function()) if isinstance(node, ast.Call)
        and ast.unparse(node.func) in ("_endpoint_cost_microdollars", "_endpoint_cost_microdollars_from_document")
    }
    assert keywords == {
        "_endpoint_cost_microdollars": ["cache_creation_tokens", "cache_read_tokens", "effective_at",
                                        "price_tier_input_tokens", "service_tier"],
        "_endpoint_cost_microdollars_from_document": ["cache_creation_tokens", "cache_read_tokens",
                                                      "price_tier_input_tokens"],
    }


def test_every_input_of_the_settle_path_is_assigned_a_fact() -> None:
    """A new authorization or endpoint attribute read by the settle function
    is a possible new price input: classify it here."""
    assert _reads("authorization") == set(AUTHORIZATION_READS)
    assert _reads("selected_endpoint") == set(ENDPOINT_READS)
    for covered in (*AUTHORIZATION_READS.values(), *ENDPOINT_READS.values()):
        assert covered in FACTS or covered in (PLAIN, IDENTITY), covered


def _covering_facts() -> set[str]:
    return {covered for covered in (
        *(fact for _statement, fact in COVERED), *AUTHORIZATION_READS.values(), *ENDPOINT_READS.values(),
    ) if covered in FACTS}


# ------------------------------------------- each fact, where the settle path's is --

SETTINGS = settings()
RUNTIME = runtime()


def _route(model_id: str, provider: str, **changes: Any) -> ModelEndpoint:
    return endpoint(id=f"{model_id}@{provider}/prepaid", model_id=model_id, provider=provider, **changes)


ORDINARY = _route("deepseek/billing-v1", "venice")


def _body(model: str, **changes: Any) -> GatewayAuthorizeRequest:
    return GatewayAuthorizeRequest(**{"api_key_hash": "key-v1", "model": model,
                                      "route_type": "chat.completions", **changes})


def _authorization(model: str, **changes: Any) -> GatewayAuthorization:
    """An authorization as authorize writes it for this requested model."""
    return replace(authorization(), requested_model_id=model, **changes)


def _answer(body: GatewayAuthorizeRequest, auth: GatewayAuthorization, routes: list[ModelEndpoint],
            *, typed: bool = True, federated: bool = False) -> dict[str, Any]:
    return authorize_additions(request=request(RUNTIME), body=body, authorization=auth, endpoints=routes,
                               settings=SETTINGS, typed=typed, federated=federated, replay=False,
                               effective_at=None)


def _facts(body: GatewayAuthorizeRequest, auth: GatewayAuthorization,
           routes: list[ModelEndpoint]) -> billing.Eligibility:
    return requested_facts(body=body, authorization=auth, endpoints=routes, typed=True, federated=False)


def _fugu() -> ModelEndpoint:
    (provider, model_id), = PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS
    return _route(model_id, provider, price_tiers=(
        PriceTier(272_000, 5_000_000, 30_000_000, 500_000), PriceTier(None, 10_000_000, 45_000_000, 1_000_000)))


class Witness(NamedTuple):
    """An otherwise ordinary authorization for which the settle path's own
    predicate holds: the body, the authorization, its candidates, and that
    predicate evaluated by the settle path's names."""
    body: GatewayAuthorizeRequest
    authorization: GatewayAuthorization
    routes: list[ModelEndpoint]
    settle_predicate: bool


def _witnesses() -> dict[str, list[Witness]]:
    model = ORDINARY.model_id
    plain_body, plain = _body(model), _authorization(model)
    selector = _route(POLYPHEMUS_MODEL_ID, "trustedrouter")
    fugu = _fugu()
    user = _authorization(model, user_provided_model_id="um-1", user_provided_model_revision=1,
                          user_model_prompt_price_microdollars_per_m=1, user_model_owner_user_id="owner",
                          user_model_completion_price_microdollars_per_m=1)
    top_level = _body(PARASAIL_LIBERTY_2_0_MODEL_ID, route_type=PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE)
    internal_route = PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX + "panel"
    internal = _authorization(model, idempotency_key=PARASAIL_LIBERTY_2_0_IDEMPOTENCY_PREFIX + "1")
    speech, transcription = next(iter(SPEECH_MODELS)), next(iter(TRANSCRIPTION_MODELS))

    def partner(body: GatewayAuthorizeRequest, auth: GatewayAuthorization) -> bool:
        return gateway._partner_billing_mode_or_error(
            requested_model_id=auth.requested_model_id, route_type=body.route_type,
            idempotency_key=auth.idempotency_key) is not None

    return {
        "video_cost": [Witness(plain_body, replace(plain, video_pricing_snapshot="{}"), [ORDINARY], True)],
        "user_model": [Witness(plain_body, user, [ORDINARY], gateway._authorized_user_model_pair(user) is not None)],
        "custom_model": [Witness(plain_body, replace(plain, custom_model_id="cm-1"), [ORDINARY], True)],
        "custom_markup": [Witness(plain_body, replace(plain, custom_model_markup_basis_points=1), [ORDINARY], True)],
        "partner": [
            Witness(top_level, _authorization(PARASAIL_LIBERTY_2_0_MODEL_ID), [ORDINARY],
                    partner(top_level, _authorization(PARASAIL_LIBERTY_2_0_MODEL_ID))),
            Witness(_body(model, route_type=internal_route), internal, [ORDINARY],
                    partner(_body(model, route_type=internal_route), internal)),
        ],
        "native_batch": [Witness(plain_body, replace(plain, native_batch_eligible=True), [ORDINARY], True)],
        "receipt_fee": [Witness(plain_body, replace(plain, receipt_fee_basis_points=1), [ORDINARY], True)],
        "tool_cost": [
            Witness(plain_body, replace(plain, additional_cost_reservation_microdollars=1), [ORDINARY], True),
            Witness(plain_body, replace(plain, model_id=FETCH_MODEL_ID), [ORDINARY], True),
            Witness(plain_body, replace(plain, model_id=speech), [ORDINARY], speech in gateway.SPEECH_MODELS),
            Witness(plain_body, replace(plain, model_id=transcription), [ORDINARY],
                    transcription in gateway.TRANSCRIPTION_MODELS),
        ],
        "app_markup": [Witness(plain_body, replace(plain, app_markup_basis_points=1), [ORDINARY], True)],
        "usage_type": [Witness(plain_body, replace(plain, usage_type=UsageType.BYOK), [ORDINARY], True)],
        "authority": [Witness(plain_body, replace(plain, settlement="deferred_home"), [ORDINARY], True)],
        "service_tier": [Witness(_body(model, service_tier="priority"), plain, [ORDINARY],
                                 gateway._actual_service_tier_or_error("priority") == "priority")],
        "polyphemus": [Witness(plain_body, plain, [ORDINARY, selector],
                               selector.model_id == gateway.POLYPHEMUS_MODEL_ID)],
        "private_tier_basis": [Witness(plain_body, plain, [ORDINARY, fugu],
                                       gateway._provider_price_tier_input_tokens(fugu, 7) == 7)],
    }


def test_the_witnesses_start_from_an_eligible_authorization() -> None:
    model = ORDINARY.model_id
    answer = _answer(_body(model), _authorization(model), [ORDINARY])
    assert answer["async_eligible"] is True and "settlement_ticket" in answer
    assert billing.exclusion(_facts(_body(model), _authorization(model), [ORDINARY])) is None
    assert gateway._provider_price_tier_input_tokens(ORDINARY, 7) is None


def test_every_covering_fact_has_a_witness() -> None:
    assert _covering_facts() == set(_witnesses())


@pytest.mark.parametrize("fact", sorted(_witnesses()))
def test_a_fact_is_true_where_the_settle_paths_predicate_is_and_answers_false_only(fact: str) -> None:
    for witness in _witnesses()[fact]:
        assert witness.settle_predicate is True, fact
        facts = _facts(witness.body, witness.authorization, witness.routes)
        value = getattr(facts, fact)
        ordinary = getattr(billing.Eligibility(), fact)
        assert value != ordinary, (fact, value)
        # The fact alone excludes: every other is at its ordinary value, bar
        # the route of a partner request, which is not an async route either.
        others = {name for name in FACTS - {fact, "streamed"} if getattr(facts, name) != getattr(
            billing.Eligibility(), name)}
        assert others <= ({"route_type"} if fact == "partner" else set()), (fact, others)
        assert billing.exclusion(facts.model_copy(update={"route_type": "chat.completions"})) == REASONS.get(
            fact, fact)
        assert _answer(witness.body, witness.authorization, witness.routes) == FALSE_ONLY, fact


def test_the_private_tier_basis_fact_is_the_settle_paths_own_condition() -> None:
    """For every Credits endpoint: the fact is true exactly when
    _provider_price_tier_input_tokens would read the provider's basis."""
    private = 0
    for route in MODEL_ENDPOINTS.values():
        if str(route.usage_type) != "Credits":
            continue
        reads_basis = gateway._provider_price_tier_input_tokens(route, 123) is not None
        facts = _facts(_body(ORDINARY.model_id), _authorization(ORDINARY.model_id), [ORDINARY, route])
        assert facts.private_tier_basis is reads_basis, route.id
        private += reads_basis
    fugu = _fugu()
    assert gateway._provider_price_tier_input_tokens(fugu, 123) == 123
    assert _facts(_body(fugu.model_id), _authorization(fugu.model_id), [fugu]).private_tier_basis is True
    assert private <= 5  # a pinned contract, not a class of providers


# ------------------------------------------------------------ over the catalog --

def _programs() -> set[str]:
    """The catalog's programs, meta-routers, private aliases and internal models."""
    return (set(META_MODEL_IDS) | set(PRIVATE_PROXY_MODEL_TARGETS)
            | {model.id for model in MODELS.values() if model.internal_only})


def _spellings() -> set[str]:
    """Every model string a request can name: catalog ids, alias keys and
    targets, the nyte/ spelling of a router id, a bare OpenAI name, and each
    with a variant or dated suffix that routing strips."""
    names = {*MODELS, *routing.ROUTING_MODEL_ALIAS_TARGETS, *routing.ROUTING_MODEL_ALIAS_TARGETS.values()}
    names |= {"nyte/" + name.removeprefix(ROUTER_MODEL_NAMESPACE) for name in names
              if name.startswith(ROUTER_MODEL_NAMESPACE)}
    names |= {name.removeprefix("openai/") for name in MODELS if name.startswith("openai/")}
    names |= {"trustedrouter/auto-routing", "nyte/auto-routing"}
    return names | {name + suffix for name in names for suffix in (*routing._VARIANT_SUFFIXES, "-2026-01-01")}


def _routing_reads_as_a_pool(name: str) -> bool | None:
    """Routing's own answer for one requested string: True when it resolves to
    anything but the one catalog model it names (a meta-router, program, tier
    or private alias), None when routing refuses the string outright."""
    canonical = routing.canonical_model_id(name)
    try:
        ids, overrides = routing._requested_model_ids({"model": name}, SETTINGS)
    except HTTPException:
        return None
    return ids != [canonical] or bool(overrides.get("pool_model_ids")) or overrides.get("usage") == "Credits"


def test_every_program_routing_knows_is_a_program_name() -> None:
    """Whatever routing resolves a requested string to: when that is a
    program, meta-router or private alias (by the catalog's own sets, or by
    routing expanding it to other models), the name rule says so. Over every
    spelling of every catalog id and alias."""
    programs = _programs()
    assert len(programs) >= 60
    # The namespace holds them all, save the partner model the rule names.
    assert {name for name in programs if not names_router_model(name)} == {PARASAIL_LIBERTY_2_0_MODEL_ID}
    assert PROGRAM_MODEL_PREFIXES == (ROUTER_MODEL_NAMESPACE, PARASAIL_LIBERTY_2_0_MODEL_ID)
    seen = {"program": 0, "ordinary": 0, "refused": 0}
    for name in sorted(_spellings()):
        canonical = routing.canonical_model_id(name)
        pool = _routing_reads_as_a_pool(name)
        is_program = canonical in programs or names_router_model(canonical) or bool(pool)
        if is_program:
            assert names_program_model(name), (name, canonical)
            seen["program"] += 1
        elif pool is None:
            seen["refused"] += 1
        else:
            # An ordinary model, by every spelling: never taken for a program.
            assert not names_program_model(name), (name, canonical)
            seen["ordinary"] += 1
        # The rule is exactly its two prefixes over the folded, router-canonical name.
        folded = name.strip().lower()
        assert names_program_model(name) is routing.canonical_router_model_id(folded).startswith(
            PROGRAM_MODEL_PREFIXES), name
    assert seen["program"] >= 200 and seen["ordinary"] >= 1_000, seen


def test_no_requestable_name_the_settle_path_prices_specially_is_async_eligible() -> None:
    """For every spelling, as the model and as a fallback of an ordinary one,
    on both async routes: when the settle path's partner classifier answers
    for it (or refuses its markers), or it is a program, the answer is false
    only. An ordinary name stays eligible, so this cannot pass by excluding
    everything."""
    programs = _programs()
    eligible = excluded = answered = 0
    for index, name in enumerate(sorted(_spellings())):
        canonical = routing.canonical_model_id(name)
        special = canonical in programs or names_router_model(canonical) or bool(_routing_reads_as_a_pool(name))
        for route in ASYNC_ROUTES:
            auth, plain = _authorization(name), _authorization(ORDINARY.model_id)
            try:
                partner = gateway._partner_billing_mode_or_error(
                    requested_model_id=auth.requested_model_id, route_type=route,
                    idempotency_key=auth.idempotency_key) is not None
            except HTTPException:
                partner = True  # authorize refuses the request itself
            as_model = _body(name, route_type=route)
            as_fallback = _body(ORDINARY.model_id, route_type=route, models=[name])
            if special or partner:
                assert _answer(as_model, auth, [ORDINARY]) == FALSE_ONLY, (name, route)
                excluded += 1
            else:
                assert billing.exclusion(_facts(as_model, auth, [ORDINARY])) is None, (name, route)
                eligible += 1
            if special:
                assert _answer(as_fallback, plain, [ORDINARY]) == FALSE_ONLY, (name, route)
            else:
                assert billing.exclusion(_facts(as_fallback, plain, [ORDINARY])) is None, (name, route)
            if not (special or partner) and index % 11 == 0:
                # The whole answer for a sample of the ordinary names (the
                # facts above decide it; signing each one costs seconds).
                assert _answer(as_model, auth, [ORDINARY])["async_eligible"] is True, (name, route)
                assert _answer(as_fallback, plain, [ORDINARY])["async_eligible"] is True, (name, route)
                answered += 1
    assert eligible >= 2_000 and excluded >= 400 and answered >= 150, (eligible, excluded, answered)


@pytest.mark.parametrize("key", [PARASAIL_LIBERTY_2_0_IDEMPOTENCY_PREFIX + "1", "partner:other", None])
def test_a_partner_marker_never_reaches_an_async_answer(key: str | None) -> None:
    """The partner classifier reads the requested model, the route and the
    idempotency key. On an async route it answers none, or refuses the
    markers (authorize then refuses the request); the fact reads the same
    function, so a marker it would honour is never eligible."""
    for name in (ORDINARY.model_id, PARASAIL_LIBERTY_2_0_MODEL_ID, "Parasail/Liberty-2.0"):
        for route in (*ASYNC_ROUTES, PARASAIL_LIBERTY_2_0_TOP_LEVEL_ROUTE,
                      PARASAIL_LIBERTY_2_0_INTERNAL_ROUTE_PREFIX + "panel"):
            auth = _authorization(name, idempotency_key=key)
            try:
                mode = gateway._partner_billing_mode_or_error(
                    requested_model_id=name, route_type=route, idempotency_key=key)
            except HTTPException:
                mode = "refused"
            answer = _answer(_body(name, route_type=route), auth, [ORDINARY])
            if mode is not None or route not in ASYNC_ROUTES or names_program_model(name):
                assert answer == FALSE_ONLY, (name, route, key)
            else:
                assert answer["async_eligible"] is True, (name, route, key)


def test_no_endpoint_the_settle_path_prices_specially_is_async_eligible() -> None:
    """For every Credits endpoint in the catalog, authorized as its own
    model's request: when the settle path would read a private tier basis for
    it, treat it as the selector, meter its model as a tool or media request,
    or it carries a request fee, the answer is false only; every other
    endpoint of an ordinary model is eligible."""
    programs = _programs()
    eligible = excluded = 0
    special_models = {FETCH_MODEL_ID, *TRANSCRIPTION_MODELS, *SPEECH_MODELS}
    for route in MODEL_ENDPOINTS.values():
        if str(route.usage_type) != "Credits":
            continue
        special = (
            gateway._provider_price_tier_input_tokens(route, 1) is not None
            or route.model_id == gateway.POLYPHEMUS_MODEL_ID
            or route.model_id == gateway.FETCH_MODEL_ID
            or route.model_id in gateway.TRANSCRIPTION_MODELS or route.model_id in gateway.SPEECH_MODELS
            or route.request_price_microdollars != 0
            or route.model_id in programs or names_router_model(route.model_id)
        )
        answer = _answer(_body(route.model_id), _authorization(route.model_id, model_id=route.model_id), [route])
        # Among an ordinary request's candidates too.
        beside = _answer(_body(ORDINARY.model_id),
                         _authorization(ORDINARY.model_id, model_id=ORDINARY.model_id), [ORDINARY, route])
        if special:
            assert answer == FALSE_ONLY, route.id
            if route.model_id not in special_models and route.model_id not in programs:
                assert beside == FALSE_ONLY, route.id
            excluded += 1
        else:
            assert answer["async_eligible"] is True, route.id
            assert beside["async_eligible"] is True, route.id
            eligible += 1
    assert eligible >= 1_000 and 1 <= excluded <= 60, (eligible, excluded)


def test_a_tool_or_media_model_is_not_a_chat_model() -> None:
    """Why the edge composes no such request: the models the settle path
    meters as tool or media requests are served on their own routes, and
    the chat resolver refuses each."""
    for model_id in (FETCH_MODEL_ID, *TRANSCRIPTION_MODELS, *SPEECH_MODELS):
        model = MODELS.get(model_id)
        assert model is None or not model.supports_chat, model_id
