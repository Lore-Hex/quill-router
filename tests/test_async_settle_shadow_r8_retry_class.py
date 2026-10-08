"""R8 witness and retry backing by original class, including real adapter outcomes."""

import copy
import datetime as dt
import hashlib
import json
import time

import pytest

from scripts.async_settle.shadow_report import report, validate_counter
from tests.test_async_settle_shadow import FIXTURE, NOW, context, signer, wire
from tests.test_async_settle_shadow_accounting import Database, synthetic_window
from tests.test_async_settle_shadow_r7_retry_original import add_retry
from tests.test_async_settle_shadow_real_exclusion import real_exclusion_window
from trusted_router.async_settle_shadow_compare import Booking, compare
from trusted_router.async_settle_shadow_evidence import (
    COUNTER,
    SAMPLE,
    retry_classification,
    sample,
)
from trusted_router.detached_jws import canonical
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore

ORIGINAL_CLASSES = (
    "verified-compatible", "verified-different", "null-hash", "other-kind",
    "other-region", "retired", "other-stream", "other-adapter", "other-route", "expired",
)


def observation(phase, *, charge=None, failure=False, rebuild=True):
    value = copy.deepcopy(FIXTURE)
    amount = 2 if phase == "settle" else 0
    value["terminal"].update(terminal_kind=phase, charge_micro=amount if charge is None else charge)
    value["payload_hash"] = hashlib.sha256(canonical(value["terminal"])).hexdigest()
    if failure:
        value["observed"]["service_tier"] = "unsupported"
        value.update(terminal=None, payload_hash=None, go_error="unsupported_observed")
    changes = {}
    if not rebuild:
        del value["billing_snapshot"]
        changes["rebuild"] = None
    ctx = context(attempted_kind=phase,
        booking=Booking(amount, "settled" if phase == "settle" else "refunded", True), **changes)
    compared = compare(wire(value), ctx, [signer().trusted])
    return sample(ctx, compared, observed_us=NOW * 1000000, router_us=1,
        comparator_us=1, booking_us=1, instance="00000000-0000-0000-0000-000000000001",
        revision="a" * 40)


def test_exact_retry_cannot_borrow_an_excluded_original(monkeypatch):
    """Reviewer's actual-worker null-hash refund witness, without /tmp side effects."""
    rows, days, proof, _, _ = real_exclusion_window(monkeypatch, phase="refund")
    assert report(rows, days, proof)["status"] == "PASS"
    originals = [r["body"] for r in rows if r["kind"] == SAMPLE
        and r["body"]["booking"]["attempted_kind"] == "refund"]
    assert len(originals) == 1
    original = originals[0]
    assert original["payload_hash"] is None
    assert original["classification"] == "unevaluable"
    exact = observation("refund")
    assert exact["classification"] == "exact"
    db = Database()
    identity = original["authorization_day"] + "/" + original["authorization_id"]
    db.rows[SAMPLE, identity] = json.dumps(original)
    assert EvidenceStore(db).insert_sample(identity, exact, time.monotonic() + 1) == "conflict"
    counter = add_retry(rows, "refund", day_index=0)
    validate_counter(counter["id"], counter["body"], partitions=True)
    result = report(rows, days, proof)
    assert result["status"] == "BLOCKED"
    assert any(g.endswith(":refund:retry_original_gap") for g in result["gaps"])
    # Truthfully recording the conflict also blocks, with a correctness reset.
    counter["body"]["duplicate_samples"] -= 1
    counter["body"]["conflicting_samples"] += 1
    terminal = next(b for b in counter["body"]["terminal_counts"] if b["phase"] == "refund" and b["duplicate_samples"])
    terminal["duplicate_samples"] -= 1
    terminal["conflicting_samples"] += 1
    result = report(rows, days, proof)
    assert result["status"] == "BLOCKED" and result["resets"]


def original_for(phase, original_class):
    original = observation("refund" if phase == "settle" else "settle") if original_class == "other-kind" else observation(
        phase, charge=999 if original_class == "verified-different" else None,
        failure=original_class == "null-hash")
    if original_class == "null-hash":
        original["streamed"] = False  # Isolate missing hash from bucket incompatibility.
    if original_class == "other-region":
        original["deployment"]["region"] = "other-region"
    if original_class == "other-stream":
        original["streamed"] = True
    if original_class == "other-adapter":
        original["adapter"] = "anthropic"
    if original_class == "other-route":
        original["route_type"] = "responses"
    return original


