"""Per-cloud archive stores.

Each cloud archives to its own object store, so the immutability guarantee is
implemented three times against three different sets of preconditions.  These
tests exist because that guarantee is *only* the precondition: an
unconditional ``put_object`` would pass every functional test in the archive
suite and silently make the archive mutable.

So the fakes here enforce the conditional semantics rather than record them --
``IfNoneMatch``/``overwrite`` actually reject a second write.  Deleting a
precondition from the store under test fails these tests instead of quietly
weakening the archive.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from clickhouse.archive_daily import (
    ARCHIVE_BUCKET,
    AzureBlobArchiveStore,
    S3ArchiveStore,
    build_archive_store,
)


def _client_error(code: str, status: int) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "PutObject",
    )


class FakeS3:
    """A fake that honours the conditional writes rather than logging them."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.unconditional_writes: list[str] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        body = kwargs["Body"]
        payload = body.read() if hasattr(body, "read") else body
        exists = key in self.objects
        if "IfNoneMatch" in kwargs:
            if kwargs["IfNoneMatch"] != "*":
                raise AssertionError("IfNoneMatch must be '*'")
            if exists:
                raise _client_error("PreconditionFailed", 412)
        elif "IfMatch" in kwargs:
            if not exists:
                raise _client_error("PreconditionFailed", 412)
            if kwargs["IfMatch"] != self.objects[key]["ETag"]:
                raise _client_error("PreconditionFailed", 412)
        else:
            # Nothing in this module may write without a precondition.
            self.unconditional_writes.append(key)
        self.objects[key] = {
            "Body": payload,
            "Metadata": {str(k).title(): v for k, v in (kwargs.get("Metadata") or {}).items()},
            "ETag": f'"etag-{len(self.objects)}-{len(payload)}"',
        }
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        del Bucket
        if Key not in self.objects:
            raise _client_error("NoSuchKey", 404)
        return {"Body": _Reader(self.objects[Key]["Body"])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        del Bucket
        if Key not in self.objects:
            raise _client_error("NotFound", 404)
        return self.objects[Key]


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    import boto3

    fake = FakeS3()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    return fake


def _store(fake: FakeS3) -> S3ArchiveStore:
    del fake
    return S3ArchiveStore(bucket="tr-archive-eu", region="eu-west-1")


def test_s3_file_write_is_conditional_and_idempotent(s3: FakeS3, tmp_path: Path) -> None:
    store = _store(s3)
    part = tmp_path / "part.parquet"
    part.write_bytes(b"rows")

    store.put_file_if_absent("day/part.parquet", part, sha256="abc", metadata={"day": "2026-08-16"})
    # Re-running the archiver for an already-archived day must be a no-op,
    # not a rewrite and not an error.
    store.put_file_if_absent("day/part.parquet", part, sha256="abc", metadata={"day": "2026-08-16"})

    assert s3.objects["day/part.parquet"]["Body"] == b"rows"
    assert s3.unconditional_writes == []


def test_s3_rejects_a_changed_object_under_an_archived_key(s3: FakeS3, tmp_path: Path) -> None:
    store = _store(s3)
    part = tmp_path / "part.parquet"
    part.write_bytes(b"rows")
    store.put_file_if_absent("k", part, sha256="abc", metadata={})

    part.write_bytes(b"different")
    with pytest.raises(RuntimeError, match="immutable archive object differs"):
        store.put_file_if_absent("k", part, sha256="def", metadata={})


def test_s3_manifest_conflict_compares_content(s3: FakeS3) -> None:
    store = _store(s3)
    store.put_json_if_absent("m.json", {"rows": 1})
    store.put_json_if_absent("m.json", {"rows": 1})

    with pytest.raises(RuntimeError, match="immutable archive manifest differs"):
        store.put_json_if_absent("m.json", {"rows": 2})


def test_s3_pointer_is_compare_and_set(s3: FakeS3) -> None:
    store = _store(s3)
    store.put_json_pointer("_latest.json", {"day": "2026-08-15"})
    store.put_json_pointer("_latest.json", {"day": "2026-08-16"})

    assert store.read_json("_latest.json") == {"day": "2026-08-16"}
    # The pointer is the one mutable object, but it still moves under a
    # precondition so two archivers cannot interleave a lost update.
    assert s3.unconditional_writes == []


def test_s3_read_json_absent_and_malformed(s3: FakeS3) -> None:
    store = _store(s3)
    assert store.read_json("missing.json") is None

    s3.objects["bad.json"] = {"Body": b"[1, 2]", "Metadata": {}, "ETag": '"e"'}
    with pytest.raises(RuntimeError, match="not a JSON object"):
        store.read_json("bad.json")


def test_s3_reads_metadata_case_insensitively(s3: FakeS3, tmp_path: Path) -> None:
    """S3 lowercases user metadata; a case-sensitive lookup would read the
    sha256 as absent and report every rerun as an immutability violation."""

    store = _store(s3)
    part = tmp_path / "p"
    part.write_bytes(b"x")
    store.put_file_if_absent("k", part, sha256="abc", metadata={})
    assert "Sha256" in s3.objects["k"]["Metadata"]  # stored title-cased by the fake

    store.put_file_if_absent("k", part, sha256="abc", metadata={})


# --------------------------------------------------------------------------
# Azure. The SDK is installed only on the Azure nodes (requirements-live.txt),
# so the module is injected here. The store's own logic is still the code
# under test -- only the transport is fake.
# --------------------------------------------------------------------------


class _AzureResourceExists(Exception):
    pass


class _AzureResourceNotFound(Exception):
    pass


class FakeBlob:
    def __init__(self, container: FakeContainer, key: str) -> None:
        self._container = container
        self._key = key

    def upload_blob(self, data: Any, **kwargs: Any) -> None:
        payload = data.read() if hasattr(data, "read") else data
        exists = self._key in self._container.blobs
        if not kwargs.get("overwrite", False):
            if exists:
                raise _AzureResourceExists(self._key)
        elif kwargs.get("match_condition") is None:
            self._container.unconditional_writes.append(self._key)
        self._container.blobs[self._key] = {
            "data": payload,
            "metadata": kwargs.get("metadata") or {},
            "etag": f"etag-{len(self._container.blobs)}",
        }

    def download_blob(self) -> Any:
        if self._key not in self._container.blobs:
            raise _AzureResourceNotFound(self._key)
        return types.SimpleNamespace(readall=lambda: self._container.blobs[self._key]["data"])

    def get_blob_properties(self) -> Any:
        if self._key not in self._container.blobs:
            raise _AzureResourceNotFound(self._key)
        record = self._container.blobs[self._key]
        return types.SimpleNamespace(metadata=record["metadata"], etag=record["etag"])


class FakeContainer:
    def __init__(self) -> None:
        self.blobs: dict[str, dict[str, Any]] = {}
        self.unconditional_writes: list[str] = []

    def get_blob_client(self, key: str) -> FakeBlob:
        return FakeBlob(self, key)


@pytest.fixture
def azure_container(monkeypatch: pytest.MonkeyPatch) -> FakeContainer:
    container = FakeContainer()

    def _module(name: str, **attrs: Any) -> types.ModuleType:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        # monkeypatch.setitem restores sys.modules after the test, so the
        # fakes cannot leak into another test's imports.
        monkeypatch.setitem(sys.modules, name, module)
        return module

    _module("azure")
    _module("azure.core", MatchConditions=types.SimpleNamespace(IfNotModified="if-not-modified"))
    _module(
        "azure.core.exceptions",
        ResourceExistsError=_AzureResourceExists,
        ResourceNotFoundError=_AzureResourceNotFound,
    )
    _module("azure.identity", DefaultAzureCredential=lambda *a, **k: object())
    _module(
        "azure.storage",
    )
    _module(
        "azure.storage.blob",
        BlobServiceClient=lambda **kwargs: types.SimpleNamespace(
            get_container_client=lambda name: container
        ),
        ContentSettings=lambda **kwargs: types.SimpleNamespace(**kwargs),
    )
    return container


def _azure_store() -> AzureBlobArchiveStore:
    return AzureBlobArchiveStore(
        account_url="https://tracct.blob.core.windows.net",
        container="tr-archive",
    )


def test_azure_file_write_is_conditional_and_idempotent(
    azure_container: FakeContainer, tmp_path: Path
) -> None:
    store = _azure_store()
    part = tmp_path / "part.parquet"
    part.write_bytes(b"rows")

    store.put_file_if_absent("k", part, sha256="abc", metadata={"day": "2026-08-16"})
    store.put_file_if_absent("k", part, sha256="abc", metadata={"day": "2026-08-16"})

    assert azure_container.blobs["k"]["data"] == b"rows"
    assert azure_container.unconditional_writes == []


def test_azure_rejects_a_changed_object_under_an_archived_key(
    azure_container: FakeContainer, tmp_path: Path
) -> None:
    store = _azure_store()
    part = tmp_path / "p"
    part.write_bytes(b"rows")
    store.put_file_if_absent("k", part, sha256="abc", metadata={})

    with pytest.raises(RuntimeError, match="immutable archive object differs"):
        store.put_file_if_absent("k", part, sha256="def", metadata={})


def test_azure_manifest_conflict_compares_content(azure_container: FakeContainer) -> None:
    store = _azure_store()
    store.put_json_if_absent("m.json", {"rows": 1})
    store.put_json_if_absent("m.json", {"rows": 1})

    with pytest.raises(RuntimeError, match="immutable archive manifest differs"):
        store.put_json_if_absent("m.json", {"rows": 2})


def test_azure_pointer_moves_under_an_etag_precondition(azure_container: FakeContainer) -> None:
    store = _azure_store()
    store.put_json_pointer("_latest.json", {"day": "2026-08-15"})
    store.put_json_pointer("_latest.json", {"day": "2026-08-16"})

    assert store.read_json("_latest.json") == {"day": "2026-08-16"}
    assert azure_container.unconditional_writes == []


def test_azure_read_json_absent_and_malformed(azure_container: FakeContainer) -> None:
    store = _azure_store()
    assert store.read_json("missing.json") is None

    azure_container.blobs["bad.json"] = {"data": b"[]", "metadata": {}, "etag": "e"}
    with pytest.raises(RuntimeError, match="not a JSON object"):
        store.read_json("bad.json")


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def test_build_archive_store_rejects_the_gcp_bucket_on_other_clouds() -> None:
    """A unit that sets --object-store but forgets --bucket would otherwise
    address a GCP bucket name in another cloud's namespace."""

    for kind in ("s3", "azure"):
        with pytest.raises(ValueError, match="must name this deployment"):
            build_archive_store(
                kind,
                project="quill-cloud-proxy",
                bucket=ARCHIVE_BUCKET,
                region="eu-west-1",
                account_url="https://tracct.blob.core.windows.net",
            )


def test_build_archive_store_requires_an_account_url_for_azure() -> None:
    with pytest.raises(ValueError, match="account-url is required"):
        build_archive_store("azure", project="p", bucket="tr-archive", account_url=None)


def test_build_archive_store_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unsupported archive object store"):
        build_archive_store("ceph", project="p", bucket="b")


def test_build_archive_store_selects_s3(s3: FakeS3) -> None:
    store = build_archive_store("s3", project="p", bucket="tr-archive-eu", region="eu-west-1")
    assert isinstance(store, S3ArchiveStore)
    assert store.scheme == "s3"


def test_build_archive_store_selects_azure(azure_container: FakeContainer) -> None:
    store = build_archive_store(
        "azure",
        project="p",
        bucket="tr-archive",
        account_url="https://tracct.blob.core.windows.net",
    )
    assert isinstance(store, AzureBlobArchiveStore)
    assert store.scheme == "azure"


def test_every_store_satisfies_the_archive_store_protocol(
    s3: FakeS3, azure_container: FakeContainer
) -> None:
    """archive_day() is typed against ArchiveStore; a backend missing a method
    would only fail at the end of a real export."""

    from clickhouse.archive_daily import ArchiveStore, GCSArchiveStore

    required = [name for name in dir(ArchiveStore) if not name.startswith("_")]
    assert set(required) >= {
        "read_json",
        "put_file_if_absent",
        "put_json_if_absent",
        "put_json_pointer",
    }
    for cls in (GCSArchiveStore, S3ArchiveStore, AzureBlobArchiveStore):
        for name in required:
            assert callable(getattr(cls, name, None)), f"{cls.__name__} lacks {name}"


def test_pointer_json_is_byte_identical_across_stores(
    s3: FakeS3, azure_container: FakeContainer
) -> None:
    """The freshness checker compares manifests across clouds; divergent JSON
    encoding would read as drift."""

    s3_store = _store(s3)
    azure_store = _azure_store()
    value = {"day": "2026-08-16", "rows": 5}
    s3_store.put_json_if_absent("m.json", value)
    azure_store.put_json_if_absent("m.json", value)

    encoded = s3.objects["m.json"]["Body"]
    assert encoded == azure_container.blobs["m.json"]["data"]
    assert json.loads(encoded) == value


# --------------------------------------------------------------------------
# Real-transport validation.
#
# Everything above runs against fakes I wrote, and a fake validates my
# understanding of S3 rather than S3 itself -- if botocore rejected the
# precondition parameters, or named them differently, every test above would
# still pass and the archiver would fail on the node with ParamValidationError.
#
# botocore's Stubber applies the real service model and the real parameter
# validator, so these tests fail if the argv the store builds is not a call
# boto3 will actually make. No network and no credentials are involved.
# --------------------------------------------------------------------------


def _stubbed_client(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        "s3",
        region_name="eu-west-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - stubbed, never sent
        aws_session_token="testing",  # noqa: S106 - stubbed, never sent
    )
    stubber = Stubber(client)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    return client, stubber


def test_real_botocore_accepts_the_immutable_file_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from botocore.stub import ANY

    _, stubber = _stubbed_client(monkeypatch)
    part = tmp_path / "part.parquet"
    part.write_bytes(b"rows")

    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "tr-archive-eu",
            "Key": "k",
            "Body": ANY,
            "ContentType": "application/vnd.apache.parquet",
            "Metadata": {"day": "2026-08-16", "sha256": "abc"},
            "IfNoneMatch": "*",
        },
    )
    with stubber:
        store = S3ArchiveStore(bucket="tr-archive-eu", region="eu-west-1")
        store.put_file_if_absent("k", part, sha256="abc", metadata={"day": "2026-08-16"})
    stubber.assert_no_pending_responses()


