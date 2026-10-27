"""Transaction outcomes and bounded publisher admission through the real owner."""
from __future__ import annotations

import pytest
from google.api_core.exceptions import Aborted, DeadlineExceeded, ServiceUnavailable

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_health_watchdog import accounting, failed, owner
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    sdk_database,  # noqa: F401
)
from tests.test_async_settle_shadow_observation_r7 import entries
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.services import async_settle_shadow_health as shell
from trusted_router.storage_gcp_shadow_observation import ObservationStore


def native_owner(native):
    db, clock, sleeps = native
    svc, wall, _, job = owner()
    clock.now = job.earliest_start_mono
    svc.store = ObservationStore(db, clock=clock.monotonic, wall=lambda: wall[0]/1e6)
    db.commit_duration = .02
    # These tests isolate the incumbent read-write transaction outcome.
    svc.incumbent_slot = job.slot-1
    return svc, job, db, clock, sleeps


def outcome(svc, job, expected, unknown=False):
    trace = next(e for e in entries(svc) if e["slot"] == job.slot)
    assert (trace["outcome"], trace["unknown_outcome"]) == (expected, unknown)
    assert svc.state.job is None
    accounting(svc)
    counts = svc.counters[trace["day"]]["publisher"]
    assert counts["scheduled_slots"] == counts["missed_slots"] + counts["claim_attempts"]
    assert counts["claim_attempts"] == counts["lost_claims"] + counts["publications"] + counts["failures"]
    return trace


def test_aborted_claim_commit_is_lost(sdk_database):  # noqa: F811
    svc, job, db, _, sleeps = native_owner(sdk_database)
    db.errors = [Aborted("claim aborted")]
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "lost_claim")
    assert trace["rpc_count"] == 2
    assert [name for name, _ in db.calls] == ["read", "commit"]
    assert not sleeps


@pytest.mark.parametrize("streaming", [False, True])
def test_aborted_claim_read_is_lost_without_timestamp(sdk_database, monkeypatch, streaming):  # noqa: F811
    db, _, _ = sdk_database
    # The actual bounded wrapper sees both call and stream errors.
    original = db.spanner_api.execute_streaming_sql
    def aborted(**kwargs):
        if not streaming:
            raise Aborted("claim read aborted")
        def consume():
            yield from original(**kwargs)
            raise Aborted("claim stream aborted")
        return consume()
    monkeypatch.setattr(db.spanner_api, "execute_streaming_sql", aborted)
    svc, job, db, _, sleeps = native_owner(sdk_database)
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "lost_claim")
    assert trace["claim_at_ns"] is None
    assert not any(name == "commit" for name, _ in db.calls)
    assert not sleeps


@pytest.mark.parametrize("rollback_error", [DeadlineExceeded, ServiceUnavailable])
def test_aborted_claim_stream_survives_rollback_failure(sdk_database, monkeypatch, rollback_error):  # noqa: F811
    db, _, _ = sdk_database
    original = db.spanner_api.execute_streaming_sql
    def aborted(**kwargs):
        def consume():
            yield from original(**kwargs)  # Give the SDK the transaction ID for rollback.
            raise Aborted("claim stream aborted")
        return consume()
    monkeypatch.setattr(db.spanner_api, "execute_streaming_sql", aborted)
    db.rollback_errors = [rollback_error("rollback failed")]
    svc, job, db, _, sleeps = native_owner(sdk_database)
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "lost_claim")
    assert trace["claim_at_ns"] is None
    assert trace["cycle_us"] == 210_000
    assert trace["rpc_count"] == 2
    assert [name for name, _ in db.calls] == ["read", "rollback"]
    assert not db.rollback_errors and not sleeps
    counts = svc.counters[m.day_name(NOW_US)]["publisher"]
    assert counts["claim_attempts"] == counts["lost_claims"] == 1
    assert counts["failures"] == counts["unknown_outcomes"] == counts["publications"] == 0


