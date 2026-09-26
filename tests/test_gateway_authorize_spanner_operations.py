from __future__ import annotations

import copy
import dataclasses
import inspect
import json
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.fakes.spanner import (
    FakeSpannerDatabase,
    _FakeSnapshot,
    _FakeTransaction,
    make_fake_store,
)
from trusted_router import (
    spend_lease_authorize,
    storage_gcp_authorize,
    storage_gcp_key_escrow,
    storage_gcp_spend_lease_authorize,
)
from trusted_router.catalog import MODEL_ENDPOINTS, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import CreditAccount, Workspace, configure_store
from trusted_router.storage_gcp import _API_KEY_AUTH_CONTEXT_SQL, SpannerBigtableStore
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counter_dml import RESERVATION_COLUMNS
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_request_records import _INSERT_GATEWAY_AUTHORIZATION_SQL
from trusted_router.storage_models import ApiKey, ApiKeyAuthContext, CreditProvenance


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _seed_typed_gateway_store() -> tuple[SpannerBigtableStore, object, object]:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="ws-rpc", name="RPC", owner_user_id="user-rpc")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id,
        "shard": 0,
        "total_credits": 50_000_000,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id,
        name="rpc-key",
        creator_user_id=workspace.owner_user_id,
        limit_microdollars=50_000_000,
    )
    configure_store(store)
    return store, database, key


def _body(key_hash: str, *, idempotency_key: str = "rpc-idem") -> GatewayAuthorizeRequest:
    return GatewayAuthorizeRequest(
        api_key_hash=key_hash,
        idempotency_key=idempotency_key,
        model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100,
        max_output_tokens=100,
    )


