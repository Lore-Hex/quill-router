"""Anthropic's own model ids resolve to the catalog model they name.

Claude Code and the Anthropic SDKs send `claude-opus-5-5`; the catalog id is
`anthropic/claude-opus-5.5`. Every case here runs on controlled rows, so the
price refresh cannot change what is asserted.
"""

from __future__ import annotations

import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.fixture_routes import bypass_catalog_caches, serve_on_fixture_route
from trusted_router import routing, routing_snapshot
from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    ROUTING_MODEL_ALIAS_TARGETS,
    Model,
)
from trusted_router.config import Settings
from trusted_router.model_aliases import canonical_router_model_id
from trusted_router.routing import (
    canonical_model_id,
    chat_route_candidates,
    native_model_id_targets,
    resolve_model_alias,
)
from trusted_router.storage import STORE

PLAIN = "anthropic/native-fixture-9.5"
DATED = "anthropic/native-fixture-8.5"
EXACT = "anthropic/native-fixture-7.5"
EXACT_DATED = "anthropic/native-fixture-7.5-early"
SHARED_A = "anthropic/native-fixture-shared-a"
SHARED_B = "anthropic/native-fixture-shared-b"
SHARED_DATED = "anthropic/native-fixture-shared-dated"
SHORT_DATE = "anthropic/native-fixture-5.5"
UNDATED_A = "anthropic/native-fixture-6.5-a"
UNDATED_B = "anthropic/native-fixture-6.5-b"
NO_MESSAGES = "anthropic/native-fixture-chat-only"
ELSEWHERE = "anthropic/native-fixture-elsewhere"
OWNED = "openai/nativefix-owned"
OWNED_TWIN = "anthropic/native-fixture-owned"
SLASHED = "anthropic/native-fixture-slashed"
INTERNAL = "anthropic/native-fixture-internal"
PREFIX = "claude-nativefix"


def _serve(monkeypatch, model_id: str, upstream_id: str, *, host: str = "anthropic",
           messages: bool = True) -> None:
    model = Model(
        id=model_id, name=model_id, provider="anthropic", context_length=200_000,
        supports_messages=messages, prepaid_available=True,
    )
    serve_on_fixture_route(
        monkeypatch, model_id, host, author="anthropic", model=model, upstream_id=upstream_id,
    )


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> None:
    bypass_catalog_caches(monkeypatch)
    _serve(monkeypatch, PLAIN, f"{PREFIX}-9-5")
    _serve(monkeypatch, DATED, f"{PREFIX}-8-5-20300101")
    # An exact id outranks another model's undated form.
    _serve(monkeypatch, EXACT, f"{PREFIX}-7-5")
    _serve(monkeypatch, EXACT_DATED, f"{PREFIX}-7-5-20300101")
    # Two models with one upstream id: neither is named by it.
    _serve(monkeypatch, SHARED_A, f"{PREFIX}-shared")
    _serve(monkeypatch, SHARED_B, f"{PREFIX}-shared")
    # A dated id whose undated form is that shared id does not settle it.
    _serve(monkeypatch, SHARED_DATED, f"{PREFIX}-shared-20300101")
    # Only an eight-digit date has an undated form.
    _serve(monkeypatch, SHORT_DATE, f"{PREFIX}-5-5-203001")
    # Two dated ids with one undated form: the dated ids resolve, the form does not.
    _serve(monkeypatch, UNDATED_A, f"{PREFIX}-6-5-20300101")
    _serve(monkeypatch, UNDATED_B, f"{PREFIX}-6-5-20300202")
    _serve(monkeypatch, NO_MESSAGES, f"{PREFIX}-chat-only", messages=False)
    _serve(monkeypatch, ELSEWHERE, f"{PREFIX}-elsewhere", host="deepinfra")
    # A spelling the bare OpenAI rule already resolves stays with that rule.
    serve_on_fixture_route(monkeypatch, OWNED, "openai", author="openai")
    _serve(monkeypatch, OWNED_TWIN, "nativefix-owned")
    # An internal-only model is asked for by its catalog id, which its admission rule reads.
    internal = Model(
        id=INTERNAL, name=INTERNAL, provider="anthropic", context_length=200_000,
        supports_messages=True, prepaid_available=True, internal_only=True,
    )
    serve_on_fixture_route(
        monkeypatch, INTERNAL, "anthropic", author="anthropic", model=internal,
        upstream_id=f"{PREFIX}-internal",
    )
    # An upstream id with a vendor prefix is not a bare id.
    _serve(monkeypatch, SLASHED, f"vendor/{PREFIX}-slashed")


