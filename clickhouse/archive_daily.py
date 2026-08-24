"""Archive closed ClickHouse analytics days as verified, immutable Parquet.

The manifest pointer is advanced only after the exported Parquet has been read
back and its row/content fingerprint matches the deduplicated ClickHouse source.
Reruns are cheap when the source fingerprint is unchanged and create a new,
immutable revision when reconciliation adds late rows.
"""

# ruff: noqa: S608
# SQL identifiers are allowlist-validated and all remaining query fragments are
# generated from typed dates, integer shard indexes, and a fixed column list.

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from clickhouse.ingest_operational_outbox import ACTIVITY_COLUMNS

PROJECT = "quill-cloud-proxy"
DATABASE = "tr"
TABLE = "provider_benchmark_samples"
ARCHIVE_BUCKET = "quill-cloud-proxy-tr-clickhouse-archive"
ARCHIVE_SCHEMA_VERSION = 1
ROWS_PER_PART = 5_000_000
UINT64_MODULUS = 1 << 64
_UNORDERED_MAP_COLUMNS = frozenset(
    {
        "latency_histogram",
        "ttfb_histogram",
        "dns_histogram",
        "tcp_connect_histogram",
        "tls_handshake_histogram",
        "gateway_processing_histogram",
        "error_counts",
    }
)
_DATETIME_MILLI_COLUMNS = frozenset({"period_start"})

log = logging.getLogger("trusted_router.analytics_archive")

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BENCHMARK_COLUMNS = (
    "id",
    "created_at",
    "provider",
    "model",
    "provider_name",
    "status",
    "usage_type",
    "source",
    "streamed",
    "input_tokens",
    "output_tokens",
    "total_cost_microdollars",
    "speed_tokens_per_second",
    "elapsed_milliseconds",
    "first_token_milliseconds",
    "ttfb_milliseconds",
    "finish_reason",
    "error_type",
    "error_status",
    "error_message",
    "region",
    "app",
)


@dataclasses.dataclass(frozen=True)
class DatasetSpec:
    columns: tuple[str, ...]
    time_column: str
    shard_column: str


# Raw client_request_events and client_minute_counters are deliberately absent:
# the telemetry contract retains their rollups, not raw client tables, in Parquet.
DATASETS: dict[str, DatasetSpec] = {
    "provider_benchmark_samples": DatasetSpec(
        columns=_BENCHMARK_COLUMNS,
        time_column="created_at",
        shard_column="id",
    ),
    "activity_generations": DatasetSpec(
        columns=ACTIVITY_COLUMNS,
        time_column="created_at",
        shard_column="generation_id",
    ),
    "synthetic_probe_samples": DatasetSpec(
        columns=(
            "id",
            "probe_type",
            "target",
            "target_url",
            "monitor_region",
            "status",
            "target_region",
            "latency_milliseconds",
            "ttfb_milliseconds",
            "dns_milliseconds",
            "tcp_connect_milliseconds",
            "tls_handshake_milliseconds",
            "gateway_processing_milliseconds",
            "connection_reused",
            "protocol",
            "http_status",
            "error_type",
            "provider",
            "model",
            "selected_provider",
            "selected_model",
            "generation_id",
            "attestation_digest",
            "source_commit",
            "cost_microdollars",
            "output_match",
            "created_at",
        ),
        time_column="created_at",
        shard_column="id",
    ),
    "synthetic_status_rollups": DatasetSpec(
        columns=(
            "id",
            "period",
            "period_start",
            "component",
            "target",
            "probe_type",
            "monitor_region",
            "target_region",
            "sample_count",
            "up_count",
            "down_count",
            "degraded_count",
            "routing_degraded_count",
            "trust_degraded_count",
            "unknown_count",
            "latency_histogram",
            "ttfb_histogram",
            "dns_histogram",
            "tcp_connect_histogram",
            "tls_handshake_histogram",
            "gateway_processing_histogram",
            "error_counts",
            "last_checked_at",
            "cost_microdollars",
        ),
        time_column="period_start",
        shard_column="id",
    ),
}


