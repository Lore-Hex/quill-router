import pytest

from scripts.async_settle.shadow_report import report, validate_counter
from tests.test_async_settle_shadow_real_exclusion import real_exclusion_window
from trusted_router.async_settle_shadow_evidence import PHASE_FIELDS


@pytest.mark.parametrize("moved_phase", ["refund", "worker", "authorize"])
def test_refund_exclusions_require_refund_attempts(monkeypatch, moved_phase):
    rows, days, proof, _, _ = real_exclusion_window(monkeypatch)
    before = report(rows, days, proof)
    assert before["status"] == "PASS"
    counter = rows[0]["body"]
    exclusion = next(x for x in counter["exclusions"] if x["phase"] == "settle")
    bucket = next(
        b
        for b in counter["counts"]
        if all(b[k] == exclusion[k] for k in ("adapter", "route_type", "streamed"))
    )
    assert bucket["refund_attempts"] == 0
    exclusion["phase"] = moved_phase
    validate_counter(rows[0]["id"], counter, partitions=True)
    result = report(rows, days, proof)
    print(
        "refund_attempts",
        bucket["refund_attempts"],
        "refund_exclusion",
        exclusion,
        "status",
        result["status"],
        "seconds",
        result["continuous_seconds"],
        "gaps",
        result["gaps"],
    )
    assert result["status"] == "BLOCKED", (
        "refund exclusion with zero refund attempts must block coverage"
    )




@pytest.mark.parametrize("phase", ["settle", "refund"])
@pytest.mark.parametrize("field", [*PHASE_FIELDS, "exclusion", "rejection", "drop"])
@pytest.mark.parametrize("delta", [-1, 1])
def test_phase_counter_damage_blocks(monkeypatch, phase, field, delta):
    rows, days, proof, _, _ = real_exclusion_window(monkeypatch, phase)
    assert report(rows, days, proof)["status"] == "PASS"
    counter = rows[0]["body"]
    bucket = next(row for row in counter["terminal_counts"] if row["phase"] == phase)
    if field == "exclusion":
        counter["exclusions"][0]["count"] += delta
    elif field in {"rejection", "drop"}:
        counter[field+"s"].append(dict(phase=phase, adapter=bucket["adapter"],
            route_type=bucket["route_type"], streamed=bucket["streamed"],
            reason="base64" if field == "rejection" else "queue_full", count=delta))
    else:
        bucket[field] += delta
    try:
        status = report(rows, days, proof)["status"]
    except ValueError:
        status = "BLOCKED"
    assert status == "BLOCKED", (phase, field, delta)


@pytest.mark.parametrize("phase", ["settle", "refund"])
def test_durable_exclusion_phase_cannot_borrow_opposite_attempt(monkeypatch, phase):
    rows, days, proof, _, _ = real_exclusion_window(monkeypatch, phase)
    sample = rows[-1]["body"]
    sample["booking"]["attempted_kind"] = "refund" if phase == "settle" else "settle"
    # Keep valid sample schema while moving just the attempted phase.
    result = report(rows, days, proof)
    assert result["status"] == "BLOCKED"
    assert any(g.endswith(":sample_phase_gap") for g in result["gaps"])


def test_deferred_terminal_drops_keep_phase():
    from tests.test_async_settle_shadow import NOW
    from trusted_router.async_settle_shadow_evidence import Counters

    counters = Counters('us-central1', 'a'*40, clock=lambda: NOW)
    counters.defer('drop', NOW, 'settle')
    counters.defer('drop', NOW, 'refund')
    row = counters.snapshot()[0][1]
    assert [(reason['phase'], reason['reason'], reason['count']) for reason in row['drops']] == [
        ('settle', 'queue_full', 1), ('refund', 'queue_full', 1)]
    assert row['first_gap_at_us'] == NOW * 1000000
