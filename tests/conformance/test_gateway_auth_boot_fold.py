"""Boot-fold differential: separate strong reads remain the behavioral oracle.

Storage cases also run against native GoogleSQL in CI. Route cases exercise the
real signature verifier and resolver, comparing the entire response plus auth,
credentials, boot verdict, error status/detail and Stage D outcome.
The folded contract is ONE strong snapshot; registration/removal or rotation
committed after it is observed next authorize, not midway through this request.
"""
from __future__ import annotations

import copy
import dataclasses
import uuid
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.requests import Request

from tests.conformance.test_gateway_auth_byok_fold import folded_store  # noqa: F401 - fixture
from tests.test_gateway_authorize_spanner_operations import (
    _lookup_body,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - fixture
)
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.config import Settings
from trusted_router.gateway_boot import (
    SPEND_LEASE_BOOT_KIND,
    SpendLeaseBoot,
    boot_auth_digest,
    parse_boot_auth_header,
)
from trusted_router.receipt_keys import b64url_encode, receipt_kid
from trusted_router.routes.internal import gateway
from trusted_router.storage_gcp import (
    _API_KEY_AUTH_CONTEXT_SQL,
    SpannerBigtableStore,
    _auth_record,
)
from trusted_router.storage_models import ApiKey, ApiKeyAuthContext, CreditAccount, Workspace

# Private boot keys by kid, for tests that re-sign a later request with the same boot.
_BOOT_KEYS: dict[str, Ed25519PrivateKey] = {}


def signed_request(store, key, *, idempotency_key="metadata-idem"):
    # A fresh boot key (so a fresh kid) per call: the native emulator store is shared
    # across tests, and a fixed kid let one case's malformed boot row break the next.
    private = Ed25519PrivateKey.generate()
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    ))}
    boot = SpendLeaseBoot(
        receipt_kid(jwk), jwk, True, True, "sha256:" + "11" * 32,
        "gcp-cs-jwt", "2026-09-01T00:00:00Z",
    )
    _BOOT_KEYS[boot.kid] = private
    store.observe_spend_lease_boot(boot)
    body = _lookup_body(key, idempotency_key=idempotency_key)
    body.stream = True
    body.route_type = "chat.completions"
    body.provider = {"usage": "credits"}
    raw = body.model_dump_json().encode()
    signature = b64url_encode(private.sign(boot_auth_digest("POST", "/", raw)))
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [
        (b"x-tr-boot-auth", f"kid={boot.kid},sig={signature}".encode()),
    ]})
    return request, body, raw, boot


def separate_context(store, lookup_hash, boot_kid=None):
    """Main oracle: metadata only; route reads BYOK/boot at their original locations."""
    with store._database.snapshot() as snapshot:
        rows = list(snapshot.execute_sql(
            _API_KEY_AUTH_CONTEXT_SQL, params={"lookup_hash": lookup_hash},
            param_types={"lookup_hash": store._param_types.STRING},
        ))
    if not rows:
        return None
    key = _auth_record(str(rows[0][0]), ApiKey)
    workspace = _auth_record(str(rows[0][1]), Workspace) if rows[0][1] is not None else None
    if workspace is not None and workspace.deleted:
        workspace = None
    return ApiKeyAuthContext(key, workspace)


BOOT_STATES = [
    "no_header", "valid", "unknown", "removed", "reregistered", "observation_merged",
    "lookup_remap", "invalid_key", "missing_key", "scope", "missing_workspace",
    "expired_key", "malformed_boot_disabled", "malformed_boot_needed",
    "malformed_byok_valid", "malformed_byok_disabled", "malformed_byok_expired",
    "malformed_byok_scope", "malformed_byok_missing_workspace", "malformed_byok_needed",
    "rejected_digest", "bad_signature", "unverified", "malformed_header", "duplicate_header",
]


