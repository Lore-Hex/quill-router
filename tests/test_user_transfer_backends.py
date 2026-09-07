"""Run the user-transfer contract against all three implementations' local fakes."""

from __future__ import annotations

import datetime as dt
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.fakes.credit_hold import reserve_for_transfer
from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.auth import Principal
from trusted_router.config import Settings
from trusted_router.routes import credit_transfers
from trusted_router.schemas import CreditTransferRequest
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import CreditAccount, CreditProvenance, User, Workspace
from trusted_router.storage_postgres import PostgresStore
from trusted_router.typed_balance import live_credit_summary


@pytest.fixture(params=["memory", "postgres", "spanner"])
def backend(request: Any) -> Any:
    if request.param == "memory":
        store = InMemoryStore()
    elif request.param == "postgres":
        store = postgres_store_on(sqlite_postgres_conn())
    else:
        store, _, _ = make_fake_store()
    for uid in ["alice", "bob", "carol"]:
        user = User(id=uid, email=f"{uid}@example.com", username=uid, identity_status="approved")
        if isinstance(store, InMemoryStore):
            store.users[uid] = user
        elif isinstance(store, PostgresStore):
            store._run_transaction(
                lambda conn, uid=uid, user=user: store._write_entity_tx(conn, "user", uid, user)
            )
        else:
            store._run_in_transaction(
                lambda tx, uid=uid, user=user: store._write_entity_tx(tx, "user", uid, user)
            )
    for wid, uid in [("a1", "alice"), ("a2", "alice"), ("b", "bob"), ("c", "carol")]:
        workspace = Workspace(id=wid, name=wid, owner_user_id=uid)
        if isinstance(store, InMemoryStore):
            created = store.create_workspace(uid, wid, trial_credit_microdollars=0)
            # Keep the fixture's stable IDs via the returned mapping below.
            workspace = created
        elif isinstance(store, PostgresStore):

            def seed(conn: Any, wid: str = wid, workspace: Workspace = workspace) -> None:
                store._write_entity_tx(conn, "workspace", wid, workspace)
                store._write_entity_tx(conn, "credit", wid, CreditAccount(workspace_id=wid))
                conn.execute(
                    "INSERT INTO tr_credit_balance (workspace_id, shard, total_credits, total_usage, reserved) VALUES (%s, 0, 0, 0, 0)",
                    (wid,),
                )

            store._run_transaction(seed)
        else:
            workspace = store.create_workspace(uid, wid, trial_credit_microdollars=0)
        request.node.__dict__.setdefault("workspace_ids", {})[wid] = workspace.id
        store.credit_workspace_typed_direct(
            workspace.id, 10_000_000, f"fund-{wid}", provenance=CreditProvenance.system_grant()
        )
    return store, request.node.workspace_ids


def test_daily_cap_is_shared_across_owned_workspaces(backend: Any) -> None:
    store, ids = backend
    when = dt.datetime(2026, 9, 5, tzinfo=dt.UTC)
    assert (
        store.transfer_workspace_credits(
            ids["a1"], ids["b"], 2_000_000, "one", daily_cap_microdollars=3_000_000, now=when
        )[0]
        == "accepted"
    )
    assert store.transfer_workspace_credits(
        ids["a2"], ids["b"], 2_000_000, "two", daily_cap_microdollars=3_000_000, now=when
    ) == ("daily_limit", None)
    assert store.list_credit_movements(ids["a2"], kinds=["user_transfer_out"]) == []
    assert live_credit_summary(ids["a2"], store=store)["available"] == 10_000_000
    assert (
        store.transfer_workspace_credits(
            ids["a2"],
            ids["b"],
            2_000_000,
            "two",
            daily_cap_microdollars=3_000_000,
            now=when + dt.timedelta(days=1),
        )[0]
        == "accepted"
    )