def _identifier(value: str, *, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a ClickHouse identifier")
    return value


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _day_bounds(day: dt.date) -> tuple[str, str]:
    start = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
    return (
        start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        (start + dt.timedelta(days=1))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    )


def _row_hash_expression(columns: Sequence[str]) -> str:
    def canonical_column(column: str) -> str:
        if column in _UNORDERED_MAP_COLUMNS:
            return f"mapSort({column})"
        if column in _DATETIME_MILLI_COLUMNS:
            return (
                "toUnixTimestamp64Milli("
                f"toDateTime64({column}, 3, 'UTC'))"
            )
        return column

    canonical = (canonical_column(column) for column in columns)
    return "cityHash64(toJSONString(tuple(" + ", ".join(canonical) + ")))"


@dataclasses.dataclass(frozen=True)
class SourceFingerprint:
    rows: int
    hash_sum: int
    hash_xor: int
    min_created_at: str | None
    max_created_at: str | None

    @property
    def revision(self) -> str:
        return (
            f"v{ARCHIVE_SCHEMA_VERSION}-{self.rows}-"
            f"{self.hash_sum:016x}-{self.hash_xor:016x}"
        )

    def matches(self, other: SourceFingerprint) -> bool:
        return (
            self.rows == other.rows
            and self.hash_sum == other.hash_sum
            and self.hash_xor == other.hash_xor
        )

    def as_dict(self) -> dict[str, int | str | None]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceFingerprint:
        return cls(
            rows=int(value["rows"]),
            hash_sum=int(value["hash_sum"]),
            hash_xor=int(value["hash_xor"]),
            min_created_at=_nullable_string(value.get("min_created_at")),
            max_created_at=_nullable_string(value.get("max_created_at")),
        )


@dataclasses.dataclass(frozen=True)
class ExportedPart:
    path: Path
    rows: int
    hash_sum: int
    hash_xor: int


@dataclasses.dataclass(frozen=True)
class ArchiveResult:
    day: dt.date
    rows: int
    revision: str
    skipped: bool
    manifest_key: str


class DailyExporter(Protocol):
    def source_fingerprint(self, day: dt.date) -> SourceFingerprint: ...

    def export_parts(
        self,
        day: dt.date,
        destination: Path,
        *,
        part_count: int,
    ) -> list[Path]: ...

    def verify_part(self, path: Path) -> ExportedPart: ...


class ArchiveStore(Protocol):
    def read_json(self, key: str) -> dict[str, Any] | None: ...

    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        metadata: dict[str, str],
    ) -> None: ...

    def put_json_if_absent(self, key: str, value: dict[str, Any]) -> None: ...

    def put_json_pointer(self, key: str, value: dict[str, Any]) -> None: ...

    # The restore drill reads the archive back through this same store, so a
    # cloud that can be written but not read would only fail at drill time.
    def download_file(self, key: str, destination: Path) -> None: ...