def prepare_state(store, key, state):
    request, body, raw, boot = signed_request(store, key)
    if state.startswith("malformed_byok_"):
        config = store.upsert_byok_provider(
            workspace_id=key.workspace_id,
            provider="anthropic" if state.endswith("needed") else "openai",
            secret_ref="fixture/malformed", key_hint="fixture",  # noqa: S106
        )
        store._write_entity("byok", f"{key.workspace_id}#{config.provider}",
                            dataclasses.asdict(config) | {"encrypted_secret": {}})
    if state.startswith("malformed_boot_"):
        store._write_entity(SPEND_LEASE_BOOT_KIND, boot.kid, {"kid": boot.kid})
    state = {
        "malformed_boot_disabled": "invalid_key", "malformed_byok_disabled": "invalid_key",
        "malformed_byok_expired": "expired_key", "malformed_byok_scope": "scope",
        "malformed_byok_missing_workspace": "missing_workspace",
    }.get(state, state)
    headers = request.scope["headers"]
    if state == "no_header":
        headers.clear()
    elif state == "unknown":
        headers[:] = [(b"x-tr-boot-auth", b"kid=unknown,sig=eA")]
    elif state == "malformed_header":
        headers[:] = [(b"x-tr-boot-auth", b"malformed")]
    elif state == "duplicate_header":
        headers.append(headers[0])
    elif state == "bad_signature":
        headers[:] = [(b"x-tr-boot-auth", f"kid={boot.kid},sig={b64url_encode(bytes(64))}".encode())]
    elif state in {"removed", "reregistered"}:
        # Prime the new path before deleting; both absence and re-registration
        # must be observed rather than returning an earlier process-local row.
        store.gateway_api_key_auth_context(key.lookup_hash, boot_kid=boot.kid)
        store._delete_entities(SPEND_LEASE_BOOT_KIND, [boot.kid])
        if state == "reregistered":
            store.observe_spend_lease_boot(dataclasses.replace(boot, registered_at="2026-09-02T00:00:00Z"))
    elif state in {"unverified", "observation_merged"}:
        store._write_entity(SPEND_LEASE_BOOT_KIND, boot.kid, dataclasses.replace(boot, verified=False))
        store.gateway_api_key_auth_context(key.lookup_hash, boot_kid=boot.kid)
        if state == "observation_merged":
            store.observe_spend_lease_boot(boot)
    elif state == "lookup_remap":
        workspace = Workspace(id=key.workspace_id + "-remap", name="Remapped", owner_user_id="owner")
        store._write_entity("workspace", workspace.id, workspace)
        _, other = store.api_keys.create(workspace_id=workspace.id, name="other", creator_user_id="owner")
        # Remap to another authenticated key; signature's lookup hash no longer binds.
        store._write_entity("api_key_lookup", key.lookup_hash,
                            {"key_id": other.hash, "workspace_id": key.workspace_id})
    elif state == "expired_key":
        store._write_entity("api_key", key.hash, dataclasses.replace(key, expires_at="2020-01-01T00:00:00Z"))
    elif state == "invalid_key":
        store._write_entity("api_key", key.hash, dataclasses.replace(key, disabled=True))
    elif state == "missing_key":
        store._delete_entities("api_key", [key.hash])
    elif state == "scope":
        store._write_entity("api_key", key.hash, dataclasses.replace(key, scopes=["profile"]))
    elif state == "missing_workspace":
        store._delete_entities("workspace", [key.workspace_id])
    return request, body, raw, boot


@pytest.mark.parametrize("state", BOOT_STATES)
def test_boot_storage_differential(folded_store, state):  # noqa: F811 - fixture
    store = folded_store
    workspace = Workspace(id="boot-fold-" + uuid.uuid4().hex, name="Boot", owner_user_id="owner")
    store._write_entity("workspace", workspace.id, workspace)
    _, key = store.api_keys.create(workspace_id=workspace.id, name="key", creator_user_id="owner")
    store.upsert_byok_provider(workspace_id=workspace.id, provider="anthropic",
                               secret_ref="fixture/anthropic", key_hint="fixture")  # noqa: S106 - fixture
    request, _, _, _ = prepare_state(store, key, state)
    headers = request.headers.getlist("X-TR-Boot-Auth")
    auth = parse_boot_auth_header(headers[0]) if len(headers) == 1 else None
    kid = auth.kid if auth else None
    expected = separate_context(store, key.lookup_hash)
    actual = store.gateway_api_key_auth_context(key.lookup_hash, boot_kid=kid)
    assert (actual is None) == (expected is None)
    if expected is not None:
        assert actual.api_key == expected.api_key
        assert actual.workspace == expected.workspace
        assert actual.boot_record_loaded == (kid is not None)
        # Storage must preserve even malformed records without decoding them.
        if kid and state not in {"unknown", "removed"}:
            assert isinstance(actual.boot_record_body, str)
        else:
            assert actual.boot_record_body is None


REASONS = [
    "ok", "stage_d_disabled", "workspace_not_pilot", "heartbeats_disabled",
    "boot_not_accepted", "not_streaming", "route", "mixed_usage_type", "pricing_kind",
    "service_tier", "settlement_backend", "replayed",
]