@pytest.mark.parametrize("changed", [{"amount": "2"}, {"recipient_username": "carol"}])
def test_mismatched_replay_is_409_on_every_backend(
    backend: Any, monkeypatch: pytest.MonkeyPatch, changed: dict[str, str]
) -> None:
    store, ids = backend
    monkeypatch.setattr(credit_transfers, "STORE", store)
    # Postgres intentionally disables the endpoint until these lookups ship.
    # Supply only identity lookups; execute its actual money transaction.
    monkeypatch.setattr(type(store), "supports_user_credit_transfers", lambda self: True)
    monkeypatch.setattr(
        type(store), "find_user_by_username", lambda self, name, **kwargs: self.get_user(name)
    )
    monkeypatch.setattr(
        type(store),
        "list_workspaces_for_user",
        lambda self, uid: [self.get_workspace(ids[{"alice": "a1", "bob": "b", "carol": "c"}[uid]])],
    )
    principal = Principal(
        user=store.get_user("alice"),
        workspace=store.get_workspace(ids["a1"]),
        api_key=None,
        is_management=True,
        scopes=frozenset(),
    )
    body = {"recipient_username": "bob", "amount": "1", "idempotency_key": "same"}
    first, duplicate = credit_transfers.execute_credit_transfer(
        principal, CreditTransferRequest(**body), Settings()
    )
    assert not duplicate
    repeated, duplicate = credit_transfers.execute_credit_transfer(
        principal, CreditTransferRequest(**body), Settings()
    )
    assert duplicate and repeated == first
    with pytest.raises(HTTPException) as error:
        credit_transfers.execute_credit_transfer(
            principal, CreditTransferRequest(**(body | changed)), Settings()
        )
    assert error.value.status_code == 409
    assert live_credit_summary(ids["a1"], store=store)["available"] == 9_000_000
    assert len(store.list_credit_movements(ids["a1"], kinds=["user_transfer_out"])) == 1


def test_postgres_endpoint_reports_unsupported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_credit_transfers import _headers, _setup_sender_and_recipient

    _setup_sender_and_recipient()
    monkeypatch.setattr(credit_transfers, "STORE", postgres_store_on(sqlite_postgres_conn()))
    response = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "bob", "amount": "1"},
    )
    assert response.status_code == 501


def test_batch_history_resolves_missing_and_existing_users(backend: Any) -> None:
    store, ids = backend
    assert store.workspace_owner_usernames([ids["a1"], ids["a2"], ids["b"], "missing"]) == {
        ids["a1"]: "alice",
        ids["a2"]: "alice",
        ids["b"]: "bob",
        "missing": None,
    }


def test_refused_transfers_release_claim_and_preserve_reserved_money(backend: Any) -> None:
    store, ids = backend
    when = dt.datetime(2026, 9, 5, tzinfo=dt.UTC)
    release_hold = reserve_for_transfer(store, ids["a1"], 9_000_000)
    assert store.transfer_workspace_credits(
        ids["a1"], ids["b"], 2_000_000, "retry", daily_cap_microdollars=3_000_000, now=when
    ) == ("insufficient", None)
    assert store.list_credit_movements(ids["a1"], kinds=["user_transfer_out"]) == []
    release_hold()
    assert (
        store.transfer_workspace_credits(
            ids["a1"], ids["b"], 2_000_000, "retry", daily_cap_microdollars=3_000_000, now=when
        )[0]
        == "accepted"
    )
    assert (
        store.transfer_workspace_credits(
            ids["a1"], ids["b"], 2_000_000, "retry", daily_cap_microdollars=3_000_000, now=when
        )[0]
        == "duplicate"
    )
    assert store.transfer_workspace_credits(
        ids["a1"], ids["b"], 2_000_000, "cap", daily_cap_microdollars=3_000_000, now=when
    ) == ("daily_limit", None)
    assert len(store.list_credit_movements(ids["a1"], kinds=["user_transfer_out"])) == 1
    assert (
        store.transfer_workspace_credits(
            ids["a1"],
            ids["b"],
            2_000_000,
            "cap",
            daily_cap_microdollars=3_000_000,
            now=when + dt.timedelta(days=1),
        )[0]
        == "accepted"
    )