def test_claim_commit_deadline_without_abort_stays_unknown(sdk_database):  # noqa: F811
    svc, job, db, _, sleeps = native_owner(sdk_database)
    db.errors = [DeadlineExceeded("claim commit reply deadline")]
    svc.submit_publisher(job.slot, NOW_US)
    trace = outcome(svc, job, "failed", unknown=True)
    assert trace["claim_at_ns"] is not None
    assert trace["cycle_us"] == 30_000
    assert trace["rpc_count"] == 2
    assert [name for name, _ in db.calls] == ["read", "commit"]
    assert not db.errors and not sleeps
    counts = svc.counters[m.day_name(NOW_US)]["publisher"]
    assert counts["claim_attempts"] == counts["failures"] == counts["unknown_outcomes"] == 1
    assert counts["lost_claims"] == counts["publications"] == 0


@pytest.mark.parametrize("stage", ["commit", "read"])
def test_sdk_reentry_is_lost_without_second_attempt(sdk_database, monkeypatch, stage):  # noqa: F811
    from google.cloud.spanner_v1 import session
    svc, job, db, _, sleeps = native_owner(sdk_database)
    reentries = []
    # Exercise the installed Session retry branch when ABORTED reaches it,
    # bypassing only the transport's conversion and SDK backoff sleep.
    def abort(**kwargs):
        raise Aborted("SDK abort", errors=[object()])
    monkeypatch.setattr(db.spanner_api, "commit" if stage == "commit" else "execute_streaming_sql", abort)
    monkeypatch.setattr(session, "_delay_until_retry", lambda *a, **kw: reentries.append(1))
    svc.submit_publisher(job.slot, NOW_US)
    outcome(svc, job, "lost_claim")
    assert reentries == [1] and not sleeps
    assert sum(name == "read" for name, _ in db.calls) == int(stage == "commit")


@pytest.mark.parametrize("stage", ["claim", "publish"])
def test_direct_aborted_transaction_is_known(monkeypatch, stage):
    svc, wall, _, job = owner()
    db = svc.store.database
    commit = db._try_commit
    commits = []
    def aborted(tx):
        commits.append(tx)
        if len(commits) == (1 if stage == "claim" else 2):
            raise Aborted("transaction aborted")
        return commit(tx)
    monkeypatch.setattr(db, "_try_commit", aborted)
    svc.submit_publisher(job.slot, wall[0])
    outcome(svc, job, "lost_claim" if stage == "claim" else "failed")
    assert len(commits) == (1 if stage == "claim" else 2)
    assert db.commits == int(stage == "publish")


def test_aborted_claim_then_commit_deadline_stays_unknown(sdk_database):  # noqa: F811
    svc, job, db, clock, sleeps = native_owner(sdk_database)
    db.errors = [Aborted("first claim aborted")]
    svc.submit_publisher(job.slot, NOW_US)
    outcome(svc, job, "lost_claim")
    # A later cycle's commit deadline has a different, unknown outcome.
    stamp = NOW_US + m.SLOT_US
    clock.now = 102.
    svc.store.wall = lambda: stamp/1e6
    db.server_ns = stamp*1000
    db.errors = [DeadlineExceeded("commit reply deadline")]
    svc.tick(stamp)
    next_job = svc.state.job
    assert next_job is not None
    svc.incumbent_slot = next_job.slot-1
    svc.submit_publisher(next_job.slot, stamp)
    outcome(svc, next_job, "failed", unknown=True)
    counts = svc.counters[m.day_name(NOW_US)]["publisher"]
    assert counts["claim_attempts"] == 2
    assert counts["lost_claims"] == counts["failures"] == counts["unknown_outcomes"] == 1
    assert [name for name, _ in db.calls] == ["read", "commit", "read", "commit"]
    assert not sleeps


