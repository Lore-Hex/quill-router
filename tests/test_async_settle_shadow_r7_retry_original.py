"""Balanced retry accounting still requires a possible retained original."""

import copy
import datetime as dt

import pytest

from scripts.async_settle.shadow_report import report, validate_counter
from tests.test_async_settle_shadow_real_exclusion import real_exclusion_window
from trusted_router.async_settle_shadow_evidence import COUNTER, PHASE_FIELDS, SAMPLE


def add_retry(rows, phase="refund", outcome="duplicate_samples", delta=1, day_index=-1):
    counter_row = [r for r in rows if r["kind"] == COUNTER][day_index]
    counter = counter_row["body"]
    bucket = next(
        b
        for b in counter["counts"]
        if (b["adapter"], b["route_type"], b["streamed"]) == ("openai", "chat.completions", False)
    )
    terminal = next(
        (
            b
            for b in counter["terminal_counts"]
            if b["phase"] == phase
            and all(b[k] == bucket[k] for k in ("adapter", "route_type", "streamed"))
        ),
        None,
    )
    if terminal is None:
        terminal = dict(
            phase=phase,
            **{k: bucket[k] for k in ("adapter", "route_type", "streamed")},
            **dict.fromkeys(PHASE_FIELDS, 0),
        )
        counter["terminal_counts"].append(terminal)
    for field in (
        "envelope_present",
        "observed_attempts",
        "observed_eligible",
        "evaluable",
        "exact",
    ):
        bucket[field] += delta
        terminal[field] += delta
    bucket[phase + "_attempts"] += delta
    for field in ("comparison_attempts", outcome):
        counter[field] += delta
        terminal[field] += delta
    counter["admission_observer"]["prediction_yes"] += delta
    return counter_row


def test_last_day_duplicate_refund_requires_original(monkeypatch):
    rows, days, proof, _, _ = real_exclusion_window(monkeypatch)
    assert report(rows, days, proof)["status"] == "PASS"
    assert all(
        r["body"]["booking"]["attempted_kind"] == "settle" for r in rows if r["kind"] == SAMPLE
    )
    counter = add_retry(rows)
    validate_counter(counter["id"], counter["body"], partitions=True)
    result = report(rows, days, proof)
    assert result["status"] == "BLOCKED"
    assert any(g.endswith(":refund:retry_original_gap") for g in result["gaps"])


@pytest.mark.parametrize("phase", ["settle", "refund"])
@pytest.mark.parametrize("outcome", ["duplicate_samples", "conflicting_samples"])
@pytest.mark.parametrize("delta", [-1, 1])
def test_balanced_retry_damage_without_same_kind_original(monkeypatch, phase, outcome, delta):
    rows, days, proof, _, _ = real_exclusion_window(monkeypatch)
    # All original observations belong to the opposite phase. Match their
    # existing counters too, so the positive-delta case has no integer gap.
    if phase == "settle":
        for row in rows:
            if row["kind"] == SAMPLE:
                row["body"]["booking"].update(attempted_kind="refund", outcome="refunded")
            elif row["kind"] == COUNTER:
                for bucket in row["body"]["counts"]:
                    bucket["refund_attempts"], bucket["settle_attempts"] = (
                        bucket["settle_attempts"],
                        0,
                    )
                for bucket in row["body"]["terminal_counts"]:
                    bucket["phase"] = "refund"
                for reason in row["body"]["exclusions"]:
                    reason["phase"] = "refund"
    assert report(rows, days, proof)["status"] == "PASS"
    counter = add_retry(rows, phase, outcome, delta)
    if delta < 0:
        with pytest.raises(ValueError, match="counter integer"):
            report(rows, days, proof)
        return
    validate_counter(counter["id"], counter["body"], partitions=True)
    result = report(rows, days, proof)
    assert result["status"] == "BLOCKED"
    assert any(g.endswith(":" + phase + ":retry_original_gap") for g in result["gaps"])


@pytest.mark.parametrize(
    "placement",
    ["same_writer", "other_day_writer", "lookback", "future", "retired", "other_region"],
)
def test_retry_original_scope(monkeypatch, placement):
    rows, days, proof, _, _ = real_exclusion_window(monkeypatch)
    add_retry(rows, "settle", day_index=0 if placement == "same_writer" else -1)
    if placement in {"lookback", "future", "retired", "other_region"}:
        # Keep the clean seed and its insertion counters unchanged. Give a
        # refund retry a separate original outside the requested metric window.
        rows, days, proof, _, _ = real_exclusion_window(monkeypatch)
        add_retry(rows, "refund")
        original = copy.deepcopy(next(r for r in rows if r["kind"] == SAMPLE))
        original["body"]["booking"].update(attempted_kind="refund", outcome="refunded")
        original["body"]["authorization_id"] = "prior-refund"
        prior = "2026-10-05" if placement != "retired" else "2026-09-01"
        original["id"] = prior + "/prior-refund"
        original["body"]["authorization_day"] = prior
        original["body"]["authorize_at_us"] = int(
            dt.datetime.fromisoformat(prior).replace(tzinfo=dt.UTC).timestamp() * 1e6
        )
        original["body"]["observed_at_us"] -= 86400_000000
        if placement == "future":
            original["body"]["observed_at_us"] += 10 * 86400_000000
        if placement == "other_region":
            original["body"]["deployment"]["region"] = "other-region"
        rows.append(original)
    result = report(rows, days, proof)
    assert result["status"] == (
        "BLOCKED" if placement in {"future", "retired", "other_region"} else "PASS"
    )
