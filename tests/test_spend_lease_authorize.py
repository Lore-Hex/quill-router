from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from tests.fakes.spanner import FakeSpannerDatabase, _ParamTypes, make_fake_store
from trusted_router.config import Settings
from trusted_router.spend_lease_authorize import (
    FenceOutcome,
    FenceView,
    GlobalLeaseView,
    SpendLeaseArbitrationConflict,
    SpendLeaseContractError,
    SpendLeaseMintLost,
    classify_fence_loss,
    derive_candidate_lease_id,
    route_local_presented,
    route_local_refusal,
    route_missing_local,
)
from trusted_router.spend_lease_state import (
    AllocationState,
    Created,
    LeaseTransition,
    SpendLease,
    SpendLeaseRefusalReason,
    SpendLeaseState,
    TerminalSource,
)
from trusted_router.spend_leases import SpendLeaseArtifact, SpendLeaseSigner
from trusted_router.storage_gcp_authorize import authorize_atomic
from trusted_router.storage_gcp_counter_dml import insert_entity_dml
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_spend_lease import (
    CandidateIdentity,
    insert_candidate,
    register_bound,
    register_claim,
    take_recovery_ownership,
)
from trusted_router.storage_gcp_spend_lease_authorize import (
    BindingPlan,
    SpendLeaseReuseLost,
    ensure_initial_fence,
)
from trusted_router.storage_models import CreditAccount, GatewayAuthorization, Workspace
from trusted_router.types import UsageType

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _lease(*, state: SpendLeaseState = SpendLeaseState.ACTIVE) -> SpendLease:
    return SpendLease(
        lease_id="lease-1",
        gen=4,
        key_hash="key-hash",
        boot_kid="boot-kid",
        workspace_id="workspace-1",
        creating_authorization_id="authorization-1",
        cap_micro=10_000,
        expires_at=NOW + timedelta(seconds=30),
        skew=timedelta(seconds=10),
        version=0,
        state=state,
    )


@pytest.mark.parametrize(
    ("local", "now", "route", "reason", "exhausted"),
    [
        (_lease(), NOW, "reuse", None, False),  # A1
        (_lease(), NOW + timedelta(seconds=45), "successor", None, False),  # A2
        (_lease(state=SpendLeaseState.DRAINING), NOW, "successor", None, False),
        (_lease(state=SpendLeaseState.CLOSED), NOW, "successor", None, False),
        (_lease(state=SpendLeaseState.TOMBSTONED), NOW, "successor", None, True),  # A3
    ],
)
def test_decision_47_table_a_local_rows(
    local: SpendLease,
    now: datetime,
    route: str,
    reason: str | None,
    exhausted: bool,
) -> None:
    decision = route_local_presented(local, now=now)
    assert (decision.route, decision.reason, decision.authoritative_exhaustion) == (
        route,
        reason,
        exhausted,
    )


@pytest.mark.parametrize(
    ("refusal", "route", "reason", "exhausted"),
    [
        (SpendLeaseRefusalReason.WINDOW_EXPIRED, "successor", None, False),
        (SpendLeaseRefusalReason.FROZEN_DRAINING, "successor", None, False),
        (SpendLeaseRefusalReason.CLOSED, "successor", None, False),
        (SpendLeaseRefusalReason.FROZEN_TOMBSTONED, "successor", None, True),
        (SpendLeaseRefusalReason.WINDOW_NOT_ELAPSED, "ordinary", "window_open", False),
        (SpendLeaseRefusalReason.EXHAUSTED, "successor", None, True),
    ],
)
def test_decision_47_table_a_typed_refusals(
    refusal: SpendLeaseRefusalReason,
    route: str,
    reason: str | None,
    exhausted: bool,
) -> None:
    decision = route_local_refusal(refusal)
    assert (decision.route, decision.reason, decision.authoritative_exhaustion) == (
        route,
        reason,
        exhausted,
    )


