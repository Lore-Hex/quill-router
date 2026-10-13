"""Payload-free ordered client history and owner snapshots for native failures."""
import dataclasses
import hashlib
import json
from contextlib import contextmanager
from unittest.mock import patch

STATUS_CODES = frozenset({"OK", "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED",
    "NOT_FOUND", "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "FAILED_PRECONDITION",
    "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED", "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED"})


def error_shape(error):
    code = getattr(error, "grpc_status_code", None)
    if code is None:
        code = getattr(error, "code", None)
        if callable(code):
            code = code()
    name = getattr(code, "name", None)
    return dict(error=type(error).__name__, code=name if name in STATUS_CODES else None)


def identity_shape(value):
    raw = value.encode()
    return dict(length=len(raw), sha256_prefix=hashlib.sha256(raw).hexdigest()[:12])


def owner_snapshot(state):
    value = dataclasses.asdict(state)
    for day in value["days"]:
        for field in ("observers", "observer_keys", "attachments", "attachment_keys"):
            day[field] = [identity_shape(owner) for owner in sorted(day[field])]
    return value


def closure_shape(value):
    if value is None:
        return None
    # No boot, revision, caller text or arbitrary additional fields.
    stamps = ("final_checkpoint_seq", "attempt_started_at_us", "observed_at_us", "elapsed_us",
              "flush_remaining_us", "shutdown_remaining_us", "physical_commit_ack_at_us", "handoff_attempt_limit")
    return {key: value.get(key) for key in stamps if type(value.get(key)) in (int, type(None))} | {
        key: identity_shape(value[key]) for key in ("boot", "closure_sha256", "counter_sha256")
        if isinstance(value.get(key), str)}

def status(svc, history=()):
    return json.dumps(dict(
        operations=list(history), wall_us=int(svc.store.wall()*1e6), mono=svc.store.clock(),
        interval_end_us=svc.interval_end_us, folded_us=svc.folded_us,
        final_plans={d: dict(end=p[2].value()["interval_end_us"], kinds=[r.kind for r in [*p[0], p[1], p[2]]])
                     for d, p in svc.final_plans.items()},
        journal={d: dataclasses.asdict(v) for d, v in svc.journal.days.items()},
        lifecycle=owner_snapshot(svc._state), sticky_loss=svc.port.lost.is_set(),
        losses=svc.journal.losses, unclosed=svc.unclosed,
        sticky_loss_reasons=dict(capture_refused=svc.port.lost.is_set(),
            observer_overflow=svc.observer.daily_overflow,
            observer_days_outside_lifecycle=sorted(set(svc.observer.daily_counts)-{d.day for d in svc._state.days}),
            attachment_overflow=svc.bases.blocked or svc.traces.blocked),
        observer_daily_since=svc.observer.daily_since, observer_mono=svc.observer.clock(),
        final_veto=sorted(svc.final_veto), generation=svc.generation, revocation_epoch=svc.revocation_epoch, signal_stamp=svc.signal_stamp,
        closing_reason=svc.closing_reason, termination_deadline=svc.termination_deadline,
        pages=len(svc.journal.pages), retained=len(svc.journal.batches),
        event_permits=svc.journal.permits, terminal_permits=svc.journal.terminal_permits,
        last_closure_ack=closure_shape(svc.last_closure_ack),
        lanes={lane.stream: len(lane.queue) for lane in (svc.bases, svc.traces)},
    ), default=lambda v: list(v) if isinstance(v, (set, frozenset, range)) else identity_shape(type(v).__name__), sort_keys=True)


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
            # Native wrappers deliberately suppress exception display. Preserve
            # the actual cause for the next CI diagnosis without changing retries.
            causes = []
            cause = error.__cause__ or error.__context__
            while cause is not None and len(causes) < 8:
                causes.append(error_shape(cause))
                cause = cause.__cause__ or cause.__context__
            record(statement="transaction_error", **error_shape(error), causes=causes)
            raise
    @contextmanager
    def read(**kwargs):
        record(statement="snapshot")
        with snapshot(**kwargs) as reader:
            yield Reader(reader)
    with patch.object(database, "run_in_transaction", run), patch.object(database, "snapshot", read):
        yield history


def announce_boundary(svc, history, label, stream):
    """Snapshot lifecycle state on its owner, then publish the complete marker."""
    svc.assert_owner()
    print(json.dumps(dict(boundary=label, status=json.loads(status(svc, history)))), file=stream, flush=True)
    print("BOUNDARY", file=stream, flush=True)



def hold_boundary(svc, history, label, stream, seen, release):
    """Workers request an owner snapshot before announcing a held crash point."""
    from concurrent.futures import Future

    delivered = Future()
    def announce():
        try:
            announce_boundary(svc, history, label, stream)
            seen.set()
            delivered.set_result(None)
        except BaseException as error:
            delivered.set_exception(error)
    svc.loop.call_soon_threadsafe(announce)
    delivered.result(timeout=2)
    if not release.wait(25):
        raise TimeoutError("native boundary release deadline")


def wait_boundary(stream, output, deadline, marker=b"BOUNDARY"):
    """Read raw pipe bytes: select cannot see lines prefetched by TextIOWrapper."""
    import os
    import select
    import time

    pending = b""
    while time.monotonic() < deadline:
        readable, _, _ = select.select([stream], [], [], max(0, deadline-time.monotonic()))
        if not readable:
            return False
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            if pending:
                output.append(pending.decode())
            return False
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            output.append(line.decode())
            if line == marker:
                return True
    return False


def track_sessions(api, manifest, save):
    """Remember exact sessions so a dead child's open emulator RW locks can end."""
    execute = api.execute_streaming_sql
    def tracked(*args, **kwargs):
        from google.cloud.spanner_v1.types import ExecuteSqlRequest
        session = ExecuteSqlRequest(kwargs["request"]).session
        if session not in manifest["sessions"]:
            manifest["sessions"].append(session)
            save()
        return execute(*args, **kwargs)
    api.execute_streaming_sql = tracked


def release_sessions(api, database_name, manifest):
    """Parent-only, after child death AND gate completion; no database scans."""
    sessions = manifest.get("sessions", [])
    assert len(sessions) <= 16, "native session bound"
    for session in sessions:
        assert session.startswith(database_name+"/sessions/")
        api.delete_session(request={"name": session}, retry=None, timeout=2)
