from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from google.api_core import exceptions as google_exceptions

import trusted_router.spend_lease_ledger as ledger_module
from tests.fakes.spend_lease_bigtable import FakeBigtableTable as _FakeBigtableTable
from tests.fakes.spend_lease_bigtable import ReadRow as _ReadRow
from trusted_router.config import Settings
from trusted_router.spend_lease_ledger import (
    BigtableSpendLeaseLedger,
    SpendLeaseCasExhausted,
    SpendLeaseLedgerError,
    SpendLeaseLedgerUnprovisioned,
    SpendLeaseVersionError,
)
from trusted_router.spend_lease_state import (
    AbsenceObservation,
    AllocationState,
    AuthorizationDurability,
    AuthorizationObservation,
    BindingAbsenceProof,
    BoundProof,
    ClaimProof,
    ClosedLeaseReplay,
    Created,
    ExistingLocal,
    FinalizationOutcome,
    Mismatch,
    RecoveryProof,
    SpendLease,
    SpendLeaseAllocation,
    TrueReplay,
    UnboundExisting,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)
REGION = "us-central1"


def _candidate() -> SpendLease:
    return SpendLease(
        lease_id="spend-lease-test",
        gen=4,
        key_hash="key-hash",
        boot_kid="boot-kid",
        workspace_id="workspace-test",
        creating_authorization_id="creator-auth",
        cap_micro=10_000,
        expires_at=NOW + timedelta(minutes=1),
        skew=timedelta(seconds=10),
        version=0,
    )


def _allocation() -> SpendLeaseAllocation:
    return SpendLeaseAllocation(
        idempotency_scope="scope-1",
        authorization_id="provisional-1",
        request_fingerprint="fingerprint-1",
        lease_id="spend-lease-test",
        gen=4,
        allocated_micro=500,
        abandon_after=NOW + timedelta(minutes=2),
        key_hash="key-hash",
        workspace_id="workspace-test",
    )


def _allocate(ledger: BigtableSpendLeaseLedger) -> Created:
    result = ledger.allocate(
        None,
        "spend-lease-test",
        region=REGION,
        idempotency_scope="scope-1",
        provisional_authorization_id="provisional-1",
        request_fingerprint="fingerprint-1",
        allocated_micro=500,
        abandon_after=NOW + timedelta(minutes=2),
        now=NOW,
    )
    assert isinstance(result, Created)
    return result


def test_health_check_classifies_missing_table_as_unprovisioned() -> None:
    table = _FakeBigtableTable()
    missing = google_exceptions.NotFound(
        "Table trustedrouter-spend-lease was not found"
    )
    table.commit_error = missing
    ledger = BigtableSpendLeaseLedger({REGION: table})

    with pytest.raises(SpendLeaseLedgerUnprovisioned) as raised:
        ledger.health_check()

    assert raised.value.table_id == "trustedrouter-spend-lease"
    assert raised.value.profile == "tr-spend-us-central1"
    assert raised.value.region == REGION
    assert raised.value.__cause__ is missing


@pytest.mark.parametrize(
    "missing",
    [
        google_exceptions.FailedPrecondition(
            "App profile tr-spend-us-central1 does not exist"
        ),
        google_exceptions.InvalidArgument(
            "Invalid app_profile_id: tr-spend-us-central1"
        ),
    ],
)
def test_health_check_classifies_missing_profile_as_unprovisioned(
    missing: Exception,
) -> None:
    table = _FakeBigtableTable()
    table.commit_error = missing
    ledger = BigtableSpendLeaseLedger({REGION: table})

    with pytest.raises(SpendLeaseLedgerUnprovisioned) as raised:
        ledger.health_check()

    assert raised.value.table_id == "trustedrouter-spend-lease"
    assert raised.value.profile == "tr-spend-us-central1"
    assert raised.value.region == REGION
    assert raised.value.__cause__ is missing