@pytest.mark.parametrize(
    ("global_lease", "route", "reason", "exhausted"),
    [
        (None, "ordinary", "ledger_unavailable", False),  # B1
        (GlobalLeaseView("ACTIVE", NOW - timedelta(seconds=11), 10), "successor", None, True),  # B2
        (GlobalLeaseView("DRAINING", NOW - timedelta(seconds=11), 10), "successor", None, True),
        (GlobalLeaseView("CLOSED", NOW + timedelta(seconds=30), 10), "successor", None, True),  # B3
        (GlobalLeaseView("TOMBSTONED", NOW + timedelta(seconds=30), 10), "successor", None, True),
        (GlobalLeaseView("ACTIVE", NOW + timedelta(seconds=30), 10), "ordinary", "ledger_unavailable", False),  # B4
    ],
)
def test_decision_47_table_b_missing_or_corrupt_local(
    global_lease: GlobalLeaseView | None,
    route: str,
    reason: str | None,
    exhausted: bool,
) -> None:
    decision = route_missing_local(global_lease, now=NOW)
    assert (decision.route, decision.reason, decision.authoritative_exhaustion) == (
        route,
        reason,
        exhausted,
    )


@pytest.mark.parametrize(
    ("current", "mark", "closed", "exhausted", "outcome", "reason"),
    [
        (None, 0, True, False, FenceOutcome.MISSING_OR_CORRUPT, None),
        (FenceView(8, 0, "winner", True), 0, True, False, FenceOutcome.LOST_RACE, "lease_transferred"),
        (FenceView(8, 0, "winner", False), 0, True, False, FenceOutcome.STALE_ADVISORY, "stale_advisory"),
        (FenceView(7, 3, "incumbent", True), 1, True, False, FenceOutcome.COUNT_EXHAUSTED, "predecessor_limit"),
        (FenceView(7, 1, "incumbent", True), 0, False, False, FenceOutcome.WINDOW_OPEN, "window_open"),
        (FenceView(7, 1, "incumbent", True), 0, True, False, FenceOutcome.CONTRACT_VIOLATION, None),
    ],
)
def test_decision_45_fence_truth_table(
    current: FenceView | None,
    mark: int,
    closed: bool,
    exhausted: bool,
    outcome: FenceOutcome,
    reason: str | None,
) -> None:
    decision = classify_fence_loss(
        observed_gen=7,
        incumbent_mark_count=mark,
        predecessor_limit=3,
        window_closed=closed,
        authoritative_exhaustion=exhausted,
        statement_window_open=True,
        current=current,
    )
    assert (decision.outcome, decision.reason) == (outcome, reason)


def test_decision_45_statement_window_guard_false_is_mint_lost_first() -> None:
    with pytest.raises(SpendLeaseMintLost):
        classify_fence_loss(
            observed_gen=7,
            incumbent_mark_count=1,
            predecessor_limit=3,
            window_closed=False,
            authoritative_exhaustion=False,
            statement_window_open=False,
            current=None,
        )


def test_binding_settings_requires_issuance() -> None:
    with pytest.raises(
        ValueError,
        match="TR_SPEND_LEASE_BINDING_ENABLED requires TR_SPEND_LEASE_ISSUANCE_ENABLED",
    ):
        Settings(environment="test", spend_lease_binding_enabled=True)

    settings = Settings(
        environment="test",
        spend_lease_issuance_enabled=True,
        spend_lease_binding_enabled=True,
        spend_lease_pilot_workspace_ids="workspace-1",
        spend_lease_signing_secret_name="projects/test/secrets/spend-lease",  # noqa: S106
        operational_analytics_sink="direct",
    )
    assert settings.spend_lease_binding_enabled is True
    assert settings.spend_lease_issuance_enabled is True


def test_candidate_id_includes_creating_authorization_and_is_stable() -> None:
    first = derive_candidate_lease_id("key", "boot", 3, "auth-1")
    assert first == derive_candidate_lease_id("key", "boot", 3, "auth-1")
    assert first != derive_candidate_lease_id("key", "boot", 3, "auth-2")
    assert len(first) == 64


