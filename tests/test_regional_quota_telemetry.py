from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.operational_analytics_direct import (
    SPEND_LEASE_SHADOW_COLUMNS,
    OperationalOutboxRow,
    normalise_operational_event,
)
from trusted_router.regional_quota_ledger import InMemoryRegionalQuotaLedger
from trusted_router.regional_quota_telemetry import (
    REGIONAL_PREDICATE_REASONS,
    regional_predicate_reason,
)
from trusted_router.routes.internal import gateway
from trusted_router.services.spend_lease_shadow_dispatch import SpendLeaseShadowDispatcher
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_regional_quota import ledger_unavailable_reason

PASSING: dict[str, Any] = dict(
    stage_c=False, enabled=True, issuance_enabled=True, in_cohort=True,
    backend_available=True, estimate=1, route_type=None, all_candidates_credits=True,
    any_exact_global=False, key_lifetime=None, key_daily=None, key_weekly=None,
    key_monthly=None, custom_model=None, user_model=None, partner_mode=None,
    additional_cost=0, native_batch=False, app_markup=0, receipt_fee=0,
)
# Independent expected order: deleting a predicate or shifting a bit must fail.
CLAUSES = [
    ("stage_c", "stage_c", True), ("disabled", "enabled", False),
    ("issuance_disabled", "issuance_enabled", False), ("cohort", "in_cohort", False),
    ("backend", "backend_available", False), ("estimate", "estimate", 0),
    ("route_type", "route_type", "embeddings"),
    ("candidate_not_credits", "all_candidates_credits", False),
    ("exact_global", "any_exact_global", True), ("key_lifetime", "key_lifetime", 0),
    ("key_daily", "key_daily", 0), ("key_weekly", "key_weekly", 0),
    ("key_monthly", "key_monthly", 0), ("custom_model", "custom_model", object()),
    ("user_model", "user_model", object()), ("partner_mode", "partner_mode", object()),
    ("additional_cost", "additional_cost", 1), ("native_batch", "native_batch", True),
    ("app_markup", "app_markup", 1), ("receipt_fee", "receipt_fee", 1),
]


@pytest.mark.parametrize("index,clause", list(enumerate(CLAUSES)))
def test_every_predicate_reason_bit_and_order(index: int, clause: tuple[str, str, Any]) -> None:
    reason, field, value = clause
    assert REGIONAL_PREDICATE_REASONS == tuple(c[0] for c in CLAUSES)
    assert regional_predicate_reason(**PASSING) == (None, 0)
    assert regional_predicate_reason(**{**PASSING, field: value}) == (reason, 1 << index)
    # Each suffix has this first failure and every subsequent bit set.
    failures = {f: v for _, f, v in CLAUSES[index:]}
    assert regional_predicate_reason(**{**PASSING, **failures}) == (
        reason, sum(1 << i for i in range(index, len(CLAUSES))),
    )


