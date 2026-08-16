from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from scripts.reconcile_custom_model_payouts import reconcile_custom_model_payouts
from tests.fakes.spanner import make_fake_store
from trusted_router.storage import InMemoryStore
from trusted_router.storage_codec import json_body
from trusted_router.storage_models import GatewayAuthorization, Generation
from trusted_router.types import UsageType


def _model(store: Any, *, owner: str, workspace: str, slug: str) -> Any:
    return store.create_user_model(
        owner_user_id=owner,
        owner_workspace_id=workspace,
        name="Reconciliation model",
        kind="machine",
        display_name="reconciler",
        endpoint_url="https://owner.example/v1",
        slug=slug,
    )


def _generation(model_id: str, *, authorization_id: str, created_at: str) -> Generation:
    return Generation(
        id=f"gen-{authorization_id}",
        request_id=f"req-{authorization_id}",
        workspace_id="ws-payer",
        key_hash="key-payer",
        model=model_id,
        provider_name="User-provided",
        app="Reconciliation test",
        tokens_prompt=100,
        tokens_completion=200,
        total_cost_microdollars=1_000_000,
        usage_type=UsageType.CREDITS,
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        custom_model_id=model_id,
        created_at=created_at,
    )


def _authorization(
    model_id: str,
    generation_id: str,
    *,
    authorization_id: str,
    created_at: str,
) -> GatewayAuthorization:
    return GatewayAuthorization(
        id=authorization_id,
        workspace_id="ws-payer",
        key_hash="key-payer",
        model_id=model_id,
        provider="user",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=1_000_000,
        settled=True,
        created_at=created_at,
        user_provided_model_id=model_id,
        user_model_owner_user_id="owner-reconcile",
        finalization_outcome="settled",
        finalized_cost_microdollars=1_000_000,
        finalized_generation_id=generation_id,
    )


def test_memory_reconciliation_applies_once_reports_orphan_and_dry_run_is_read_only() -> None:
    store = InMemoryStore()
    now = datetime.now(UTC)
    created_at = now.isoformat().replace("+00:00", "Z")
    model = _model(
        store,
        owner="owner-reconcile",
        workspace="ws-owner",
        slug="memory-reconcile",
    )
    generation = _generation(
        model.id,
        authorization_id="auth-memory",
        created_at=created_at,
    )
    authorization = _authorization(
        model.id,
        generation.id,
        authorization_id="auth-memory",
        created_at=created_at,
    )
    store.add_generation(generation)
    store.api_keys.gateway_authorizations[authorization.id] = authorization
    assert store.credit_user_earnings(
        "orphan-owner",
        12,
        "custom_model_payout:auth-orphan",
        custom_model_id=model.id,
        payer_workspace_id="ws-payer",
    )
    cutoff = now - timedelta(hours=1)

    dry_records: list[dict[str, Any]] = []
    dry = reconcile_custom_model_payouts(
        store,
        since=cutoff,
        emit=dry_records.append,
    )
    assert dry.missing_payouts == 1
    assert dry.payouts_applied == 0
    assert dry.orphan_payouts == 1
    assert store.earnings_summary("owner-reconcile")["available"] == 0
    assert any(record["type"] == "missing_payout" for record in dry_records)
    assert any(record["type"] == "orphan_payout" for record in dry_records)

    applied = reconcile_custom_model_payouts(store, since=cutoff, apply=True)
    assert applied.missing_payouts == 1
    assert applied.payouts_applied == 1
    assert store.earnings_summary("owner-reconcile")["available"] == 700_000

    rerun = reconcile_custom_model_payouts(store, since=cutoff, apply=True)
    assert rerun.missing_payouts == 0
    assert rerun.payouts_applied == 0
    assert store.earnings_summary("owner-reconcile")["available"] == 700_000


def test_spanner_fake_reconciliation_scans_applies_once_and_reports_orphan() -> None:
    store, database, _bigtable = make_fake_store(generation_records_enabled=True)
    now = datetime.now(UTC)
    created_at = now.isoformat().replace("+00:00", "Z")
    model = _model(
        store,
        owner="owner-reconcile",
        workspace="ws-owner",
        slug="spanner-reconcile",
    )
    generation = _generation(
        model.id,
        authorization_id="auth-spanner",
        created_at=created_at,
    )
    authorization = _authorization(
        model.id,
        generation.id,
        authorization_id="auth-spanner",
        created_at=created_at,
    )
    store.add_generation(generation)
    database.gateway_authorizations[authorization.id] = {
        "authorization_id": authorization.id,
        "created_at": now,
        "settled": True,
        "terminal_at": None,
        "payload": json_body(authorization),
    }
    assert store.credit_user_earnings(
        "orphan-owner",
        12,
        "custom_model_payout:auth-spanner-orphan",
        custom_model_id=model.id,
        payer_workspace_id="ws-payer",
    )
    cutoff = now - timedelta(hours=1)

    dry = reconcile_custom_model_payouts(store, since=cutoff)
    assert dry.missing_payouts == 1
    assert dry.orphan_payouts == 1
    assert store.earnings_summary("owner-reconcile")["available"] == 0

    first = reconcile_custom_model_payouts(store, since=cutoff, apply=True)
    second = reconcile_custom_model_payouts(store, since=cutoff, apply=True)
    assert first.payouts_applied == 1
    assert second.missing_payouts == 0
    assert second.payouts_applied == 0
    assert store.earnings_summary("owner-reconcile")["available"] == 700_000


def test_deleted_model_is_still_reconciled_to_the_frozen_owner() -> None:
    """The payee is the owner frozen on the authorization; deleting the model
    afterwards must not turn a missing payout into owner_unknown."""
    store = InMemoryStore()
    now = datetime.now(UTC)
    created_at = now.isoformat().replace("+00:00", "Z")
    model = _model(store, owner="owner-reconcile", workspace="ws-owner", slug="gone-soon")
    generation = _generation(model.id, authorization_id="auth-gone", created_at=created_at)
    authorization = _authorization(
        model.id, generation.id, authorization_id="auth-gone", created_at=created_at
    )
    store.add_generation(generation)
    store.api_keys.gateway_authorizations[authorization.id] = authorization
    assert store.delete_user_model(model.id, owner_user_id="owner-reconcile")

    applied = reconcile_custom_model_payouts(store, since=now - timedelta(hours=1), apply=True)
    assert applied.owner_unknown == 0
    assert applied.missing_payouts == 1 and applied.payouts_applied == 1
    assert store.earnings_summary("owner-reconcile")["total_earned"] == 700_000