@pytest.mark.parametrize(
    "failure",
    [
        google_exceptions.PermissionDenied("missing permission"),
        google_exceptions.DeadlineExceeded("request timed out"),
    ],
)
def test_health_check_propagates_non_provisioning_failures_unchanged(
    failure: Exception,
) -> None:
    table = _FakeBigtableTable()
    table.commit_error = failure
    ledger = BigtableSpendLeaseLedger({REGION: table})

    with pytest.raises(type(failure)) as raised:
        ledger.health_check()

    assert raised.value is failure


def test_health_check_propagates_unrelated_precondition_failure_unchanged() -> None:
    table = _FakeBigtableTable()
    failure = google_exceptions.FailedPrecondition(
        "single-row transactions are disabled for this routing policy"
    )
    table.commit_error = failure
    ledger = BigtableSpendLeaseLedger({REGION: table})

    with pytest.raises(google_exceptions.FailedPrecondition) as raised:
        ledger.health_check()

    assert raised.value is failure


@pytest.mark.parametrize(
    "failure",
    [
        google_exceptions.FailedPrecondition("column family lease not found"),
        google_exceptions.InvalidArgument("instance missing"),
    ],
)
def test_health_check_propagates_unrelated_missing_resource_unchanged(
    failure: Exception,
) -> None:
    table = _FakeBigtableTable()
    table.commit_error = failure
    ledger = BigtableSpendLeaseLedger({REGION: table})

    with pytest.raises(type(failure)) as raised:
        ledger.health_check()

    assert raised.value is failure


def test_allocate_created_writes_decision_33_row_at_version_one() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)

    result = _allocate(ledger)

    spread = hashlib.sha256(b"spend-lease-test").hexdigest()[:4]
    expected_key = f"{spread}#spend#{REGION}#spend-lease-test".encode()
    assert result.replayed is False
    assert set(table.rows) == {expected_key}
    assert table.rows[expected_key][b"version"] == b"1"
    assert ledger.get("spend-lease-test", region=REGION) == result.lease


@pytest.mark.parametrize(
    "result_kind",
    [
        "true_replay",
        "existing_local",
        "mismatch",
        "unbound_existing",
        "closed_lease_replay",
        "created_replayed",
    ],
)
def test_allocate_replay_results_write_nothing(
    result_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)
    allocation = _allocation()
    carried_lease = replace(_candidate(), allocations=(allocation,), version=1)
    results = {
        "true_replay": TrueReplay(carried_lease, allocation, True),
        "existing_local": ExistingLocal(carried_lease, allocation, True),
        # Mismatch is intentionally non-replayed but still carries no new record.
        "mismatch": Mismatch(carried_lease, "other-authorization"),
        "unbound_existing": UnboundExisting(carried_lease, "other-authorization"),
        "closed_lease_replay": ClosedLeaseReplay(carried_lease, "other-authorization"),
        "created_replayed": Created(
            carried_lease,
            allocation,
            True,
            provisional_id="provisional-1",
        ),
    }
    expected = results[result_kind]

    def classified_result(_self: SpendLease, **_kwargs: Any) -> Any:
        return expected

    monkeypatch.setattr(SpendLease, "allocate", classified_result)
    commits_before = table.commit_attempts

    actual = ledger.allocate(
        None,
        "spend-lease-test",
        region=REGION,
        idempotency_scope="scope-1",
        provisional_authorization_id="provisional-1",
        request_fingerprint="fingerprint-1",
        allocated_micro=500,
        abandon_after=NOW + timedelta(minutes=2),
        now=NOW,
    )

    assert actual is expected
    assert table.commit_attempts == commits_before
    assert ledger.get("spend-lease-test", region=REGION) == _candidate()


def test_transition_refuses_skipped_version_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)
    allocation = _allocation()
    skipped = replace(_candidate(), allocations=(allocation,), version=2)
    crafted = Created(skipped, allocation, False, provisional_id="provisional-1")

    def skipped_result(_self: SpendLease, **_kwargs: Any) -> Created:
        return crafted

    monkeypatch.setattr(SpendLease, "allocate", skipped_result)
    commits_before = table.commit_attempts

    with pytest.raises(SpendLeaseVersionError, match="expected 1"):
        _allocate(ledger)

    assert table.commit_attempts == commits_before
    assert ledger.get("spend-lease-test", region=REGION) == _candidate()