@pytest.mark.parametrize("phase", ["settle", "refund"])
@pytest.mark.parametrize("original_class", ORIGINAL_CLASSES)
def test_adapter_retry_original_class(phase, original_class):
    original, retry = original_for(phase, original_class), observation(phase)
    if original_class == "expired":
        retry["observed_at_us"] += 3 * 86400_000000
    expected = {"verified-compatible": "duplicate", "verified-different": "conflict",
        "null-hash": "conflict", "other-kind": "winner_polarity", "other-region": "conflict",
        "retired": "duplicate", "other-stream": "conflict", "other-adapter": "conflict",
        "other-route": "conflict", "expired": "proof_expired"}[original_class]
    # Retention is a separate report/write boundary, not payload classification.
    if original_class == "retired":
        for row in (original, retry):
            row["authorization_day"] = "2026-09-01"
            row["authorize_at_us"] = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp() * 1e6)
    identity = original["authorization_day"] + "/" + original["authorization_id"]
    db = Database()
    db.rows[SAMPLE, identity] = json.dumps(original)
    assert retry_classification(original, retry) == expected
    if expected == "proof_expired":
        with pytest.raises(ValueError, match="^proof_expired$"):
            EvidenceStore(db).insert_sample(identity, retry, time.monotonic() + 1)
    else:
        assert EvidenceStore(db).insert_sample(identity, retry, time.monotonic() + 1) == expected
    assert json.loads(db.rows[SAMPLE, identity]) == original


@pytest.mark.parametrize("phase", ["settle", "refund"])
@pytest.mark.parametrize("outcome", ["duplicate_samples", "conflicting_samples"])
@pytest.mark.parametrize("delta", [-1, 1])
@pytest.mark.parametrize("original_class", ORIGINAL_CLASSES)
def test_retry_damage_original_class(phase, outcome, delta, original_class):
    rows, days, proof = synthetic_window()
    # Preserve a positive seed of the opposite kind; it cannot back this retry.
    if phase == "settle":
        for row in rows:
            if row["kind"] == SAMPLE:
                row["body"]["booking"].update(attempted_kind="refund", outcome="refunded")
            elif row["kind"] == COUNTER:
                for bucket in row["body"]["counts"]:
                    bucket["refund_attempts"], bucket["settle_attempts"] = bucket["settle_attempts"], 0
                for bucket in row["body"]["terminal_counts"]:
                    bucket["phase"] = "refund"
    original = original_for(phase, original_class)
    prior = "2026-09-01" if original_class == "retired" else "2026-10-03" if original_class == "expired" else "2026-10-05"
    original.update(authorization_day=prior, authorization_id="prior-original",
        authorize_at_us=int(dt.datetime.fromisoformat(prior).replace(tzinfo=dt.UTC).timestamp() * 1e6),
        observed_at_us=(NOW - (3 if original_class == "expired" else 1) * 86400) * 1000000)
    rows.append(dict(kind=SAMPLE, id=prior + "/prior-original", body=original))
    mismatch = original_class == "verified-different"
    assert report(rows, days, proof)["status"] == ("BLOCKED" if mismatch else "PASS")
    counter = add_retry(rows, phase, outcome, delta, day_index=0)
    if delta < 0:
        with pytest.raises(ValueError, match="counter integer"):
            report(rows, days, proof)
        return
    validate_counter(counter["id"], counter["body"], partitions=True)
    result = report(rows, days, proof)
    # The report lacks the discarded retry hash. Either verified non-null
    # original can back a possible identical retry or a different-hash conflict;
    # the pairwise adapter test above checks the actual hash relationship.
    backed = original_class in {"verified-compatible", "verified-different"}
    assert any(g.endswith(":" + phase + ":retry_original_gap") for g in result["gaps"]) is not backed
    assert result["status"] == ("PASS" if backed and not mismatch and outcome == "duplicate_samples" else "BLOCKED")
    assert bool(result["resets"]) is (mismatch or outcome == "conflicting_samples")


@pytest.mark.parametrize("phase", ["settle", "refund"])
@pytest.mark.parametrize("damage", ["both-null", "original-unverified", "retry-unverified", "diagnostic-change"])
def test_retry_requires_verified_hash_not_diagnostic_equality(phase, damage):
    original, retry = observation(phase), observation(phase)
    if damage == "both-null":
        original = observation(phase, failure=True)
        retry = copy.deepcopy(original)
    elif damage == "diagnostic-change":
        retry = observation(phase, rebuild=False)
    else:
        row = original if damage == "original-unverified" else retry
        row["classification"] = "unevaluable"
        row["provenance"]["binding_verified"] = False
    identity = original["authorization_day"] + "/" + original["authorization_id"]
    db = Database()
    db.rows[SAMPLE, identity] = json.dumps(original)
    expected = "duplicate" if damage == "diagnostic-change" else "conflict"
    assert EvidenceStore(db).insert_sample(identity, retry, time.monotonic() + 1) == expected
    assert json.loads(db.rows[SAMPLE, identity]) == original