@pytest.fixture
def fixed_operation_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Count storage operations against a fixed prepaid/BYOK route shape.

    Hourly catalog changes can add/remove BYOK eligibility reads. They must
    not change the fixture under an exact transaction-operation assertion.
    """
    model = "anthropic/claude-haiku-4.5"
    for endpoint_id, endpoint in tuple(MODEL_ENDPOINTS.items()):
        if endpoint.model_id == model:
            monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    for usage_type, suffix in (("Credits", "prepaid"), ("BYOK", "byok")):
        endpoint = ModelEndpoint(
            id=f"{model}@anthropic/{suffix}",
            model_id=model,
            provider="anthropic",
            usage_type=usage_type,
            upstream_id="claude-haiku-4-5-20251001",
            supported_parameters=("max_tokens",),
            prompt_price_microdollars_per_million_tokens=1_000_000,
            completion_price_microdollars_per_million_tokens=5_000_000,
        )
        monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)


def test_typed_authorize_route_does_not_call_legacy_idempotency_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, database, key = _seed_typed_gateway_store()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("typed authorize must never probe the legacy entity index")

    monkeypatch.setattr(
        SpannerBigtableStore,
        "get_gateway_authorization_by_idempotency_key",
        forbidden,
    )

    response = gateway._authorize_gateway_sync(
        _request(), _body(key.hash), Settings(environment="test")
    )

    assert response["data"]["authorization_id"]
    assert response["data"]["idempotent_replay"] is False
    assert database.transaction_tags[-1] == "tr_authorize"


def test_typed_authorize_route_does_not_call_typed_pretransaction_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, key = _seed_typed_gateway_store()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("typed fresh authorize must rely on its in-transaction probe")

    monkeypatch.setattr(
        SpannerBigtableStore,
        "get_typed_authorization_by_idempotency",
        forbidden,
    )

    response = gateway._authorize_gateway_sync(
        _request(), _body(key.hash), Settings(environment="test")
    )

    assert response["data"]["authorization_id"]
    assert response["data"]["idempotent_replay"] is False


def test_spend_lease_binding_flag_off_never_calls_prepare_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, key = _seed_typed_gateway_store()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("flag-off authorize touched the unit-2 binding path")

    for module in (spend_lease_authorize, storage_gcp_spend_lease_authorize):
        for name, value in vars(module).items():
            if (
                not name.startswith("_")
                and inspect.isfunction(value)
                and value.__module__ == module.__name__
            ):
                monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(
        SpannerBigtableStore,
        "prepare_gateway_spend_lease_binding",
        forbidden,
    )

    response = gateway._authorize_gateway_sync(
        _request(), _body(key.hash), Settings(environment="test")
    )
    assert response["data"]["authorization_id"]


def test_typed_authorize_replay_and_mismatch_still_come_from_transaction() -> None:
    _store, _database, key = _seed_typed_gateway_store()
    settings = Settings(environment="test")
    first = gateway._authorize_gateway_sync(_request(), _body(key.hash), settings)

    replay = gateway._authorize_gateway_sync(_request(), _body(key.hash), settings)

    assert replay["data"]["authorization_id"] == first["data"]["authorization_id"]
    assert replay["data"]["credit_reservation_id"] == first["data"]["credit_reservation_id"]
    assert replay["data"]["idempotent_replay"] is True

    changed = _body(key.hash)
    changed.max_output_tokens += 1
    with pytest.raises(Exception) as raised:
        gateway._authorize_gateway_sync(_request(), changed, settings)
    assert getattr(raised.value, "status_code", None) == 409


def test_typed_replay_race_returns_stored_authorization_without_second_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation guard: restoring the reservation-id assert makes this fail."""
    store, database, key = _seed_typed_gateway_store()
    key.usage_shard_count = 2
    store._write_entity("api_key", key.hash, key)
    settings = Settings(environment="test")
    first = gateway._authorize_gateway_sync(_request(), _body(key.hash), settings)

    real_authorize_atomic = storage_gcp_authorize.authorize_atomic
    forced_retry = False

    def force_cold_retry(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal forced_retry
        result = real_authorize_atomic(*args, **kwargs)
        if not forced_retry:
            assert result["outcome"] == AuthorizeOutcome.REPLAY
            forced_retry = True
            return {"outcome": AuthorizeOutcome.KEY_LIMIT_EXCEEDED}
        return result

    monkeypatch.setattr(storage_gcp_authorize, "authorize_atomic", force_cold_retry)
    monkeypatch.setattr(
        storage_gcp_key_escrow,
        "rebalance_key_limit_headroom",
        lambda *_args, **_kwargs: True,
    )

    replay = gateway._authorize_gateway_sync(_request(), _body(key.hash), settings)

    assert forced_retry is True
    assert replay["data"]["idempotent_replay"] is True
    assert replay["data"]["authorization_id"] == first["data"]["authorization_id"]
    assert (
        replay["data"]["credit_reservation_id"]
        == first["data"]["credit_reservation_id"]
    )
    assert list(database.reservations) == [first["data"]["credit_reservation_id"]]


def test_typed_replay_has_exact_sequential_spanner_operation_count(
    fixed_operation_catalog: None,
) -> None:
    _store, database, key = _seed_typed_gateway_store()
    gateway._BROADCAST_EMPTY_CACHE.clear()
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    gateway._authorize_gateway_sync(_request(), _body(key.hash), Settings(environment="test"))
    before = (
        database.snapshot_execute_sql_calls,
        database.transaction_execute_sql_calls,
        database.transaction_execute_update_calls,
        database.transaction_batch_update_calls,
    )

    replay = gateway._authorize_gateway_sync(
        _request(), _body(key.hash), Settings(environment="test")
    )

    after = (
        database.snapshot_execute_sql_calls,
        database.transaction_execute_sql_calls,
        database.transaction_execute_update_calls,
        database.transaction_batch_update_calls,
    )
    operation_count = sum(end - start for start, end in zip(before, after, strict=True))
    assert replay["data"]["idempotent_replay"] is True
    # Stage C adds one same-transaction read of the nullable receipt columns.
    # It is unconditional on replay so rollback cannot let a receipt-less
    # request reuse a historical locally admitted authorization.
    # One provider BYOK lookup; no dependence on optional live catalog routes.
    assert operation_count == 6


def test_typed_accepted_authorization_is_returned_without_post_commit_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key = _seed_typed_gateway_store()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("accepted authorize already has the exact inserted record")

    monkeypatch.setattr(SpannerBigtableStore, "get_gateway_authorization", forbidden)
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=key.workspace_id,
        key_hash=key.hash,
        estimate=1_000_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="m",
        provider="anthropic",
        requested_model_id="m",
        candidate_model_ids=["m"],
        region="us",
        endpoint_id="e",
        candidate_endpoint_ids=["e"],
        idempotency_key="direct-idem",
        idempotency_fingerprint="direct-fingerprint",
    )

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None


