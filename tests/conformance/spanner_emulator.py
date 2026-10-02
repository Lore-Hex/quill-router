"""Disposable native GoogleSQL resources, exclusively on a loopback emulator."""
from __future__ import annotations

import os
import re
import socket
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from functools import wraps
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from tests.conformance.spanner_ddl import DDL

NULL_FILTERED_INDEXES = {
    match[1].lower() for ddl in DDL
    if (match := re.search(r"CREATE (?:UNIQUE )?NULL_FILTERED INDEX (\w+)", ddl))
}
NULL_FILTERED_HINT = "spanner_emulator.disable_query_null_filtered_index_check=true"
_TABLE_HINT = re.compile(r"@\{[^{}]*\}")
_FORCE_INDEX = re.compile(r"(?:\{|,)\s*FORCE_INDEX\s*=\s*(\w+)\s*(?=,|\})", re.I)


def sql_code(sql: str) -> str:
    """Mask GoogleSQL quotes/comments, preserving offsets for surgical edits.

    Prefixes (r, b, rb, br, in either case) do not change quote boundaries:
    even raw literals cannot close on an escaped quote. Triple quotes must be
    recognized before single quotes. Fail closed on unterminated regions.
    """
    masked = list(sql)
    pos = 0
    while pos < len(sql):
        start = pos
        if sql.startswith(("--", "#"), pos):
            end = sql.find("\n", pos)
            pos = len(sql) if end < 0 else end
        elif sql.startswith("/*", pos):
            end = sql.find("*/", pos + 2)
            if end < 0:
                raise ValueError("unterminated SQL block comment")
            pos = end + 2
        elif sql[pos] in "\"'`":
            quote = sql[pos]
            delimiter = quote * 3 if quote != "`" and sql.startswith(quote * 3, pos) else quote
            pos += len(delimiter)
            while pos < len(sql):
                if sql[pos] == "\\":
                    pos += 2
                elif sql.startswith(delimiter, pos):
                    pos += len(delimiter)
                    break
                else:
                    pos += 1
            else:
                raise ValueError("unterminated SQL quote")
            # Backticks are identifiers. Expose only a simple FORCE_INDEX
            # value in a real hint block; keep all other identifiers masked.
            prefix = "".join(masked[:start])
            if quote == "`" and re.search(r"@\{[^{}]*\bFORCE_INDEX\s*=\s*$", prefix, re.I):
                identifier = sql[start + 1:pos - 1]
                if re.fullmatch(r"\w+", identifier):
                    masked[start] = masked[pos - 1] = " "
                    continue
        else:
            pos += 1
            continue
        masked[start:pos] = ["\n" if char == "\n" else " " for char in sql[start:pos]]
    return "".join(masked)


def names_null_filtered_index(sql: str) -> bool:
    return bool(set(re.findall(r"\b\w+\b", sql_code(sql).lower())) & NULL_FILTERED_INDEXES)


def emulator_sql(sql: str) -> str:
    """Preserve SQL verbatim except for the emulator's index-eligibility hint."""
    def merge(match: re.Match[str]) -> str:
        block = sql[match.start():match.end()]
        if not any(index[1].lower() in NULL_FILTERED_INDEXES for index in _FORCE_INDEX.finditer(match[0])):
            return block
        existing = re.compile(r"(\bspanner_emulator\.disable_query_null_filtered_index_check\s*=\s*)(true|false)\b", re.I)
        if hint := existing.search(match[0]):
            return block if hint[2].lower() == "true" else block[:hint.start(2)] + "true" + block[hint.end(2):]
        return block[:-1] + ", " + NULL_FILTERED_HINT + "}"

    # Match only code, splice into the original so literals/comments stay exact.
    result = sql
    for match in reversed(list(_TABLE_HINT.finditer(sql_code(sql)))):
        result = result[:match.start()] + merge(match) + result[match.end():]
    return result


def _snapshot_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def wrapped(self: Any, **kwargs: Any) -> Any:
        from google.cloud.spanner_v1.snapshot import Snapshot

        # Validate construction AND use-time serialization of ORIGINAL options without RPCs
        # or session acquisition. Do not hide errors by dropping bounds first.
        Snapshot(session=None, **kwargs)._build_transaction_selector_pb()
        # Fresh emulator schemas and read-your-writes conformance need strong reads.
        kwargs.pop("exact_staleness", None)
        kwargs.pop("max_staleness", None)
        return original(self, **kwargs)

    return wrapped


def _sdk_wrapper(original: Callable[..., Any], argument: str) -> Callable[..., Any]:
    def rewrite(value: Any) -> Any:
        if argument != "statements":
            return emulator_sql(value)
        return [emulator_sql(item) if isinstance(item, str)
                else (emulator_sql(item[0]), *item[1:]) for item in value]

    @wraps(original)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        if args:
            args = (rewrite(args[0]), *args[1:])
        elif argument in kwargs:
            kwargs[argument] = rewrite(kwargs[argument])
        return original(self, *args, **kwargs)

    return wrapped