def test_real_botocore_accepts_the_pointer_compare_and_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, stubber = _stubbed_client(monkeypatch)

    stubber.add_response(
        "head_object", {"ETag": '"abc123"'}, {"Bucket": "tr-archive-eu", "Key": "_latest.json"}
    )
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "tr-archive-eu",
            "Key": "_latest.json",
            "Body": _json_bytes_for_test({"day": "2026-08-16"}),
            "ContentType": "application/json",
            "IfMatch": '"abc123"',
        },
    )
    with stubber:
        store = S3ArchiveStore(bucket="tr-archive-eu", region="eu-west-1")
        store.put_json_pointer("_latest.json", {"day": "2026-08-16"})
    stubber.assert_no_pending_responses()


def test_real_botocore_maps_a_missing_pointer_to_create_if_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First run on a new cloud: no pointer exists yet, so the CAS degrades to
    a create. A 404 here must not be mistaken for a precondition failure."""

    _, stubber = _stubbed_client(monkeypatch)

    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    stubber.add_response(
        "put_object",
        {},
        {
            "Bucket": "tr-archive-eu",
            "Key": "_latest.json",
            "Body": _json_bytes_for_test({"day": "2026-08-16"}),
            "ContentType": "application/json",
            "IfNoneMatch": "*",
        },
    )
    with stubber:
        store = S3ArchiveStore(bucket="tr-archive-eu", region="eu-west-1")
        store.put_json_pointer("_latest.json", {"day": "2026-08-16"})
    stubber.assert_no_pending_responses()


def test_real_botocore_412_is_classified_as_a_precondition_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rerun path: a genuine S3 412 must route to the sha256 comparison,
    not escape as an unhandled ClientError."""

    _, stubber = _stubbed_client(monkeypatch)
    part = tmp_path / "p"
    part.write_bytes(b"rows")

    stubber.add_client_error(
        "put_object", service_error_code="PreconditionFailed", http_status_code=412
    )
    stubber.add_response(
        "head_object",
        {"Metadata": {"sha256": "abc"}},
        {"Bucket": "tr-archive-eu", "Key": "k"},
    )
    with stubber:
        store = S3ArchiveStore(bucket="tr-archive-eu", region="eu-west-1")
        # Same content already archived -> silent no-op, not an error.
        store.put_file_if_absent("k", part, sha256="abc", metadata={})
    stubber.assert_no_pending_responses()