def test_typed_accepted_authorization_matches_persisted_record() -> None:
    store, _database, key = _seed_typed_gateway_store()
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id=key.workspace_id,
        key_hash=key.hash,
        estimate=1_000_000,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="m",
        provider="anthropic",
        requested_model_id="m",
        candidate_model_ids=["m"],
        region="us",
        endpoint_id="e",
        candidate_endpoint_ids=["e"],
        idempotency_key="direct-equivalence",
        idempotency_fingerprint="direct-equivalence-fingerprint",
    )
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None

    persisted = store.get_gateway_authorization(authorization.id)

    assert persisted is not None
    assert dataclasses.asdict(authorization) == dataclasses.asdict(persisted)


@pytest.mark.parametrize("armed", [False, True])
def test_fresh_typed_gateway_authorize_has_exact_sequential_spanner_operation_count(
    armed: bool, fixed_operation_catalog: None,
) -> None:
    store, database, key = _seed_typed_gateway_store()
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=armed)
    gateway._BROADCAST_EMPTY_CACHE.clear()
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    before = (
        database.snapshot_execute_sql_calls,
        database.transaction_execute_sql_calls,
        database.transaction_execute_update_calls,
        database.transaction_batch_update_calls,
    )

    response = gateway._authorize_gateway_sync(
        _request(), _body(key.hash), Settings(environment="test")
    )

    after = (
        database.snapshot_execute_sql_calls,
        database.transaction_execute_sql_calls,
        database.transaction_execute_update_calls,
        database.transaction_batch_update_calls,
    )
    operation_count = sum(end - start for start, end in zip(before, after, strict=True))
    assert response["data"]["authorization_id"]
    # Representative steady-state fresh request: the workspace's observed-empty
    # broadcast cache is warm, while this idempotency key and authorization are new.
    # Seven SQL/batch calls (previously eight) for this fixed prepaid/BYOK catalog.
    # Armed authorization adds one selected-shard pause/epoch read.
    assert operation_count == 7 + int(armed)


