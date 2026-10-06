"""Record main's real writer, never execute historical code in current tests.

Run only in a separate git archive of main 896a9f0d; see gateway_authorizations_main.md.
The observation helpers also run against current code to compare race/admission behavior.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import STORE, InMemoryStore, configure_store

MODEL = "bytedance/seedance-2.5"


def request():
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def seed_model(kind, *, backend="memory"):
    if backend == "memory":
        configure_store(InMemoryStore())
        store = STORE.target
        database = None
        workspace = store.create_workspace("owner", "recorded", trial_credit_microdollars=10_000_000)
        _, key = store.create_api_key(workspace_id=workspace.id, name="recorded", creator_user_id="owner",
                                     limit_microdollars=10_000_000)
    else:
        store, database, key = _seed_typed_gateway_store()
        store.request_record_write_mode = backend
    common = dict(owner_user_id="owner", owner_workspace_id=key.workspace_id, name="Recorded video")
    if kind == "user":
        model = store.create_user_model(**common, kind="machine", endpoint_url="https://owner.example/v1",
                                       prompt_price_microdollars_per_million_tokens=100,
                                       completion_price_microdollars_per_million_tokens=200)
        model = store.set_user_model_online(model.id, owner_user_id="owner", online=True)
    elif kind == "custom":
        model = store.create_custom_model(**common, base_model_id=MODEL, hidden_prompt="policy")
    else:
        model = None
    body = dict(api_key_lookup_hash=key.lookup_hash, model=model.id if model else MODEL,
                route_type="videos", idempotency_key="recorded-video", request_fingerprint="a" * 64,
                max_tokens=1, additional_cost_reservation_microdollars=900_000)
    return store, database, key, model, body


def authorize(body):
    return gateway._authorize_gateway_sync(
        request(), GatewayAuthorizeRequest(**body), Settings(environment="test", user_models_dispatch_enabled=True),
    )["data"]


def outcome(body):
    try:
        data = authorize(body)
    except HTTPException as exc:
        return {"status": exc.status_code}
    return {"status": 200, "replay": data["idempotent_replay"]}


def money_snapshot(store, database, key):
    if database is not None:
        return copy.deepcopy(database.typed)
    return (copy.deepcopy(store.get_credit_account(key.workspace_id)),
            store.credit_money_snapshot(key.workspace_id), copy.deepcopy(store.get_key_by_hash(key.hash)))


def observe_inconsistent_identity(kind, field, backend):
    store, database, key, model, body = seed_model(kind, backend=backend)
    body[field] = model.revision + 1 if field == "custom_model_revision" else "tr-custom-model/different"
    first = authorize(body)
    auth = copy.deepcopy(store.get_gateway_authorization(first["authorization_id"]))
    before = money_snapshot(store, database, key)
    retry = authorize(body)
    assert retry["authorization_id"] == auth.id
    assert retry.get("invocation_nonce") is None
    assert store.get_gateway_authorization(auth.id) == auth
    assert money_snapshot(store, database, key) == before
    return {"first_replay": first["idempotent_replay"], "retry_replay": retry["idempotent_replay"],
            "frozen_revision": auth.user_provided_model_revision if kind == "user" else auth.custom_model_revision,
            "frozen_id_matches_live": (auth.user_provided_model_id if kind == "user" else auth.custom_model_id) == model.id}


def observe_race(monkeypatch, backend, reverse=False):
    """Commit the winner during the loser's routing, before typed admission."""
    store, database, key, model, wrapper = seed_model("custom", backend=backend)
    base = {**wrapper, "model": MODEL, "custom_model_id": model.id,
            "custom_model_revision": model.revision, "provider": {"usage": "credits"}}
    winner_body, loser_body = (base, wrapper) if reverse else (wrapper, base)
    route = gateway.video_route_endpoint_candidates
    winner = None
    before = None

    def interleave(*args, **kwargs):
        nonlocal winner, before
        monkeypatch.setattr(gateway, "video_route_endpoint_candidates", route)
        winner = authorize(winner_body)
        before = money_snapshot(store, database, key)
        return route(*args, **kwargs)

    monkeypatch.setattr(gateway, "video_route_endpoint_candidates", interleave)
    loser = authorize(loser_body)
    assert winner is not None and not winner["idempotent_replay"]
    assert loser["authorization_id"] == winner["authorization_id"]
    assert loser.get("invocation_nonce") is None
    assert money_snapshot(store, database, key) == before
    return {"status": 200, "replay": loser["idempotent_replay"], "same_authorization": True,
            "unchanged_money": True}


def record_authorization(kind):
    store, _, key, model, body = seed_model(kind)
    if kind == "catalog":
        body["provider"] = {"only": ["venice"]}
    elif kind == "chat":
        body.update(model="anthropic/claude-haiku-4.5", route_type="chat.completions",
                    additional_cost_reservation_microdollars=0)
    first = authorize(body)
    auth = store.get_gateway_authorization(first["authorization_id"])
    retries = [{"body": body, "main": outcome(body), "current_status": 200}]
    if kind == "catalog":
        derived = {**body, "max_tokens": 400_000, "video_resolution": "1080p"}
        retries.append({"body": derived, "main": outcome(derived), "current_status": 200})
    conflict = {**body, "request_fingerprint": "b" * 64}
    retries.append({"body": conflict, "main": outcome(conflict), "current_status": 409})
    return {"request": body, "authorization": asdict(auth), "key": asdict(key),
            "workspace": asdict(store.get_workspace(key.workspace_id)),
            "credit_account": asdict(store.get_credit_account(key.workspace_id)),
            "credit_money": asdict(store.credit_money[key.workspace_id]),
            "user_model": asdict(model) if model else None, "retries": retries}


@pytest.mark.skipif(not os.environ.get("QR_MAIN_FIXTURE_OUTPUT"), reason="historical fixture generator only")
def test_record_main(monkeypatch):
    output = Path(os.environ["QR_MAIN_FIXTURE_OUTPUT"])
    records = {kind: record_authorization(kind) for kind in ("catalog", "user", "chat")}
    observations = {}
    for backend in ("memory", "typed", "legacy"):
        for reverse in (False, True):
            with monkeypatch.context() as patch:
                observations[f"race/{backend}/{reverse}"] = observe_race(patch, backend, reverse)
        for kind in ("user", "custom"):
            for field in ("custom_model_revision", "custom_model_id"):
                observations[f"identity/{backend}/{kind}/{field}"] = observe_inconsistent_identity(kind, field, backend)
    output.write_text(json.dumps({"source_commit": "896a9f0d", "authorizations": records,
                                  "observations": observations}, indent=2, sort_keys=True) + "\n")
