from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.fakes.spanner import _FakeTransaction, _ParamTypes, make_fake_store
from trusted_router import spend_leases, storage_gcp_authorize
from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.config import Settings
from trusted_router.receipt_keys import b64url_decode
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.spend_leases import (
    SPEND_LEASE_COHORT,
    SpendLeaseSigner,
    freeze_spend_lease_catalog,
    mint_shadow_spend_lease,
)
from trusted_router.storage import CreditAccount, Workspace, configure_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_request_records import insert_gateway_authorization
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

GOLDENS = Path(__file__).parent / "fixtures" / "stage_c" / "origin_main"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("shard", [0, 3], ids=["shard-zero", "selected-shard-three"])
@pytest.mark.parametrize(
    "paused,armed",
    [(False, False), (True, False), (True, True)],
    ids=["clear-flag-off", "paused-flag-off", "paused-flag-on"],
)
def test_flag_off_authorize_response_is_byte_exact_origin_main(
    monkeypatch: pytest.MonkeyPatch, paused: bool, armed: bool, shard: int,
) -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=armed)
    store.trust_settings = settings
    pause_reads: list[dict[str, Any]] = []
    execute_sql = _FakeTransaction.execute_sql

    def record_sql(self: Any, sql: str, **kwargs: Any) -> Any:
        if "billing_pause_causes" in sql:
            pause_reads.append(kwargs["params"])
        return execute_sql(self, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_sql", record_sql)
    monkeypatch.setattr(type(store), "_credit_shard_candidates", lambda *_: (shard,))
    workspace = Workspace(
        id="ws-origin-golden",
        name="Golden",
        owner_user_id="user-origin-golden",
    )
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity(
        "credit",
        workspace.id,
        CreditAccount(workspace_id=workspace.id),
    )
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, shard)] = {
        "workspace_id": workspace.id,
        "shard": shard,
        "total_credits": 50_000_000,
        "total_usage": 0,
        "reserved": 0,
        "billing_pause_causes": ["abuse"] if paused else [],
        "pause_epoch": 1 if paused else 0,
        "source_updated_at": None,
        "updated_at": None,
    }
    _raw_key, key = store.api_keys.create(
        workspace_id=workspace.id,
        name="golden",
        creator_user_id=workspace.owner_user_id,
    )
    configure_store(store)
    monkeypatch.setattr(
        gateway,
        "_new_gateway_authorization_id",
        lambda: "gwa-origin-main-golden",
    )
    monkeypatch.setattr(
        storage_gcp_authorize.uuid,
        "uuid4",
        lambda: uuid.UUID("00000000-0000-4000-8000-000000000001"),
    )
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash,
        idempotency_key="origin-main-golden",
        model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100,
        max_output_tokens=100,
    )
    def authorize() -> Any:
        return gateway._authorize_gateway_sync(  # noqa: SLF001
            Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
            body,
            settings,
        )

    if armed:
        with pytest.raises(HTTPException) as rejected:
            authorize()
        assert rejected.value.status_code == 403
        assert rejected.value.detail["error"]["message"] == "billing_paused"
        assert pause_reads == [{"ws": workspace.id, "shard": shard}]
        assert database.typed[CREDIT_BALANCE_TABLE][(workspace.id, shard)]["reserved"] == 0
        assert database.typed["tr_key_limit"][(key.hash, 0)]["reserved"] == 0
        assert not database.reservations
        assert not database.typed.get("tr_gateway_authorization")
    else:
        response = authorize()
        response["data"]["api_key_hash"] = "<api-key-hash>"
        assert _canonical(response) == (GOLDENS / "authorize_response.json").read_bytes()
        assert pause_reads == []
        assert database.typed[CREDIT_BALANCE_TABLE][(workspace.id, shard)]["reserved"] > 0


def test_flag_off_lease_claims_are_byte_exact_origin_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = MODELS["anthropic/claude-haiku-4.5"]
    endpoint = next(
        candidate
        for candidate in endpoints_for_model(model.id)
        if candidate.usage_type == "Credits"
    )
    catalog = freeze_spend_lease_catalog(
        [(model, endpoint)],
        region="us-central1",
        route_type="chat.completions",
        service_tier=None,
        stage_c=False,
    )
    monkeypatch.setattr(
        spend_leases.uuid,
        "uuid4",
        lambda: uuid.UUID("00000000-0000-4000-8000-000000000002"),
    )
    artifact = mint_shadow_spend_lease(
        signer=SpendLeaseSigner(lambda: bytes(range(32))),
        key_hash="a" * 64,
        workspace_id="ws-origin-golden",
        boot_kid="boot-origin-golden",
        cap_micro=1_000_000,
        gen=7,
        catalog=catalog,
        ttl_seconds=60,
        now=2_000_000_000,
    )
    claims = b64url_decode(artifact.token.split(".")[1])
    origin_main_claims = (GOLDENS / "lease_claims.json").read_bytes()

    assert claims == origin_main_claims
    assert json.loads(claims)["cohort"] == SPEND_LEASE_COHORT


def test_flag_off_authorization_insert_sql_is_byte_exact_origin_main() -> None:
    class RecordingTransaction:
        sql = ""

        def execute_update(self, sql: str, **_kwargs: object) -> int:
            self.sql = sql
            return 1

    transaction = RecordingTransaction()
    authorization = GatewayAuthorization(
        id="a",
        workspace_id="w",
        key_hash="k",
        model_id="m",
        provider="p",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=1,
        credit_reservation_id="r",
    )

    insert_gateway_authorization(
        transaction,
        _ParamTypes,
        authorization,
        created_at=None,
    )

    assert transaction.sql.encode() == (
        GOLDENS / "gateway_authorization_insert.sql"
    ).read_bytes()