def test_broadcast_empty_results_are_cached_until_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, key = _seed_typed_gateway_store()
    gateway._BROADCAST_EMPTY_CACHE.clear()
    now = [100.0]
    calls = 0

    def list_empty(_self: object, workspace_id: str) -> list[object]:
        nonlocal calls
        assert workspace_id == key.workspace_id
        calls += 1
        return []

    monkeypatch.setattr(gateway.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(SpannerBigtableStore, "list_broadcast_destinations", list_empty)

    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    assert calls == 1

    now[0] += gateway._BROADCAST_EMPTY_CACHE_TTL_SECONDS + 0.001
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == []
    assert calls == 2


def test_positive_broadcast_results_are_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, key = _seed_typed_gateway_store()
    gateway._BROADCAST_EMPTY_CACHE.clear()
    destination = object()
    calls = 0

    def list_positive(_self: object, workspace_id: str) -> list[object]:
        nonlocal calls
        assert workspace_id == key.workspace_id
        calls += 1
        return [destination]

    monkeypatch.setattr(SpannerBigtableStore, "list_broadcast_destinations", list_positive)

    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == [destination]
    assert gateway._broadcast_destinations_for_authorize(key.workspace_id) == [destination]
    assert calls == 2
    assert key.workspace_id not in gateway._BROADCAST_EMPTY_CACHE


def test_broadcast_empty_cache_evicts_oldest_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _database, _key = _seed_typed_gateway_store()
    gateway._BROADCAST_EMPTY_CACHE.clear()
    monkeypatch.setattr(gateway, "_BROADCAST_EMPTY_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(
        SpannerBigtableStore,
        "list_broadcast_destinations",
        lambda _self, _workspace_id: [],
    )

    for workspace_id in ("ws-1", "ws-2", "ws-3"):
        assert gateway._broadcast_destinations_for_authorize(workspace_id) == []

    assert list(gateway._BROADCAST_EMPTY_CACHE) == ["ws-2", "ws-3"]


def _lookup_body(key: ApiKey, *, idempotency_key: str = "metadata-idem") -> GatewayAuthorizeRequest:
    body = _body(key.hash, idempotency_key=idempotency_key)
    body.api_key_hash = None
    body.api_key_lookup_hash = key.lookup_hash
    return body


@pytest.fixture
def metadata_catalog(fixed_operation_catalog: None, monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = MODEL_ENDPOINTS["anthropic/claude-haiku-4.5@anthropic/byok"]
    # Two endpoints share one credential; Google also has a legacy storage alias.
    for suffix, provider in (("duplicate", "anthropic"), ("google", "google-ai-studio")):
        extra = dataclasses.replace(endpoint, id=f"{endpoint.id}-{suffix}", provider=provider)
        monkeypatch.setitem(MODEL_ENDPOINTS, extra.id, extra)


@pytest.fixture
def spanner_operations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict]]:
    """Capture actual snapshot/transaction calls in order, including commit."""
    operations: list[tuple[str, str, dict]] = []

    def wrap(cls: type, method: str, label: str) -> None:
        original = getattr(cls, method)

        def recorded(self: object, sql: str, **kwargs: Any) -> Any:
            if getattr(self, "_in_batch", False):
                return original(self, sql, **kwargs)
            operations.append((label, " ".join(sql.split()), copy.deepcopy(kwargs.get("params", {}))))
            return original(self, sql, **kwargs)

        monkeypatch.setattr(cls, method, recorded)

    wrap(_FakeSnapshot, "execute_sql", "RO")
    wrap(_FakeTransaction, "execute_sql", "T1 SELECT")
    wrap(_FakeTransaction, "execute_update", "T1 DML")
    original_batch = _FakeTransaction.batch_update

    def batched(self: Any, statements: Any, **kwargs: Any) -> Any:
        operations.append(("T1 BATCH", "", {"statements": copy.deepcopy(statements)}))
        return original_batch(self, statements, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "batch_update", batched)
    original = FakeSpannerDatabase.run_in_transaction

    def committed(self: FakeSpannerDatabase, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        operations.append(("COMMIT", "", {}))
        return result

    monkeypatch.setattr(FakeSpannerDatabase, "run_in_transaction", committed)
    return operations


@pytest.mark.parametrize("armed", [False, True])
def test_warm_lookup_authorize_exact_sequence_and_contents(
    armed: bool, metadata_catalog: None, spanner_operations: list[tuple[str, str, dict]],
) -> None:
    store, database, key = _seed_typed_gateway_store()
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=armed)
    settings = Settings(environment="test")
    # Warm both the empty-broadcast and credit-configuration caches through authorize.
    gateway._authorize_gateway_sync(_request(), _lookup_body(key, idempotency_key="warmup"), settings)
    spanner_operations.clear()
    database.snapshot_calls.clear()
    response = gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)["data"]
    operations = spanner_operations
    # Warm lookup: 2 metadata reads + idempotency + credit + batch + commit.
    # Trust adds its selected-shard read; pre-6a had one extra key RPC.
    assert len(operations) == 6 + int(armed)
    assert operations[-2][0] == "T1 BATCH"
    batch = operations[-2][2]["statements"]
    assert len(batch) == 3
    assert all(set(params) == set(types) for _, params, types in batch)
    # Expand only for the existing statement/parameter assertions below.
    operations = [*operations[:-2], *[
        ("T1 DML", " ".join(sql.split()), params) for sql, params, _ in batch
    ], operations[-1]]
    assert operations[:2] == [
        ("RO", " ".join("""
            /* api_key_auth_context */
            SELECT key_record.body, workspace_record.body
            FROM tr_entities AS lookup_record
            JOIN tr_entities AS key_record
              ON key_record.kind='api_key'
             AND key_record.id=JSON_VALUE(lookup_record.body, '$.key_id')
            LEFT JOIN tr_entities AS workspace_record
              ON workspace_record.kind='workspace'
             AND workspace_record.id=JSON_VALUE(key_record.body, '$.workspace_id')
            WHERE lookup_record.kind='api_key_lookup'
              AND lookup_record.id=@lookup_hash
        """.split()), {"lookup_hash": key.lookup_hash}),
        ("RO", "SELECT id, body FROM tr_entities WHERE kind=@kind AND id IN UNNEST(@ids)",
         {"kind": "byok", "ids": ["ws-rpc#anthropic", "ws-rpc#gemini", "ws-rpc#google-ai-studio"]}),
    ]
    # No staleness (nor multi-use) option, for either authentication or credentials.
    assert database.snapshot_calls == [{}, {}]
    assert operations[2][0:2] == ("T1 SELECT",
        "SELECT reservation_id, credit_reserved_micro, key_reserved_micro, "
        "hold_usage_type, authorization_id, idempotency_fingerprint, settled, "
        "credit_shard, ws_shard, key_shard FROM tr_reservation WHERE idempotency_scope=@scope")
    reservation = operations[-3][2]
    assert operations[2][2] == {"scope": reservation["idempotency_scope"]}
    assert operations[3] == ("T1 DML",
        "UPDATE tr_credit_balance SET reserved = reserved + @est "
        "WHERE workspace_id=@ws AND shard=@shard "
        "AND (total_credits - total_usage - reserved) >= @est",
        {"est": reservation["credit_reserved_micro"], "ws": key.workspace_id, "shard": 0})
    if armed:
        assert operations[4] == ("T1 SELECT",
            "SELECT billing_pause_causes, pause_epoch FROM tr_credit_balance "
            "WHERE workspace_id=@ws AND shard=@shard", {"ws": key.workspace_id, "shard": 0})
    assert operations[-4] == ("T1 DML",
        "UPDATE tr_key_limit SET reserved = reserved + @est "
        "WHERE key_hash=@kh AND shard=@shard AND limit_micro IS NOT NULL "
        "AND (@is_byok = FALSE OR include_byok = TRUE) "
        "AND (limit_micro - usage - IF(include_byok, byok_usage, 0) - reserved) >= @est",
        {"est": reservation["key_reserved_micro"], "kh": key.hash, "shard": 0, "is_byok": False})
    assert operations[-3][0:2] == ("T1 DML",
        f"INSERT INTO tr_reservation ({', '.join(RESERVATION_COLUMNS)}) "  # noqa: S608 - fixed columns
        f"VALUES ({', '.join('@' + c for c in RESERVATION_COLUMNS)})")  # noqa: S608 - fixed columns
    assert set(reservation) == set(RESERVATION_COLUMNS)
    assert reservation["workspace_id"] == key.workspace_id
    assert reservation["key_hash"] == key.hash
    assert reservation["reservation_id"] == response["credit_reservation_id"]
    assert reservation["authorization_id"] == response["authorization_id"]
    assert reservation["hold_usage_type"] == "Credits"
    assert reservation["credit_reserved_micro"] == reservation["key_reserved_micro"] == 600
    assert operations[-2][0:2] == ("T1 DML", " ".join(_INSERT_GATEWAY_AUTHORIZATION_SQL.split()))
    inserted = operations[-2][2]
    assert inserted["workspace_id"] == key.workspace_id
    assert inserted["key_hash"] == key.hash
    assert inserted["authorization_id"] == response["authorization_id"]
    assert inserted["reservation_id"] == response["credit_reservation_id"]
    assert inserted["estimated_microdollars"] == 600
    assert inserted["usage_type"] == "Credits"
    assert json.loads(inserted["payload"])["candidate_endpoint_ids"] == [
        "anthropic/claude-haiku-4.5@anthropic/prepaid"
    ]
    assert operations[-1] == ("COMMIT", "", {})


