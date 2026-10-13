"""Read-only, bounded export adapter. No money APIs or write operations.

Owns SDK parameters, credentials, clients, snapshots and streams. SQL remains
registered in the pure reader. Constructed only by the dormant export tool.
"""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

from trusted_router.storage_gcp_shadow_attachments import AuthRequest
from trusted_router.storage_gcp_shadow_attachments import Lifetime as Lifetime
from trusted_router.storage_gcp_shadow_io import (
    configure,
    observe_spanner_rpcs,
    owned_database,
    rpc_scope,
)
from trusted_router.storage_gcp_shadow_io import timestamp_ns as timestamp_ns

DATA_SCOPE = 'https://www.googleapis.com/auth/spanner.data'


def database_for_export(project: str, instance: str, database: str) -> Any:
    from google.cloud.spanner_v1 import Client
    # owned_database preserves these client options as well as credentials.
    client = Client(project=project, client_options={'scopes': [DATA_SCOPE]},
                    disable_builtin_metrics=True)
    return client.instance(instance).database(database)


def prepare(database: Any) -> Any:
    result = owned_database(database)
    configure(result)
    return result


@contextmanager
def open_page(database: Any, sql: str, params: Mapping[str, Any], snapshot_ns: int | None,
              deadline: float, clock: Callable[[], float], record_rpc: Callable[[int], None],
              convert: Callable[..., int], *, page_factory: Callable[..., Any],
              record_factory: Callable[..., Any], health_kind: str) -> Iterator[Any]:
    from google.api_core.datetime_helpers import DatetimeWithNanoseconds
    from google.cloud.spanner_v1 import param_types as pt
    from google.protobuf.timestamp_pb2 import Timestamp

    options = {} if snapshot_ns is None else {'read_timestamp': DatetimeWithNanoseconds.from_timestamp_pb(
        Timestamp(seconds=snapshot_ns // 1_000_000_000, nanos=snapshot_ns % 1_000_000_000))}
    types = {name: pt.INT64 if name == 'page_size' else pt.STRING for name in params}
    with observe_spanner_rpcs(record_rpc), rpc_scope(deadline, .2, clock):
        with database.snapshot(**options) as snapshot:
            stream = snapshot.execute_sql(sql, params=params, param_types=types, retry=None,
                timeout=max(.000001, deadline-clock()), request_options={'priority': 'PRIORITY_LOW'})

            def read_snapshot() -> int:
                return convert(stream.metadata.transaction.read_timestamp)

            rows = iter(stream)

            def records() -> Generator[Any, None, None]:
                for values in rows:
                    key, body, updated = values if 'day_start' in params else (params['id'], *values)

                    def metadata(updated: Any = updated) -> Mapping[str, Any]:
                        return dict(kind=params['kind'], updated_at_ns=convert(updated),
                            updated_at_semantics='store_commit_timestamp' if params['kind'] == health_kind else 'writer_updated_at',
                            read_snapshot_ns=read_snapshot())
                    yield record_factory(key, body, metadata)

            delivered = records()
            try:
                yield page_factory(delivered, read_snapshot)
            finally:
                # Finish the measured iterator while its page recorder is active.
                delivered.close()
                close_rows = getattr(rows, 'close', None)
                if close_rows is not None:
                    close_rows()
                response = getattr(stream, '_response_iterator', None)
                close = getattr(response, 'close', None)
                if close is not None:
                    close()


def authorize(lifetime: Any, url: str, headers: dict[str, str]) -> None:
    import google.auth
    auth = AuthRequest(lifetime)
    credentials, _ = google.auth.default(request=cast(Any, auth),
        scopes=['https://www.googleapis.com/auth/devstorage.read_only'])
    lifetime.remaining()
    credentials.before_request(auth, 'GET', url, headers)

