"""Faults at the owned client's original transaction and exact-read boundaries."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from trusted_router.async_settle_shadow_journal import CLOSE_RESULT_KIND, CLOSURE_KIND
from trusted_router.storage_gcp_shadow_observation import UnknownCommit


@contextmanager
def original_reply_fault(store, *, kind, fault, now):
    """Let the REAL transaction land, then lose/delay its original reply once."""
    database = store.database
    original = database.run_in_transaction
    history = SimpleNamespace(attempts=0, fired=False)
    def transaction(callback, **options):
        selected = False
        class Transaction:
            def __init__(self, tx):
                self.tx = tx
            def __getattr__(self, name):
                return getattr(self.tx, name)
            def insert(self, **kwargs):
                nonlocal selected
                selected = selected or any(row[0] == kind for row in kwargs["values"])
                return self.tx.insert(**kwargs)
            def insert_or_update(self, **kwargs):
                nonlocal selected
                selected = selected or any(row[0] == kind for row in kwargs["values"])
                return self.tx.insert_or_update(**kwargs)
        result = original(lambda tx: callback(Transaction(tx)), **options)
        if selected:
            history.attempts += 1
        if selected and not history.fired:
            history.fired = True
            if fault == "lost":
                raise UnknownCommit("original client reply lost after landing")
            now[0] += 3.
        return result
    with patch.object(database, "run_in_transaction", transaction):
        yield history


def verify_final_fault(svc, fault):
    now = [100.]
    svc.store.clock = lambda: now[0]
    day = next(iter(svc.journal.days))
    svc.closing_reason = "shutdown"
    svc.termination_deadline = now[0]+9
    with original_reply_fault(svc.store, kind=CLOSURE_KIND, fault=fault, now=now) as history:
        svc.flush(now[0]+4)
        assert history.fired and history.attempts == 1
        assert day in svc.final_veto and day not in {d.day for d in svc.state.days if d.closed}
        from trusted_router.storage_gcp_shadow_observation import Budget
        key = day+"/"+svc.journal.boot
        assert svc.store.read(CLOSURE_KIND, key, Budget(now[0]+4, svc.store.clock)) is not None
        svc.flush(now[0]+4)
        assert history.attempts == 1
        assert svc.store.read(CLOSE_RESULT_KIND, key, Budget(now[0]+4, svc.store.clock)) is None
        assert day in svc.final_veto


def verify_handoff_fault(svc):
    now = [100.]
    svc.store.clock = lambda: now[0]
    svc.closing_reason = "shutdown"
    svc.termination_deadline = now[0]+9
    with original_reply_fault(svc.store, kind=CLOSE_RESULT_KIND, fault="lost", now=now) as history:
        svc.flush(now[0]+4)
        assert history.fired and history.attempts == 1 and svc.handoff is not None
        svc.flush(now[0]+4)
        assert svc.handoff is None and not svc.journal.days
        assert history.attempts == 1
