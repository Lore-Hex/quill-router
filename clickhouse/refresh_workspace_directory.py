"""Refresh the no-PII ClickHouse workspace directory from Spanner.

Scheduled every 30 minutes since 2026-08-26 (tr-clickhouse-workspace-directory
.timer). An earlier version of this docstring said "not a scheduled job,
deliberately", reasoning that the tenant_id map only matters for the closed set
of pre-2026-08-19 rows. That reasoning covered the MAP and forgot the
DIRECTORY: workspace/domain signup reporting joins new activity to
``workspace_directory`` for names, so every workspace created after the last
manual run reports as an unnamed id. It was run by hand on Aug 23 and Aug 24
and then not again; by Aug 26 the directory was 685 workspaces behind while
live usage stayed current. A dimension table that only updates when somebody
remembers it is how that happens, so now the timer remembers.

The projection permits workspace ids and workspace names only; all other
entity attributes are discarded before a ClickHouse payload is built.
ReplacingMergeTree uses ``refreshed_at`` as its version, making repeated runs
idempotent. A workspace rename therefore converges on the newest row at
``FINAL``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from trusted_router.storage_operational_analytics import analytics_surrogate

PROJECT = "quill-cloud-proxy"
SPANNER_INSTANCE = "trusted-router-nam6"
SPANNER_DATABASE = "trusted-router"
CLICKHOUSE_URL = "http://localhost:8123/"
DIRECTORY_TABLE = "tr.workspace_directory"
MAP_TABLE = "tr.tenant_workspace_map"


class WorkspaceSource(Protocol):
    def fetch(self) -> list[Mapping[str, Any]]: ...


class DirectoryWriter(Protocol):
    def upsert_directory(self, rows: list[dict[str, Any]]) -> None: ...

    def upsert_tenant_map(self, rows: list[dict[str, Any]]) -> None: ...


@dataclass(frozen=True)
class RefreshResult:
    source_rows: int
    directory_rows: int
    tenant_map_rows: int


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _clickhouse_datetime(value: Any, *, field: str) -> str:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"workspace {field} is not an ISO timestamp") from exc
    else:
        raise ValueError(f"workspace {field} is not an ISO timestamp")
    return _utc(parsed).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


class SpannerWorkspaceSource:
    """Read only workspace entity bodies from the system of record."""

    def __init__(self, *, project: str, instance: str, database: str) -> None:
        from google.cloud import spanner
        from google.cloud.spanner_v1 import param_types

        self._database = (
            spanner.Client(project=project, disable_builtin_metrics=True)
            .instance(instance)
            .database(database)
        )
        self._pt = param_types

    def fetch(self) -> list[Mapping[str, Any]]:
        with self._database.snapshot() as snapshot:
            values = snapshot.execute_sql(
                "SELECT body FROM tr_entities WHERE kind=@kind ORDER BY id",
                params={"kind": "workspace"},
                param_types={"kind": self._pt.STRING},
            )
            bodies: list[Mapping[str, Any]] = []
            for row in values:
                body = json.loads(str(row[0]))
                if not isinstance(body, dict):
                    raise ValueError("workspace body is not a JSON object")
                bodies.append(body)
            return bodies


class ClickHouseDirectoryWriter:
    """Synchronous JSONEachRow inserts through the ClickHouse HTTP interface."""

    def __init__(self, *, url: str, password: str) -> None:
        self._url = url
        self._password = password

    def _insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payload = (
            "\n".join(json.dumps(row, separators=(",", ":"), sort_keys=True) for row in rows)
            + "\n"
        ).encode()
        separator = "&" if "?" in self._url else "?"
        endpoint = self._url + separator + urllib.parse.urlencode(
            {"query": f"INSERT INTO {table} FORMAT JSONEachRow"}
        )
        request = urllib.request.Request(  # noqa: S310 - configured node URL
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/x-ndjson",
                "X-ClickHouse-User": "tr",
                "X-ClickHouse-Key": self._password,
            },
        )
        with urllib.request.urlopen(request, timeout=300):  # noqa: S310 - configured node URL
            pass

    def upsert_directory(self, rows: list[dict[str, Any]]) -> None:
        self._insert(DIRECTORY_TABLE, rows)

    def upsert_tenant_map(self, rows: list[dict[str, Any]]) -> None:
        self._insert(MAP_TABLE, rows)


def _project_workspace(body: Mapping[str, Any], *, refreshed_at: dt.datetime) -> dict[str, Any]:
    workspace_id = str(body["id"])
    return {
        "tenant_id": analytics_surrogate("workspace", workspace_id),
        "workspace_id": workspace_id,
        "workspace_name": str(body["name"]),
        "deleted": int(bool(body.get("deleted", False))),
        "workspace_created_at": _clickhouse_datetime(body["created_at"], field="created_at"),
        "refreshed_at": _clickhouse_datetime(refreshed_at, field="refreshed_at"),
    }


def refresh_workspace_directory(
    source: WorkspaceSource,
    writer: DirectoryWriter | None,
    *,
    dry_run: bool = False,
    started_at: dt.datetime | None = None,
) -> RefreshResult:
    """Read, project, and insert one complete workspace directory snapshot."""
    job_start = _utc(started_at or dt.datetime.now(dt.UTC)).replace(microsecond=0)
    bodies = source.fetch()
    directory_rows = [_project_workspace(body, refreshed_at=job_start) for body in bodies]
    tenant_map_rows = [
        {"tenant_id": row["tenant_id"], "workspace_id": row["workspace_id"]}
        for row in directory_rows
    ]
    if not dry_run:
        if writer is None:
            raise ValueError("writer is required unless dry-run is selected")
        writer.upsert_directory(directory_rows)
        writer.upsert_tenant_map(tenant_map_rows)
    return RefreshResult(
        source_rows=len(bodies),
        directory_rows=len(directory_rows),
        tenant_map_rows=len(tenant_map_rows),
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", PROJECT))
    parser.add_argument(
        "--spanner-instance",
        default=os.environ.get("SPANNER_INSTANCE_ID", SPANNER_INSTANCE),
    )
    parser.add_argument(
        "--spanner-database",
        default=os.environ.get("SPANNER_DATABASE_ID", SPANNER_DATABASE),
    )
    parser.add_argument(
        "--clickhouse-url",
        default=os.environ.get("CLICKHOUSE_URL", CLICKHOUSE_URL),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    source: WorkspaceSource | None = None,
    writer: DirectoryWriter | None = None,
) -> int:
    args = _parse_args(argv)
    selected_source = source or SpannerWorkspaceSource(
        project=args.project,
        instance=args.spanner_instance,
        database=args.spanner_database,
    )
    selected_writer = writer
    if not args.dry_run and selected_writer is None:
        selected_writer = ClickHouseDirectoryWriter(
            url=args.clickhouse_url,
            password=os.environ["CH_PASSWORD"],
        )
    result = refresh_workspace_directory(
        selected_source,
        selected_writer,
        dry_run=args.dry_run,
        started_at=dt.datetime.now(dt.UTC),
    )
    print(
        f"source_rows={result.source_rows} "
        f"workspace_directory_rows={result.directory_rows} "
        f"tenant_workspace_map_rows={result.tenant_map_rows} "
        f"dry_run={str(args.dry_run).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