@pytest.mark.parametrize("regional_outcome", [
    "unavailable", "unpaid_workspace", "reconciliation_stale", "trust_gate_unarmed",
    "billing_paused", "idempotency_mismatch", "error", "served", "replay",
])
def test_gateway_keeps_regional_outcome_before_global_fallback(
    monkeypatch: pytest.MonkeyPatch, regional_outcome: str,
) -> None:
    store, db, _ = make_fake_store(
        request_record_write_mode="typed", operational_analytics_outbox_enabled=True,
    )
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    ws = store.create_workspace("owner", "coverage", trial_credit_microdollars=100_000_000)
    _, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    configure_store(store)
    evidence: list[dict[str, Any]] = []
    def deliver(event_id: str, payload: dict[str, Any]) -> None:
        store.record_spend_lease_shadow(event_id, payload)
        evidence.append(payload)

    dispatcher = SpendLeaseShadowDispatcher(deliver)
    monkeypatch.setattr(gateway, "_SPEND_LEASE_SHADOW_DISPATCHER", dispatcher)
    real_global = type(store).authorize_gateway_typed
    real_regional = type(store).authorize_gateway_regional
    global_calls: list[str] = []

    def global_authorize(self: Any, **kwargs: Any) -> Any:
        global_calls.append("global")
        return real_global(self, **kwargs)

    def regional_authorize(self: Any, **kwargs: Any) -> Any:
        if regional_outcome == "error":
            raise RuntimeError("regional error")
        if regional_outcome == "served":
            return real_regional(self, **kwargs)
        if regional_outcome == "replay":
            # A global replay must not be counted as served by regional leases.
            for field in ("lease_ttl_seconds", "lease_max_microdollars",
                          "lease_max_available_basis_points", "lease_shard_count", "observation"):
                kwargs.pop(field)
            _, auth = real_global(self, **kwargs, has_credit_candidate=True,
                                  reservation_usage_type="Credits", skip_key_limit=True)
            return "replay", auth
        if regional_outcome == "unavailable":
            kwargs["observation"]["regional_unavailable_reason"] = "occupied_fence"
        return regional_outcome, None

    monkeypatch.setattr(type(store), "authorize_gateway_typed", global_authorize)
    monkeypatch.setattr(type(store), "authorize_gateway_regional", regional_authorize)
    settings = Settings(
        environment="test", regional_quota_leases_enabled=True,
        regional_quota_lease_issuance_enabled=True,
        regional_quota_lease_pilot_workspace_ids=ws.id,
        spend_lease_issuance_enabled=False,
    )
    client = TestClient(create_app(settings, configure_store_arg=False, init_observability=False),
                        raise_server_exceptions=False)
    response = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key.hash, "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 1000, "max_output_tokens": 100,
        "route_type": "chat.completions", "idempotency_key": "coverage",
        "region": "us-central1",
    })
    assert dispatcher.wait_for_idle(2)
    dispatcher.close()
    assert len(evidence) == 1
    event = evidence[0]
    assert event["regional_predicate_reason"] is None
    assert event["regional_predicate_mask"] == 0
    assert event["regional_outcome"] == regional_outcome
    assert event["regional_requested_region"] == "us-central1"
    assert event["regional_resolved_region"] == "us-central1"
    fallback = regional_outcome in {
        "unavailable", "unpaid_workspace", "reconciliation_stale", "trust_gate_unarmed",
    }
    assert bool(global_calls) == fallback
    if fallback or regional_outcome in {"served", "replay"}:
        assert response.status_code == 200, response.text
        assert event["server_verdict"] == "accepted"
        assert event["authorization_id"] == response.json()["data"]["authorization_id"]
        assert event["event_id"] != event["authorization_id"]
    else:
        assert response.status_code in {403, 409, 500}
    assert event["regional_unavailable_reason"] == (
        "occupied_fence" if regional_outcome == "unavailable" else None
    )
    # Actual event -> outbox payload -> BOTH ClickHouse canonicalizers.
    from tests.test_operational_analytics_direct import _load_drainer
    outbox = [row for row in db.operational_analytics_outbox if row["event_kind"] == "spend_lease_shadow"]
    assert len(outbox) == 1
    assert json.loads(outbox[0]["payload"]) == event
    row = OperationalOutboxRow(0, datetime.now(UTC), "spend_lease_shadow",
                               event["event_id"], outbox[0]["payload"])
    canonical = normalise_operational_event(row)[0].row
    assert _load_drainer().normalise_operational_event(row)[0].row == canonical
    fields = ("authorization_id", "regional_predicate_reason", "regional_predicate_mask",
              "regional_outcome", "regional_unavailable_reason", "regional_requested_region",
              "regional_resolved_region")
    root = Path(__file__).resolve().parents[1]
    assert "ALTER TABLE spend_lease_shadow\n" in (
        root / "clickhouse/018_regional_coverage_single_node.sql"
    ).read_text()
    assert "ALTER TABLE tr.spend_lease_shadow ON CLUSTER trustedrouter" in (
        root / "clickhouse/017_regional_coverage_replicated.sql"
    ).read_text()
    for field in fields:
        assert field in SPEND_LEASE_SHADOW_COLUMNS
        assert canonical[field] == event[field]
        for migration in ("017_regional_coverage_replicated.sql", "018_regional_coverage_single_node.sql"):
            assert f"ADD COLUMN IF NOT EXISTS {field} Nullable(" in (root / "clickhouse" / migration).read_text()