class _RecordingLedger:
    def __init__(self) -> None:
        self.binds = 0
        self.compensations = 0
        self.leases: dict[str, SpendLease] = {}

    def supports_region(self, _region: str) -> bool:
        return True

    def initialize(self, candidate: SpendLease, *, region: str) -> LeaseTransition:
        del region
        existing = self.leases.setdefault(candidate.lease_id, candidate)
        return LeaseTransition(existing, existing is not candidate)

    def get(self, lease_id: str, *, region: str) -> SpendLease | None:
        del region
        return self.leases.get(lease_id)

    def allocate(self, authorization_view: object, lease_id: str, **kwargs: object) -> object:
        del authorization_view
        lease = self.leases[lease_id]
        result = lease.allocate(
            authorization_view=None,
            idempotency_scope=str(kwargs["idempotency_scope"]),
            provisional_authorization_id=str(kwargs["provisional_authorization_id"]),
            request_fingerprint=str(kwargs["request_fingerprint"]),
            allocated_micro=cast(int, kwargs["allocated_micro"]),
            abandon_after=cast(datetime, kwargs["abandon_after"]),
            now=cast(datetime, kwargs["now"]),
        )
        if isinstance(result, Created):
            self.leases[lease_id] = result.lease
        return result

    def bind(self, lease_id: str, **kwargs: object) -> object:
        self.binds += 1
        result = self.leases[lease_id].bind(
            expected_provisional_id=str(kwargs["expected_provisional_id"]),
            proof=kwargs["proof"],  # type: ignore[arg-type]
        )
        self.leases[lease_id] = result.lease
        return result

    def compensate(self, lease_id: str, **kwargs: object) -> object:
        self.compensations += 1
        result = self.leases[lease_id].compensate(
            idempotency_scope=str(kwargs["idempotency_scope"]),
            expected_provisional_id=str(kwargs["expected_provisional_id"]),
            claim=kwargs["claim"],  # type: ignore[arg-type]
            absence=kwargs["absence"],  # type: ignore[arg-type]
        )
        self.leases[lease_id] = result.lease
        return result


def _atomic_harness(*, total_credits: int = 10_000) -> tuple[
    FakeSpannerDatabase, BindingPlan, _RecordingLedger
]:
    db = FakeSpannerDatabase()
    db.typed["tr_credit_balance"] = {
        ("workspace-1", 0): {
            "workspace_id": "workspace-1",
            "shard": 0,
            "total_credits": total_credits,
            "total_usage": 0,
            "reserved": 0,
        }
    }
    db.typed["tr_key_limit"] = {
        ("key-hash", 0): {
            "key_hash": "key-hash",
            "shard": 0,
            "limit_micro": 100_000,
            "usage": 0,
            "byok_usage": 0,
            "reserved": 0,
            "include_byok": True,
        }
    }
    ensure_initial_fence(db, _ParamTypes, "fence-1")
    expires_at = datetime.now(UTC) + timedelta(minutes=1)
    identity = CandidateIdentity(
        lease_id="candidate-1",
        gen=1,
        key_hash="key-hash",
        boot_kid="boot-kid",
        cap_micro=2_000,
        skew_seconds=10,
        workspace_id="workspace-1",
        region="us-central1",
        creating_authorization_id="authorization-1",
        idempotency_scope="scope-1",
        expires_at=expires_at,
    )
    db.run_in_transaction(
        lambda transaction: insert_candidate(
            transaction, _ParamTypes, identity, created_at=NOW
        )
    )
    artifact = SpendLeaseArtifact(
        token="test-token",  # noqa: S106 - inert signed-artifact fixture
        lease_id=identity.lease_id, cap_micro=identity.cap_micro,
        gen=identity.gen, iat=int(NOW.timestamp()), exp=int(identity.expires_at.timestamp()),
        issuer_kid="issuer", boot_kid=identity.boot_kid, catalog_version="catalog",
    )
    ledger = _RecordingLedger()
    ledger.initialize(
        SpendLease(
            identity.lease_id,
            identity.gen,
            identity.key_hash,
            identity.boot_kid,
            identity.workspace_id,
            identity.creating_authorization_id,
            identity.cap_micro,
            identity.expires_at,
            timedelta(seconds=identity.skew_seconds),
            0,
        ),
        region=identity.region,
    )
    created = ledger.allocate(
        None,
        identity.lease_id,
        region=identity.region,
        idempotency_scope=identity.idempotency_scope,
        provisional_authorization_id=identity.creating_authorization_id,
        request_fingerprint="fingerprint-1",
        allocated_micro=500,
        abandon_after=identity.expires_at + timedelta(seconds=identity.skew_seconds),
        now=NOW,
    )
    assert isinstance(created, Created)
    plan = BindingPlan(
        ledger=ledger,  # type: ignore[arg-type]
        scope=identity.idempotency_scope,
        fence_id="fence-1",
        region=identity.region,
        provisional_id=identity.creating_authorization_id,
        artifact=artifact,
        allocation_micro=500,
        admission_deadline=identity.expires_at + timedelta(seconds=10),
        mode="mint",
        candidate=identity,
        observed_gen=0,
        incumbent_lease_id=None,
        incumbent_window_closed=True,
        authoritative_exhaustion=False,
    )
    return db, plan, ledger


