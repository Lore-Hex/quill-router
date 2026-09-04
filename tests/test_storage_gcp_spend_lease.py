from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes.spanner import FakeSpannerDatabase
from trusted_router import spend_leases
from trusted_router.storage_gcp_request_records import (
    insert_gateway_authorization,
    read_gateway_authorization,
    read_gateway_authorization_admission_columns,
)
from trusted_router.storage_gcp_spend_lease import (
    AUTHORIZATION_ADMISSION_TYPED_COLUMNS,
    AUTHORIZATION_TYPED_COLUMNS,
    CandidateIdentity,
    SpendLeaseDataError,
    arm_bound_retention,
    authorization_admission_typed_columns,
    authorization_typed_columns,
    complete_candidate,
    due_candidates,
    due_open,
    insert_candidate,
    merge_authorization_typed_columns,
    read_open_row,
    read_registration,
    register_bound,
    register_claim,
    take_recovery_ownership,
    upgrade_candidate_to_open,
)
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

PT = SimpleNamespace(STRING="STRING", INT64="INT64", BOOL="BOOL", TIMESTAMP="TIMESTAMP")
NOW = datetime.now(UTC).replace(microsecond=0)
LEASE_ARTIFACT = "opaque-signed-artifact"


def _txn(db: FakeSpannerDatabase, fn: Callable[[Any], Any]) -> Any:
    return db.run_in_transaction(fn)


def _candidate(
    lease_id: str = "lease-1",
    *,
    creator: str = "authorization-1",
    expires_at: datetime | None = None,
) -> CandidateIdentity:
    return CandidateIdentity(
        lease_id=lease_id,
        gen=7,
        key_hash="k" * 64,
        boot_kid="boot-1",
        cap_micro=50_000,
        skew_seconds=30,
        workspace_id="workspace-1",
        region="us-central1",
        creating_authorization_id=creator,
        idempotency_scope=f"workspace-1:{lease_id}",
        expires_at=expires_at or NOW - timedelta(minutes=5),
    )


def _insert(db: FakeSpannerDatabase, identity: CandidateIdentity) -> int:
    return int(
        _txn(
            db,
            lambda transaction: insert_candidate(
                transaction,
                PT,
                identity,
                created_at=NOW - timedelta(minutes=10),
            ),
        )
    )


def test_bound_then_bound_returns_zero_and_reads_first_registration() -> None:
    db = FakeSpannerDatabase()
    scope = "workspace-1:request-1"

    first = _txn(
        db,
        lambda transaction: register_bound(
            transaction, PT, scope, "authorization-1", "lease-1", 3, 700
        ),
    )

    def lose_and_read(transaction: Any) -> tuple[int, Any]:
        count = register_bound(
            transaction, PT, scope, "authorization-2", "lease-2", 4, 900
        )
        return count, read_registration(transaction, PT, scope)

    second, registration = _txn(db, lose_and_read)
    assert (first, second) == (1, 0)
    assert registration is not None
    assert registration.kind == "BOUND"
    assert registration.authorization_id == "authorization-1"
    assert registration.lease is not None
    assert (
        registration.lease.lease_id,
        registration.lease.gen,
        registration.lease.allocated_micro,
    ) == ("lease-1", 3, 700)
    assert registration.provisional_id is None


def test_claim_then_bound_returns_zero_and_reads_claim_registration() -> None:
    db = FakeSpannerDatabase()
    scope = "workspace-1:request-claim"
    assert _txn(db, lambda transaction: register_claim(transaction, PT, scope, "prov-1")) == 1

    def lose_and_read(transaction: Any) -> tuple[int, Any]:
        count = register_bound(
            transaction, PT, scope, "authorization-1", "lease-1", 1, 100
        )
        return count, read_registration(transaction, PT, scope)

    count, registration = _txn(db, lose_and_read)
    assert count == 0
    assert registration is not None
    assert registration.kind == "CLAIM"
    assert registration.provisional_id == "prov-1"
    assert registration.authorization_id is None
    assert registration.lease is None