class ClickHouseDailyExporter:
    def __init__(
        self,
        *,
        password: str,
        database: str = DATABASE,
        table: str = TABLE,
        user: str = "tr",
    ) -> None:
        self._password = password
        self._database = _identifier(database, label="database")
        self._table = _identifier(table, label="table")
        # The user was hardcoded to "tr" while the database was already a
        # parameter, so this exporter silently only worked on the GCP cluster --
        # the same latent bug ClickHouseOperationalWriter carried. The AWS-EU
        # node authenticates as "default" into database "default" (its schema is
        # applied unqualified), and an explicit --user beats CLICKHOUSE_USER in
        # the environment, so this could not have been corrected from the unit
        # file. Archiving another cloud requires it to be a parameter.
        self._user = _identifier(user, label="user")
        try:
            self._spec = DATASETS[self._table]
        except KeyError:
            raise ValueError(f"unsupported archive dataset: {self._table}") from None

    @property
    def dataset(self) -> str:
        return self._table

    @property
    def qualified_table(self) -> str:
        return f"{self._database}.{self._table}"

    def _client(self, query: str, *, stdout: Any = subprocess.PIPE) -> bytes:
        env = os.environ.copy()
        env["CLICKHOUSE_PASSWORD"] = self._password
        result = subprocess.run(  # noqa: S603 - fixed executable and argv, no shell
            [
                "/usr/bin/clickhouse-client",
                "--user",
                self._user,
                "--database",
                self._database,
                "--query",
                query,
            ],
            env=env,
            stdout=stdout,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"ClickHouse archive query failed: {detail}")
        return result.stdout if isinstance(result.stdout, bytes) else b""

    def source_fingerprint(self, day: dt.date) -> SourceFingerprint:
        start, end = _day_bounds(day)
        query = _fingerprint_query(
            self.qualified_table,
            where=(
                f"{self._spec.time_column} >= "
                f"toDateTime64({_sql_string(start)}, 3, 'UTC') "
                f"AND {self._spec.time_column} < "
                f"toDateTime64({_sql_string(end)}, 3, 'UTC')"
            ),
            final=True,
            columns=self._spec.columns,
            time_column=self._spec.time_column,
        )
        return _parse_fingerprint(self._client(query))

    def earliest_day(self) -> dt.date | None:
        payload = self._client(
            "SELECT if(count() = 0, '', "
            f"toString(toDate(min({self._spec.time_column})))) "
            f"FROM {self.qualified_table} FINAL"
        )
        value = payload.decode("utf-8").strip().splitlines()[-1]
        return dt.date.fromisoformat(value) if value else None

    def export_parts(
        self,
        day: dt.date,
        destination: Path,
        *,
        part_count: int,
    ) -> list[Path]:
        destination.mkdir(parents=True, exist_ok=True)
        start, end = _day_bounds(day)
        paths: list[Path] = []
        for part in range(part_count):
            path = destination / f"part-{part:05d}-of-{part_count:05d}.parquet"
            where = (
                f"{self._spec.time_column} >= "
                f"toDateTime64({_sql_string(start)}, 3, 'UTC') "
                f"AND {self._spec.time_column} < "
                f"toDateTime64({_sql_string(end)}, 3, 'UTC') "
                f"AND cityHash64({self._spec.shard_column}) % {part_count} = {part}"
            )
            query = (  # noqa: S608 - identifiers are validated; values are generated.
                f"SELECT {', '.join(self._spec.columns)} "
                f"FROM {self.qualified_table} FINAL WHERE {where} "
                f"ORDER BY {self._spec.time_column}, {self._spec.shard_column} "
                "FORMAT Parquet"
            )
            with path.open("wb") as stream:
                self._client(query, stdout=stream)
            paths.append(path)
        return paths

    def verify_part(self, path: Path) -> ExportedPart:
        query = _fingerprint_query(
            f"file({_sql_string(str(path))}, Parquet)",
            where=None,
            final=False,
            columns=self._spec.columns,
            time_column=self._spec.time_column,
        )
        fingerprint = _parse_fingerprint(_run_clickhouse_local(query))
        return ExportedPart(
            path=path,
            rows=fingerprint.rows,
            hash_sum=fingerprint.hash_sum,
            hash_xor=fingerprint.hash_xor,
        )


