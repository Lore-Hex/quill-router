from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from google.cloud.bigtable.row_filters import ValueRegexFilter

import trusted_router.spend_lease_ledger as ledger_module
from trusted_router.config import Settings
from trusted_router.spend_lease_ledger import (
    BigtableSpendLeaseLedger,
    SpendLeaseCasExhausted,
    SpendLeaseLedgerError,
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


@dataclass
class _Cell:
    value: bytes


class _ReadRow:
    def __init__(self, values: dict[bytes, bytes]) -> None:
        self.cells = {
            "lease": {qualifier: [_Cell(value)] for qualifier, value in values.items()}
        }


class _FakeConditionalRow:
    def __init__(self, table: _FakeBigtableTable, row_key: bytes, filter_: Any) -> None:
        self.table = table
        self.row_key = row_key
        self.filter = filter_
        self.mutations: list[tuple[bool, bytes, bytes]] = []

    def set_cell(
        self,
        _family: str,
        column: bytes,
        value: bytes,
        *,
        state: bool,
    ) -> None:
        self.mutations.append((state, column, value))

    def commit(self) -> bool:
        self.table.commit_attempts += 1
        current = self.table.rows.get(self.row_key)
        regex_filter = next(
            (
                candidate
                for candidate in self.filter.filters
                if isinstance(candidate, ValueRegexFilter)
            ),
            None,
        )
        if regex_filter is None:
            matched = current is not None and b"version" in current
        else:
            expected = regex_filter.regex.removeprefix(b"^").removesuffix(b"$")
            matched = (
                not self.table.force_cas_misses
                and current is not None
                and re.fullmatch(expected, current.get(b"version", b"")) is not None
            )
        selected = [mutation for mutation in self.mutations if mutation[0] == matched]
        if selected:
            values = self.table.rows.setdefault(self.row_key, {})
            for _state, column, value in selected:
                values[column] = value
        if self.table.raise_after_next_applied_commit and selected:
            self.table.raise_after_next_applied_commit = False
            raise TimeoutError("reply lost after durable apply")
        return matched


class _FakeBigtableTable:
    def __init__(self) -> None:
        self.rows: dict[bytes, dict[bytes, bytes]] = {}
        self.commit_attempts = 0
        self.force_cas_misses = False
        self.raise_after_next_applied_commit = False

    def row(self, row_key: bytes, *, filter_: Any) -> _FakeConditionalRow:
        return _FakeConditionalRow(self, row_key, filter_)

    def read_row(self, row_key: bytes, *, filter_: Any) -> _ReadRow | None:
        del filter_
        values = self.rows.get(row_key)
        return None if values is None else _ReadRow(values)