def test_claim_is_armed_at_insert_and_bound_is_armed_explicitly() -> None:
    db = FakeSpannerDatabase()
    claim_scope = "workspace-1:claim"
    bound_scope = "workspace-1:bound"
    assert _txn(
        db, lambda transaction: register_claim(transaction, PT, claim_scope, "prov-1")
    ) == 1
    assert _txn(
        db,
        lambda transaction: register_bound(
            transaction, PT, bound_scope, "authorization-1", "lease-1", 1, 100
        ),
    ) == 1

    claim = db.spend_lease_arbitrations[
        (spend_leases.spend_lease_scope_salt(claim_scope), claim_scope)
    ]
    bound = db.spend_lease_arbitrations[
        (spend_leases.spend_lease_scope_salt(bound_scope), bound_scope)
    ]
    assert claim["terminal_at"] is not None
    assert bound["terminal_at"] is None

    terminal_at = NOW + timedelta(hours=1)
    assert _txn(
        db,
        lambda transaction: arm_bound_retention(
            transaction, PT, "authorization-1", terminal_at
        ),
    ) == 1
    assert bound["terminal_at"] is None  # committed records are replaced, not mutated in place
    assert db.spend_lease_arbitrations[
        (spend_leases.spend_lease_scope_salt(bound_scope), bound_scope)
    ]["terminal_at"] == terminal_at


def test_every_scope_key_path_uses_shared_scope_salt_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeSpannerDatabase()
    calls: list[str] = []

    def sentinel(scope: str) -> str:
        calls.append(scope)
        return "beef"

    monkeypatch.setattr(spend_leases, "spend_lease_scope_salt", sentinel)
    bound_scope = "workspace-1:bound"
    claim_scope = "workspace-1:claim"
    assert _txn(
        db,
        lambda transaction: register_bound(
            transaction, PT, bound_scope, "authorization-1", "lease-1", 1, 100
        ),
    ) == 1
    assert _txn(
        db, lambda transaction: register_claim(transaction, PT, claim_scope, "prov-1")
    ) == 1
    registration = _txn(
        db, lambda transaction: read_registration(transaction, PT, bound_scope)
    )

    assert registration is not None
    assert set(db.spend_lease_arbitrations) == {
        ("beef", bound_scope),
        ("beef", claim_scope),
    }
    assert calls == [bound_scope, claim_scope, bound_scope]


def test_candidate_insert_is_primary_key_idempotent_and_read_preserves_identity() -> None:
    db = FakeSpannerDatabase()
    identity = _candidate()
    assert _insert(db, identity) == 1
    assert _insert(db, identity) == 0

    with db.snapshot() as snapshot:
        row = read_open_row(snapshot, PT, identity.lease_id)
    assert row is not None
    assert row.phase == "candidate"
    assert row.identity == identity
    assert row.recovering_at is None
    assert row.created_at == NOW - timedelta(minutes=10)
    assert row.next_attempt_at == identity.expires_at + timedelta(seconds=identity.skew_seconds)


def test_upgrade_requires_candidate_phase_and_matching_creator() -> None:
    db = FakeSpannerDatabase()
    identity = _candidate()
    assert _insert(db, identity) == 1
    assert _txn(
        db,
        lambda transaction: upgrade_candidate_to_open(
            transaction,
            PT,
            identity.lease_id,
            "wrong-creator",
            identity.expires_at,
            identity.skew_seconds,
        ),
    ) == 0
    assert _txn(
        db,
        lambda transaction: upgrade_candidate_to_open(
            transaction,
            PT,
            identity.lease_id,
            identity.creating_authorization_id,
            identity.expires_at,
            identity.skew_seconds,
        ),
    ) == 1
    assert _txn(
        db,
        lambda transaction: upgrade_candidate_to_open(
            transaction,
            PT,
            identity.lease_id,
            identity.creating_authorization_id,
            identity.expires_at,
            identity.skew_seconds,
        ),
    ) == 0


def test_recovery_ownership_fences_later_upgrade_and_records_proof_time() -> None:
    db = FakeSpannerDatabase()
    identity = _candidate()
    assert _insert(db, identity) == 1
    assert _txn(
        db, lambda transaction: take_recovery_ownership(transaction, PT, identity.lease_id)
    ) == 1
    assert _txn(
        db,
        lambda transaction: upgrade_candidate_to_open(
            transaction,
            PT,
            identity.lease_id,
            identity.creating_authorization_id,
            identity.expires_at,
            identity.skew_seconds,
        ),
    ) == 0
    with db.snapshot() as snapshot:
        row = read_open_row(snapshot, PT, identity.lease_id)
    assert row is not None
    assert row.phase == "recovering"
    assert row.recovering_at is not None


