"""Compare the previous two-query metadata path with the folded strong read.

Run over the offline fake and the native GoogleSQL emulator in CI. The oracle
uses the unchanged bearer auth SQL and the standalone BYOK batch reader.
BYOK has no enabled/disabled field: legacy unknown flags must remain ignored.
"""
from __future__ import annotations

import dataclasses
import uuid

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp import _API_KEY_AUTH_CONTEXT_SQL, _auth_record
from trusted_router.storage_models import ApiKey, ApiKeyAuthContext, Workspace


@pytest.fixture(params=["fake", "spanner-emulator"])
def folded_store(request):
    if request.param == "fake":
        yield make_fake_store()[0]
    else:
        from tests.conformance.spanner_emulator import emulator_store

        _, _, instance_id = request.getfixturevalue("native_emulator_resources")
        with emulator_store(instance_id) as store:
            yield store


@pytest.mark.parametrize("key_type", [
    "standard", "management", "oauth", "strict", "uncapped", "byok_excluded",
    "federated", "disabled", "revoked", "missing", "missing_lookup",
])
@pytest.mark.parametrize("config_state", [
    "absent", "present", "deleted", "several", "empty_providers", "encrypted",
    "legacy_disabled_flags", "missing_workspace", "deleted_workspace", "paused_workspace",
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
            old_key, old_workspace, store.get_byok_providers(old_key.workspace_id, providers),
        )
    actual = store.gateway_api_key_auth_context(key.lookup_hash, providers=providers)
    assert actual == expected
    if key_type == "disabled":
        assert actual.api_key.disabled is True
    if actual is not None:
        assert set(actual.byok_configs) == set(providers)


def test_folded_byok_configs_skip_a_slug_the_fold_did_not_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BYOK slug missing from the auth-time fold falls back to a direct read, never KeyError."""
    from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
    from trusted_router.routes.internal import gateway

    endpoint = MODEL_ENDPOINTS["anthropic/claude-haiku-4.5@anthropic/byok"]
    candidates = [(MODELS[endpoint.model_id], endpoint)]
    reads: list[tuple[str, str]] = []

    class _Store:
        def get_byok_provider(self, workspace_id: str, provider: str) -> None:
            reads.append((workspace_id, provider))
            return None

    monkeypatch.setattr(gateway, "STORE", _Store())
    configs = gateway._byok_configs_for_candidates(candidates, "ws-fold", {})
    assert configs == {}
    assert gateway._get_byok_provider("ws-fold", endpoint.provider, configs) is None
    assert reads and all(workspace == "ws-fold" for workspace, _ in reads)