def _run_plan(
    db: FakeSpannerDatabase, plan: BindingPlan | None
) -> dict[str, object]:
    def build(authorization_id: str, reservation_id: str) -> GatewayAuthorization:
        return GatewayAuthorization(
            id=authorization_id,
            workspace_id="workspace-1",
            key_hash="key-hash",
            model_id="model-1",
            provider="provider-1",
            usage_type=UsageType.CREDITS,
            estimated_microdollars=500,
            credit_reservation_id=reservation_id,
        )

    def build_lease(
        authorization_id: str, reservation_id: str, bound: bool
    ) -> GatewayAuthorization:
        authorization = build(authorization_id, reservation_id)
        if bound:
            assert plan is not None
            authorization.spend_lease_token = plan.artifact.token
            authorization.spend_lease_id = plan.artifact.lease_id
            authorization.spend_lease_gen = plan.artifact.gen
            authorization.spend_lease_allocated_micro = plan.allocation_micro
            authorization.spend_lease_status = "active"
            authorization.spend_lease_exp = plan.artifact.exp
        return authorization

    return authorize_atomic(
        db,
        _ParamTypes,
        workspace_id="workspace-1",
        key_hash="key-hash",
        estimate=500,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        idempotency_scope="scope-1",
        idempotency_fingerprint="fingerprint-1",
        expires_at=NOW + timedelta(hours=1),
        build_authorization=build,
        request_record_write_mode="typed",
        authorization_id="authorization-1",
        spend_lease_hook=(
            None
            if plan is None
            else lambda transaction, shard: plan.transaction_hook(
                transaction, _ParamTypes, "workspace-1", shard
            )
        ),
        build_authorization_for_lease=build_lease if plan is not None else None,
    )


def test_decision_44_success_durable_seam_and_token_invariant() -> None:
    db, plan, ledger = _atomic_harness()

    result = _run_plan(db, plan)
    plan.bind_after_commit()

    assert result["bound"] is True
    assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == 2_500
    assert len(db.spend_lease_arbitrations) == 1
    assert db.spend_lease_open["candidate-1"]["phase"] == "open"
    assert ("spend_lease", "candidate-1") in db.rows
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    authorization = db.gateway_authorizations["authorization-1"]
    assert bool(authorization["spend_lease_token"]) is bool(result["bound"])
    assert ledger.binds == 1


def test_decision_44_hold_zero_is_the_ordinary_reject_seam() -> None:
    db, plan, ledger = _atomic_harness(total_credits=0)

    result = _run_plan(db, plan)

    assert result["outcome"] == "insufficient_credits"
    assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == 0
    assert db.spend_lease_arbitrations == {}
    assert json.loads(db.rows[("spend_lease_active_grant", "fence-1")].body)["gen"] == 0
    assert db.spend_lease_open["candidate-1"]["phase"] == "candidate"
    assert db.reservations == db.gateway_authorizations == {}
    assert ledger.leases["candidate-1"].allocations[0].state.value == "reserved"


def test_decision_44_claim_loss_restores_escrow_and_commits_unbound() -> None:
    db, plan, ledger = _atomic_harness()
    db.run_in_transaction(
        lambda transaction: register_claim(
            transaction, _ParamTypes, plan.scope, plan.provisional_id
        )
    )

    result = _run_plan(db, plan)
    plan.compensate_with_claim(db, _ParamTypes)

    assert result["no_lease_reason"] == "scope_arbitrated"
    assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == 500
    assert db.spend_lease_open["candidate-1"]["phase"] == "candidate"
    assert ("spend_lease", "candidate-1") not in db.rows
    assert not db.gateway_authorizations["authorization-1"]["spend_lease_token"]
    assert ledger.compensations == 1


def test_decision_44_escrow_zero_keeps_only_request_hold() -> None:
    db, plan, ledger = _atomic_harness(total_credits=1_000)

    result = _run_plan(db, plan)
    plan.compensate_with_claim(db, _ParamTypes)

    assert result["no_lease_reason"] == "escrow_headroom"
    assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == 500
    assert db.spend_lease_arbitrations
    registration = next(iter(db.spend_lease_arbitrations.values()))
    assert registration["registration_kind"] == "CLAIM"
    assert json.loads(db.rows[("spend_lease_active_grant", "fence-1")].body)["gen"] == 0
    assert db.spend_lease_open["candidate-1"]["phase"] == "candidate"
    assert ("spend_lease", "candidate-1") not in db.rows
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    assert not db.gateway_authorizations["authorization-1"]["spend_lease_token"]
    assert ledger.compensations == 1


