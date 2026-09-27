"""Disposable native GoogleSQL + Bigtable resources, exclusively on loopback emulators."""
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


def names_null_filtered_index(sql: str) -> bool:
    return bool(set(re.findall(r"\b\w+\b", sql.lower())) & NULL_FILTERED_INDEXES)


def emulator_sql(sql: str) -> str:
    """Preserve SQL verbatim except for the emulator's index-eligibility hint."""
    def merge(match: re.Match[str]) -> str:
        block = match[0]
        if not any(index[1].lower() in NULL_FILTERED_INDEXES for index in _FORCE_INDEX.finditer(block)):
            return block
        existing = re.compile(r"(\bspanner_emulator\.disable_query_null_filtered_index_check\s*=\s*)(true|false)\b", re.I)
        if existing.search(block):
            return existing.sub(lambda hint: hint[0] if hint[2].lower() == "true" else hint[1] + "true", block)
        return block[:-1] + ", " + NULL_FILTERED_HINT + "}"

    return _TABLE_HINT.sub(merge, sql)


def _snapshot_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def wrapped(self: Any, **kwargs: Any) -> Any:
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
    names = ("SPANNER_EMULATOR_HOST", "BIGTABLE_EMULATOR_HOST")
    if not enabled:
        pytest.skip("native GoogleSQL acceptance requires TR_CONFORMANCE_EMULATOR_SCHEMA=1 and Spanner/Bigtable emulators; no SQL was validated")
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
def emulator_resources() -> Iterator[tuple[Any, Any, str]]:
    require_emulators()
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigtable, spanner
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
        table = bigtable.Client(project=project, credentials=AnonymousCredentials(), admin=True).instance(instance_id).table("generations")
        table_created = False
        database = None
        try:
            database = instance.database("conformance", ddl_statements=DDL[:20])
            # No dialect override: Google's default is GOOGLE_STANDARD_SQL.
            database.create().result(timeout=120)
            # Bound schema RPC sizes; retain production order and wait for indexes.
            for offset in range(20, len(DDL), 20):
                database.update_ddl(DDL[offset:offset + 20]).result(timeout=120)
            table.create(column_families={name: None for name in ("m", "activity", "benchmark", "synthetic", "rollup")})
            table_created = True
            yield database, table, instance_id
        finally:
            try:
                if database is not None:
                    database.close()
            finally:
                try:
                    if table_created:
                        table.delete()
                finally:
                    instance.delete()


@contextmanager
def emulator_store(instance_id: str) -> Iterator[Any]:
    from trusted_router.storage_gcp import SpannerBigtableStore

    store = SpannerBigtableStore(
        project_id="tr-conformance", spanner_instance_id=instance_id,
        spanner_database_id="conformance", bigtable_instance_id=instance_id,
        generation_table="generations",
    )
    try:
        yield store
    finally:
        store._database.close()
