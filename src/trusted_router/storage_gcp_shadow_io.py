"""V2-owned native transport. Never installed on the money client's API object."""
from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


def elapsed_clock() -> float:
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


@dataclass(frozen=True)
class Scope:
    deadline: float
    cap: float
    clock: Callable[[], float]

    def remaining(self) -> float:
        remaining = min(self.cap, self.deadline - self.clock())
        if remaining <= 0:
            raise TimeoutError("shadow_budget")
        return remaining


_SCOPE: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("v2_rpc_scope", default=None)
_RECORDER: contextvars.ContextVar[Callable[[int], None] | None] = contextvars.ContextVar("v2_rpc_recorder", default=None)


@contextmanager
def rpc_scope(deadline: float, cap: float, clock: Callable[[], float]) -> Iterator[None]:
    current = _SCOPE.get()
    scope = Scope(min(deadline, current.deadline) if current else deadline,
                  min(cap, current.cap) if current else cap, clock)
    scope.remaining()
    token = _SCOPE.set(scope)
    try:
        yield
        scope.remaining()
    finally:
        _SCOPE.reset(token)


@contextmanager
def observe_spanner_rpcs(record: Callable[[int], None]) -> Iterator[None]:
    token = _RECORDER.set(record)
    try:
        yield
    finally:
        _RECORDER.reset(token)


def scope_now() -> Scope:
    scope = _SCOPE.get()
    if scope is None:
        raise RuntimeError("shadow_scope_missing")
    return scope


class Sessions:
    """One bounded checkout lane; no SDK pool/maintenance-thread wait escapes."""
    def __init__(self, database: Any) -> None:
        self.database = database
        self.lock = threading.Lock()
        self.session: Any = None

    def get_session(self, transaction_type: Any) -> Any:
        scope = scope_now()
        if not self.lock.acquire(timeout=scope.remaining()):
            raise TimeoutError("shadow_checkout")
        try:
            scope.remaining()
            if self.session is None:
                from google.cloud.spanner_v1.session import Session
                session = Session(self.database)
                session.create()
                self.session = session
            scope.remaining()
            return self.session
        except Exception:
            self.lock.release()
            raise

    def put_session(self, session: Any) -> None:
        self.lock.release()


def owned_database(database: Any) -> Any:
    """Construct only after v2 selection, with a distinct API and session manager."""
    from google.cloud.spanner_v1 import Client
    from google.cloud.spanner_v1.database import Database
    if not isinstance(database, Database):
        return database  # Explicit offline transport harnesses already own their API.
    client = database._instance._client
    owned = Client(project=client.project, credentials=client.credentials,
                   client_options=client._client_options, disable_builtin_metrics=True)
    result = owned.instance(database._instance.instance_id).database(database.database_id)
    result._sessions_manager = Sessions(result)
    # The SDK sets its thread-local transaction flag before checkout, outside its
    # finally block. Own the checkout so a timeout cannot poison the next cycle.
    def transaction(callback: Any, **kwargs: Any) -> Any:
        session = result._sessions_manager.get_session(None)
        try:
            return session.run_in_transaction(callback, **kwargs)
        finally:
            result._sessions_manager.put_session(session)
    owned_result: Any = result
    owned_result.run_in_transaction = transaction
    return result


def configure(database: Any) -> None:
    if getattr(database, "_shadow_v2_io", False):
        return
    api = getattr(database, "spanner_api", None)
    if api is None:
        return
    for name, original in (
            ("begin_transaction", api.begin_transaction if hasattr(api, "begin_transaction") else None),
            ("commit", api.commit if hasattr(api, "commit") else None),
            ("execute_sql", api.execute_sql if hasattr(api, "execute_sql") else None),
            ("execute_batch_dml", api.execute_batch_dml if hasattr(api, "execute_batch_dml") else None),
            ("execute_streaming_sql", api.execute_streaming_sql if hasattr(api, "execute_streaming_sql") else None),
            ("read", api.read if hasattr(api, "read") else None),
            ("streaming_read", api.streaming_read if hasattr(api, "streaming_read") else None),
            ("rollback", api.rollback if hasattr(api, "rollback") else None),
            ("create_session", api.create_session if hasattr(api, "create_session") else None),
            ("batch_create_sessions", api.batch_create_sessions if hasattr(api, "batch_create_sessions") else None),
            ("get_session", api.get_session if hasattr(api, "get_session") else None),
            ("list_sessions", api.list_sessions if hasattr(api, "list_sessions") else None),
            ("delete_session", api.delete_session if hasattr(api, "delete_session") else None),
    ):
        if not callable(original):
            continue
        # Offline SDK harnesses may arrive with the baseline wrapper installed.
        original = getattr(original, "__wrapped__", original)
        def bounded(*args: Any, _call: Any = original, _name: str = name, **kwargs: Any) -> Any:
            scope = scope_now()
            kwargs.update(timeout=scope.remaining(), retry=None)
            record = _RECORDER.get()
            started = scope.clock()
            streaming = False
            def measured() -> None:
                if record is not None:
                    record(max(0, round((scope.clock()-started)*1_000_000)))
            try:
                result = _call(*args, **kwargs)
                if _name in ("execute_streaming_sql", "streaming_read"):
                    streaming = True
                    def consume() -> Iterator[Any]:
                        try:
                            for response in result:
                                scope.remaining()
                                yield response
                        except Exception:
                            manager = getattr(database, "_sessions_manager", None)
                            if isinstance(manager, Sessions):
                                manager.session = None
                            raise RuntimeError("shadow_stream_failed") from None
                        finally:
                            measured()
                    return consume()
                scope.remaining()
                return result
            except Exception:
                manager = getattr(database, "_sessions_manager", None)
                if isinstance(manager, Sessions):
                    manager.session = None  # Reacquire only on a later scheduled operation.
                # Non-SDK exceptions stop Session/Transaction/stream retry loops.
                raise RuntimeError("shadow_rpc_failed") from None
            finally:
                if not streaming:
                    measured()
        setattr(api, name, bounded)
    database._shadow_v2_io = True


def timestamp_ns(value: Any) -> int:
    """Convert SDK nanosecond datetime or protobuf Timestamp without float loss."""
    from google.api_core.datetime_helpers import DatetimeWithNanoseconds
    from google.protobuf.timestamp_pb2 import Timestamp
    from proto.datetime_helpers import DatetimeWithNanoseconds as ProtoDatetime
    if isinstance(value, (DatetimeWithNanoseconds, ProtoDatetime)):
        value = value.timestamp_pb()
    if not isinstance(value, Timestamp):
        raise ValueError("timestamp_precision")
    return value.seconds * 1_000_000_000 + value.nanos