def _json_bytes_for_test(value: dict[str, Any]) -> bytes:
    from clickhouse.archive_daily import _json_bytes

    return _json_bytes(value)


# --------------------------------------------------------------------------
# Running the exporter on a non-GCP node.
#
# The archiver shells out to clickhouse-client with an explicit --user. That
# user was hardcoded to "tr", which only exists on the GCP cluster; the AWS-EU
# node authenticates as "default" into database "default". An explicit --user
# beats CLICKHOUSE_USER in the environment, so this could not be corrected
# from the systemd unit -- the per-cloud object stores were necessary but not
# sufficient to archive another cloud.
# --------------------------------------------------------------------------


def test_exporter_argv_carries_the_configured_user_and_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clickhouse import archive_daily

    seen: dict[str, Any] = {}

    class _Result:
        returncode = 0
        stdout = b"{}"
        stderr = b""

    def _fake_run(argv: Any, **kwargs: Any) -> Any:
        seen["argv"] = list(argv)
        seen["env"] = kwargs.get("env") or {}
        return _Result()

    monkeypatch.setattr(archive_daily.subprocess, "run", _fake_run)
    exporter = archive_daily.ClickHouseDailyExporter(
        password="pw",  # noqa: S106 - test literal
        database="default",
        table="activity_generations",
        user="default",
    )
    exporter._client("SELECT 1")

    argv = seen["argv"]
    assert argv[argv.index("--user") + 1] == "default"
    assert argv[argv.index("--database") + 1] == "default"
    # A leftover "tr" anywhere in argv means the AWS node authenticates as a
    # user that does not exist there.
    assert "tr" not in argv
    assert seen["env"]["CLICKHOUSE_PASSWORD"] == "pw"