def test_open_upgrade_fences_later_recovery_ownership() -> None:
    db = FakeSpannerDatabase()
    identity = _candidate()
    assert _insert(db, identity) == 1
    assert _txn(
        db,
        lambda transaction: upgrade_candidate_to_open(
            transaction,
            PT,
            identity.lease_id,
            identity.creating_authorization_id,
            identity.expires_at,
            identity.skew_seconds,
        ),
    ) == 1
    assert _txn(
        db, lambda transaction: take_recovery_ownership(transaction, PT, identity.lease_id)
    ) == 0


def test_complete_candidate_only_matches_recovering_phase() -> None:
    db = FakeSpannerDatabase()
    identity = _candidate()
    assert _insert(db, identity) == 1
    assert _txn(
        db, lambda transaction: complete_candidate(transaction, PT, identity.lease_id)
    ) == 0
    assert _txn(
        db, lambda transaction: take_recovery_ownership(transaction, PT, identity.lease_id)
    ) == 1
    assert _txn(
        db, lambda transaction: complete_candidate(transaction, PT, identity.lease_id)
    ) == 1
    assert _txn(
        db, lambda transaction: complete_candidate(transaction, PT, identity.lease_id)
    ) == 0


def test_due_scans_are_phase_partitioned_ordered_and_exclude_done_rows() -> None:
    db = FakeSpannerDatabase()
    done = _candidate("lease-done", expires_at=NOW - timedelta(minutes=30))
    candidate = _candidate("lease-candidate", expires_at=NOW - timedelta(minutes=20))
    opened = _candidate("lease-open", expires_at=NOW - timedelta(minutes=10))
    for identity in (opened, done, candidate):
        assert _insert(db, identity) == 1
    assert _txn(
        db, lambda transaction: take_recovery_ownership(transaction, PT, done.lease_id)
    ) == 1
    assert _txn(
        db, lambda transaction: complete_candidate(transaction, PT, done.lease_id)
    ) == 1
    assert _txn(
        db,
        lambda transaction: upgrade_candidate_to_open(
            transaction,
            PT,
            opened.lease_id,
            opened.creating_authorization_id,
            opened.expires_at,
            opened.skew_seconds,
        ),
    ) == 1

    with db.snapshot() as snapshot:
        candidate_rows = due_candidates(snapshot, PT, 10)
    with db.snapshot() as snapshot:
        open_rows = due_open(snapshot, PT, 10)
    assert [row.lease_id for row in candidate_rows] == ["lease-candidate"]
    assert [row.lease_id for row in open_rows] == ["lease-open"]
    assert "lease-done" not in {
        *(row.lease_id for row in candidate_rows),
        *(row.lease_id for row in open_rows),
    }


def test_authorization_typed_columns_round_trip_merge_and_reject_zero_allocation() -> None:
    payload = {
        "authorization_id": "authorization-1",
        "spend_lease_id": "lease-1",
        "spend_lease_gen": 3,
        "spend_lease_allocated_micro": 700,
        "spend_lease_token": "signed-token",
        "spend_lease_status": "active",
        "spend_lease_exp": 1_800_000_000,
        "idempotency_fingerprint": "f" * 64,
        "finalization_outcome": "settled",
        "finalized_cost_microdollars": 650,
    }
    typed = authorization_typed_columns(payload)
    assert tuple(typed) == AUTHORIZATION_TYPED_COLUMNS
    assert typed["spend_lease_exp"] == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert merge_authorization_typed_columns(None, typed) == {
        **{key: value for key, value in payload.items() if key != "authorization_id"},
        "settlement": "spend_lease",
    }

    old_writer_payload = dict(payload, spend_lease_status="expired", finalization_outcome="refunded")
    merged = merge_authorization_typed_columns(
        old_writer_payload,
        {"spend_lease_status": None, "finalization_outcome": "settled"},
    )
    assert merged["spend_lease_status"] == "expired"
    assert merged["finalization_outcome"] == "settled"

    with pytest.raises(SpendLeaseDataError, match="NULL or positive"):
        authorization_typed_columns({"spend_lease_allocated_micro": 0})
    with pytest.raises(SpendLeaseDataError, match="NULL or positive"):
        merge_authorization_typed_columns(
            {"spend_lease_allocated_micro": 0},
            {},
        )