@pytest.mark.parametrize("observation", ["regional_quota_observation_enabled", "spend_lease_observation_enabled"])
def test_observation_without_any_issuance_records_early_rejections_and_retries(
    monkeypatch: pytest.MonkeyPatch, observation: str,
) -> None:
    evidence: list[dict[str, Any]] = []
    dispatcher = SpendLeaseShadowDispatcher(lambda _id, payload: evidence.append(payload))
    monkeypatch.setattr(gateway, "_SPEND_LEASE_SHADOW_DISPATCHER", dispatcher)
    settings = Settings(environment="test", **{observation: True})
    client = TestClient(create_app(settings, init_observability=False))
    for _ in range(2):
        response = client.post("/v1/internal/gateway/authorize", json={
            "api_key_hash": "bad-key", "model": "anthropic/claude-opus-4.7",
        })
        assert response.status_code == 401
    assert dispatcher.wait_for_idle(2)
    dispatcher.close()
    assert len(evidence) == 2 and evidence[0]["event_id"] != evidence[1]["event_id"]
    for event in evidence:
        assert event["regional_outcome"] == "not_attempted"
        assert event["regional_predicate_reason"] is None
        assert event["regional_predicate_mask"] is None


def test_ledger_timeout_classification_uses_existing_exception_chain() -> None:
    from google.api_core.exceptions import DeadlineExceeded
    for cause in (TimeoutError(), DeadlineExceeded("deadline")):
        outer = RuntimeError("ledger failed")
        outer.__cause__ = cause
        assert ledger_unavailable_reason(outer) == "ledger_timeout"
    assert ledger_unavailable_reason(RuntimeError("non-timeout ledger error")) == "other"


@pytest.mark.parametrize("reason", [
    "unmapped_region", "insufficient_grant", "occupied_fence", "exhausted_lease",
    "expired_lease", "ledger_timeout", "other",
])
def test_unavailable_subreasons_from_real_storage_decisions(
    monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    from dataclasses import replace
    from datetime import timedelta

    from tests.test_regional_accounting_v2 import _authorize, _setup
    from trusted_router import storage_gcp_regional_quota as quota

    store, _db, key, args = _setup()
    ledger = store._regional_quota_ledger
    if reason == "unmapped_region":
        monkeypatch.setattr(type(ledger), "supports_region", lambda _self, _region: False)
    elif reason == "insufficient_grant":
        args["estimate"] = 100_000_001
    elif reason in {"ledger_timeout", "other"}:
        def fail(_self: Any, _lease: Any) -> Any:
            if reason == "ledger_timeout":
                raise TimeoutError("test timeout")
            raise RuntimeError("test failure")
        monkeypatch.setattr(type(ledger), "initialize", fail)
    else:
        auth = _authorize(store, args)
        global_lease = quota.get_global_regional_quota_lease(
            store, workspace_id=key.workspace_id, region=auth.region, lease_id=auth.regional_lease_id,
        )
        assert global_lease is not None
        if reason == "exhausted_lease":
            args["estimate"] = global_lease.granted_microdollars
        elif reason == "occupied_fence":
            # An existing quarantined lease retains its fence until reconciled.
            quota.quarantine_regional_quota_lease(store, global_lease, reason="test")
            store._regional_quota_lease_cache.clear()
        else:
            expired = replace(global_lease, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
            store._write_entity("regional_quota_lease", expired.entity_id, expired)
            for cache_key in store._regional_quota_lease_cache:
                store._regional_quota_lease_cache[cache_key] = expired
    evidence: dict[str, Any] = {}
    outcome, auth = store.authorize_gateway_regional(
        authorization_id="unavailable", **args, lease_ttl_seconds=60,
        lease_max_microdollars=10_000_000, lease_max_available_basis_points=1000,
        lease_shard_count=16, observation=evidence,
    )
    assert (outcome, auth) == ("unavailable", None)
    assert evidence["regional_unavailable_reason"] == reason


def test_grant_pool_cap_observation_reuses_existing_trust_checks() -> None:
    from tests.test_trust_eligibility_pr2 import arm_store, workspace_state
    from trusted_router.storage_gcp_regional_quota import grant_regional_quota_lease

    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    workspace_state(db, 1)
    evidence: dict[str, Any] = {}
    common = dict(
        workspace_id="workspace", region="us-central1", requested_microdollars=4_000_000,
        per_lease_cap_microdollars=10_000_000, max_available_basis_points=1000,
        ttl_seconds=60, minimum_grant_microdollars=1, observation=evidence,
    )
    assert grant_regional_quota_lease(store, quota_shard=0, **common) is not None
    assert grant_regional_quota_lease(store, quota_shard=1, **common) is not None
    assert grant_regional_quota_lease(store, quota_shard=2, **common) is None
    assert evidence == {"regional_unavailable_reason": "pool_cap"}