@pytest.mark.parametrize(("change", "status", "message", "error_type"), [
    ("disabled", 401, "Invalid API key", "invalid_api_key"),
    ("revoked", 401, "Invalid API key", "invalid_api_key"),
    ("missing_key", 401, "Invalid API key", "invalid_api_key"),
    ("missing_lookup", 401, "Invalid API key", "invalid_api_key"),
    ("expired", 401, "Invalid API key", "invalid_api_key"),
    ("scope", 403, "API key is missing required scope: inference", "insufficient_scope"),
    ("deleted_workspace", 403, "Workspace is unavailable", "forbidden"),
    ("missing_workspace", 403, "Workspace is unavailable", "forbidden"),
    ("paused_workspace", 503, "Workspace billing is paused", "service_unavailable"),
    ("pause_causes", 503, "Workspace billing is paused", "service_unavailable"),
])
def test_metadata_changes_between_authorizes_fail_closed(
    change: str, status: int, message: str, error_type: str, fixed_operation_catalog: None,
) -> None:
    store, database, key = _seed_typed_gateway_store()
    settings = Settings(environment="test")
    gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)
    if change == "disabled":
        store.update_key(key.hash, {"disabled": True})
    elif change == "expired":
        key.expires_at = "2000-01-01T00:00:00Z"
        store._write_entity("api_key", key.hash, key)
    elif change == "scope":
        key.scopes = ["profile"]
        store._write_entity("api_key", key.hash, key)
    elif change == "revoked":
        assert store.delete_key(key.hash)
    elif change == "missing_key":
        store._delete_entities("api_key", [key.hash])
    elif change == "missing_lookup":
        store._delete_entities("api_key_lookup", [key.lookup_hash])
    elif change == "missing_workspace":
        store._delete_entities("workspace", [key.workspace_id])
    else:
        workspace = store.get_workspace(key.workspace_id)
        assert workspace is not None
        workspace.deleted = change == "deleted_workspace"
        workspace.billing_paused = change == "paused_workspace"
        workspace.billing_pause_causes = ["operator"] if change == "pause_causes" else []
        store._write_entity("workspace", key.workspace_id, workspace)
    before = database.transaction_execute_update_calls
    with pytest.raises(HTTPException) as raised:
        gateway._authorize_gateway_sync(
            _request(), _lookup_body(key, idempotency_key="after-change"), settings,
        )
    assert raised.value.status_code == status
    assert raised.value.detail == {"error": {
        "code": status, "message": message, "type": error_type, "source": "router",
    }}
    assert raised.value.headers == ({"Retry-After": "30"} if status == 503 else None)
    assert database.transaction_execute_update_calls == before


