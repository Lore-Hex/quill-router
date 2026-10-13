"""Small standalone fixture reader shared with native restart children."""
import copy
import json
from functools import lru_cache
from pathlib import Path

NOW_US = 1791763200000000

@lru_cache
def authorize_event():
    values = json.loads((Path(__file__).parent/"fixtures/async_settlement/shadow_v2/receipt_pages.json").read_text())["variants"]
    return next(v["object"] for v in values if v["schema"] == "AUTHORIZE_EVENT" and v["expected"] == "accept")

def event_for(reservation):
    value = copy.deepcopy(authorize_event())
    value.update(seq=f"{reservation.event_seq:020}", event_at_us=reservation.observed_us,
                 receipt_at_us=reservation.observed_us, authorization_day=reservation.day,
                 receipt_locator=reservation.coordinates)
    return value


def observation_service(store):
    """Single constructor path for fake, native parent and native child histories."""
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    return Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), store)


def bind_clock(svc, clock):
    """The observer captures its clock at construction; replace the pair together."""
    svc.store.clock = svc.observer.clock = clock
    svc.observer.daily_since = clock()


def variant(file, schema, folder="shadow_v2"):
    values = json.loads((Path(__file__).parent/"fixtures/async_settlement"/folder/(file+".json")).read_text())["variants"]
    return copy.deepcopy(next(v["object"] for v in values if v.get("schema", v.get("target")) == schema and v["expected"] == "accept"))


def saturate(svc, stamp):
    """Reach count caps through public admissions, without padded journal/lane objects."""
    from trusted_router import async_settle_shadow_lifecycle as m
    from trusted_router.async_settle_shadow_journal import SAMPLE_KIND, Row
    from trusted_router.detached_jws import canonical
    day = next(iter(svc.journal.days))
    svc.tick(stamp-240*m.SLOT_US+500_000, publisher=True)
    svc.tick(stamp, publisher=True)
    assert sum(len(lane["entries"]) for lane in (svc.traces.schema.parse(o.body) for o in svc.traces.queue)) == 240
    for _ in range(2):
        svc.journal.grant(day, 100)
    retained = []
    for index in range(200):
        reservation = svc.port.reserve(stamp)
        if reservation is None:
            break
        assert svc.port.finalize(reservation, event_for(reservation), fold_owed=index < 32), "full-queue finalize"
        rows = ()
        if index < 32:
            sample = variant("sample", "SAMPLE")
            sample["authorization_id"] = f"{svc.journal.boot}-{index}"
            raw = canonical(sample)
            row = Row(SAMPLE_KIND, sample["authorization_day"]+"/"+sample["authorization_id"], raw+b" "*(8192-len(raw)))
            rows = (row,)
            retained.append(row)
        assert svc.port.complete(reservation, rows), "full-queue retained admission"
    svc.sync_fold_obligations()
    for lane in (svc.bases, svc.traces):
        for index in range(241):
            if lane.stream == "basis":
                value = variant("health_basis", "HEALTH_BASIS", "shadow_v2_a1")
                value["reader_boot"] = svc.journal.boot
                value["read_elapsed_ns"] -= index
            else:
                slot = stamp//m.SLOT_US+index
                value = dict(v=2, policy_version="shadow-v2", contract_revision="a1", day=day,
                    boot=svc.journal.boot, slot=slot, claim_start_us=None, claim_at_ns=None,
                    observed_at_ns=None, publisher_heartbeat_ns=None, ack_at_us=None, cycle_us=None,
                    body_sha256=None, outcome="missed", unknown_outcome=False, late_cycle=False,
                    rpc_count=0, rpc_elapsed_us=0)
            try:
                lane.append(day, value)
            except ValueError:
                break
        assert len(lane.queue) == 4 and lane.blocked, f"{lane.stream} did not saturate: {len(lane.queue)}"
        for item in lane.queue:
            svc.accept(m.AttachmentAdmitted(stamp, item.key, day))
    assert len(svc.journal.pages) == 4, "journal cap not reached"
    assert len(retained) == 32 and svc.journal.work_bytes == 256*1024, "retained cap not reached"
    assert len(svc.state.days[0].observers) == 32, "observer owner cap not reached"
    assert len(svc.state.days[0].attachments) == 8 and svc.state.job is not None, "attachment/publisher owners absent"
    assert svc.port.reserve(stamp, terminal=True) is None, "terminal cap must refuse"
    assert svc.port.lost.is_set() and svc.journal.losses, "saturation must record sticky capture loss"
    svc.journal.loss(day)
    return retained
