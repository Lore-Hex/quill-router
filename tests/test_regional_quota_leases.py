from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from trusted_router.config import Settings
from trusted_router.services.regional_quota_leases import (
    HoldState,
    LeaseExhaustedError,
    LeaseFenceMismatchError,
    LeaseIdempotencyConflictError,
    LeaseSettlementError,
    LeaseState,
    LeaseUnavailableError,
    RegionalQuotaLease,
    RegionalQuotaLeaseError,
    bounded_lease_grant_microdollars,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)


def _lease(*, grant: int = 1_000, expires_in: int = 60) -> RegionalQuotaLease:
    return RegionalQuotaLease(
        lease_id="lease-1",
        workspace_id="workspace-1",
        region="us-east4",
        fencing_token=7,
        granted_microdollars=grant,
        expires_at=NOW + timedelta(seconds=expires_in),
    )


def test_reserve_settle_and_refund_preserve_exact_integer_accounting() -> None:
    first = (
        _lease()
        .reserve(
            hold_id="a",
            fingerprint="fp-a",
            amount_microdollars=400,
            fencing_token=7,
            now=NOW,
        )
        .lease
    )
    second = first.reserve(
        hold_id="b",
        fingerprint="fp-b",
        amount_microdollars=300,
        fencing_token=7,
        now=NOW,
    ).lease

    settled = second.settle(hold_id="a", actual_microdollars=275, fencing_token=7).lease
    refunded = settled.refund(hold_id="b", fencing_token=7).lease

    assert refunded.spent_microdollars == 275
    assert refunded.reserved_microdollars == 0
    assert refunded.available_microdollars == 725
    assert refunded.holds[0].state == HoldState.SETTLED
    assert refunded.holds[1].state == HoldState.REFUNDED


def test_reservation_and_terminal_transitions_are_idempotent() -> None:
    transition = _lease().reserve(
        hold_id="a",
        fingerprint="fp",
        amount_microdollars=400,
        fencing_token=7,
        now=NOW,
    )
    replay = transition.lease.reserve(
        hold_id="a",
        fingerprint="fp",
        amount_microdollars=400,
        fencing_token=7,
        now=NOW,
    )
    assert replay.replayed is True
    assert replay.lease is transition.lease

    settled = replay.lease.settle(hold_id="a", actual_microdollars=250, fencing_token=7)
    settled_replay = settled.lease.settle(hold_id="a", actual_microdollars=250, fencing_token=7)
    assert settled_replay.replayed is True
    assert settled_replay.lease is settled.lease


@pytest.mark.parametrize(
    ("fingerprint", "amount"),
    [("different", 400), ("fp", 401)],
)
def test_idempotency_reuse_with_changed_inputs_fails(fingerprint: str, amount: int) -> None:
    lease = (
        _lease()
        .reserve(
            hold_id="a",
            fingerprint="fp",
            amount_microdollars=400,
            fencing_token=7,
            now=NOW,
        )
        .lease
    )
    with pytest.raises(LeaseIdempotencyConflictError):
        lease.reserve(
            hold_id="a",
            fingerprint=fingerprint,
            amount_microdollars=amount,
            fencing_token=7,
            now=NOW,
        )


def test_lease_fails_closed_on_expiry_exhaustion_and_stale_fence() -> None:
    with pytest.raises(LeaseUnavailableError, match="expired"):
        _lease(expires_in=0).reserve(
            hold_id="a",
            fingerprint="fp",
            amount_microdollars=1,
            fencing_token=7,
            now=NOW,
        )
    with pytest.raises(LeaseExhaustedError):
        _lease(grant=100).reserve(
            hold_id="a",
            fingerprint="fp",
            amount_microdollars=101,
            fencing_token=7,
            now=NOW,
        )
    with pytest.raises(LeaseFenceMismatchError):
        _lease().reserve(
            hold_id="a",
            fingerprint="fp",
            amount_microdollars=1,
            fencing_token=6,
            now=NOW,
        )