def test_joined_metadata_matches_separate_parsers_and_canonical_workspace() -> None:
    store, database, key = _seed_typed_gateway_store()
    # Forward-compatible fields and a misleading lookup workspace must behave
    # exactly like get_by_lookup_hash followed by get_workspace.
    raw = dataclasses.asdict(key) | {"future_field": "ignored"}
    store._write_entity("api_key", key.hash, raw)
    store._write_entity("api_key_lookup", key.lookup_hash,
                        {"key_id": key.hash, "workspace_id": "ws-wrong"})
    expected_key = store.get_key_by_lookup_hash(key.lookup_hash)
    expected_workspace = store.get_workspace(key.workspace_id)
    database.snapshot_calls.clear()
    context = store.gateway_api_key_auth_context(key.lookup_hash)
    assert context == ApiKeyAuthContext(expected_key, expected_workspace)
    assert type(context.api_key) is ApiKey
    assert type(context.workspace) is Workspace
    assert database.snapshot_calls == [{}]


def test_remapped_lookup_uses_new_keys_workspace(fixed_operation_catalog: None) -> None:
    store, _database, key = _seed_typed_gateway_store()
    settings = Settings(environment="test")
    gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)
    other = Workspace(id="ws-other", name="Other", owner_user_id="other", billing_paused=True)
    store._write_entity("workspace", other.id, other)
    _, other_key = store.api_keys.create(workspace_id=other.id, name="other", creator_user_id="other")
    store._write_entity("api_key_lookup", key.lookup_hash,
                        {"key_id": other_key.hash, "workspace_id": key.workspace_id})
    context = store.gateway_api_key_auth_context(key.lookup_hash)
    assert context.api_key == other_key
    assert context.workspace == other
    with pytest.raises(HTTPException) as raised:
        gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)
    assert raised.value.status_code == 503
    assert raised.value.detail["error"]["message"] == "Workspace billing is paused"
    assert raised.value.detail["error"]["type"] == "service_unavailable"