def test_exporter_still_defaults_to_the_gcp_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """GCP's units pass no user, so the default must keep them byte-identical."""

    from clickhouse import archive_daily

    seen: dict[str, Any] = {}

    class _Result:
        returncode = 0
        stdout = b"{}"
        stderr = b""

    monkeypatch.setattr(
        archive_daily.subprocess,
        "run",
        lambda argv, **kw: (seen.__setitem__("argv", list(argv)), _Result())[1],
    )
    archive_daily.ClickHouseDailyExporter(
        password="pw",  # noqa: S106 - test literal
    )._client("SELECT 1")

    argv = seen["argv"]
    assert argv[argv.index("--user") + 1] == "tr"
    assert argv[argv.index("--database") + 1] == "tr"


def test_exporter_rejects_a_non_identifier_user() -> None:
    """The user reaches an argv, so it gets the same allowlist as the database."""

    from clickhouse import archive_daily

    with pytest.raises(ValueError, match="user must be a ClickHouse identifier"):
        archive_daily.ClickHouseDailyExporter(
            password="pw",  # noqa: S106 - test literal
            user="default; DROP",
        )


def test_completion_log_names_the_store_actually_written(
    s3: FakeS3, azure_container: FakeContainer
) -> None:
    """The success line hardcoded gs:// for every store, so an operator reading
    AWS logs would be told the object landed in GCS."""

    from clickhouse.archive_daily import GCSArchiveStore

    assert GCSArchiveStore.scheme == "gs"
    assert S3ArchiveStore.scheme == "s3"
    assert AzureBlobArchiveStore.scheme == "azure"
    # Every store must expose it, since the log reads it off whichever is live.
    for cls in (GCSArchiveStore, S3ArchiveStore, AzureBlobArchiveStore):
        assert isinstance(getattr(cls, "scheme", None), str)