def test_settlement_cannot_exceed_hold_or_cross_refund_boundary() -> None:
    lease = (
        _lease()
        .reserve(
            hold_id="a",
            fingerprint="fp",
            amount_microdollars=100,
            fencing_token=7,
            now=NOW,
        )
        .lease
    )
    with pytest.raises(LeaseSettlementError, match="fit inside"):
        lease.settle(hold_id="a", actual_microdollars=101, fencing_token=7)
    refunded = lease.refund(hold_id="a", fencing_token=7).lease
    with pytest.raises(LeaseSettlementError, match="refunded"):
        refunded.settle(hold_id="a", actual_microdollars=50, fencing_token=7)


def test_drain_blocks_new_work_and_close_waits_for_open_holds() -> None:
    lease = (
        _lease()
        .reserve(
            hold_id="a",
            fingerprint="fp",
            amount_microdollars=100,
            fencing_token=7,
            now=NOW,
        )
        .lease
    )
    draining = lease.begin_drain(fencing_token=7)
    assert draining.state == LeaseState.DRAINING
    with pytest.raises(LeaseUnavailableError, match="draining"):
        draining.reserve(
            hold_id="b",
            fingerprint="fp-b",
            amount_microdollars=1,
            fencing_token=7,
            now=NOW,
        )
    with pytest.raises(LeaseUnavailableError, match="open reservations"):
        draining.close(fencing_token=7)
    closed = draining.refund(hold_id="a", fencing_token=7).lease.close(fencing_token=7)
    assert closed.state == LeaseState.CLOSED


def test_bounded_grant_is_integer_only_and_caps_global_exposure() -> None:
    assert (
        bounded_lease_grant_microdollars(
            available_microdollars=10_000_003,
            requested_microdollars=5_000_000,
            per_lease_cap_microdollars=3_000_000,
            max_available_basis_points=1_000,
        )
        == 1_000_000
    )
    assert (
        bounded_lease_grant_microdollars(
            available_microdollars=100,
            requested_microdollars=1,
            per_lease_cap_microdollars=50,
            max_available_basis_points=1_000,
        )
        == 1
    )


def test_many_independent_reservations_never_exceed_grant() -> None:
    lease = _lease(grant=10_000)
    for index in range(100):
        lease = lease.reserve(
            hold_id=f"hold-{index}",
            fingerprint=f"fp-{index}",
            amount_microdollars=100,
            fencing_token=7,
            now=NOW,
        ).lease
    assert lease.accounted_microdollars == 10_000
    with pytest.raises(LeaseExhaustedError):
        lease.reserve(
            hold_id="overflow",
            fingerprint="overflow",
            amount_microdollars=1,
            fencing_token=7,
            now=NOW,
        )


@given(
    grant=st.integers(min_value=1, max_value=1_000_000),
    requested=st.lists(
        st.integers(min_value=1, max_value=100_000),
        min_size=0,
        max_size=50,
    ),
)
def test_arbitrary_reservation_sequences_never_exceed_escrow(
    grant: int,
    requested: list[int],
) -> None:
    lease = _lease(grant=grant)
    for index, amount in enumerate(requested):
        if amount > lease.available_microdollars:
            with pytest.raises(LeaseExhaustedError):
                lease.reserve(
                    hold_id=f"hold-{index}",
                    fingerprint=f"fp-{index}",
                    amount_microdollars=amount,
                    fencing_token=7,
                    now=NOW,
                )
        else:
            lease = lease.reserve(
                hold_id=f"hold-{index}",
                fingerprint=f"fp-{index}",
                amount_microdollars=amount,
                fencing_token=7,
                now=NOW,
            ).lease
        assert 0 <= lease.accounted_microdollars <= grant
        assert lease.available_microdollars == grant - lease.accounted_microdollars