def test_origin_main_golden_commit_and_literal_deploy_default_are_pinned() -> None:
    assert (GOLDENS / "origin_main_commit.txt").read_text() == (
        "7d205ace5085dc7d686f725db626e2e4f57e3d24"
    )
    rollout = (Path(__file__).parents[1] / "scripts" / "deploy" / "rollout.sh").read_text()
    assert '"TR_SPEND_LEASE_ADMISSION_ACCEPT=false"' in rollout
    assert "TR_SPEND_LEASE_ADMISSION_ACCEPT=${" not in rollout


@pytest.mark.parametrize("backend,byok", [("memory", False), ("memory", True), ("postgres", False), ("postgres", True), ("spanner", True)])
def test_flag_off_legacy_pause_keeps_holds_and_skips_trust_queries(
    backend: str, byok: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
    from trusted_router import storage_legacy_trust
    from trusted_router.storage import InMemoryStore

    store: Any
    db: Any = None
    conn: Any = None
    if backend == "memory":
        store = InMemoryStore()
    elif backend == "postgres":
        conn = sqlite_postgres_conn()
        store = postgres_store_on(conn)
    else:
        store, db, _ = make_fake_store(request_record_write_mode="legacy")
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=False)
    ws = store.create_workspace("owner", "unarmed", trial_credit_microdollars=1000)
    _raw, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000,
    )
    if backend == "memory":
        store.credit_trust_shards[(ws.id, 0)].update(billing_pause_causes=["abuse"], pause_epoch=7)
    elif backend == "postgres":
        conn.execute(
            "UPDATE tr_credit_balance SET billing_pause_causes = %s, pause_epoch = 7 WHERE workspace_id = %s",
            ('["abuse"]', ws.id),
        )
    else:
        for (workspace_id, _), row in db.typed[CREDIT_BALANCE_TABLE].items():
            if workspace_id == ws.id:
                row.update(billing_pause_causes=["abuse"], pause_epoch=7)

    def unexpected_trust_read(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("flag-off legacy authorize entered the trust program")

    for name in ("postgres_pause", "spanner_pause_epoch", "create_spanner_legacy_authorization"):
        monkeypatch.setattr(storage_legacy_trust, name, unexpected_trust_read)
    if backend == "memory":
        monkeypatch.setattr(type(store), "_legacy_paused", unexpected_trust_read)
        monkeypatch.setattr(type(store), "_legacy_pause_epoch", unexpected_trust_read)
        monkeypatch.setattr(type(store), "_recover_released_credit_locked", unexpected_trust_read)
    assert storage_legacy_trust.legacy_pause_epoch(store, ws.id) == 0
    usage = UsageType.BYOK if byok else UsageType.CREDITS
    store.reserve_key_limit(key.hash, 100, usage_type=usage)
    reservation = None if byok else store.reserve(ws.id, key.hash, 100, idempotency_key="unarmed")
    authorization = store.create_gateway_authorization(
        workspace_id=ws.id, key_hash=key.hash, model_id="m", provider="p", usage_type=usage,
        estimated_microdollars=100, credit_reservation_id=reservation.id if reservation else None,
        idempotency_key="unarmed", expected_pause_epoch=0,
    )
    assert authorization.estimated_microdollars == 100
    assert store.get_gateway_authorization_by_idempotency_key(ws.id, key.hash, "unarmed").id == authorization.id
    if backend == "memory":
        assert store.api_keys.get_by_hash(key.hash).reserved_microdollars == 100
        assert store.credit_money[ws.id].reserved_microdollars == (0 if byok else 100)
        assert not store._reservation_pause_epochs
        if reservation:
            store.refund(reservation.id)
            assert store.credit_money[ws.id].reserved_microdollars == 0
    elif backend == "postgres":
        assert conn.balance(ws.id) == (1000, 0, 0 if byok else 100)
        assert conn.count_entities("reservation_pause_epoch") == 0
        assert conn.execute("SELECT reserved FROM tr_key_limit WHERE key_hash = %s", (key.hash,)).fetchone()[0] == 100
    else:
        assert store.api_keys.get_by_hash(key.hash).reserved_microdollars == 100


def test_flag_off_regional_pause_preserves_grant_and_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_trust_eligibility_pr2 import regional_args
    from trusted_router import trust_eligibility
    from trusted_router.regional_quota_ledger import InMemoryRegionalQuotaLedger

    store, db, _ = make_fake_store(request_record_write_mode="typed")
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=False)
    ws = store.create_workspace("owner", "regional-unarmed", trial_credit_microdollars=200_000_000)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    for (workspace_id, _), row in db.typed[CREDIT_BALANCE_TABLE].items():
        if workspace_id == ws.id:
            row.update(billing_pause_causes=["abuse"], pause_epoch=1, trust_tier=0)

    def unexpected_trust_read(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("flag-off regional authorize entered the trust program")

    monkeypatch.setattr(trust_eligibility, "billing_paused_tx", unexpected_trust_read)
    monkeypatch.setattr(trust_eligibility, "lease_eligibility", unexpected_trust_read)
    outcome, authorization = store.authorize_gateway_regional(
        authorization_id="regional-unarmed", **regional_args(ws.id, key)
    )
    assert outcome == "accepted" and authorization is not None
    leases = [json.loads(record.body) for (kind, _), record in db.rows.items() if kind == "regional_quota_lease"]
    assert len(leases) == 1
    assert leases[0]["state"] == "active"
    assert leases[0]["granted_microdollars"] == 10_000_000 // 16
    assert "issuance_tier" not in leases[0] and "tier_cap_micro" not in leases[0]
    local = store._regional_quota_ledger.get(leases[0]["lease_id"], region="us-central1")
    assert local is not None and local.reserved_microdollars == 10000