class GCSArchiveStore:
    scheme = "gs"

    def __init__(self, *, project: str, bucket: str) -> None:
        import google.cloud.storage as gcs_storage

        self._bucket = gcs_storage.Client(project=project).bucket(bucket)

    def read_json(self, key: str) -> dict[str, Any] | None:
        blob = self._bucket.blob(key)
        if not blob.exists():
            return None
        value = json.loads(blob.download_as_text())
        if not isinstance(value, dict):
            raise RuntimeError(f"archive object {key} is not a JSON object")
        return value

    def download_file(self, key: str, destination: Path) -> None:
        self._bucket.blob(key).download_to_filename(str(destination))

    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        metadata: dict[str, str],
    ) -> None:
        from google.api_core.exceptions import PreconditionFailed

        blob = self._bucket.blob(key)
        blob.metadata = {**metadata, "sha256": sha256}
        try:
            blob.upload_from_filename(
                str(path),
                content_type="application/vnd.apache.parquet",
                if_generation_match=0,
            )
        except PreconditionFailed:
            blob.reload()
            if (blob.metadata or {}).get("sha256") != sha256:
                raise RuntimeError(
                    f"immutable archive object differs: gs://{self._bucket.name}/{key}"
                ) from None

    def put_json_if_absent(self, key: str, value: dict[str, Any]) -> None:
        from google.api_core.exceptions import PreconditionFailed

        encoded = _json_bytes(value)
        blob = self._bucket.blob(key)
        try:
            blob.upload_from_string(
                encoded,
                content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed:
            if blob.download_as_bytes() != encoded:
                raise RuntimeError(
                    f"immutable archive manifest differs: gs://{self._bucket.name}/{key}"
                ) from None

    def put_json_pointer(self, key: str, value: dict[str, Any]) -> None:
        blob = self._bucket.blob(key)
        generation = 0
        if blob.exists():
            blob.reload()
            generation = int(blob.generation or 0)
        blob.upload_from_string(
            _json_bytes(value),
            content_type="application/json",
            if_generation_match=generation,
        )


class S3ArchiveStore:
    """The same immutable archive on S3, for the AWS deployment.

    S3's conditional writes carry the same meaning as the GCS store's
    generation preconditions: ``IfNoneMatch="*"`` is create-if-absent, and a
    pointer move is a compare-and-set on the current ETag.  So a 412 here is
    a concurrent writer or a genuine content difference -- never a transport
    error -- and is resolved by re-reading and comparing, exactly as the GCS
    store resolves ``PreconditionFailed``, rather than by retrying.

    A retry would be the dangerous choice: retrying an unconditional write is
    how an "immutable" archive quietly stops being immutable.
    """

    scheme = "s3"

    def __init__(self, *, bucket: str, region: str | None = None) -> None:
        import boto3

        self._bucket = bucket
        self._client = boto3.client("s3", region_name=region)

    def _uri(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    @staticmethod
    def _is_precondition_failure(error: Any) -> bool:
        response = getattr(error, "response", {}) or {}
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        # ConditionalRequestConflict is S3's 409 for two conditional writes
        # racing on the same key; it means the same thing as a 412 here.
        return code in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {409, 412}

    @staticmethod
    def _is_missing(error: Any) -> bool:
        response = getattr(error, "response", {}) or {}
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in {"NoSuchKey", "404", "NotFound"} or status == 404

    def read_json(self, key: str) -> dict[str, Any] | None:
        from botocore.exceptions import ClientError

        try:
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as error:
            if self._is_missing(error):
                return None
            raise
        value = json.loads(body)
        if not isinstance(value, dict):
            raise RuntimeError(f"archive object {key} is not a JSON object")
        return value

    def _stored_sha256(self, key: str) -> str | None:
        # S3 lowercases user metadata keys on the way out.
        head = self._client.head_object(Bucket=self._bucket, Key=key)
        metadata = {str(k).lower(): v for k, v in (head.get("Metadata") or {}).items()}
        return metadata.get("sha256")

    def download_file(self, key: str, destination: Path) -> None:
        self._client.download_file(self._bucket, key, str(destination))

    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        metadata: dict[str, str],
    ) -> None:
        from botocore.exceptions import ClientError

        with path.open("rb") as stream:
            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=stream,
                    ContentType="application/vnd.apache.parquet",
                    Metadata={**metadata, "sha256": sha256},
                    IfNoneMatch="*",
                )
            except ClientError as error:
                if not self._is_precondition_failure(error):
                    raise
                if self._stored_sha256(key) != sha256:
                    raise RuntimeError(
                        f"immutable archive object differs: {self._uri(key)}"
                    ) from None

    def put_json_if_absent(self, key: str, value: dict[str, Any]) -> None:
        from botocore.exceptions import ClientError

        encoded = _json_bytes(value)
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=encoded,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if not self._is_precondition_failure(error):
                raise
            existing = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
            if existing != encoded:
                raise RuntimeError(
                    f"immutable archive manifest differs: {self._uri(key)}"
                ) from None

    def put_json_pointer(self, key: str, value: dict[str, Any]) -> None:
        from botocore.exceptions import ClientError

        encoded = _json_bytes(value)
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if not self._is_missing(error):
                raise
            head = None
        condition = {"IfNoneMatch": "*"} if head is None else {"IfMatch": head["ETag"]}
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=encoded,
            ContentType="application/json",
            **condition,
        )


