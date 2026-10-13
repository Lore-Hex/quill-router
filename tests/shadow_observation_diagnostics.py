"""Payload-free ordered client history and owner snapshots for native failures."""
import dataclasses
import json
from contextlib import contextmanager
from unittest.mock import patch


def status(svc, history=()):
    return json.dumps(dict(
        operations=list(history), wall_us=int(svc.store.wall()*1e6), mono=svc.store.clock(),
        interval_end_us=svc.interval_end_us, folded_us=svc.folded_us,
        final_plans={d: dict(end=p[2].value()["interval_end_us"], kinds=[r.kind for r in [*p[0], p[1], p[2]]])
                     for d, p in svc.final_plans.items()},
        journal={d: dataclasses.asdict(v) for d, v in svc.journal.days.items()},
        lifecycle=dataclasses.asdict(svc._state), sticky_loss=svc.port.lost.is_set(),
        losses=svc.journal.losses, unclosed=svc.unclosed,
        sticky_loss_reasons=dict(capture_refused=svc.port.lost.is_set(),
            observer_overflow=svc.observer.daily_overflow,
            observer_days_outside_lifecycle=sorted(set(svc.observer.daily_counts)-{d.day for d in svc._state.days}),
            attachment_overflow=svc.bases.blocked or svc.traces.blocked),
        observer_daily_since=svc.observer.daily_since, observer_mono=svc.observer.clock(),
        final_veto=sorted(svc.final_veto), generation=svc.generation,
        closing_reason=svc.closing_reason, termination_deadline=svc.termination_deadline,
        pages=len(svc.journal.pages), retained=len(svc.journal.batches),
        event_permits=svc.journal.permits, terminal_permits=svc.journal.terminal_permits,
        last_closure_ack=svc.last_closure_ack,
        lanes={lane.stream: len(lane.queue) for lane in (svc.bases, svc.traces)},
    ), default=lambda v: list(v) if isinstance(v, (set, frozenset, range)) else repr(v), sort_keys=True)


@contextmanager
def client_history(store):
    """Wrap the owned database, including reads inside REAL transactions."""
    history = []
    def record(**event):
        history.append(dict(event, wall_us=int(store.wall()*1e6), mono=store.clock()))
    database = store.database
    transaction, snapshot = database.run_in_transaction, database.snapshot
    class Reader:
        def __init__(self, reader):
            self.reader = reader
        def __getattr__(self, name):
            return getattr(self.reader, name)
        def execute_sql(self, sql, **kwargs):
            record(statement=sql.split()[0], kind=kwargs.get("params", {}).get("kind"))
            return self.reader.execute_sql(sql, **kwargs)
        def insert(self, **kwargs):
            record(statement="insert", kinds=[r[0] for r in kwargs["values"]])
            return self.reader.insert(**kwargs)
        def insert_or_update(self, **kwargs):
            record(statement="insert_or_update", kinds=[r[0] for r in kwargs["values"]])
            return self.reader.insert_or_update(**kwargs)
    def run(callback, **kwargs):
        record(statement="transaction")
        try:
            result = transaction(lambda tx: callback(Reader(tx)), **kwargs)
            record(statement="transaction_return")
            return result
        except Exception as error:
            record(statement="transaction_error", error=type(error).__name__, reason=str(error))
            raise
    @contextmanager
    def read(**kwargs):
        record(statement="snapshot")
        with snapshot(**kwargs) as reader:
            yield Reader(reader)
    with patch.object(database, "run_in_transaction", run), patch.object(database, "snapshot", read):
        yield history
