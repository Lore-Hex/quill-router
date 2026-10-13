"""Incremental Port differentials and deferred-fold owner witnesses."""
import copy
import random
from dataclasses import replace

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.shadow_observation_port_oracle import FullPort
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    event_for,
    fixture,
    service,
)
from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router.async_settle_shadow_v2_evidence import COUNTS, EVENT
from trusted_router.async_settle_shadow_v2_native import event_bytes
from trusted_router.async_settle_shadow_v2_schema import INT64

DAY = "2026-10-12"


def delta(phase="authorize", n=1, adapter="openai", route="responses", streamed=False):
    v = dict(phase=phase, dimensions=dict(adapter=adapter, route_type=route, streamed=streamed),
             **dict.fromkeys(COUNTS, 0), eligible_demand=0)
    if phase == "authorize":
        v.update(authorize_attempts=n, authorize_fresh=n)
    else:
        v.update({phase+"_attempts": n}, C=n)
    return v


@exhaustive
def test_incremental_fold_matches_full_validation_at_every_checkpoint():
    rng = random.Random(171608)  # noqa: S311
    fast, old = service(), service()
    old.journal.boot = fast.journal.boot
    old.counters = copy.deepcopy(fast.counters)
    old.port = FullPort(old)
    cases = [delta(n=INT64), delta(n=1)]
    # Reset between sequences, preserving overflow, nullable demand, phase,
    # projection and all integer-cell boundary controls.
    for sequence in range(8):
        if sequence:
            fast, old = service(), service()
            old.journal.boot = fast.journal.boot
            old.counters = copy.deepcopy(fast.counters)
            old.port = FullPort(old)
        for i in range(25):
            v = copy.deepcopy(cases[i]) if sequence == 0 and i < 2 else delta(
                rng.choice(("authorize", "settle", "refund")), rng.randrange(8),
                rng.choice((None, "openai", "other")), rng.choice((None, "responses")), rng.choice((None, False, True)))
            if (sequence or i >= 2) and rng.randrange(3) == 0:
                v[rng.choice(COUNTS+["eligible_demand"])] = rng.choice((None, -1, True, INT64, INT64+1, 2))
            if i % 13 == 0 and (sequence or i >= 2):
                v["dimensions"]["adapter"] = "invalid"
            a, b = fast.port.fold_counts(DAY, v), old.port.fold_counts(DAY, v)
            assert a == b, v
            fast.journal.checkpoint(DAY)
            old.journal.checkpoint(DAY)
            assert fast.counter_row(DAY, NOW_US+100_000_000, closed=False).body == old.counter_row(DAY, NOW_US+100_000_000, closed=False).body


def outcome(function, value):
    try:
        return function(value)
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        return None


@exhaustive
def test_native_event_matches_old_schema_and_port_decisions():
    rng = random.Random(1716083)  # noqa: S311
    events = [v["object"] for v in fixture("receipt_pages") if v["schema"] in ("AUTHORIZE_EVENT", "TERMINAL_EVENT")]
    for v in events:
        assert outcome(event_bytes, v) == outcome(EVENT.serialize, v)
    for _ in range(500):
        v = copy.deepcopy(rng.choice(events))
        fields = [v, *[x for x in v.values() if isinstance(x, dict)]]
        target = rng.choice(fields)
        target[rng.choice(list(target))] = rng.choice((None, True, -1, INT64+1, {}, [], "x", "a\\b", "é", "\ud800"))
        assert outcome(event_bytes, v) == outcome(EVENT.serialize, v), v
    for mutate in (lambda v: None, lambda v: v.update(seq="00000000000000000002"),
                   lambda v: v.update(event_at_us=NOW_US+1), lambda v: v["receipt_locator"].update(boot="other")):
        fast, old = service(), service()
        old.journal.boot = fast.journal.boot
        old.port = FullPort(old)
        for svc in (fast, old):
            svc.journal.grant(DAY, 100)
        r, o = fast.port.reserve(NOW_US), old.port.reserve(NOW_US)
        v = event_for(r)
        mutate(v)
        assert fast.port.finalize(r, v) == old.port.finalize(o, v)
        assert fast.port.lost.is_set() == old.port.lost.is_set()
        assert fast.port.finalize(r, v) == old.port.finalize(o, v)


@pytest.mark.parametrize("success", [False, True])
def test_model_deferred_fold_owner_blocks_final_and_failed_fold_vetoes(success):
    state, _ = m.step(m.State(publisher_enabled=False), m.Tick(NOW_US))
    state, _ = m.step(state, m.ObserverEnqueued(NOW_US, "fold:1", DAY))
    state, _ = m.step(state, m.ShutdownRequested(NOW_US))
    state, effects = m.step(state, m.FlushReply(NOW_US, DAY, True, True))
    assert state.days[0].observers == {"fold:1"}
    assert not state.days[0].final and not any(isinstance(e, m.StartFinalClose) for e in effects)
    state, effects = m.step(state, m.ObserverFolded(NOW_US, DAY, "fold:1", success))
    if success:
        state, effects = m.step(state, m.FlushReply(NOW_US, DAY, True, True))
    assert state.days[0].final == success and state.days[0].veto == (not success)
    assert m.final_barrier(state) and m.no_post_final_owners(state)