EXPECTED = {
    f"{PREFIX}-9-5": PLAIN,
    f"{PREFIX}-8-5-20300101": DATED,
    f"{PREFIX}-8-5": DATED,
    f"{PREFIX}-7-5": EXACT,
    f"{PREFIX}-7-5-20300101": EXACT_DATED,
    f"{PREFIX}-6-5-20300101": UNDATED_A,
    f"{PREFIX}-6-5-20300202": UNDATED_B,
    f"{PREFIX}-shared-20300101": SHARED_DATED,
    f"{PREFIX}-5-5-203001": SHORT_DATE,
}


def test_the_map_is_read_from_the_direct_route(rows: None) -> None:
    targets = native_model_id_targets()
    assert {k: v for k, v in targets.items() if "nativefix" in k} == EXPECTED
    assert list(targets) == sorted(targets)


@pytest.mark.parametrize(("requested", "expected"), sorted(EXPECTED.items()))
def test_a_native_id_resolves_to_its_catalog_model(rows: None, requested: str, expected: str) -> None:
    assert resolve_model_alias(requested) == expected
    assert canonical_model_id(requested + ":nitro") == expected
    assert [c.id for c in chat_route_candidates({"model": requested}, Settings(environment="test"))] == [expected]


@pytest.mark.parametrize("requested", [
    f"{PREFIX}-shared",           # two models claim it
    f"{PREFIX}-6-5",              # two dated ids share the form
    f"{PREFIX}-5-5",              # six digits are not a date
    f"{PREFIX}-chat-only",        # the model is not a Messages model
    f"{PREFIX}-internal",         # an internal-only model
    f"{PREFIX}-elsewhere",        # not the direct route's id
    f"{PREFIX}-9-5-20300101",     # a date the catalog does not list
    f"{PREFIX}-9-5-2030-01-01",   # the other vendor's dated form
    f"{PREFIX}-9",
    f"{PREFIX.upper()}-9-5",
    "nativefix",
])
def test_any_other_spelling_is_left_alone(rows: None, requested: str) -> None:
    assert resolve_model_alias(requested) == requested
    with pytest.raises(HTTPException) as refused:
        chat_route_candidates({"model": requested}, Settings(environment="test"))
    assert refused.value.status_code == 400


def test_an_older_rule_keeps_its_spelling(rows: None) -> None:
    assert "nativefix-owned" not in native_model_id_targets()
    assert resolve_model_alias("nativefix-owned") == OWNED


def test_a_router_alias_or_alias_target_keeps_its_spelling(
    rows: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import model_aliases

    key = f"{PREFIX}-9-5"
    monkeypatch.setitem(model_aliases._ROUTER_MODEL_ID_ALIASES, key, "trustedrouter/auto")
    assert key not in native_model_id_targets()
    assert resolve_model_alias(key) == "trustedrouter/auto"
    monkeypatch.delitem(model_aliases._ROUTER_MODEL_ID_ALIASES, key)
    assert native_model_id_targets()[key] == PLAIN
    monkeypatch.setitem(ROUTING_MODEL_ALIAS_TARGETS, key, DATED)
    assert key not in native_model_id_targets()
    assert resolve_model_alias(key) == DATED
    assert routing_snapshot.live_aliases().alias_targets[key] == DATED


def _before(model_id: str) -> str:
    """resolve_model_alias as it was before native ids."""
    model_id = canonical_router_model_id(model_id)
    if model_id in ROUTING_MODEL_ALIAS_TARGETS:
        return ROUTING_MODEL_ALIAS_TARGETS[model_id]
    if model_id in MODELS:
        return model_id
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model_id)
    for candidate in (model_id, base):
        if candidate in MODELS:
            return candidate
        if f"openai/{candidate}" in MODELS:
            return f"openai/{candidate}"
    return model_id