def test_initialize_surviving_row_never_resets_state() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    candidate = _candidate()
    ledger.initialize(candidate, region=REGION)
    allocated = _allocate(ledger).lease
    commits_before = table.commit_attempts

    replay = ledger.initialize(candidate, region=REGION)

    assert replay.replayed is True
    assert replay.lease == allocated
    assert table.commit_attempts == commits_before
    assert ledger.get("spend-lease-test", region=REGION) == allocated


def test_ambiguous_commit_that_landed_rereads_as_replay_without_second_write() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)
    table.raise_after_next_applied_commit = True
    commits_before = table.commit_attempts

    result = _allocate(ledger)

    assert result.replayed is True
    assert table.commit_attempts == commits_before + 1
    assert ledger.get("spend-lease-test", region=REGION) == result.lease
    assert result.lease.version == 1


@pytest.mark.parametrize("transition_name", ["compensate", "bind", "tombstone_unminted"])
def test_replayed_mutating_transition_is_write_free(transition_name: str) -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)

    if transition_name in {"compensate", "bind"}:
        _allocate(ledger)

    if transition_name == "compensate":
        claim = ClaimProof("scope-1", "provisional-1")
        absence = BindingAbsenceProof(
            "scope-1",
            "provisional-1",
            AbsenceObservation.ABSENT_ROW,
        )

        def apply_transition() -> Any:
            return ledger.compensate(
                "spend-lease-test",
                region=REGION,
                idempotency_scope="scope-1",
                expected_provisional_id="provisional-1",
                claim=claim,
                absence=absence,
            )

    elif transition_name == "bind":
        proof = BoundProof(
            idempotency_scope="scope-1",
            authorization_id="authorization-1",
            lease_id="spend-lease-test",
            gen=4,
            allocated_micro=500,
        )

        def apply_transition() -> Any:
            return ledger.bind(
                "spend-lease-test",
                region=REGION,
                expected_provisional_id="provisional-1",
                proof=proof,
            )

    else:
        proof = RecoveryProof(
            recovering_at=NOW,
            creating_authorization_id="creator-auth",
        )

        def apply_transition() -> Any:
            return ledger.tombstone_unminted(
                "spend-lease-test",
                region=REGION,
                proof=proof,
            )

    commits_before = table.commit_attempts
    first = apply_transition()
    row = next(iter(table.rows.values()))
    stored_version_after_first = row[b"version"]
    stored_state_after_first = row[b"state"]

    replay = apply_transition()

    assert first.replayed is False
    assert replay.replayed is True
    assert table.commit_attempts == commits_before + 1
    assert row[b"version"] == stored_version_after_first
    assert row[b"state"] == stored_state_after_first
    assert replay.lease.version == first.lease.version


def test_corrupt_stored_json_raises_ledger_error_not_decoder_error() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)
    row = next(iter(table.rows.values()))
    row[b"state"] = b"{not-json"
    commits_before = table.commit_attempts

    with pytest.raises(SpendLeaseLedgerError) as raised:
        ledger.get("spend-lease-test", region=REGION)

    assert not isinstance(raised.value, ValueError)
    assert table.commit_attempts == commits_before


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_required_key",
        "unknown_lease_state",
        "invalid_expires_at",
        "invalid_duration",
        "unknown_contradiction_discriminator",
    ],
)
def test_get_treats_structurally_invalid_json_as_no_lease(corruption: str) -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)
    _allocate(ledger)
    row = next(iter(table.rows.values()))
    payload = json.loads(row[b"state"])

    if corruption == "missing_required_key":
        del payload["lease_id"]
    elif corruption == "unknown_lease_state":
        payload["state"] = "future_state"
    elif corruption == "invalid_expires_at":
        payload["expires_at"] = "not-an-iso-datetime"
    elif corruption == "invalid_duration":
        payload["skew"] = "not-an-iso-duration"
    else:
        payload["allocations"][0]["contradiction_proof"] = {"type": "future_proof"}

    row[b"state"] = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    assert json.loads(row[b"state"]) == payload
    commits_before = table.commit_attempts

    with pytest.raises(SpendLeaseLedgerError) as raised:
        ledger.get("spend-lease-test", region=REGION)

    assert not isinstance(raised.value, (KeyError, ValueError, TypeError))
    assert table.commit_attempts == commits_before