def test_pure_transitions_are_safe_to_compute_concurrently() -> None:
    lease = _lease(grant=10_000)

    def reserve(index: int) -> RegionalQuotaLease:
        return lease.reserve(
            hold_id=f"hold-{index}",
            fingerprint=f"fp-{index}",
            amount_microdollars=100,
            fencing_token=7,
            now=NOW,
        ).lease

    with ThreadPoolExecutor(max_workers=8) as pool:
        candidates = list(pool.map(reserve, range(32)))

    assert all(candidate.accounted_microdollars == 100 for candidate in candidates)
    assert lease.accounted_microdollars == 0


def test_invalid_construction_and_grant_inputs_fail_closed() -> None:
    with pytest.raises(RegionalQuotaLeaseError, match="timezone-aware"):
        RegionalQuotaLease(
            lease_id="lease",
            workspace_id="workspace",
            region="us-east4",
            fencing_token=1,
            granted_microdollars=1,
            expires_at=datetime(2026, 7, 31),
        )
    with pytest.raises(RegionalQuotaLeaseError, match="cannot be negative"):
        bounded_lease_grant_microdollars(
            available_microdollars=-1,
            requested_microdollars=1,
            per_lease_cap_microdollars=1,
            max_available_basis_points=1_000,
        )


def test_regional_leases_default_off_and_fail_closed_without_dependencies() -> None:
    defaults = Settings(environment="test")
    assert defaults.regional_quota_leases_enabled is False
    assert defaults.regional_quota_lease_shard_count == 16
    with pytest.raises(ValidationError, match="PILOT_WORKSPACE_IDS"):
        Settings(environment="test", regional_quota_leases_enabled=True)
    with pytest.raises(ValidationError, match="Spanner GCP backend"):
        Settings(
            environment="staging",
            regional_quota_leases_enabled=True,
            regional_quota_lease_pilot_workspace_ids="workspace-1",
        )
    with pytest.raises(ValidationError, match="typed request records"):
        Settings(
            environment="staging",
            storage_backend="spanner-bigtable",
            regional_quota_leases_enabled=True,
            regional_quota_lease_pilot_workspace_ids="workspace-1",
        )
    settings = Settings(
        environment="test",
        regional_quota_leases_enabled=True,
        regional_quota_lease_pilot_workspace_ids=" workspace-1,workspace-2 ",
    )
    assert settings.regional_quota_lease_pilot_workspaces == {
        "workspace-1",
        "workspace-2",
    }


@pytest.mark.parametrize("count", [0, 65])
def test_regional_lease_shard_count_is_bounded(count: int) -> None:
    with pytest.raises(ValidationError, match="LEASE_SHARD_COUNT"):
        Settings(environment="test", regional_quota_lease_shard_count=count)


def test_regional_lease_production_config_requires_fixed_profiles_and_outbox() -> None:
    common = {
        "environment": "staging",
        "storage_backend": "spanner-bigtable",
        "request_record_write_mode": "typed",
        "regional_quota_leases_enabled": True,
        "regional_quota_lease_pilot_workspace_ids": "workspace-1",
    }
    with pytest.raises(ValidationError, match="settle outbox"):
        Settings(**common)
    with pytest.raises(ValidationError, match="fixed regional Bigtable app profiles"):
        Settings(**common, settle_outbox_enabled=True)
    settings = Settings(
        **common,
        settle_outbox_enabled=True,
        regional_quota_bigtable_app_profiles=(
            "us-central1=tr-quota-us-central1,europe-west4=tr-quota-europe-west4"
        ),
    )
    assert settings.regional_quota_bigtable_app_profile_map == {
        "us-central1": "tr-quota-us-central1",
        "europe-west4": "tr-quota-europe-west4",
    }


@pytest.mark.parametrize(
    "value",
    ["missing-separator", "=profile", "region=", "region=a,region=b"],
)
def test_regional_profile_map_rejects_ambiguous_entries(value: str) -> None:
    with pytest.raises(ValueError):
        _ = Settings(
            environment="test",
            regional_quota_bigtable_app_profiles=value,
        ).regional_quota_bigtable_app_profile_map