def test_decision_44_fence_zero_runs_all_inverses_before_unbound_commit() -> None:
    db, plan, ledger = _atomic_harness()
    fence = db.rows[("spend_lease_active_grant", "fence-1")]
    fence.body = json.dumps(
        {
            "lease_id": "winner-2",
            "gen": 2,
            "open_predecessor_count": 0,
            "lease_status": "active",
        }
    )
    db.run_in_transaction(
        lambda transaction: insert_entity_dml(
            transaction,
            _ParamTypes,
            "spend_lease",
            "winner-2",
            json.dumps(
                {
                    "state": "ACTIVE",
                    "lease_id": "winner-2",
                    "gen": 2,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                }
            ),
        )
    )

    result = _run_plan(db, plan)
    plan.compensate_with_claim(db, _ParamTypes)

    assert result["no_lease_reason"] == "lease_transferred"
    assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == 500
    registration = next(iter(db.spend_lease_arbitrations.values()))
    assert registration["registration_kind"] == "CLAIM"
    assert json.loads(fence.body)["lease_id"] == "winner-2"
    assert db.spend_lease_open["candidate-1"]["phase"] == "candidate"
    assert ("spend_lease", "candidate-1") not in db.rows
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    assert not db.gateway_authorizations["authorization-1"]["spend_lease_token"]
    assert ledger.compensations == 1


def test_decision_46_foreign_bound_loser_leaves_candidate_to_recovery() -> None:
    db, plan, ledger = _atomic_harness()
    db.run_in_transaction(
        lambda transaction: register_bound(
            transaction,
            _ParamTypes,
            plan.scope,
            "foreign-authorization",
            "foreign-candidate",
            1,
            500,
        )
    )

    with pytest.raises(SpendLeaseArbitrationConflict):
        db.run_in_transaction(
            lambda transaction: plan.transaction_hook(
                transaction, _ParamTypes, "workspace-1", 0
            )
        )

    assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == 0
    assert db.spend_lease_open["candidate-1"]["phase"] == "candidate"
    assert ledger.leases["candidate-1"].allocations[0].state.value == "reserved"
    assert ledger.compensations == 0
    assert ("spend_lease", "candidate-1") not in db.rows


def test_decision_44_mint_lost_rolls_back_every_mint_effect() -> None:
    db, plan, _ledger = _atomic_harness()
    db.run_in_transaction(
        lambda transaction: take_recovery_ownership(
            transaction, _ParamTypes, "candidate-1"
        )
    )

    with pytest.raises(SpendLeaseMintLost):
        _run_plan(db, plan)

    result = _run_plan(db, None)

    assert result["outcome"] == "accepted"
    assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == 500
    assert db.spend_lease_arbitrations == {}
    assert db.spend_lease_open["candidate-1"]["phase"] == "recovering"
    assert ("spend_lease", "candidate-1") not in db.rows
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    assert not db.gateway_authorizations["authorization-1"]["spend_lease_token"]


def test_decision_47_a1_transfer_aborts_without_returning_presented_token() -> None:
    db, mint_plan, _ledger = _atomic_harness()
    fence = db.rows[("spend_lease_active_grant", "fence-1")]
    fence.body = json.dumps(
        {
            "lease_id": "winner-2",
            "gen": 2,
            "open_predecessor_count": 0,
            "lease_status": "active",
        }
    )
    reuse_plan = replace(
        mint_plan,
        mode="reuse",
        candidate=None,
        artifact=replace(mint_plan.artifact, lease_id="presented-1", gen=1),
        observed_gen=1,
    )

    with pytest.raises(SpendLeaseReuseLost, match="lease_transferred"):
        db.run_in_transaction(
            lambda transaction: reuse_plan.transaction_hook(
                transaction, _ParamTypes, "workspace-1", 0
            )
        )

    assert db.spend_lease_arbitrations == {}


def _insert_global_lease(
    db: FakeSpannerDatabase,
    *,
    lease_id: str,
    gen: int,
    expires_at: datetime,
    holds_predecessor_slot: bool = False,
) -> None:
    db.run_in_transaction(
        lambda transaction: insert_entity_dml(
            transaction,
            _ParamTypes,
            "spend_lease",
            lease_id,
            json.dumps(
                {
                    "state": "ACTIVE",
                    "lease_id": lease_id,
                    "gen": gen,
                    "expires_at": expires_at.isoformat(),
                    "holds_predecessor_slot": holds_predecessor_slot,
                }
            ),
        )
    )