def _spellings() -> set[str]:
    out = {"", "nope/model", "totally-unknown", "gpt-unknown-2025-01-01", "claude-unknown-9-9",
           *ROUTING_MODEL_ALIAS_TARGETS, *MODELS}
    out.update(e.upstream_id for e in MODEL_ENDPOINTS.values() if e.upstream_id)
    for model_id in list(MODELS):
        bare = model_id.split("/", 1)[-1]
        out.update({
            bare, bare + "-2025-04-14", bare + "-20250414", model_id + "-2025-04-14",
            model_id + "-20250414", bare.replace(".", "-"), model_id.replace(".", "-"),
            re.sub(r"-\d{8}$", "", bare),
        })
    return out


def test_only_a_native_id_resolves_differently(rows: None) -> None:
    """Over the whole catalog: the one difference from the old resolver is a
    listed native id, and such an id used to resolve to nothing."""
    targets = native_model_id_targets()
    changed = 0
    for spelling in _spellings():
        if spelling in targets:
            assert _before(spelling) == spelling and spelling not in MODELS, spelling
            assert resolve_model_alias(spelling) == targets[spelling], spelling
            changed += 1
        else:
            assert resolve_model_alias(spelling) == _before(spelling), spelling
    assert changed >= len(EXPECTED)
    for spelling, model_id in targets.items():
        assert "/" not in spelling and MODELS[model_id].supports_messages
        upstream_ids = [e.upstream_id or "" for e in MODEL_ENDPOINTS.values()
                        if e.model_id == model_id and e.provider == routing.NATIVE_ID_PROVIDER]
        assert any(spelling in (u, re.sub(r"-\d{8}$", "", u)) for u in upstream_ids)


