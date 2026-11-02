"""V2-owned native transport. Never installed on the money client's API object."""
from __future__ import annotations

import contextvars
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from google.api_core.exceptions import Cancelled, DeadlineExceeded, NotFound


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


class SessionUnavailable(RuntimeError):
    """No owned session is ready; a cycle must not attempt storage."""


class SessionCheckoutTimeout(TimeoutError):
    """The session lane remained occupied through the checkout deadline."""


class Sessions:
    """One bounded checkout lane; no SDK pool/maintenance-thread wait escapes."""
    def __init__(self, database: Any, *, managed: bool = False, lane: str = "observation") -> None:
        self.database = database
        self.managed = managed
        self.lane = lane
        self.boot = str(uuid.uuid4())
        self.next_recovery = float("-inf")
        self.lock = threading.Lock()
        self.session: Any = None
        self.retired: Any = None
        self.creation_unknown = False
        self.reconciliation_names: list[str] = []
        self.reconciliation_token = ""

    def retire(self) -> None:
        # Keep the server identity until deletion succeeds. Dropping it here
        # leaks one server-side session on every failed scheduled operation.
        if self.session is not None:
            self.retired, self.session = self.session, None

    @property
    def ready(self) -> bool:
        return not self.creation_unknown and (not self.managed or self.session is not None)

    def recovery_due(self, clock: Callable[[], float]) -> bool:
        return not self.ready and clock() >= self.next_recovery

    def prepare(self, clock: Callable[[], float], boot: str | None = None) -> bool:
        """One installation/recovery operation, outside any cycle RPC scope."""
        if _SCOPE.get() is not None:
            raise RuntimeError("shadow_session_prepare_in_cycle")
        if boot is not None and boot != self.boot:
            if self.managed or self.session is not None or self.retired is not None or self.creation_unknown:
                raise RuntimeError("shadow_session_boot_changed")
            self.boot = boot
        self.managed = True
        if self.ready or not self.recovery_due(clock):
            return self.ready
        try:
            with rpc_scope(clock()+5, 5, clock):
                session = self._checkout(create=True)
                self.put_session(session)
        except Exception:
            return False
        return True

    def close(self, clock: Callable[[], float]) -> bool:
        """Delete this retired lane only after all its worker calls have returned.

        Unknown creation uses the same bounded inventory reconciliation as
        recovery, but cleanup never allocates another session.
        """
        try:
            with rpc_scope(clock()+5, 5, clock):
                with self.lock:
                    self.retire()
                    if self.creation_unknown:
                        self._reconcile()
                    if self.retired is not None:
                        self.retired.delete()
                        self.retired = None
            return True
        except Exception:
            return False

    def get_session(self, transaction_type: Any) -> Any:
        return self._checkout(create=not self.managed)

    def _checkout(self, *, create: bool) -> Any:
        scope = scope_now()
        if not self.lock.acquire(timeout=scope.remaining()):
            raise SessionCheckoutTimeout("shadow_checkout")
        try:
            # Read readiness while owning the lane, including after recovery
            # held it until the deadline. No RPC has started at this point.
            if not create and not self.ready:
                raise SessionUnavailable("shadow_session_unavailable")
            scope.remaining()
            if create and self.managed and not self.ready:
                if scope.clock() < self.next_recovery:
                    raise SessionUnavailable("shadow_session_recovery_wait")
                self.next_recovery = scope.clock()+60
            if self.creation_unknown:
                if not create or not self.managed:
                    raise SessionUnavailable("shadow_session_creation_unknown")
                self._reconcile()
            if self.retired is not None:
                self.retired.delete()
                self.retired = None
                scope.remaining()
            if self.session is None:
                from google.cloud.spanner_v1.session import Session
                session = Session(self.database, labels={"tr_component": "shadow-v2", "tr_boot": self.boot, "tr_lane": self.lane})
                self.creation_unknown = True
                session.create()
                self.session = session
                self.creation_unknown = False
            scope.remaining()
            return self.session
        except SessionUnavailable:
            self.lock.release()
            raise
        except Exception:
            self.retire()
            self.lock.release()
            raise

    def put_session(self, session: Any) -> None:
        self.lock.release()

    def _reconcile(self) -> None:
        """Finish the boot-filtered inventory and cleanup before allocating."""
        scope = scope_now()
        try:
            # The pinned SDK exposes listing on spanner_api, not Database.
            # One page per recovery window bounds actual ListSessions RPCs.
            pager = self.database.spanner_api.list_sessions(request=dict(
                database=self.database.name,
                filter=(f"labels.tr_component:shadow-v2 AND labels.tr_boot:{self.boot}"
                        f" AND labels.tr_lane:{self.lane}"),
                page_token=self.reconciliation_token))
            page = next(iter(pager.pages))
            scope.remaining()
        except Exception:
            self.reconciliation_names = []
            self.reconciliation_token = ""
            raise
        # Spanner label filters are substring matches: generation "1" also
        # finds "10". Only this exact lane/boot may be deleted by its owner.
        labels = {"tr_component": "shadow-v2", "tr_boot": self.boot, "tr_lane": self.lane}
        self.reconciliation_names.extend(session.name for session in page.sessions
                                         if all(session.labels.get(key) == value for key, value in labels.items()))
        self.reconciliation_token = page.next_page_token
        if self.reconciliation_token:
            raise SessionUnavailable("shadow_session_inventory_incomplete")
        names, self.reconciliation_names = self.reconciliation_names, []
        held = self.session.name if self.session is not None else None
        for name in names:
            if name != held:
                self.database.spanner_api.delete_session(name=name)
                scope.remaining()
        self.retired = None
        self.creation_unknown = False