@contextmanager
def emulator_sdk_shim() -> Iterator[None]:
    """Scoped SDK boundary shared by acceptance SQL and the real native store.

    Only emulator_resources installs this, after the emulator safety checks.
    Transaction inherits execute_sql from _SnapshotBase. BatchSnapshot
    forwarding and Database partitioned DML are covered too; repeated SDK
    forwarding cannot duplicate table hints. Snapshot staleness is suppressed.
    """
    from google.cloud.spanner_v1.database import BatchSnapshot, Database
    from google.cloud.spanner_v1.snapshot import _SnapshotBase
    from google.cloud.spanner_v1.transaction import Transaction

    with ExitStack() as stack:
        for cls, method, argument in (
            (_SnapshotBase, "execute_sql", "sql"),
            (Transaction, "execute_update", "dml"),
            (Transaction, "batch_update", "statements"),
            (Database, "execute_partitioned_dml", "dml"),
            (BatchSnapshot, "execute_sql", "sql"),
        ):
            stack.enter_context(patch.object(cls, method, _sdk_wrapper(getattr(cls, method), argument)))
        stack.enter_context(patch.object(Database, "snapshot", _snapshot_wrapper(Database.snapshot)))
        yield


def require_emulators() -> None:
    enabled = os.environ.get("TR_CONFORMANCE_EMULATOR_SCHEMA") == "1"
    names = ("SPANNER_EMULATOR_HOST",)
    if not enabled:
        pytest.skip("native GoogleSQL acceptance requires TR_CONFORMANCE_EMULATOR_SCHEMA=1 and the Spanner emulator; no SQL was validated")
    for name in names:
        host = os.environ.get(name, "")
        assert host, f"{name} is required when native emulator coverage is enabled"
        address, port = host.rsplit(":", 1)
        assert address in {"localhost", "127.0.0.1", "[::1]"}, f"{name}: loopback emulator required"
        # Opted-in but unavailable is a FAILURE, never a green skipped CI job.
        with socket.create_connection((address.strip("[]"), int(port)), timeout=5):
            pass
    assert not os.environ.get("GCP_SERVICE_ACCOUNT_KEY_JSON"), "emulator tests must not use service-account keys"


@contextmanager
def emulator_resources() -> Iterator[tuple[Any, str]]:
    require_emulators()
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import spanner
    from google.cloud.spanner_v1.database_sessions_manager import DatabaseSessionsManager

    # The SDK sleeps once per polling interval; close() joins that thread.
    # Set this before constructing clients and restore after database.close(),
    # including setup, body and teardown exceptions.
    with emulator_sdk_shim(), patch.object(
        DatabaseSessionsManager, "_MAINTENANCE_THREAD_POLLING_INTERVAL",
        timedelta(milliseconds=100),
    ):
        project = "tr-conformance"
        instance_id = "conf-" + uuid4().hex[:12]
        client = spanner.Client(project=project, credentials=AnonymousCredentials(), disable_builtin_metrics=True)
        instance = client.instance(instance_id, configuration_name=f"projects/{project}/instanceConfigs/emulator-config")
        instance.create().result(timeout=60)
        database = None
        try:
            database = instance.database("conformance", ddl_statements=DDL[:20])
            # No dialect override: Google's default is GOOGLE_STANDARD_SQL.
            database.create().result(timeout=120)
            # Bound schema RPC sizes; retain production order and wait for indexes.
            for offset in range(20, len(DDL), 20):
                database.update_ddl(DDL[offset:offset + 20]).result(timeout=120)
            yield database, instance_id
        finally:
            try:
                if database is not None:
                    database.close()
            finally:
                instance.delete()


@contextmanager
def emulator_store(instance_id: str) -> Iterator[Any]:
    from google.cloud.spanner_v1 import KeySet

    from tests.fakes.analytics_pipeline import OutboxAnalyticsReader
    from trusted_router.storage_gcp import SpannerStore

    store = SpannerStore(
        project_id="tr-conformance", spanner_instance_id=instance_id,
        spanner_database_id="conformance",
        operational_analytics_outbox_enabled=True, analytics_outbox_enabled=True,
    )

    def rows(table: str, columns: tuple[str, ...]) -> list[tuple[Any, ...]]:
        with store._database.snapshot() as snapshot:
            return [tuple(row) for row in snapshot.read(table=table, columns=columns, keyset=KeySet(all_=True))]

    # The reader stands in for ClickHouse: it replays the rows the real
    # outboxes committed to the emulator through the reference semantics.
    store._operational_analytics = OutboxAnalyticsReader(
        operational_rows=lambda: rows("tr_operational_analytics_outbox", ("event_kind", "event_id", "payload")),
        benchmark_rows=lambda: rows("tr_analytics_outbox", ("event_id", "payload")),
    )
    try:
        yield store
    finally:
        store._database.close()