@pytest.mark.parametrize(
    ("case", "expected_reason", "expected_bound"),
    [
        pytest.param("missing", None, None, id="missing-or-corrupt"),
        pytest.param("lost", "lease_transferred", False, id="lost-race"),
        pytest.param("stale", "stale_advisory", False, id="stale-advisory"),
        pytest.param("count", "predecessor_limit", False, id="count-exhausted"),
        pytest.param("window", "window_open", False, id="window-open"),
        pytest.param("pass", None, True, id="all-guards-pass"),
    ],
)
def test_decision_45_fence_truth_table_runs_statement_through_hook(
    case: str,
    expected_reason: str | None,
    expected_bound: bool | None,
) -> None:
    db, plan, _ledger = _atomic_harness()
    fence_key = ("spend_lease_active_grant", plan.fence_id)
    fence = db.rows[fence_key]
    if case == "missing":
        del db.rows[fence_key]
    elif case in {"lost", "stale"}:
        fence.body = json.dumps(
            {
                "lease_id": "winner-2",
                "gen": 2,
                "open_predecessor_count": 0,
                "lease_status": "active",
            }
        )
        _insert_global_lease(
            db,
            lease_id="winner-2",
            gen=2,
            expires_at=(
                datetime.now(UTC) + timedelta(minutes=5)
                if case == "lost"
                else datetime.now(UTC) - timedelta(minutes=5)
            ),
        )
    elif case == "count":
        fence.body = json.dumps(
            {
                "lease_id": "incumbent-1",
                "gen": 0,
                "open_predecessor_count": 3,
                "lease_status": "active",
            }
        )
        _insert_global_lease(
            db,
            lease_id="incumbent-1",
            gen=0,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        plan = replace(plan, incumbent_lease_id="incumbent-1")
    elif case == "window":
        plan = replace(plan, incumbent_window_closed=False)

    if case == "missing":
        with pytest.raises(SpendLeaseContractError, match="fence loss"):
            _run_plan(db, plan)
        return

    result = _run_plan(db, plan)
    assert result["bound"] is expected_bound
    assert result["no_lease_reason"] == expected_reason


def test_decision_45_statement_clock_expiry_is_mint_lost_before_other_causes() -> None:
    db, plan, _ledger = _atomic_harness()
    expired_at = datetime.now(UTC) - timedelta(seconds=2)
    assert plan.candidate is not None
    candidate = replace(plan.candidate, expires_at=expired_at)
    plan = replace(
        plan,
        artifact=replace(plan.artifact, exp=int(expired_at.timestamp())),
        candidate=candidate,
    )
    db.spend_lease_open[candidate.lease_id]["expires_at"] = expired_at
    db.now = expired_at + timedelta(seconds=1)

    with pytest.raises(SpendLeaseMintLost, match="candidate expiry guard"):
        _run_plan(db, plan)

    fence = json.loads(db.rows[("spend_lease_active_grant", plan.fence_id)].body)
    assert fence["gen"] == 0


def test_decision_44_fence_loss_unmarks_marked_incumbent() -> None:
    db, plan, _ledger = _atomic_harness()
    incumbent_id = "winner-marked"
    _insert_global_lease(
        db,
        lease_id=incumbent_id,
        gen=2,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.rows[("spend_lease_active_grant", plan.fence_id)].body = json.dumps(
        {
            "lease_id": incumbent_id,
            "gen": 2,
            "open_predecessor_count": 0,
            "lease_status": "active",
        }
    )
    plan = replace(plan, incumbent_lease_id=incumbent_id)

    result = _run_plan(db, plan)

    assert result["spend_lease_outcome"] == "fence_lost_race"
    incumbent = json.loads(db.rows[("spend_lease", incumbent_id)].body)
    assert incumbent["holds_predecessor_slot"] is False


def _store_binding_harness() -> tuple[Any, FakeSpannerDatabase, Any, BindingPlan, _RecordingLedger]:
    store, db, _table = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="workspace-1", name="Test", owner_user_id="user-1")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    db.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
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
        name="binding",
        creator_user_id=workspace.owner_user_id,
        limit_microdollars=50_000_000,
    )
    ledger = _RecordingLedger()
    store._spend_lease_ledger = ledger
    plan, reason = store.prepare_gateway_spend_lease_binding(
        workspace_id=workspace.id,
        key_hash=key.hash,
        authorization_id="authorization-bound",
        idempotency_key="idem-bound",
        idempotency_fingerprint="fingerprint-bound",
        estimate=500,
        boot_kid="boot-1",
        region="us-central1",
        signer=SpendLeaseSigner(lambda: bytes(range(32))),
        catalog={"version": "catalog-1", "candidates": []},
        ttl_seconds=60,
        skew_seconds=10,
        max_microdollars=1_000_000,
        max_available_basis_points=1_000,
        echo_lease_id=None,
        echo_state="empty",
    )
    assert reason is None and isinstance(plan, BindingPlan)
    return store, db, key, plan, ledger