def test_byok_batch_covers_candidates_aliases_and_removal(
    metadata_catalog: None, spanner_operations: list[tuple[str, str, dict]],
) -> None:
    store, database, key = _seed_typed_gateway_store()
    settings = Settings(environment="test")
    configs = {}
    for provider in ("anthropic", "gemini", "google-ai-studio"):
        configs[provider] = store.upsert_byok_provider(
            workspace_id=key.workspace_id, provider=provider,
            secret_ref=f"secret-{provider}", key_hint=provider,
        )
    assert store.get_byok_providers(key.workspace_id, list(configs)) == configs
    gateway._authorize_gateway_sync(_request(), _lookup_body(key, idempotency_key="warm"), settings)
    spanner_operations.clear()
    first = gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)["data"]
    # Even with all BYOK credentials, Credits wins the reservation semantics.
    assert first["limit_usage_type"] == "Credits"
    assert first["credit_reservation_id"]
    candidates = first["route_candidates"]
    assert len(candidates) == 4
    assert {c["byok_secret_ref"] for c in candidates if c["usage_type"] == "BYOK"} == {
        "secret-anthropic", "secret-google-ai-studio",
    }
    assert [op[1] for op in spanner_operations if op[0] == "RO"] == [
        " ".join(_API_KEY_AUTH_CONTEXT_SQL.split()),
        "SELECT id, body FROM tr_entities WHERE kind=@kind AND id IN UNNEST(@ids)",
    ]
    # Removing the preferred Google alias must expose the legacy envelope;
    # removing anthropic excludes BOTH candidates on the very next authorize.
    assert store.delete_byok_provider(key.workspace_id, "anthropic")
    assert store.delete_byok_provider(key.workspace_id, "google-ai-studio")
    second = gateway._authorize_gateway_sync(
        _request(), _lookup_body(key, idempotency_key="removed"), settings,
    )["data"]
    assert second["limit_usage_type"] == "Credits"
    byok = [c for c in second["route_candidates"] if c["usage_type"] == "BYOK"]
    assert len(byok) == 1
    assert byok[0]["byok_secret_ref"] == "secret-gemini"  # noqa: S105 - fixture reference
    assert byok[0]["byok_provider"] == "gemini"
    assert store.delete_byok_provider(key.workspace_id, "gemini")
    # BYOK-only request now has the existing exact no-candidate error.
    body = _lookup_body(key, idempotency_key="no-byok")
    body.provider = {"only": ["google-ai-studio"], "allow_fallbacks": False}
    with pytest.raises(HTTPException) as raised:
        gateway._authorize_gateway_sync(_request(), body, settings)
    assert raised.value.status_code == 400
    assert raised.value.detail["error"]["type"] == "provider_not_supported"
    assert raised.value.detail["error"]["message"] == "No authorized route candidates are available for this workspace"
    assert database.snapshot_calls  # all reads above remain strong
    assert all(not call for call in database.snapshot_calls)


def test_authorize_reads_topup_after_insufficient_credits(fixed_operation_catalog: None) -> None:
    store, database, key = _seed_typed_gateway_store()
    settings = Settings(environment="test")
    database.typed[CREDIT_BALANCE_TABLE][(key.workspace_id, 0)]["total_credits"] = 0
    with pytest.raises(HTTPException) as raised:
        gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)
    assert raised.value.status_code == 402
    assert raised.value.detail["error"]["type"] == "insufficient_credits"
    assert raised.value.detail["error"]["message"] == "Insufficient credits"
    assert store.credit_workspace_typed_direct(
        key.workspace_id, 1_000_000, "topup-rtt", provenance=CreditProvenance.system_grant(),
    )
    response = gateway._authorize_gateway_sync(_request(), _lookup_body(key), settings)["data"]
    assert response["credit_reservation_id"]
    assert response["idempotent_replay"] is False


@pytest.mark.parametrize("verdict", ["revoked", "paused", "disabled"])
def test_joined_federated_metadata_still_revalidates_home(
    verdict: str, fixed_operation_catalog: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key = _seed_typed_gateway_store()
    key.federated_home = "https://home.invalid"
    store._write_entity("api_key", key.hash, key)
    calls = []

    def revalidate(cached: ApiKey, lookup_hash: str) -> ApiKey | None:
        calls.append(lookup_hash)
        assert cached == key
        if verdict == "revoked":
            return None
        if verdict == "disabled":
            return dataclasses.replace(cached, disabled=True)
        workspace = store.get_workspace(key.workspace_id)
        workspace.billing_paused = True
        store._write_entity("workspace", workspace.id, workspace)
        return cached

    monkeypatch.setattr(gateway, "_federated_key_still_valid", revalidate)
    with pytest.raises(HTTPException) as raised:
        gateway._authorize_gateway_sync(_request(), _lookup_body(key), Settings(environment="test"))
    assert calls == [key.lookup_hash]
    assert raised.value.status_code == (503 if verdict == "paused" else 401)
    assert raised.value.detail["error"]["type"] == (
        "service_unavailable" if verdict == "paused" else "invalid_api_key"
    )
    assert raised.value.detail["error"]["message"] == (
        "Workspace billing is paused" if verdict == "paused" else "Invalid API key"
    )