@pytest.mark.parametrize("resolution", ["fold", "fail", "invalid"])
def test_port_deferred_fold_survives_checkpoint_and_worker_resolution(resolution):
    from concurrent.futures import ThreadPoolExecutor
    svc = service()
    svc.journal.grant(DAY, 100)
    r = svc.port.reserve(NOW_US)
    assert svc.port.finalize(r, event_for(r), fold_owed=True)
    assert svc.port.complete(r)
    svc.closing_reason = "shutdown"
    svc.termination_deadline = svc.store.clock()+9
    svc.flush(svc.store.clock()+4)
    assert not svc.state.days[0].final and svc.state.days[0].observers
    assert svc.journal.days[DAY].checkpoint_seq >= 1
    assert not svc.counter_row(DAY, NOW_US+100_000_000, closed=False).value()["closed"]
    with ThreadPoolExecutor(max_workers=1) as worker:
        if resolution == "fail":
            assert worker.submit(svc.port.fail_fold, r).result()
        else:
            v = delta()
            if resolution == "invalid":
                v["authorize_fresh"] = -1
            assert worker.submit(svc.port.fold_counts, DAY, v, obligation=r).result() == (resolution == "fold")
    svc.flush(svc.store.clock()+4)
    if resolution == "fold":
        assert not svc.journal.days
    else:
        assert svc.port.lost.is_set() and svc.state.days[0].veto
        assert not svc.state.days[0].final


def test_deferred_fold_origin_day_and_nonreused_owner():
    state, _ = m.step(m.State(publisher_enabled=False), m.Tick(NOW_US))
    state, _ = m.step(state, m.Tick(NOW_US+m.DAY_US))
    state, effects = m.step(state, m.ObserverEnqueued(NOW_US+m.DAY_US, "fold:old", DAY))
    assert not any(isinstance(e, m.Fault) for e in effects)
    assert state.days[0].observers == {"fold:old"}
    frozen = replace(state, days=(replace(state.days[0], final=True), state.days[1]))
    after, effects = m.step(frozen, m.ObserverEnqueued(NOW_US+m.DAY_US, "fold:new", DAY))
    assert after == frozen and any(isinstance(e, m.Fault) for e in effects)


@exhaustive
def test_row_validation_cache_preserves_rejection_and_returns_independent_values():
    from dataclasses import replace

    from trusted_router.async_settle_shadow_journal import COUNTER_KIND, Row
    from trusted_router.async_settle_shadow_v2_a1_evidence import validate_row
    from trusted_router.detached_jws import canonical
    base = fixture("counter")[0]["object"]
    key = base["day"]+"/"+base["boot"]
    row = Row.build(COUNTER_KIND, key, base)
    expected = validate_row(row.kind, row.key, row.body)
    value = row.value()
    value["counts"][0]["authorize_attempts"] = -1
    assert row.value() == expected
    for changed in (replace(row, key="wrong"), replace(row, body=b"{}")):
        assert not changed._validated
        with pytest.raises(ValueError):
            changed.value()
    for field in ("A", "eligible_demand", "event_seq"):
        invalid = dict(base, **{field: -1})
        with pytest.raises(ValueError):
            Row.build(COUNTER_KIND, key, invalid)
        with pytest.raises(ValueError):
            Row(COUNTER_KIND, key, canonical(invalid)).value()


@exhaustive
def test_incremental_fold_size_limit_and_maintenance_cells_match_oracle():
    import itertools

    from trusted_router.detached_jws import canonical
    fast, old = service(), service()
    old.journal.boot = fast.journal.boot
    old.counters = copy.deepcopy(fast.counters)
    old.port = FullPort(old)
    accepted, rejected = 0, 0
    for phase, adapter, route, streamed in itertools.product(
            ("authorize", "settle", "refund"), (None, "openai", "anthropic", "other"),
            (None, "responses", "chat.completions", "other"), (None, True, False)):
        v = delta(phase, 1, adapter, route, streamed)
        a, b = fast.port.fold_counts(DAY, v), old.port.fold_counts(DAY, v)
        assert a == b
        accepted += a
        rejected += not a
        assert canonical(fast.counters[DAY]) == canonical(old.counters[DAY])
        assert fast.port.fold_cells[DAY][3] == len(canonical(fast.counters[DAY]))
    assert accepted and rejected
    fast.operation(DAY, "missed_slots", 100)
    fast.histogram(DAY, "rpc_us", 1000)
    fast.observer._daily_bucket((NOW_US+100_000_000)/1e6)["health_reads"] = 1000
    fast.fold(NOW_US+100_000_000)
    assert fast.port.fold_cells[DAY][3] == len(canonical(fast.counters[DAY]))


def test_port_event_validates_once_and_reuses_single_serialization(monkeypatch):
    from trusted_router import async_settle_shadow_v2_native as native
    from trusted_router.async_settle_shadow_v2_evidence import AUTHORIZE_EVENT
    svc = service()
    svc.journal.grant(DAY, 100)
    reservation = svc.port.reserve(NOW_US)
    event = event_for(reservation)
    event["candidates"] = [dict(endpoint_id="one", adapter="openai", features=["fusion"])]
    expected = EVENT.serialize(event)
    schemas, encodings = [], []
    validate, encode = native.native_size, native.canonical
    def checked(schema, value):
        schemas.append(schema)
        return validate(schema, value)
    def encoded(value):
        encodings.append(value)
        return encode(value)
    monkeypatch.setattr(native, "native_size", checked)
    monkeypatch.setattr(native, "canonical", encoded)
    result = svc.port.finalize(reservation, event)
    assert result == expected and result is svc.journal.slot(reservation).body
    assert sum(schema is AUTHORIZE_EVENT for schema in schemas) == 1
    assert len(encodings) == 1 and encodings[0] is event