@pytest.mark.parametrize("paused", ["a1", "b"])
def test_pause_committed_after_route_validation_refuses_transfer(
    backend: Any, monkeypatch: pytest.MonkeyPatch, paused: str,
) -> None:
    store, ids = backend
    monkeypatch.setattr(credit_transfers, "STORE", store)
    monkeypatch.setattr(type(store), "supports_user_credit_transfers", lambda self: True)
    monkeypatch.setattr(type(store), "find_user_by_username", lambda self, name, **kwargs: self.get_user(name))
    monkeypatch.setattr(type(store), "list_workspaces_for_user",
                        lambda self, uid: [self.get_workspace(ids[{"alice": "a1", "bob": "b"}[uid]])])
    original = type(store).transfer_workspace_credits

    def pause_then_transfer(self: Any, *args: Any, **kwargs: Any) -> Any:
        # The route has finished both workspace checks; commit the pause now.
        self.update_workspace(ids[paused], billing_paused=True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(store), "transfer_workspace_credits", pause_then_transfer)
    principal = Principal(user=store.get_user("alice"), workspace=store.get_workspace(ids["a1"]),
                          api_key=None, is_management=True, scopes=frozenset())
    with pytest.raises(HTTPException) as error:
        credit_transfers.execute_credit_transfer(principal, CreditTransferRequest(
            recipient_username="bob", amount="1", idempotency_key="pause-race"), Settings())
    assert error.value.status_code == 403
    for wid in (ids["a1"], ids["b"]):
        assert live_credit_summary(wid, store=store)["available"] == 10_000_000
        assert store.list_credit_movements(wid, kinds=["user_transfer_out", "user_transfer_in"]) == []