def _authorize_store(
    store: Any,
    key_hash: str,
    plan: BindingPlan,
) -> tuple[Any, GatewayAuthorization | None]:
    result: tuple[Any, GatewayAuthorization | None] = store.authorize_gateway_typed(
        workspace_id="workspace-1",
        key_hash=key_hash,
        authorization_id="authorization-bound",
        estimate=500,
        has_credit_candidate=True,
        reservation_usage_type=UsageType.CREDITS,
        model_id="model-1",
        provider="provider-1",
        requested_model_id="model-1",
        candidate_model_ids=["model-1"],
        region="us-central1",
        endpoint_id="endpoint-1",
        candidate_endpoint_ids=["endpoint-1"],
        idempotency_key="idem-bound",
        idempotency_fingerprint="fingerprint-bound",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        spend_lease_binding_plan=plan,
    )
    return result


def _assert_no_spend_lease_fields(authorization: GatewayAuthorization) -> None:
    assert (
        authorization.spend_lease_token,
        authorization.spend_lease_id,
        authorization.spend_lease_cap_micro,
        authorization.spend_lease_gen,
        authorization.spend_lease_iat,
        authorization.spend_lease_exp,
        authorization.spend_lease_issuer_kid,
        authorization.spend_lease_boot_kid,
        authorization.spend_lease_catalog_version,
        authorization.spend_lease_status,
        authorization.spend_lease_allocated_micro,
    ) == (None,) * 11