@pytest.mark.parametrize("state", BOOT_STATES)
@pytest.mark.parametrize("reason", REASONS)
@pytest.mark.parametrize("usage", ["credits", "byok"])
def test_complete_boot_authorize_differential(state, reason, usage, fixed_operation_catalog, monkeypatch):  # noqa: F811 - fixture
    store, database, key = _seed_typed_gateway_store()
    store.upsert_byok_provider(workspace_id=key.workspace_id, provider="anthropic",
                               secret_ref="fixture/anthropic", key_hint="fixture")  # noqa: S106 - fixture
    request, body, raw, boot = prepare_state(store, key, state)
    if state == "lookup_remap":
        workspace_id = key.workspace_id + "-remap"
        store._write_entity("credit", workspace_id, CreditAccount(workspace_id=workspace_id))
        balance = database.typed["tr_credit_balance"][(key.workspace_id, 0)]
        database.typed["tr_credit_balance"][(workspace_id, 0)] = balance | {"workspace_id": workspace_id}
    body.provider = {"usage": usage}
    settings = Settings(environment="test", stage_d_eligibility_enabled=True, stage_d_pilot_workspace_ids="")
    accepted = {boot.image_digest} if state != "rejected_digest" else set()
    resolver_calls = []
    request.scope["app"] = SimpleNamespace(state=SimpleNamespace(stage_d_policy_resolver=SimpleNamespace(
        kick=lambda: resolver_calls.append("kick"),
        accepted_image_digests=lambda: frozenset(accepted),
    )))
    eligibility = gateway._stage_d_eligibility_reason

    def eligibility_case(**kwargs):
        # Exercise every resolver result using its real ordered predicates.
        overrides = {
            "stage_d_disabled": {"eligibility_enabled": False},
            "workspace_not_pilot": {"pilot_workspace_ids": frozenset({"other"})},
            "heartbeats_disabled": {"heartbeat_enabled": False},
            "boot_not_accepted": {"boot_accepted": False},
            "not_streaming": {"stream": False},
            "route": {"route_type": "embeddings"},
            "pricing_kind": {"standard_endpoint_pricing": False},
            "service_tier": {"service_tier": "priority"},
            "settlement_backend": {"settlement_backend": False},
        }.get(reason, {})
        if reason == "mixed_usage_type":
            endpoint = MODEL_ENDPOINTS["anthropic/claude-haiku-4.5@anthropic/byok"]
            overrides["endpoint_candidates"] = [*kwargs["endpoint_candidates"], (MODELS[endpoint.model_id], endpoint)]
        return eligibility(**(kwargs | overrides))

    monkeypatch.setattr(gateway, "_stage_d_eligibility_reason", eligibility_case)
    folded = SpannerBigtableStore.gateway_api_key_auth_context
    consume = gateway._byok_configs_for_candidates
    initial = {name: copy.deepcopy(value) for name, value in vars(database).items()
               if isinstance(value, dict)}
    outcomes = []
    for joined in (False, True):
        for name, value in initial.items():
            setattr(database, name, copy.deepcopy(value))
        resolver_calls.clear()
        captured = []
        counter = iter(range(1, 1000))
        # Identical generated identifiers let us compare the complete response.
        monkeypatch.setattr(uuid, "uuid4", lambda counter=counter: uuid.UUID(int=next(counter)))

        def resolve(self, *args, joined=joined, captured=captured, **kwargs):
            context = (folded if joined else separate_context)(self, *args, **kwargs)
            return context

        def consumed(*args, captured=captured, **kwargs):
            configs = consume(*args, **kwargs)
            captured.append(copy.deepcopy(configs))
            return configs

        monkeypatch.setattr(gateway, "_byok_configs_for_candidates", consumed)

        monkeypatch.setattr(SpannerBigtableStore, "gateway_api_key_auth_context", resolve)
        headers = request.headers.getlist("X-TR-Boot-Auth")
        auth = parse_boot_auth_header(headers[0]) if len(headers) == 1 else None
        # Same shape as _authorize_gateway_sync's boot context (#1418 dropped the
        # Stage C failure reason and lease echo).
        context = {"boot_auth": auth, "boot_verified": False, "raw_body": raw}
        try:
            response = gateway._authorize_gateway_sync_impl(request, body, settings, context)
            if reason == "replayed":
                response = gateway._authorize_gateway_sync_impl(request, body, settings, context)
            status = 200
        except HTTPException as exc:
            response, status = {"detail": exc.detail, "headers": exc.headers}, exc.status_code
        except TypeError as exc:
            response, status = {"exception": type(exc).__name__, "message": str(exc)}, 500
        outcomes.append((captured, context["boot_verified"],
                         status, response, list(resolver_calls)))
    assert outcomes[1] == outcomes[0]
    _, verified, status, response, calls = outcomes[1]
    if state in {"invalid_key", "missing_key", "scope", "expired_key", "malformed_boot_disabled",
                 "malformed_byok_disabled", "malformed_byok_expired", "malformed_byok_scope"}:
        assert status == (403 if state in {"scope", "malformed_byok_scope"} else 401)
        assert not calls and not verified  # No boot handling before key/scope checks.
    elif state == "malformed_boot_needed":
        assert status == 500 and not verified
    else:
        assert verified == (state in {"valid", "reregistered", "observation_merged", "missing_workspace",
                                     "malformed_byok_valid", "malformed_byok_missing_workspace",
                                     "malformed_byok_needed"})
        if state in {"missing_workspace", "malformed_byok_missing_workspace"}:
            assert status == 403 and calls
        elif state in {"valid", "malformed_byok_valid", "malformed_byok_needed"}:
            assert status == (500 if state == "malformed_byok_needed" and usage == "byok" else 200)
            if status == 200 and usage == "credits":
                assert response["data"]["stage_d"]["reason"] == reason


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_boot_and_policy_are_fresh_on_every_authorize():
    store, _, key = _seed_typed_gateway_store()
    request, body, raw, boot = signed_request(store, key)
    accepted = {boot.image_digest}
    calls = []
    request.scope["app"] = SimpleNamespace(state=SimpleNamespace(stage_d_policy_resolver=SimpleNamespace(
        kick=lambda: calls.append("kick"), accepted_image_digests=lambda: frozenset(accepted),
    )))
    settings = Settings(environment="test", stage_d_eligibility_enabled=True, stage_d_pilot_workspace_ids="")
    for step, expected in enumerate(["ok", "boot_not_accepted", "boot_not_accepted", "ok", "boot_not_accepted", "ok"]):
        if step == 1:
            store._delete_entities(SPEND_LEASE_BOOT_KIND, [boot.kid])
        elif step == 2:
            store.observe_spend_lease_boot(dataclasses.replace(boot, verified=False))
        elif step == 3:
            store.observe_spend_lease_boot(boot)  # Merge verification into an existing row.
        elif step == 4:
            accepted.clear()
        elif step == 5:
            accepted.add(boot.image_digest)
        body.idempotency_key = f"fresh-{step}"
        raw = body.model_dump_json().encode()
        signature = b64url_encode(_BOOT_KEYS[boot.kid].sign(boot_auth_digest("POST", "/", raw)))
        request = Request(request.scope | {"headers": [
            (b"x-tr-boot-auth", f"kid={boot.kid},sig={signature}".encode()),
        ]})
        response = gateway._authorize_gateway_sync(request, body, settings, raw)["data"]
        assert response["stage_d"]["reason"] == expected
        assert len(calls) == step + 1


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_boot_removal_after_auth_snapshot_takes_effect_next_authorize(monkeypatch):
    """ONE strong snapshot pins boot state, unlike main's later standalone read."""
    store, _, key = _seed_typed_gateway_store()
    request, body, raw, boot = signed_request(store, key)
    settings = Settings(environment="test", stage_d_eligibility_enabled=True,
                        stage_d_pilot_workspace_ids="",
                        spend_lease_accepted_gcp_image_digests=boot.image_digest)
    folded = SpannerBigtableStore.gateway_api_key_auth_context
    verdicts = []
    for joined in (False, True):
        store.observe_spend_lease_boot(boot)

        def resolve(self, *args, joined=joined, **kwargs):
            context = (folded if joined else separate_context)(self, *args, **kwargs)
            # A separate commit lands between metadata and main's boot read.
            self._delete_entities(SPEND_LEASE_BOOT_KIND, [boot.kid])
            return context

        monkeypatch.setattr(SpannerBigtableStore, "gateway_api_key_auth_context", resolve)
        body.idempotency_key = f"snapshot-race-{joined}"
        context = {"boot_auth": parse_boot_auth_header(request.headers["X-TR-Boot-Auth"]),
                   "boot_verified": False, "raw_body": raw}
        response = gateway._authorize_gateway_sync_impl(request, body, settings, context)
        verdicts.append((context["boot_verified"], response["data"]["stage_d"]["reason"]))
    assert verdicts == [(False, "boot_not_accepted"), (True, "ok")]
    monkeypatch.setattr(SpannerBigtableStore, "gateway_api_key_auth_context", folded)
    body.idempotency_key = "snapshot-race-next"
    response = gateway._authorize_gateway_sync(request, body, settings, raw)
    assert response["data"]["stage_d"]["reason"] == "boot_not_accepted"
