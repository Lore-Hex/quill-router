"""Compare the previous two-query metadata path with the folded strong read.

Run over the offline fake and the native GoogleSQL emulator in CI. The oracle
uses the unchanged bearer auth SQL and the standalone BYOK batch reader.
The fold uses one strong snapshot: rotations committed afterward are visible
on the next authorize, unlike the original separate strong reads.
BYOK has no enabled/disabled field: legacy unknown flags must remain ignored.
"""
from __future__ import annotations

import dataclasses
import uuid

import pytest

from tests.fakes.spanner import make_fake_store
from tests.test_gateway_authorize_spanner_operations import (
    fixed_operation_catalog,  # noqa: F401 - fixture
    metadata_catalog,  # noqa: F401 - fixture
)
from trusted_router.storage_gcp import _API_KEY_AUTH_CONTEXT_SQL, _auth_record
from trusted_router.storage_models import ApiKey, ApiKeyAuthContext, Workspace


@pytest.fixture(params=["fake", "spanner-emulator"])
def folded_store(request):
    if request.param == "fake":
        yield make_fake_store()[0]
    else:
        from tests.conformance.spanner_emulator import emulator_store

        _, instance_id = request.getfixturevalue("native_emulator_resources")
        with emulator_store(instance_id) as store:
            yield store


@pytest.mark.parametrize("key_type", [
    "standard", "management", "oauth", "strict", "uncapped", "byok_excluded",
    "federated", "disabled", "revoked", "missing", "missing_lookup",
])
@pytest.mark.parametrize("config_state", [
    "absent", "present", "deleted", "several", "empty_providers", "encrypted",
    "legacy_disabled_flags", "missing_workspace", "deleted_workspace", "paused_workspace",
    "colliding_workspace",
])
def test_folded_auth_byok_matches_two_queries(folded_store, key_type, config_state):
    store = folded_store
    workspace_id = "fold-" + uuid.uuid4().hex
    workspace = Workspace(id=workspace_id, name="Fold", owner_user_id="fold-owner")
    store._write_entity("workspace", workspace_id, workspace)
    _, key = store.api_keys.create(
        workspace_id=workspace_id, name="fold", creator_user_id=workspace.owner_user_id,
        limit_microdollars=None if key_type == "uncapped" else 123456,
    )
    key.management = key_type == "management"
    key.budget_strict = key_type == "strict"
    key.include_byok_in_limit = key_type != "byok_excluded"
    key.disabled = key_type == "disabled"
    if key_type == "oauth":
        key.app_id = "fold-app"
        key.scopes = ["inference"]
    if key_type == "federated":
        key.federated_home = "https://home.example"
        key.secret_hash = ""
    store._write_entity("api_key", key.hash, dataclasses.asdict(key) | {"future_field": "ignored"})
    # Lookup metadata must not redirect the workspace or credential read.
    store._write_entity("api_key_lookup", key.lookup_hash,
                        {"key_id": key.hash, "workspace_id": "wrong-workspace"})
    providers = ["anthropic", "gemini", "google-ai-studio", "absent", "anthropic"]
    if config_state != "absent":
        for provider in ("anthropic", "gemini", "google-ai-studio"):
            config = store.upsert_byok_provider(
                workspace_id=workspace_id, provider=provider,
                secret_ref=f"fixture/{provider}", key_hint="fixture",
            )
            record = dataclasses.asdict(config) | {"future_field": "ignored"}
            if config_state == "legacy_disabled_flags":
                record.update(enabled=False, disabled=True)
            if config_state == "encrypted":
                record["encrypted_secret"] = {
                    "algorithm": "fixture", "key_ref": "fixture", "encrypted_dek": "dek",
                    "dek_nonce": "nonce", "ciphertext": "cipher", "nonce": "nonce",
                }
            store._write_entity("byok", f"{workspace_id}#{provider}", record)
        if config_state == "deleted":
            for provider in providers[:3]:
                assert store.delete_byok_provider(workspace_id, provider)
    if config_state == "colliding_workspace":
        for provider in providers[:3]:
            store.delete_byok_provider(workspace_id, provider)
        store.upsert_byok_provider(workspace_id=workspace_id + "#other", provider="anthropic",
                                   secret_ref="fixture/other", key_hint="other")  # noqa: S106
    if config_state == "empty_providers":
        providers = []
    elif config_state == "present":
        providers = ["anthropic"]
    elif config_state == "missing_workspace":
        store._delete_entities("workspace", [workspace_id])
    elif config_state in {"deleted_workspace", "paused_workspace"}:
        workspace.deleted = config_state == "deleted_workspace"
        workspace.billing_paused = config_state == "paused_workspace"
        store._write_entity("workspace", workspace_id, workspace)
    if key_type == "revoked":
        assert store.delete_key(key.hash)
    elif key_type == "missing":
        store._delete_entities("api_key", [key.hash])
    elif key_type == "missing_lookup":
        store._delete_entities("api_key_lookup", [key.lookup_hash])

    with store._database.snapshot() as snapshot:
        old_rows = list(snapshot.execute_sql(
            _API_KEY_AUTH_CONTEXT_SQL, params={"lookup_hash": key.lookup_hash},
            param_types={"lookup_hash": store._param_types.STRING},
        ))
    expected = None
    if old_rows:
        old_key = _auth_record(str(old_rows[0][0]), ApiKey)
        old_workspace = (_auth_record(str(old_rows[0][1]), Workspace)
                         if old_rows[0][1] is not None else None)
        if old_workspace is not None and old_workspace.deleted:
            old_workspace = None
        expected = ApiKeyAuthContext(
            old_key, old_workspace,
        )
    actual = store.gateway_api_key_auth_context(key.lookup_hash)
    if expected is None:
        assert actual is None
        return
    assert actual.api_key == expected.api_key
    assert actual.workspace == expected.workspace
    # Compare only credentials consumed for request candidates, not prefetch fields.
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routes.internal import gateway

    endpoint = MODEL_ENDPOINTS["anthropic/claude-haiku-4.5@anthropic/byok"]
    candidates = [(MODELS[endpoint.model_id], endpoint)] if providers else []
    consumed = gateway._byok_configs_for_candidates(
        candidates, workspace_id, folded_rows=actual.byok_rows,
    )
    assert consumed == store.get_byok_providers(workspace_id, ["anthropic"] if providers else [])


