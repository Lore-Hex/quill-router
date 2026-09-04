from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from starlette.requests import Request

from tests.fakes.spanner import _ParamTypes, make_fake_store
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


def test_flag_off_authorize_response_is_byte_exact_origin_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
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
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id,
        "shard": 0,
        "total_credits": 50_000_000,
        "total_usage": 0,
        "reserved": 0,
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
    response = gateway._authorize_gateway_sync(  # noqa: SLF001
        Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
        body,
        Settings(environment="test"),
    )
    response["data"]["api_key_hash"] = "<api-key-hash>"

    assert _canonical(response) == (GOLDENS / "authorize_response.json").read_bytes()


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
        "7f4344b03d621748b7c1520e766f5ff61ed0a942"
    )
    rollout = (Path(__file__).parents[1] / "scripts" / "deploy" / "rollout.sh").read_text()
    assert '"TR_SPEND_LEASE_ADMISSION_ACCEPT=false"' in rollout
    assert "TR_SPEND_LEASE_ADMISSION_ACCEPT=${" not in rollout