def test_admission_lock_wait_is_bounded_and_releases(monkeypatch):
    svc, wall, mono, job = owner()
    lock = svc.admission_lock
    waits = []
    class Held:
        def __enter__(self):
            raise AssertionError("unbounded admission wait")
        def acquire(self, *, timeout):
            waits.append(timeout)
            mono[0] += timeout
            wall[0] += round(timeout*1e6)
            return False
        def release(self):
            raise AssertionError("unowned admission lock released")
    monkeypatch.setattr(svc, "admission_lock", Held())
    reply = shell.publish_cycle(svc.store, svc.journal.boot, svc.settings.effective_serving_region,
                               svc.settings.source_revision, job, NOW_US, svc)
    monkeypatch.setattr(svc, "admission_lock", lock)
    svc.publisher_reply(reply)
    assert waits == [.5]
    outcome(svc, job, "missed")
    assert not svc.store.database.commits
    wall[0], mono[0] = NOW_US+m.SLOT_US, 102.
    svc.store.database.server_ns = wall[0]*1000
    svc.publish_cycle(wall[0]//m.SLOT_US, wall[0])
    assert entries(svc)[-1]["outcome"] == "published"
    accounting(svc)


def test_released_vetoed_day_enumerates_remaining_slots():
    # Three opportunities remain in this day, including the outstanding job.
    stamp = NOW_US + m.DAY_US - 3*m.SLOT_US
    svc, wall, mono, job = owner(stamp)
    day = m.day_name(stamp)
    svc.accept(m.ObserverEnqueued(stamp, "read"))
    svc.accept(m.ObserverFolded(stamp, day, "read", success=False))
    svc.accept(m.PublisherFailed(stamp, job.slot, job.generation, "publisher_watchdog"))
    failed(svc, job)
    trace = entries(svc)[0]
    assert (trace["cycle_us"], trace["rpc_count"], trace["unknown_outcome"], trace["claim_at_ns"]) == (0, 0, True, None)
    wall[0], mono[0] = NOW_US+m.DAY_US, 106.
    svc.tick(wall[0])
    old = next(d for d in svc.state.days if d.day == day)
    assert old.veto and old.cursor == old.last_slot
    assert old.scheduled == 3 and old.missed == 2 and old.attempted == old.failed == 1
    assert [e["outcome"] for e in entries(svc) if e["day"] == day] == ["failed", "missed", "missed"]
    accounting(svc)


@pytest.mark.parametrize("gap", [0, 1])
def test_losing_claim_rolls_back_without_empty_commit(gap):
    from trusted_router.storage_gcp_shadow_observation import Budget, ContentionLoss
    svc, wall, _, job = owner()
    db = svc.store.database
    db.server_ns = (job.slot-gap)*m.SLOT_US*1000
    assert svc.store.claim("incumbent", job.slot-gap, wall[0], Budget(101., svc.store.clock))
    db.server_ns = wall[0]*1000
    commits = db.commits
    with pytest.raises(ContentionLoss, match="slot_already_claimed"):
        svc.store.claim("follower", job.slot, wall[0], Budget(101., svc.store.clock))
    assert db.commits == commits


def test_one_aborted_cycle_keeps_recent_accepted_incumbent(sdk_database):  # noqa: F811
    svc, job, db, _, _ = native_owner(sdk_database)
    db.errors = [Aborted("claim aborted")]
    svc.submit_publisher(job.slot, NOW_US)
    outcome(svc, job, "lost_claim")
    assert svc.is_incumbent(job.slot+1)
    assert svc.is_incumbent(job.slot+2)  # Third slot since the accepted publication.
    assert not svc.is_incumbent(job.slot+3)


@pytest.mark.parametrize("rollback_error", [None, DeadlineExceeded, ServiceUnavailable])
def test_claim_already_taken_uses_sdk_rollback_not_commit(sdk_database, monkeypatch, rollback_error):  # noqa: F811
    import json

    from tests.test_async_settle_shadow_health_stagger import claim
    svc, job, db, _, sleeps = native_owner(sdk_database)
    original = db.spanner_api.execute_streaming_sql
    def claimed(**kwargs):
        for response in original(**kwargs):
            response._pb.values[0].string_value = json.dumps(claim("other", job.slot))
            yield response
    monkeypatch.setattr(db.spanner_api, "execute_streaming_sql", claimed)
    if rollback_error is not None:
        db.rollback_errors = [rollback_error("rollback response failed")]
    svc.submit_publisher(job.slot, NOW_US)
    outcome(svc, job, "lost_claim")
    assert [name for name, _ in db.calls] == ["read", "rollback"]
    assert not sleeps