class AzureBlobArchiveStore:
    """The same immutable archive on Azure Blob Storage.

    ``overwrite=False`` is Azure's create-if-absent, and a pointer move is an
    ``If-Match`` on the current ETag, so the preconditions line up with the
    other two stores.  ``ResourceExistsError`` and
    ``ResourceModifiedError`` are the conflict signals, and both are resolved
    by comparison rather than retry for the reason given on
    :class:`S3ArchiveStore`.
    """

    scheme = "azure"

    def __init__(self, *, account_url: str, container: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self._container_name = container
        self._container = BlobServiceClient(
            account_url=account_url,
            credential=DefaultAzureCredential(),
        ).get_container_client(container)

    def _uri(self, key: str) -> str:
        return f"azure://{self._container_name}/{key}"

    def read_json(self, key: str) -> dict[str, Any] | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            body = self._container.get_blob_client(key).download_blob().readall()
        except ResourceNotFoundError:
            return None
        value = json.loads(body)
        if not isinstance(value, dict):
            raise RuntimeError(f"archive object {key} is not a JSON object")
        return value

    def download_file(self, key: str, destination: Path) -> None:
        with destination.open("wb") as stream:
            self._container.get_blob_client(key).download_blob().readinto(stream)

    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        metadata: dict[str, str],
    ) -> None:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        blob = self._container.get_blob_client(key)
        with path.open("rb") as stream:
            try:
                blob.upload_blob(
                    stream,
                    overwrite=False,
                    metadata={**metadata, "sha256": sha256},
                    content_settings=ContentSettings(
                        content_type="application/vnd.apache.parquet"
                    ),
                )
            except ResourceExistsError:
                existing = blob.get_blob_properties().metadata or {}
                if existing.get("sha256") != sha256:
                    raise RuntimeError(
                        f"immutable archive object differs: {self._uri(key)}"
                    ) from None

    def put_json_if_absent(self, key: str, value: dict[str, Any]) -> None:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        encoded = _json_bytes(value)
        blob = self._container.get_blob_client(key)
        try:
            blob.upload_blob(
                encoded,
                overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except ResourceExistsError:
            if blob.download_blob().readall() != encoded:
                raise RuntimeError(
                    f"immutable archive manifest differs: {self._uri(key)}"
                ) from None

    def put_json_pointer(self, key: str, value: dict[str, Any]) -> None:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceNotFoundError
        from azure.storage.blob import ContentSettings

        encoded = _json_bytes(value)
        blob = self._container.get_blob_client(key)
        settings = ContentSettings(content_type="application/json")
        try:
            etag = blob.get_blob_properties().etag
        except ResourceNotFoundError:
            blob.upload_blob(encoded, overwrite=False, content_settings=settings)
            return
        blob.upload_blob(
            encoded,
            overwrite=True,
            content_settings=settings,
            etag=etag,
            match_condition=MatchConditions.IfNotModified,
        )


def build_archive_store(
    kind: str,
    *,
    project: str,
    bucket: str,
    region: str | None = None,
    account_url: str | None = None,
) -> ArchiveStore:
    """Select this deployment's object store.

    Each cloud archives to its own object store in its own account.  That is
    the point rather than an accident: an archive written across a cloud
    boundary would make the cloud it describes non-recoverable exactly when
    the *other* cloud is the one that is unreachable.
    """

    if kind == "gcs":
        return GCSArchiveStore(project=project, bucket=bucket)
    # ARCHIVE_BUCKET names a GCP bucket. Reaching another cloud's store while
    # still carrying it means --bucket was never set, and the useful failure
    # is here rather than a 404 that reads like a permissions problem.
    if bucket == ARCHIVE_BUCKET:
        raise ValueError(f"--bucket must name this deployment's {kind} bucket, not {bucket!r}")
    if kind == "s3":
        return S3ArchiveStore(bucket=bucket, region=region)
    if kind == "azure":
        if not account_url:
            raise ValueError("--account-url is required for the azure object store")
        return AzureBlobArchiveStore(account_url=account_url, container=bucket)
    raise ValueError(f"unsupported archive object store: {kind}")


def _nullable_string(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _fingerprint_query(
    table_expression: str,
    *,
    where: str | None,
    final: bool,
    columns: Sequence[str] = _BENCHMARK_COLUMNS,
    time_column: str = "created_at",
) -> str:
    suffix = " FINAL" if final else ""
    where_clause = f" WHERE {where}" if where else ""
    row_hash = _row_hash_expression(columns)
    return (  # noqa: S608 - table expressions are validated or locally generated.
        "SELECT count() AS rows, "
        f"sum({row_hash}) AS hash_sum, "
        f"groupBitXor({row_hash}) AS hash_xor, "
        f"if(count() = 0, NULL, toString(min({time_column}))) AS min_created_at, "
        f"if(count() = 0, NULL, toString(max({time_column}))) AS max_created_at "
        f"FROM {table_expression}{suffix}{where_clause} FORMAT JSONEachRow"
    )


def _parse_fingerprint(payload: bytes) -> SourceFingerprint:
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("ClickHouse fingerprint response was not an object")
    return SourceFingerprint.from_dict(value)


def _run_clickhouse_local(query: str) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed executable and argv, no shell
        ["/usr/bin/clickhouse-local", "--query", query],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"Parquet verification failed: {detail}")
    return result.stdout


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _combine_parts(parts: Sequence[ExportedPart]) -> SourceFingerprint:
    rows = sum(part.rows for part in parts)
    hash_sum = sum(part.hash_sum for part in parts) % UINT64_MODULUS
    hash_xor = 0
    for part in parts:
        hash_xor ^= part.hash_xor
    return SourceFingerprint(
        rows=rows,
        hash_sum=hash_sum,
        hash_xor=hash_xor,
        min_created_at=None,
        max_created_at=None,
    )


def archive_day(
    exporter: DailyExporter,
    store: ArchiveStore,
    day: dt.date,
    *,
    rows_per_part: int = ROWS_PER_PART,
    now: dt.datetime | None = None,
    dataset: str = TABLE,
) -> ArchiveResult:
    if rows_per_part < 1:
        raise ValueError("rows_per_part must be positive")
    if dataset not in DATASETS:
        raise ValueError(f"unsupported archive dataset: {dataset}")
    source = exporter.source_fingerprint(day)
    date_prefix = f"raw/{dataset}/day={day.isoformat()}"
    pointer_key = f"{date_prefix}/_latest.json"
    latest = store.read_json(pointer_key)
    if latest is not None:
        prior = SourceFingerprint.from_dict(dict(latest["source_fingerprint"]))
        if source.matches(prior):
            return ArchiveResult(
                day=day,
                rows=source.rows,
                revision=str(latest["revision"]),
                skipped=True,
                manifest_key=str(latest["manifest"]),
            )

    part_count = math.ceil(source.rows / rows_per_part) if source.rows else 0
    revision = source.revision
    revision_prefix = f"{date_prefix}/revisions/{revision}"
    manifest_key = f"{revision_prefix}/manifest.json"
    exported_at = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    manifest_parts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=f"tr-archive-{day.isoformat()}-") as temporary:
        paths = exporter.export_parts(day, Path(temporary), part_count=part_count)
        if len(paths) != part_count:
            raise RuntimeError(f"expected {part_count} Parquet parts, exporter returned {len(paths)}")
        verified = [exporter.verify_part(path) for path in paths]
        actual = _combine_parts(verified)
        if not source.matches(actual):
            raise RuntimeError(
                "archive parity mismatch: "
                f"source rows/hash={source.rows}/{source.hash_sum}/{source.hash_xor}, "
                f"parquet rows/hash={actual.rows}/{actual.hash_sum}/{actual.hash_xor}"
            )
        for index, part in enumerate(verified):
            sha256 = _sha256(part.path)
            key = f"{revision_prefix}/{part.path.name}"
            store.put_file_if_absent(
                key,
                part.path,
                sha256=sha256,
                metadata={
                    "archive-day": day.isoformat(),
                    "archive-revision": revision,
                    "rows": str(part.rows),
                    "part": str(index),
                },
            )
            manifest_parts.append(
                {
                    "key": key,
                    "rows": part.rows,
                    "bytes": part.path.stat().st_size,
                    "sha256": sha256,
                    "hash_sum": part.hash_sum,
                    "hash_xor": part.hash_xor,
                }
            )

    manifest: dict[str, Any] = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "dataset": dataset,
        "day": day.isoformat(),
        "revision": revision,
        "exported_at": exported_at.isoformat().replace("+00:00", "Z"),
        "source_fingerprint": source.as_dict(),
        # The exact column list the fingerprint was computed over. Column
        # additions (activity_generations grew client_* columns) change the
        # fingerprint expression, so a verifier must recompute against the
        # columns of THIS revision, not whatever the current schema says.
        "columns": list(DATASETS[dataset].columns),
        "parts": manifest_parts,
        "parquet_rows": sum(int(part["rows"]) for part in manifest_parts),
    }
    store.put_json_if_absent(manifest_key, manifest)
    store.put_json_pointer(
        pointer_key,
        {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "day": day.isoformat(),
            "revision": revision,
            "manifest": manifest_key,
            "source_fingerprint": source.as_dict(),
            "updated_at": exported_at.isoformat().replace("+00:00", "Z"),
        },
    )
    return ArchiveResult(
        day=day,
        rows=source.rows,
        revision=revision,
        skipped=False,
        manifest_key=manifest_key,
    )