def test_a_candidate_slug_absent_from_the_fetched_range_is_explicitly_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fetched range is complete for its workspace: an absent slug is None, with no extra read."""
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routes.internal import gateway

    endpoint = MODEL_ENDPOINTS["anthropic/claude-haiku-4.5@anthropic/byok"]
    candidates = [(MODELS[endpoint.model_id], endpoint)]

    class _NoReads:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected store read: {name}")

    monkeypatch.setattr(gateway, "STORE", _NoReads())
    configs = gateway._byok_configs_for_candidates(candidates, "ws-fold", folded_rows={"ws-fold#other": "{}"})
    assert configs and all(value is None for value in configs.values())
    assert gateway._get_byok_provider("ws-fold", endpoint.provider, configs) is None


@pytest.mark.parametrize("scenario_index", range(6))
def test_byok_range_manifest_rows(folded_store, scenario_index):
    """One snapshot contains 0/1/3 own rows, collision rows, and nullable boot lookup."""
    import json

    from tests.conformance.spanner_sql_inventory import load_manifest
    from trusted_router.storage_gcp import _GATEWAY_API_KEY_AUTH_CONTEXT_SQL

    scenario = load_manifest()["expressions"]["storage_gcp:constants:3"]["scenarios"][scenario_index]
    store = folded_store
    for entity in scenario["entities"]:
        store._write_entity(entity["kind"], entity["id"], entity["body"])
    with store._database.snapshot() as snapshot:
        rows = list(snapshot.execute_sql(
            _GATEWAY_API_KEY_AUTH_CONTEXT_SQL,
            params=scenario["values"],
            param_types=dict.fromkeys(scenario["types"], store._param_types.STRING),
        ))
    assert len(rows) == 1
    key, workspace, byok, boot = rows[0]
    assert json.loads(key) == {"workspace_id": "ws"}
    assert json.loads(workspace) == {"id": "ws"}
    assert {entity_id: json.loads(body) for entity_id, body in byok} == {
        entity["id"]: entity["body"] for entity in scenario["entities"] if entity["kind"] == "byok"
    }
    assert (json.loads(boot) if boot else None) == (
        {"kid": "fixture-boot-kid"} if scenario["values"]["boot_kid"] else None
    )


@pytest.mark.parametrize("key_type", [
    "standard", "management", "oauth", "strict", "uncapped", "byok_excluded",
    "federated", "disabled", "expired", "scope", "missing", "missing_lookup",
])
@pytest.mark.parametrize("config_state", [
    "absent", "present", "deleted", "several", "legacy_disabled_flags",
    "missing_workspace", "deleted_workspace", "paused_workspace", "colliding_workspace",
])
def test_complete_byok_authorize_differential(key_type, config_state, metadata_catalog, monkeypatch):  # noqa: F811 - fixture
    """Main reads only metadata before auth; compare consumed configs and full outcomes.

    This quiescent comparison excludes concurrent commits: the new contract pins
    key, workspace, BYOK and boot to one strong snapshot (see the boot race test).
    """
    import copy

    from fastapi import HTTPException

    from tests.conformance.test_gateway_auth_boot_fold import separate_context
    from tests.test_gateway_authorize_spanner_operations import (
        _lookup_body,
        _request,
        _seed_typed_gateway_store,
    )
    from trusted_router.config import Settings
    from trusted_router.routes.internal import gateway
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_models import OAuthApp

    store, database, key = _seed_typed_gateway_store()
    key.management = key_type == "management"
    key.budget_strict = key_type == "strict"
    key.include_byok_in_limit = key_type != "byok_excluded"
    key.disabled = key_type == "disabled"
    if key_type == "uncapped":
        key.limit_microdollars = None
    elif key_type == "expired":
        key.expires_at = "2020-01-01T00:00:00Z"
    elif key_type == "scope":
        key.scopes = ["profile"]
    elif key_type == "oauth":
        key.app_id, key.scopes = "fold-app", ["inference"]
        store.create_oauth_app(OAuthApp(id=key.app_id, owner_user_id="user-rpc",
                                        name="Fold", redirect_uris=[]))
    elif key_type == "federated":
        key.federated_home = "https://home.invalid"
        monkeypatch.setattr(gateway, "_federated_key_still_valid", lambda cached, _: cached)
    store._write_entity("api_key", key.hash, key)
    if key_type == "missing":
        store._delete_entities("api_key", [key.hash])
    elif key_type == "missing_lookup":
        store._delete_entities("api_key_lookup", [key.lookup_hash])
    providers = ["anthropic", "gemini", "google-ai-studio"] if config_state == "several" else ["anthropic"]
    if config_state != "absent":
        workspace_id = key.workspace_id + ("#other" if config_state == "colliding_workspace" else "")
        for provider in providers:
            config = store.upsert_byok_provider(workspace_id=workspace_id, provider=provider,
                                               secret_ref=f"fixture/{provider}", key_hint="fixture")
            if config_state == "legacy_disabled_flags":
                store._write_entity("byok", f"{workspace_id}#{provider}",
                                    dataclasses.asdict(config) | {"enabled": False, "disabled": True})
            elif config_state == "deleted":
                store.delete_byok_provider(workspace_id, provider)
    workspace = store.get_workspace(key.workspace_id)
    if config_state == "missing_workspace":
        store._delete_entities("workspace", [workspace.id])
    elif config_state in {"deleted_workspace", "paused_workspace"}:
        workspace.deleted = config_state == "deleted_workspace"
        workspace.billing_paused = config_state == "paused_workspace"
        store._write_entity("workspace", workspace.id, workspace)
    initial = {name: copy.deepcopy(value) for name, value in vars(database).items()
               if isinstance(value, dict)}
    folded = SpannerBigtableStore.gateway_api_key_auth_context
    consume = gateway._byok_configs_for_candidates
    outcomes = []
    for joined in (False, True):
        for name, value in initial.items():
            setattr(database, name, copy.deepcopy(value))
        captured = []
        counter = iter(range(1, 1000))
        monkeypatch.setattr(uuid, "uuid4", lambda counter=counter: uuid.UUID(int=next(counter)))
        monkeypatch.setattr(SpannerBigtableStore, "gateway_api_key_auth_context",
                            folded if joined else separate_context)

        def consumed(*args, captured=captured, **kwargs):
            configs = consume(*args, **kwargs)
            captured.append(copy.deepcopy(configs))
            return configs

        monkeypatch.setattr(gateway, "_byok_configs_for_candidates", consumed)
        body = _lookup_body(key)
        body.provider = {"usage": "byok"}
        boot_context = {"boot_auth": None, "boot_verified": False, "raw_body": b""}
        try:
            response = gateway._authorize_gateway_sync_impl(
                _request(), body, Settings(environment="test"), boot_context,
            )
            status = 200
        except HTTPException as exc:
            response, status = {"detail": exc.detail, "headers": exc.headers}, exc.status_code
        outcomes.append((captured, boot_context["boot_verified"], status, response))
    assert outcomes[1] == outcomes[0]
    if key_type in {"disabled", "expired", "missing", "missing_lookup"}:
        assert outcomes[1][2] == 401
    elif key_type == "scope" or config_state in {"missing_workspace", "deleted_workspace"}:
        assert outcomes[1][2] == 403
    elif config_state == "paused_workspace":
        assert outcomes[1][2] == 503
    elif config_state in {"absent", "deleted", "colliding_workspace"}:
        assert outcomes[1][2] == 400
    else:
        assert outcomes[1][2] == 200
