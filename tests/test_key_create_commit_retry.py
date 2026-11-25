"""Key creation survives a commit whose response is lost.

The Spanner client resends a commit when its response does not arrive
(UNAVAILABLE). Spanner applies a resent commit of a read-write transaction once
(the request names the transaction), but may apply a single-use transaction's
mutations again (google.spanner.v1.CommitRequest, single_use_transaction).
Creation inserts the key's tr_key_limit rows, so through a single-use batch the
resent commit failed with ALREADY_EXISTS after everything had committed, and the
caller never got the key's secret. It now commits a read-write transaction.

The real google-cloud-spanner client runs here over an RPC stub that keeps
Spanner's documented commit semantics; no database is involved.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.exceptions import AlreadyExists, ServiceUnavailable
from google.api_core.retry import Retry, if_exception_type
from google.auth.credentials import AnonymousCredentials
from google.cloud import spanner
from google.cloud.spanner_v1.batch import Batch
from google.cloud.spanner_v1.types import CommitResponse, Transaction
from google.protobuf.json_format import MessageToDict

import trusted_router.storage_gcp_keys as keys_module
from trusted_router.storage_gcp_keys import SpannerApiKeys

_PRIMARY_KEYS = {"tr_entities": ("kind", "id"), "tr_key_limit": ("key_hash", "shard")}


class _LossySpanner:
    """Spanner's commit, as documented, with the first response lost.

    A commit naming a transaction is applied once; resent, it returns the
    first outcome. A single-use commit is applied each time it arrives. An
    insert of a row that exists fails the commit whole with ALREADY_EXISTS."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, tuple[Any, ...]], dict[str, Any]] = {}
        self.commits = 0
        self.outcomes: dict[bytes, CommitResponse] = {}
        self.begun = 0

    def begin_transaction(self, *, request: Any, metadata: Any) -> Transaction:
        self.begun += 1
        return Transaction(id=f"txn-{self.begun}".encode())

    def _commit(self, *, request: Any, metadata: Any) -> CommitResponse:
        self.commits += 1
        transaction_id = request.transaction_id
        if transaction_id and transaction_id in self.outcomes:
            return self.outcomes[transaction_id]
        pending = copy.deepcopy(self.rows)
        for mutation in request.mutations:
            operation = mutation._pb.WhichOneof("operation")
            write = getattr(mutation._pb, operation)
            for value in write.values:
                row = dict(zip(write.columns, MessageToDict(value), strict=True))
                identity = (write.table, tuple(row[c] for c in _PRIMARY_KEYS[write.table]))
                if operation == "insert" and identity in pending:
                    raise AlreadyExists(f"{identity} exists")
                pending[identity] = row
        self.rows = pending
        response = CommitResponse(commit_timestamp=dt.datetime.now(dt.UTC))
        if transaction_id:
            self.outcomes[transaction_id] = response
        if self.commits == 1:
            raise ServiceUnavailable("committed; the response was lost")
        return response

    def api(self) -> SimpleNamespace:
        resend = Retry(predicate=if_exception_type(ServiceUnavailable), initial=0, maximum=0, timeout=5)
        return SimpleNamespace(begin_transaction=self.begin_transaction, commit=resend(self._commit))


def _io(monkeypatch: pytest.MonkeyPatch, spanner_stub: _LossySpanner) -> SimpleNamespace:
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:1")
    client = spanner.Client(project="synthetic", credentials=AnonymousCredentials())
    database = client.instance("synthetic").database("synthetic")
    database._spanner_api = spanner_stub.api()
    session = database.session()
    session._session_id = "synthetic-session"

    def write_entity(writer: Any, kind: str, entity_id: str, value: Any) -> None:
        body = dataclasses.asdict(value) if dataclasses.is_dataclass(value) and not isinstance(value, type) else value
        writer.insert_or_update(table="tr_entities", columns=("kind", "id", "body", "updated_at"),
                                values=[(kind, entity_id, json.dumps(body), spanner.COMMIT_TIMESTAMP)])

    return SimpleNamespace(
        database=SimpleNamespace(
            run_in_transaction=lambda func, **kw: session.run_in_transaction(func, **kw),
            batch=lambda: Batch(session),
        ),
        spanner_module=spanner,
        param_types=spanner.param_types,
        write_entity_tx=write_entity,
        write_entity_batch=write_entity,
    )


def test_a_create_whose_commit_response_is_lost_still_returns_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _LossySpanner()
    raw, key = SpannerApiKeys(_io(monkeypatch, stub)).create(
        workspace_id="ws-synthetic", name="k", creator_user_id=None, limit_daily_microdollars=None,
    )
    assert raw and key.hash
    assert stub.commits == 2, "the commit was resent after its response was lost"
    tables = sorted(table for table, _pk in stub.rows)
    assert tables == ["tr_entities", "tr_entities", "tr_entities", "tr_key_limit"]
    assert (("tr_key_limit", (key.hash, "0"))) in stub.rows


def test_the_single_use_batch_it_replaces_failed_after_committing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: the same create through a single-use batch, as it was,
    commits every record and then fails the resent commit."""
    stub = _LossySpanner()
    io = _io(monkeypatch, stub)

    def batch_create(self: SpannerApiKeys, **kwargs: Any) -> None:
        key = SimpleNamespace(hash="kh-batch")
        with io.database.batch() as batch:
            batch.insert(table="tr_key_limit", columns=("key_hash", "shard"), values=[(key.hash, 0)])
            io.write_entity_batch(batch, "api_key", key.hash, {"hash": key.hash})

    monkeypatch.setattr(keys_module.SpannerApiKeys, "create", batch_create)
    with pytest.raises(AlreadyExists):
        SpannerApiKeys(io).create(workspace_id="ws-synthetic", name="k", creator_user_id=None)
    assert stub.commits == 2 and len(stub.rows) == 2, "every record committed before the failure"