def owned_database(database: Any, *, lane: str = "observation") -> Any:
    """Construct only after v2 selection, with a distinct API and session manager."""
    from google.cloud.spanner_v1 import Client
    from google.cloud.spanner_v1.database import Database
    if not isinstance(database, Database):
        return database  # Explicit offline transport harnesses already own their API.
    client = database._instance._client
    owned = Client(project=client.project, credentials=client.credentials,
                   client_options=client._client_options, disable_builtin_metrics=True)
    result = owned.instance(database._instance.instance_id).database(database.database_id)
    result._sessions_manager = Sessions(result, lane=lane)
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


@dataclass
class Transport:
    database: Any
    failure: Callable[[str, str], None] | None = None


# Wrappers retain their state; the registry does not keep discarded clients alive.
# Identity keys also support unhashable, non-weakrefable offline stand-ins.
_TRANSPORTS: weakref.WeakValueDictionary[int, Transport] = weakref.WeakValueDictionary()


def configure(database: Any) -> Transport | None:
    existing = _TRANSPORTS.get(id(database))
    if existing is not None:
        return existing
    api = getattr(database, "spanner_api", None)
    if api is None:
        return None
    from google.api_core.exceptions import Aborted

    transport = Transport(database)
    _TRANSPORTS[id(database)] = transport
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
            def failed(error: Exception) -> None:
                # Notify before replacing the exception or retiring its session.
                # This hook is confined to the v2-owned client, never money IO.
                callback = transport.failure
                if callback is not None and _name in ("commit", "execute_streaming_sql"):
                    if isinstance(error, (DeadlineExceeded, Cancelled)):
                        callback(_name, "DeadlineExceeded" if isinstance(error, DeadlineExceeded) else "Cancelled")
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
                        except Exception as error:
                            failed(error)
                            manager = getattr(database, "_sessions_manager", None)
                            if isinstance(manager, Sessions):
                                manager.retire()
                            raise RuntimeError("shadow_stream_failed") from (error if isinstance(error, Aborted) else None)
                        finally:
                            measured()
                    return consume()
                # Let the SDK record a successful creation's server identity
                # before checkout enforces the deadline. Otherwise a late
                # reply becomes an orphan we can neither reuse nor delete.
                if _name != "create_session":
                    scope.remaining()
                return result
            except Exception as error:
                failed(error)
                # Deletion is idempotent, but NotFound from a query is not:
                # the missing resource might be a table rather than a session.
                if _name == "delete_session" and isinstance(error, NotFound):
                    return None
                manager = getattr(database, "_sessions_manager", None)
                if isinstance(manager, Sessions):
                    manager.retire()  # Delete before any later replacement.
                # Non-SDK exceptions stop Session/Transaction/stream retry loops.
                # Only ABORTED needs to reach the transaction outcome classifier.
                raise RuntimeError("shadow_rpc_failed") from (error if isinstance(error, Aborted) else None)
            finally:
                if not streaming:
                    measured()
        setattr(api, name, bounded)
    return transport


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
