import pytest

from scripts.async_settle.shadow_report import report, validate_counter
from tests.test_async_settle_shadow_accounting import synthetic_window
from trusted_router.async_settle_shadow_evidence import COUNT_FIELDS


@pytest.mark.parametrize(
    "damage", ["unknown_relabel", "phantom_comparison", "contradictory_sample"]
)
def test_contradictory_counter_evidence_cannot_pass(damage):
    rows, days, proof = synthetic_window()
    assert report(rows, days, proof)["status"] == "PASS"
    counter = rows[2]["body"]
    bucket = counter["counts"][0]
    bucket.update(settle_attempts=99, observed_attempts=99, observed_unknown=99)
    assert report(rows, days, proof)["status"] == "BLOCKED"
    bucket.update(observed_unknown=0, observed_ineligible=99)
    counter["exclusions"] = [
        dict(
            phase="settle",
            reason="service_tier",
            count=99,
            **{k: bucket[k] for k in ("adapter", "route_type", "streamed")},
        )
    ]
    if damage == "phantom_comparison":
        # No compare outcomes, and no persisted sample for this writer; merely
        # call the observations duplicates to satisfy the aggregate equations.
        counter.update(comparison_attempts=99, duplicate_samples=99)
    elif damage == "contradictory_sample":
        rows, days, proof = synthetic_window()
        counter = rows[0]["body"]
        bucket = next(b for b in counter["counts"] if b["exact"])
        bucket.update(observed_eligible=0, observed_ineligible=1)
        counter["exclusions"] = [
            dict(
                phase="settle",
                reason="service_tier",
                count=1,
                **{k: bucket[k] for k in ("adapter", "route_type", "streamed")},
            )
        ]
    for row in rows:
        if row["kind"] == "async_settle_shadow_counter":
            validate_counter(row["id"], row["body"])
    result = report(rows, days, proof)
    print(damage, result["status"], result["continuous_seconds"], result["gaps"])
    assert result["status"] == "BLOCKED", (
        "contradictory coverage evidence passed all aggregate equations"
    )


def test_phantom_comparisons_have_an_explicit_gap():
    rows, days, proof = synthetic_window()
    counter = rows[2]["body"]
    counter.update(comparison_attempts=99, duplicate_samples=99)
    result = report(rows, days, proof)
    assert any(g.endswith(":comparison_outcome_gap") for g in result["gaps"])


@pytest.mark.parametrize(
    "field",
    [
        *COUNT_FIELDS,
        "comparison_attempts",
        "samples_inserted",
        "duplicate_samples",
        "conflicting_samples",
        "comparison_dropped",
        "booking_pending",
        "booking_unknown",
        "exclusion",
    ],
)
@pytest.mark.parametrize("delta", [-1, 1])
def test_each_coverage_counter_damage_blocks(monkeypatch, field, delta):
    from tests.test_async_settle_shadow_real_exclusion import real_exclusion_window

    rows, days, proof, _, _ = real_exclusion_window(monkeypatch)
    counter = rows[0]["body"]
    bucket = next(b for b in counter["counts"] if b["exact"])
    if field == "exclusion":
        counter["exclusions"][0]["count"] += delta
    elif field in counter:
        counter[field] += delta
    else:
        bucket[field] += delta
    try:
        status = report(rows, days, proof)["status"]
    except ValueError:
        # Invalid negative integers/schema are rejected before report rendering.
        status = "BLOCKED"
    assert status == "BLOCKED", (field, delta)


@pytest.mark.parametrize(
    "dimension", ["adapter", "route_type", "streamed", "eligibility", "classification"]
)
def test_durable_samples_cannot_borrow_another_bucket_or_class(dimension):
    rows, days, proof = synthetic_window()
    counter = rows[0]["body"]
    bucket = next(b for b in counter["counts"] if b["exact"])
    if dimension in {"adapter", "route_type", "streamed"}:
        other = next(b for b in counter["counts"] if b[dimension] != bucket[dimension])
        for field in (
            "settle_attempts",
            "envelope_present",
            "observed_attempts",
            "observed_eligible",
            "evaluable",
            "exact",
        ):
            other[field], bucket[field] = bucket[field], 0
    elif dimension == "eligibility":
        bucket.update(
            observed_eligible=0, observed_ineligible=1, exact=0, evaluable=0, unevaluable=1
        )
        counter["exclusions"] = [
            dict(
                phase="settle",
                reason="service_tier",
                count=1,
                **{k: bucket[k] for k in ("adapter", "route_type", "streamed")},
            )
        ]
    else:
        bucket.update(exact=0, explained=1)
    result = report(rows, days, proof)
    assert result["status"] == "BLOCKED"
    assert any(g.endswith(":sample_classification_gap") for g in result["gaps"])