def test_stage_c_authorization_columns_validate_and_round_trip_typed_storage() -> None:
    assert len(AUTHORIZATION_TYPED_COLUMNS[:9] + AUTHORIZATION_ADMISSION_TYPED_COLUMNS) == 11
    assert AUTHORIZATION_ADMISSION_TYPED_COLUMNS == (
        "spend_lease_admission_receipt",
        "spend_lease_receipt_hash",
    )
    receipt = "header.payload.signature"
    receipt_hash = "a" * 64
    assert authorization_admission_typed_columns(
        {
            "spend_lease_admission_receipt": receipt,
            "spend_lease_receipt_hash": receipt_hash,
        }
    ) == {
        "spend_lease_admission_receipt": receipt,
        "spend_lease_receipt_hash": receipt_hash,
    }
    with pytest.raises(SpendLeaseDataError, match="NULL together"):
        authorization_admission_typed_columns(
            {
                "spend_lease_admission_receipt": receipt,
                "spend_lease_receipt_hash": None,
            }
        )
    with pytest.raises(SpendLeaseDataError, match="lowercase SHA-256"):
        authorization_admission_typed_columns(
            {
                "spend_lease_admission_receipt": receipt,
                "spend_lease_receipt_hash": "A" * 64,
            }
        )

    db = FakeSpannerDatabase()
    authorization = GatewayAuthorization(
        id="authorization-stage-c",
        workspace_id="workspace-stage-c",
        key_hash="k" * 64,
        model_id="model-stage-c",
        provider="provider-stage-c",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=100,
        credit_reservation_id="reservation-stage-c",
        spend_lease_admission_receipt=receipt,
        spend_lease_receipt_hash=receipt_hash,
    )
    _txn(
        db,
        lambda transaction: insert_gateway_authorization(
            transaction,
            PT,
            authorization,
            created_at=NOW,
        ),
    )
    with db.snapshot() as snapshot:
        stored = read_gateway_authorization_admission_columns(
            snapshot,
            PT,
            authorization.id,
        )
    assert stored == {
        "spend_lease_admission_receipt": receipt,
        "spend_lease_receipt_hash": receipt_hash,
    }


def _insert_bound_authorization(
    db: FakeSpannerDatabase,
    *,
    authorization_id: str,
    settlement: str,
) -> None:
    authorization = GatewayAuthorization(
        id=authorization_id,
        workspace_id="workspace-reader",
        key_hash="k" * 64,
        model_id="model-reader",
        provider="provider-reader",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=1_000,
        credit_reservation_id="reservation-reader",
        settlement=settlement,
        spend_lease_id="lease-reader",
        spend_lease_gen=4,
        spend_lease_allocated_micro=900,
        spend_lease_token=LEASE_ARTIFACT,
        spend_lease_status="active",
        spend_lease_exp=1_800_000_000,
    )
    _txn(
        db,
        lambda transaction: insert_gateway_authorization(
            transaction,
            PT,
            authorization,
            created_at=NOW,
        ),
    )


def test_payload_erased_complete_typed_tuple_reads_back_as_spend_lease() -> None:
    db = FakeSpannerDatabase()
    authorization_id = "authorization-erased-reader"
    _insert_bound_authorization(
        db,
        authorization_id=authorization_id,
        settlement="spend_lease",
    )
    db.gateway_authorizations[authorization_id]["payload"] = None

    with db.snapshot() as snapshot:
        authorization = read_gateway_authorization(snapshot, PT, authorization_id)

    assert authorization is not None
    assert authorization.settlement == "spend_lease"
    assert authorization.spend_lease_id == "lease-reader"
    assert authorization.spend_lease_gen == 4
    assert authorization.spend_lease_allocated_micro == 900
    assert authorization.spend_lease_token == LEASE_ARTIFACT
    assert authorization.spend_lease_status == "active"
    assert authorization.spend_lease_exp == 1_800_000_000


def test_payload_local_complete_typed_tuple_reads_back_as_spend_lease_and_logs_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = FakeSpannerDatabase()
    authorization_id = "authorization-mismatch-reader"
    _insert_bound_authorization(
        db,
        authorization_id=authorization_id,
        settlement="local",
    )

    with caplog.at_level(logging.ERROR):
        with db.snapshot() as snapshot:
            authorization = read_gateway_authorization(snapshot, PT, authorization_id)

    assert authorization is not None
    assert authorization.settlement == "spend_lease"
    assert authorization.spend_lease_id == "lease-reader"
    assert authorization.spend_lease_gen == 4
    assert authorization.spend_lease_allocated_micro == 900
    assert "spend_lease.settlement_kind_mismatch" in caplog.text
