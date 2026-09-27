"""Disposable native GoogleSQL + Bigtable resources, exclusively on loopback emulators."""
from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from tests.conformance.spanner_ddl import DDL


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
    # Set this BEFORE constructing any client, exclusively in the emulator path.
    DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL = timedelta(milliseconds=100)

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