def _configure_unbound_entrypoint_case(
    db: FakeSpannerDatabase,
    plan: BindingPlan,
    case: str,
) -> BindingPlan:
    fence_key = ("spend_lease_active_grant", plan.fence_id)
    if case == "escrow_refused":
        db.typed["tr_credit_balance"][("workspace-1", 0)]["total_credits"] = 500
    elif case == "scope_claimed":
        db.run_in_transaction(
            lambda transaction: register_claim(
                transaction, _ParamTypes, plan.scope, plan.provisional_id
            )
        )
    elif case in {"fence_lost_race", "fence_stale_advisory"}:
        db.rows[fence_key].body = json.dumps(
            {
                "lease_id": "winner-2",
                "gen": 2,
                "open_predecessor_count": 0,
                "lease_status": "active",
            }
        )
        _insert_global_lease(
            db,
            lease_id="winner-2",
            gen=2,
            expires_at=(
                datetime.now(UTC) + timedelta(minutes=5)
                if case == "fence_lost_race"
                else datetime.now(UTC) - timedelta(minutes=5)
            ),
        )
    elif case == "fence_count_exhausted":
        db.rows[fence_key].body = json.dumps(
            {
                "lease_id": "incumbent-1",
                "gen": 0,
                "open_predecessor_count": 3,
                "lease_status": "active",
            }
        )
        _insert_global_lease(
            db,
            lease_id="incumbent-1",
            gen=0,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        plan = replace(plan, incumbent_lease_id="incumbent-1")
    elif case == "fence_window_open":
        plan = replace(plan, incumbent_window_closed=False)
    elif case == "mint_lost":
        db.run_in_transaction(
            lambda transaction: take_recovery_ownership(
                transaction, _ParamTypes, plan.artifact.lease_id
            )
        )
    elif case == "reuse_transfer":
        db.rows[fence_key].body = json.dumps(
            {
                "lease_id": "winner-2",
                "gen": 2,
                "open_predecessor_count": 0,
                "lease_status": "active",
            }
        )
        _insert_global_lease(
            db,
            lease_id="winner-2",
            gen=2,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        plan = replace(plan, mode="reuse", candidate=None, observed_gen=1)
    elif case == "reuse_expiry":
        db.rows[fence_key].body = json.dumps(
            {
                "lease_id": plan.artifact.lease_id,
                "gen": 1,
                "open_predecessor_count": 0,
                "lease_status": "active",
            }
        )
        _insert_global_lease(
            db,
            lease_id=plan.artifact.lease_id,
            gen=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        plan = replace(
            plan,
            mode="reuse",
            candidate=None,
            observed_gen=1,
            admission_deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
    else:  # pragma: no cover - parametrization owns the cases
        raise AssertionError(f"unknown unbound entrypoint case: {case}")
    return plan


@pytest.mark.parametrize(
    ("case", "expected_reason", "expected_outcome"),
    [
        ("escrow_refused", "escrow_headroom", "escrow_refused"),
        ("scope_claimed", "scope_arbitrated", "scope_claimed"),
        ("fence_lost_race", "lease_transferred", "fence_lost_race"),
        ("fence_stale_advisory", "stale_advisory", "fence_stale_advisory"),
        ("fence_count_exhausted", "predecessor_limit", "fence_count_exhausted"),
        ("fence_window_open", "window_open", "fence_window_open"),
        ("mint_lost", "mint_lost", "mint_lost"),
        ("reuse_transfer", "lease_transferred", "ordinary"),
        ("reuse_expiry", "lease_expired", "ordinary"),
    ],
)
def test_entrypoint_unbound_seams_return_no_spend_lease_fields(
    case: str,
    expected_reason: str,
    expected_outcome: str,
) -> None:
    store, db, key, plan, _ledger = _store_binding_harness()
    plan = _configure_unbound_entrypoint_case(db, plan, case)

    verdict, authorization = _authorize_store(store, key.hash, plan)

    assert verdict.spend_lease_bound is False
    assert verdict.no_lease_reason == expected_reason
    assert verdict.spend_lease_outcome == expected_outcome
    assert authorization is not None
    _assert_no_spend_lease_fields(authorization)


def test_entrypoint_mint_lost_commits_one_fresh_ordinary_authorization() -> None:
    store, db, key, plan, ledger = _store_binding_harness()
    plan = _configure_unbound_entrypoint_case(db, plan, "mint_lost")
    fence_before = db.rows[("spend_lease_active_grant", plan.fence_id)].body
    commits_before = db.commits

    verdict, authorization = _authorize_store(store, key.hash, plan)

    assert db.commits == commits_before + 1
    assert verdict.no_lease_reason == "mint_lost"
    assert verdict.spend_lease_bound is False
    assert authorization is not None
    _assert_no_spend_lease_fields(authorization)
    assert len(db.reservations) == len(db.gateway_authorizations) == 1
    assert db.typed["tr_credit_balance"][("workspace-1", 0)]["reserved"] == 500
    assert db.spend_lease_arbitrations == {}
    assert db.rows[("spend_lease_active_grant", plan.fence_id)].body == fence_before
    assert db.spend_lease_open[plan.artifact.lease_id]["phase"] == "recovering"
    assert ledger.leases[plan.artifact.lease_id].allocations[0].state == AllocationState.RESERVED


@pytest.mark.parametrize(
    "case",
    [pytest.param("reuse_transfer", id="a1-transfer"), pytest.param("reuse_expiry", id="a1-expiry")],
)
def test_entrypoint_a1_reuse_losses_compensate_with_claim(case: str) -> None:
    store, db, key, plan, ledger = _store_binding_harness()
    plan = _configure_unbound_entrypoint_case(db, plan, case)

    verdict, authorization = _authorize_store(store, key.hash, plan)

    assert verdict.spend_lease_bound is False
    assert authorization is not None
    allocation = ledger.leases[plan.artifact.lease_id].allocations[0]
    assert allocation.terminal_source == TerminalSource.BINDING_ABSENCE
    assert ledger.compensations == 1
    registration = next(iter(db.spend_lease_arbitrations.values()))
    assert registration["registration_kind"] == "CLAIM"
    assert registration["provisional_id"] == plan.provisional_id


def test_flag_on_store_path_mints_binds_and_returns_token_iff_bound() -> None:
    store, _db, key, plan, ledger = _store_binding_harness()

    verdict, authorization = _authorize_store(store, key.hash, plan)

    assert verdict.spend_lease_bound is True
    assert authorization is not None
    assert bool(authorization.spend_lease_token) is verdict.spend_lease_bound
    assert ledger.binds == 1