def _days_to_archive(
    *,
    date: dt.date | None,
    lookback_days: int,
    backfill_start: dt.date | None = None,
) -> list[dt.date]:
    if date is not None:
        return [date]
    if backfill_start is None and lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    today = dt.datetime.now(dt.UTC).date()
    if backfill_start is not None:
        if backfill_start >= today:
            return []
        return [
            backfill_start + dt.timedelta(days=offset)
            for offset in range((today - backfill_start).days)
        ]
    return [today - dt.timedelta(days=offset) for offset in range(lookback_days, 0, -1)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", PROJECT))
    parser.add_argument("--bucket", default=os.environ.get("ARCHIVE_BUCKET", ARCHIVE_BUCKET))
    parser.add_argument("--database", default=os.environ.get("TR_CLICKHOUSE_DATABASE", DATABASE))
    # The AWS-EU node authenticates as "default"; see ClickHouseDailyExporter.
    parser.add_argument("--clickhouse-user", default=os.environ.get("TR_CLICKHOUSE_USER", "tr"))
    parser.add_argument("--table", action="append", choices=tuple(DATASETS))
    parser.add_argument("--date", type=dt.date.fromisoformat)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--rows-per-part", type=int, default=ROWS_PER_PART)
    # Which cloud this node archives to. Defaults to gcs so the existing
    # GCP units keep their current behaviour with no argv change.
    parser.add_argument(
        "--object-store",
        choices=("gcs", "s3", "azure"),
        default=os.environ.get("TR_ARCHIVE_OBJECT_STORE", "gcs"),
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    parser.add_argument("--account-url", default=os.environ.get("AZURE_STORAGE_ACCOUNT_URL"))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = build_archive_store(
        args.object_store,
        project=args.project,
        bucket=args.bucket,
        region=args.region,
        account_url=args.account_url,
    )
    tables = tuple(dict.fromkeys(args.table or tuple(DATASETS)))
    for table in tables:
        exporter = ClickHouseDailyExporter(
            password=os.environ["CH_PASSWORD"],
            database=args.database,
            table=table,
            user=args.clickhouse_user,
        )
        backfill_start = (
            exporter.earliest_day() if args.backfill and args.date is None else None
        )
        for day in _days_to_archive(
            date=args.date,
            lookback_days=args.lookback_days,
            backfill_start=backfill_start,
        ):
            result = archive_day(
                exporter,
                store,
                day,
                rows_per_part=args.rows_per_part,
                dataset=table,
            )
            log.info(
                "analytics_archive.completed dataset=%s day=%s rows=%d "
                "revision=%s skipped=%s manifest=%s://%s/%s",
                table,
                result.day,
                result.rows,
                result.revision,
                result.skipped,
                getattr(store, "scheme", "gs"),
                args.bucket,
                result.manifest_key,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