@pytest.mark.parametrize("backend_name", ["memory", "postgres", "spanner"])
def test_daily_cap_races_across_owned_workspaces(
    backend_name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if backend_name == "memory":
        store: Any = InMemoryStore()
    elif backend_name == "spanner":
        store, _, _ = make_fake_store()
    else:
        dsn = os.environ.get("TR_CONFORMANCE_POSTGRES_DSN")
        if not dsn:
            pytest.skip("concurrent Postgres transactions need TR_CONFORMANCE_POSTGRES_DSN")
        store = PostgresStore(dsn)
        store.apply_schema()
    unique = uuid.uuid4().hex
    alice = store.ensure_user(f"race-alice-{unique}@example.com", trial_credit_microdollars=0)
    bob = store.ensure_user(f"race-bob-{unique}@example.com", trial_credit_microdollars=0)
    senders = [store.create_workspace(alice.id, str(i), trial_credit_microdollars=0).id for i in range(2)]
    recipients = [store.create_workspace(bob.id, str(i), trial_credit_microdollars=0).id for i in range(2)]
    for wid in senders:
        store.credit_workspace_once(wid, 10_000_000, f"fund-{wid}")
    start = threading.Barrier(2)
    observed = threading.Event()
    read_lock = threading.Lock()
    readers: set[int] = set()

    def after_cap_read() -> None:
        # Hold the first snapshot before debit so an unprotected second
        # transaction can read the same usage. Correct locks serialize this;
        # Spanner instead lets both read and aborts/retries a stale transaction.
        with read_lock:
            readers.add(threading.get_ident())
            if len(readers) == 2:
                observed.set()
        observed.wait(timeout=0.5)

    if backend_name == "memory":
        class Counter(dict):
            def get(self, key: Any, default: Any = None) -> Any:
                value = super().get(key, default)
                after_cap_read()
                return value
        store.user_transfer_daily = Counter()
    else:
        original = type(store)._read_entity_tx
        def read(self: Any, tx: Any, kind: str, *args: Any, **kwargs: Any) -> Any:
            value = original(self, tx, kind, *args, **kwargs)
            if kind == "user_transfer_daily":
                after_cap_read()
            return value
        monkeypatch.setattr(type(store), "_read_entity_tx", read)

    def send(i: int) -> str:
        start.wait(timeout=5)
        return store.transfer_workspace_credits(senders[i], recipients[i], 2_000_000,
            f"race-{unique}-{i}", daily_cap_microdollars=3_000_000)[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(send, range(2)))
    assert sorted(outcomes) == ["accepted", "daily_limit"]
    assert sum(len(store.list_credit_movements(wid, kinds=["user_transfer_out"])) for wid in senders) == 1
    assert sum(live_credit_summary(wid, store=store)["available"] for wid in senders) == 18_000_000
    assert sum(live_credit_summary(wid, store=store)["available"] for wid in recipients) == 2_000_000


@pytest.mark.parametrize("backend_name", ["memory", "postgres", "spanner"])
def test_recipient_lookup_has_identical_read_shape(
    backend_name: str, client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe actual dictionary/SQL reads, not mocked Store lookup calls."""
    reads: list[tuple[str, str, str]] = []

    class ReadLog(dict):
        def __init__(self, kind: str, values: dict) -> None:
            super().__init__(values)
            self.kind = kind

        def get(self, key: Any, default: Any = None) -> Any:
            reads.append(("get", self.kind, key))
            return super().get(key, default)

    sender = User(id="sender-id", email="alice@example.com", username="alice",
                  identity_status="approved")
    recipient = User(id="recipient-id", email="bob@example.com", username="bob")
    workspace = Workspace(id="sender-workspace", name="Sender", owner_user_id=sender.id)
    if backend_name == "memory":
        store: Any = InMemoryStore()
        store.users = ReadLog("user", {sender.id: sender, recipient.id: recipient})
        store.user_ids_by_username = ReadLog("username_user", {"bob": recipient.id})
    elif backend_name == "postgres":
        # Requests run serially, but FastAPI dispatches them to a worker thread.
        conn = sqlite_postgres_conn(check_same_thread=False)
        store = postgres_store_on(conn)
        for user in (sender, recipient):
            store._run_transaction(
                lambda tx, user=user: store._write_entity_tx(tx, "user", user.id, user)
            )
        store._run_transaction(lambda tx: store._write_entity_tx(
            tx, "username_user", "bob", {"user_id": recipient.id}))
    else:
        store, db, _ = make_fake_store()
        for user in (sender, recipient):
            store._run_in_transaction(
                lambda tx, user=user: store._write_entity_tx(tx, "user", user.id, user)
            )
        store._run_in_transaction(lambda tx: store._write_entity_tx(
            tx, "username_user", "bob", {"user_id": recipient.id}))

    principal = Principal(user=sender, workspace=workspace, api_key=None,
                          is_management=True, scopes=frozenset())
    monkeypatch.setattr(credit_transfers, "STORE", store)
    monkeypatch.setattr(credit_transfers, "principal_from_request", lambda *_: principal)
    # Exercise the real Postgres lookup while its unrelated workspace support
    # remains disabled in production (covered by the 501 test above).
    monkeypatch.setattr(type(store), "supports_user_credit_transfers", lambda self: True)
    attempts = []
    for username in ("unknown", "bob"):
        reads.clear()
        if backend_name == "postgres":
            conn.statements.clear()
        elif backend_name == "spanner":
            db.snapshot_sql.clear()
            db.snapshot_sql_params.clear()
        response = client.post("/v1/credits/transfers", json={
            "recipient_username": username, "amount": "1"})
        if backend_name == "postgres":
            reads.extend((sql, params[0], params[1]) for sql, params in conn.statements)
        elif backend_name == "spanner":
            reads.extend((sql, params["kind"], params["id"])
                         for sql, params in zip(db.snapshot_sql, db.snapshot_sql_params, strict=True))
        attempts.append((response, list(reads)))

    missing, unverified = attempts
    assert missing[0].status_code == unverified[0].status_code == 404
    assert missing[0].content == unverified[0].content
    # Assert the miss first: reverting equalization must expose exactly one read.
    assert len(missing[1]) == 2, missing[1]
    assert len(unverified[1]) == 2, unverified[1]
    assert [(sql, kind) for sql, kind, _ in missing[1]] == [
        (sql, kind) for sql, kind, _ in unverified[1]]
    assert [(kind, key) for _, kind, key in missing[1]] == [
        ("username_user", "unknown"), ("user", sender.id)]
    assert [(kind, key) for _, kind, key in unverified[1]] == [
        ("username_user", "bob"), ("user", recipient.id)]