def test_the_snapshot_lists_native_ids_beside_the_alias_targets(
    rows: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Go passes a listed key to Python (evaluate.go resolve); a key is not a
    requested model, so no catalog model leaves the evaluator's cohort."""
    settings = Settings(environment="test")
    listed = routing_snapshot.live_aliases().alias_targets
    assert listed == {**native_model_id_targets(), **ROUTING_MODEL_ALIAS_TARGETS}
    assert {k: v for k, v in listed.items() if "nativefix" in k} == EXPECTED
    requested = routing_snapshot.live_requested_models(settings)
    monkeypatch.setattr(routing, "native_model_id_targets", dict)
    assert routing_snapshot.live_aliases().alias_targets == dict(ROUTING_MODEL_ALIAS_TARGETS)
    assert routing_snapshot.live_requested_models(settings) == requested
    assert not {model.id for model in requested} & set(EXPECTED.values())


def _authorize(client: TestClient, raw_key: str, model: str, route_type: str | None = "messages",
               **fields):
    key = STORE.get_key_by_raw(raw_key)
    assert key is not None
    body = {
        "api_key_lookup_hash": key.lookup_hash, "model": model,
        "estimated_input_tokens": 100, "max_output_tokens": 20, **fields,
    }
    if route_type is not None:
        body["route_type"] = route_type
    return client.post("/v1/internal/gateway/authorize", json=body)


def test_authorize_selects_the_same_route_for_either_spelling(
    rows: None, client: TestClient, inference_key: str,
) -> None:
    by_catalog_id = _authorize(client, inference_key, PLAIN)
    by_native_id = _authorize(client, inference_key, f"{PREFIX}-9-5")
    assert by_catalog_id.status_code == by_native_id.status_code == 200, by_native_id.text
    selected = []
    for response in (by_catalog_id, by_native_id):
        auth = STORE.get_gateway_authorization(response.json()["data"]["authorization_id"])
        assert auth is not None
        selected.append((auth.model_id, auth.endpoint_id, auth.estimated_microdollars))
    assert selected[0] == selected[1]
    assert selected[0][:2] == (PLAIN, f"{PLAIN}@anthropic/prepaid")
    unknown = _authorize(client, inference_key, f"{PREFIX}-9-5-20300101")
    assert unknown.status_code == 400


def test_the_messages_route_takes_a_native_id(
    rows: None, client: TestClient, inference_headers: dict[str, str],
) -> None:
    body = {"max_tokens": 8, "messages": [{"role": "user", "content": "ping"}]}
    refusal = "Model does not support Anthropic Messages"
    by_native_id = client.post(
        "/v1/messages", headers=inference_headers, json={**body, "model": f"{PREFIX}-9-5"},
    )
    by_catalog_id = client.post("/v1/messages", headers=inference_headers, json={**body, "model": PLAIN})
    assert refusal not in by_native_id.text
    assert by_native_id.status_code == by_catalog_id.status_code
    for model in (f"{PREFIX}-chat-only", f"{PREFIX}-9-5-20300101", f"{PREFIX}-shared"):
        refused = client.post("/v1/messages", headers=inference_headers, json={**body, "model": model})
        assert refused.status_code == 400 and refusal in refused.text, model


NO_FALLBACKS = {"provider": {"allow_fallbacks": False}}


@pytest.mark.parametrize("route_type", ["chat.completions", "responses", None])
@pytest.mark.parametrize("spelling", [f"{PREFIX}-9-5", f"{PREFIX}-9-5:nitro", f"{PREFIX}-8-5"])
def test_a_native_id_without_fallbacks_is_refused_off_the_messages_route(
    rows: None, client: TestClient, inference_key: str, route_type: str | None, spelling: str,
) -> None:
    """The gateway's fallbacks-off check on these routes knows a catalog id
    only; the refusal comes before any hold."""
    key = STORE.get_key_by_raw(inference_key)
    assert key is not None
    before = STORE.get_credit_account(key.workspace_id)
    refused = _authorize(client, inference_key, spelling, route_type, **NO_FALLBACKS)
    assert refused.status_code == 400, refused.text
    target = DATED if spelling.endswith("-8-5") else PLAIN
    assert f"request {target} by its catalog id" in refused.text
    assert STORE.get_credit_account(key.workspace_id) == before
    # The positive controls differ in one fact each and are authorized.
    assert _authorize(client, inference_key, spelling, route_type).status_code == 200
    assert _authorize(client, inference_key, spelling, "messages", **NO_FALLBACKS).status_code == 200
    by_catalog_id = spelling.replace(f"{PREFIX}-9-5", PLAIN).replace(f"{PREFIX}-8-5", DATED)
    assert _authorize(client, inference_key, by_catalog_id, route_type, **NO_FALLBACKS).status_code == 200


def test_native_model_id_target_names_only_a_native_resolution(rows: None) -> None:
    from trusted_router.routing import native_model_id_target

    assert native_model_id_target(f"{PREFIX}-9-5") == PLAIN
    assert native_model_id_target(f"{PREFIX}-9-5:floor") == PLAIN
    assert native_model_id_target(f"{PREFIX}-8-5-20300101") == DATED
    for other in (PLAIN, PLAIN + ":nitro", "nativefix-owned", f"{PREFIX}-shared", f"{PREFIX}-internal",
                  "totally-unknown", "trustedrouter/auto", ""):
        assert native_model_id_target(other) is None, other