def test_get_treats_incomplete_bigtable_row_as_no_lease() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)
    row = next(iter(table.rows.values()))
    assert json.loads(row[b"state"])["lease_id"] == "spend-lease-test"
    del row[b"version"]
    commits_before = table.commit_attempts

    with pytest.raises(SpendLeaseLedgerError) as decoder_error:
        ledger_module._state_and_version(_ReadRow(row))

    with pytest.raises(SpendLeaseLedgerError) as raised:
        ledger.get("spend-lease-test", region=REGION)

    assert not isinstance(decoder_error.value, (KeyError, ValueError, TypeError))
    assert not isinstance(raised.value, (KeyError, ValueError, TypeError))
    assert table.commit_attempts == commits_before


def test_cas_exhaustion_stops_after_sixteen_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)
    table.force_cas_misses = True
    monkeypatch.setattr(ledger_module, "sleep", lambda _seconds: None)
    commits_before = table.commit_attempts

    with pytest.raises(SpendLeaseCasExhausted):
        _allocate(ledger)

    assert table.commit_attempts - commits_before == 16
    assert ledger.get("spend-lease-test", region=REGION) == _candidate()


def test_unknown_region_raises_typed_error_naming_region() -> None:
    ledger = BigtableSpendLeaseLedger({REGION: _FakeBigtableTable()})

    with pytest.raises(SpendLeaseLedgerError, match="europe-west4"):
        ledger.get("spend-lease-test", region="europe-west4")


def test_canonical_encoding_synthesizes_bound_proof_on_read() -> None:
    table = _FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    ledger.initialize(_candidate(), region=REGION)
    _allocate(ledger)
    proof = BoundProof(
        idempotency_scope="scope-1",
        authorization_id="authorization-1",
        lease_id="spend-lease-test",
        gen=4,
        allocated_micro=500,
    )
    bound = ledger.bind(
        "spend-lease-test",
        region=REGION,
        expected_provisional_id="provisional-1",
        proof=proof,
    )

    # A read reconstructs the InitVar proof from the committed allocation's
    # stored binding fields, so a later proof-requiring transition succeeds.
    assert ledger.get("spend-lease-test", region=REGION) == bound.lease
    mirrored = ledger.mirror(
        "spend-lease-test",
        region=REGION,
        observation=AuthorizationObservation(
            idempotency_scope="scope-1",
            authorization_id="authorization-1",
            request_fingerprint="fingerprint-1",
            lease_id="spend-lease-test",
            gen=4,
            allocated_micro=500,
            key_hash="key-hash",
            workspace_id="workspace-test",
            durability=AuthorizationDurability.TERMINAL,
            finalization_outcome=FinalizationOutcome.SETTLED,
            finalized_cost_microdollars=275,
        ),
    )

    assert mirrored.allocation.state == AllocationState.SETTLED
    stored_state = next(iter(table.rows.values()))[b"state"]
    payload = json.loads(stored_state)
    assert payload["skew"] == "PT10S"
    assert stored_state == json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def test_spend_lease_bigtable_settings_parse_fixed_profiles() -> None:
    settings = Settings(
        environment="test",
        spend_lease_bigtable_app_profiles=(
            "us-central1=spend-us,europe-west4=spend-eu"
        ),
    )

    assert settings.spend_lease_bigtable_table == "trustedrouter-spend-lease"
    assert settings.spend_lease_bigtable_app_profile_map == {
        "us-central1": "spend-us",
        "europe-west4": "spend-eu",
    }


@pytest.mark.parametrize(
    "value",
    ["missing-separator", "=profile", "region=", "region=a,region=b"],
)
def test_spend_lease_profile_map_rejects_ambiguous_entries(value: str) -> None:
    with pytest.raises(ValueError, match="TR_SPEND_LEASE_BIGTABLE_APP_PROFILES"):
        _ = Settings(
            environment="test",
            spend_lease_bigtable_app_profiles=value,
        ).spend_lease_bigtable_app_profile_map
