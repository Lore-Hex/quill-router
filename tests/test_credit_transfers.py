from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.store_protocol import Store
from trusted_router.typed_balance import live_credit_summary


def _verified_user(username: str) -> tuple[str, str]:
    user = STORE.ensure_user(f"{username}@example.com")
    STORE.set_user_identity_status(user.id, status="approved", verified_name=username.title())
    STORE.claim_user_username(user.id, username)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    return user.id, workspace.id


def _setup_sender_and_recipient() -> tuple[str, str, str, str]:
    sender_id, sender_workspace = _verified_user("alice")
    recipient_id, recipient_workspace = _verified_user("bob")
    return sender_id, sender_workspace, recipient_id, recipient_workspace


def _headers(username: str) -> dict[str, str]:
    return {"x-trustedrouter-user": f"{username}@example.com"}


def _available(workspace_id: str) -> int:
    summary = live_credit_summary(workspace_id)
    assert summary is not None
    return summary["available"]


def test_credit_transfer_happy_path_moves_exact_amount_and_writes_both_ledger_sides(
    client: TestClient,
) -> None:
    _sender_id, sender_workspace, _recipient_id, recipient_workspace = (
        _setup_sender_and_recipient()
    )
    sender_before = _available(sender_workspace)
    recipient_before = _available(recipient_workspace)
    response = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "bob", "amount": "2.50", "idempotency_key": "happy"},
    )
    assert response.status_code == 201, response.text
    transfer = response.json()["data"]
    assert transfer["amount_microdollars"] == 2_500_000
    assert _available(sender_workspace) == sender_before - 2_500_000
    assert _available(recipient_workspace) == recipient_before + 2_500_000
    outgoing = STORE.list_credit_movements(sender_workspace, kinds=["user_transfer_out"])
    incoming = STORE.list_credit_movements(recipient_workspace, kinds=["user_transfer_in"])
    assert [(row.movement_id, row.amount_microdollars) for row in outgoing] == [
        (transfer["id"], -2_500_000)
    ]
    assert [(row.movement_id, row.amount_microdollars) for row in incoming] == [
        (transfer["id"], 2_500_000)
    ]


def test_credit_transfer_idempotency_moves_once_and_returns_same_transfer(
    client: TestClient,
) -> None:
    _sender_id, sender_workspace, _recipient_id, recipient_workspace = (
        _setup_sender_and_recipient()
    )
    sender_before = _available(sender_workspace)
    recipient_before = _available(recipient_workspace)
    request = {"recipient_username": "bob", "amount": "2", "idempotency_key": "retry-me"}
    first = client.post("/v1/credits/transfers", headers=_headers("alice"), json=request)
    second = client.post("/v1/credits/transfers", headers=_headers("alice"), json=request)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["data"] == first.json()["data"]
    assert _available(sender_workspace) == sender_before - 2 * MICRODOLLARS_PER_DOLLAR
    assert _available(recipient_workspace) == recipient_before + 2 * MICRODOLLARS_PER_DOLLAR


@pytest.mark.parametrize(
    ("recipient_username", "amount", "expected_status"),
    [
        pytest.param("bob", "3", 409, id="different-amount"),
        pytest.param("carol", "2", 409, id="different-recipient"),
        pytest.param("bob", "2", 200, id="identical-body"),
    ],
)
def test_credit_transfer_replay_matches_original_body_without_moving_money(
    client: TestClient,
    recipient_username: str,
    amount: str,
    expected_status: int,
) -> None:
    from dataclasses import asdict

    _, sender_workspace, _, recipient_workspace = _setup_sender_and_recipient()
    _, other_workspace = _verified_user("carol")
    workspaces = (sender_workspace, recipient_workspace, other_workspace)
    request = {"recipient_username": "bob", "amount": "2", "idempotency_key": "replay-body"}
    first = client.post("/v1/credits/transfers", headers=_headers("alice"), json=request)
    assert first.status_code == 201, first.text

    balances_before = [_available(workspace) for workspace in workspaces]
    movements_before = [
        [asdict(row) for row in STORE.list_credit_movements(workspace)]
        for workspace in workspaces
    ]
    replay = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={**request, "recipient_username": recipient_username, "amount": amount},
    )
    assert replay.status_code == expected_status, replay.text
    if expected_status == 409:
        assert replay.json()["error"]["type"] == "bad_request"
        assert replay.json()["error"]["message"] == "Idempotency key does not match transfer"
    else:
        assert replay.json()["data"] == first.json()["data"]
    assert [_available(workspace) for workspace in workspaces] == balances_before
    assert [
        [asdict(row) for row in STORE.list_credit_movements(workspace)]
        for workspace in workspaces
    ] == movements_before


def test_credit_transfer_concurrency_cannot_overspend_available_balance() -> None:
    _sender_id, sender_workspace, _recipient_id, _recipient_workspace = (
        _setup_sender_and_recipient()
    )
    available = _available(sender_workspace)
    amount = max(MICRODOLLARS_PER_DOLLAR, (available // 20_000 + 1) * 10_000)
    import sys
    import threading

    barrier = threading.Barrier(2)

    def contend(frame, event, arg):  # type: ignore[no-untyped-def]
        if event == "line" and frame.f_code.co_name == "transfer_workspace_credits":
            import linecache
            if linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip() == "sender.total_credits_microdollars -= amount":
                # With the production lock, the second worker cannot reach
                # this point. Without it, force both reads before either write.
                if not frame.f_locals["self"]._lock._is_owned():
                    barrier.wait(timeout=300)
        return contend

    def send(key: str) -> int:
        # Exercise the store directly so the trace runs on this worker thread.
        sys.settrace(contend)
        try:
            outcome, _ = STORE.transfer_workspace_credits(
                sender_workspace, _recipient_workspace, amount, key,
                daily_cap_microdollars=100_000_000,
            )
            return 201 if outcome == "accepted" else 402
        finally:
            sys.settrace(None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(send, ("race-a", "race-b")))
    assert sorted(statuses) == [201, 402]
    assert _available(sender_workspace) >= 0


def test_credit_transfer_never_spends_fully_reserved_funds(client: TestClient) -> None:
    sender_id, sender_workspace, _recipient_id, recipient_workspace = (
        _setup_sender_and_recipient()
    )
    available = _available(sender_workspace)
    _raw, key = STORE.create_api_key(
        workspace_id=sender_workspace,
        name="hold key",
        creator_user_id=sender_id,
    )
    STORE.reserve(sender_workspace, key.hash, available, idempotency_key="full-hold")
    recipient_before = _available(recipient_workspace)
    response = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "bob", "amount": "1", "idempotency_key": "held"},
    )
    assert response.status_code == 402
    assert _available(recipient_workspace) == recipient_before
    assert STORE.list_credit_movements(sender_workspace, kinds=["user_transfer_out"]) == []


def test_credit_transfer_rejects_unverified_self_and_suspended_accounts(
    client: TestClient,
) -> None:
    sender_id, sender_workspace, recipient_id, recipient_workspace = (
        _setup_sender_and_recipient()
    )
    sender = STORE.get_user(sender_id)
    recipient = STORE.get_user(recipient_id)
    assert sender is not None and recipient is not None
    request = {"recipient_username": "bob", "amount": "1"}
    sender.identity_status = "none"
    assert client.post("/v1/credits/transfers", headers=_headers("alice"), json=request).status_code == 403
    sender.identity_status = "approved"
    recipient.identity_status = "none"
    assert client.post("/v1/credits/transfers", headers=_headers("alice"), json=request).status_code == 404
    recipient.identity_status = "approved"
    self_send = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "alice", "amount": "1"},
    )
    assert self_send.status_code == 400
    sender.suspended = True
    assert client.post("/v1/credits/transfers", headers=_headers("alice"), json=request).status_code == 403
    sender.suspended = False
    recipient.suspended = True
    assert client.post("/v1/credits/transfers", headers=_headers("alice"), json=request).status_code == 404
    recipient.suspended = False
    recipient.disabled = True
    assert client.post("/v1/credits/transfers", headers=_headers("alice"), json=request).status_code == 404
    assert _available(sender_workspace) > 0
    assert _available(recipient_workspace) > 0


def test_credit_transfer_rejects_delegated_key_by_key_type_not_scopes(
    client: TestClient,
) -> None:
    sender_id, sender_workspace, _recipient_id, _recipient_workspace = (
        _setup_sender_and_recipient()
    )
    raw, key = STORE.create_api_key(
        workspace_id=sender_workspace,
        name="malformed delegated management key",
        creator_user_id=sender_id,
        scopes=[],
        app_id="oauth-app",
        management=True,
    )
    assert key.app_id and key.scopes == [] and key.management
    response = client.post(
        "/v1/credits/transfers",
        headers={"authorization": f"Bearer {raw}"},
        json={"recipient_username": "bob", "amount": "1"},
    )
    assert response.status_code == 403
    assert "delegated" in response.text.lower()


def test_credit_transfer_daily_cap_blocks_excess_and_rolls_next_utc_day(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_sender_and_recipient()
    client.app.state.settings.user_credit_transfer_daily_cap_microdollars = (  # type: ignore[attr-defined]
        2 * MICRODOLLARS_PER_DOLLAR
    )
    current = [dt.datetime(2026, 9, 5, 23, 59, tzinfo=dt.UTC)]
    original = InMemoryStore.transfer_workspace_credits

    def at_test_time(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["now"] = current[0]
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InMemoryStore, "transfer_workspace_credits", at_test_time)
    request = {"recipient_username": "bob", "amount": "2", "idempotency_key": "cap-a"}
    assert client.post("/v1/credits/transfers", headers=_headers("alice"), json=request).status_code == 201
    request["idempotency_key"] = "cap-b"
    assert client.post("/v1/credits/transfers", headers=_headers("alice"), json=request).status_code == 429
    current[0] += dt.timedelta(minutes=2)
    assert client.post("/v1/credits/transfers", headers=_headers("alice"), json=request).status_code == 201


def test_credit_transfer_conserves_ledger_value_across_batch(client: TestClient) -> None:
    _sender_id, alice_workspace, _recipient_id, bob_workspace = (
        _setup_sender_and_recipient()
    )
    _carol_id, carol_workspace = _verified_user("carol")
    requests = [
        ("alice", "bob", "1", "batch-1"),
        ("alice", "carol", "2", "batch-2"),
        ("bob", "carol", "1", "batch-3"),
    ]
    for sender_name, recipient_name, amount, key in requests:
        response = client.post(
            "/v1/credits/transfers",
            headers=_headers(sender_name),
            json={
                "recipient_username": recipient_name,
                "amount": amount,
                "idempotency_key": key,
            },
        )
        assert response.status_code == 201, response.text
    movements = [
        movement
        for workspace_id in (alice_workspace, bob_workspace, carol_workspace)
        for movement in STORE.list_credit_movements(
            workspace_id, kinds=["user_transfer_out", "user_transfer_in"]
        )
    ]
    assert len(movements) == 6
    assert sum(movement.amount_microdollars for movement in movements) == 0


def test_credit_transfer_unknown_username_is_clean_404_without_unverified_oracle(
    client: TestClient,
) -> None:
    sender_id, _sender_workspace, recipient_id, _recipient_workspace = (
        _setup_sender_and_recipient()
    )
    sender = STORE.get_user(sender_id)
    assert sender is not None
    sender.identity_status = "approved"
    recipient = STORE.get_user(recipient_id)
    assert recipient is not None
    recipient.identity_status = "none"
    existing = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "bob", "amount": "1"},
    )
    missing = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "nobody", "amount": "1"},
    )
    assert (existing.status_code, existing.content) == (missing.status_code, missing.content)
    assert existing.status_code == 404
    sender.identity_status = "approved"
    unknown = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "nobody", "amount": "1"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["type"] == "not_found"


def test_credit_transfer_history_and_backend_capability_surface(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_sender_and_recipient()
    sent = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "bob", "amount": "1.25", "idempotency_key": "history"},
    )
    assert sent.status_code == 201
    alice_history = client.get("/v1/credits/transfers", headers=_headers("alice"))
    bob_history = client.get("/v1/credits/transfers", headers=_headers("bob"))
    assert alice_history.json()["data"][0]["direction"] == "sent"
    assert bob_history.json()["data"][0]["direction"] == "received"
    assert alice_history.json()["data"][0]["id"] == bob_history.json()["data"][0]["id"]

    monkeypatch.setattr(
        InMemoryStore,
        "supports_user_credit_transfers",
        lambda _self: False,
    )
    unsupported = client.post(
        "/v1/credits/transfers",
        headers=_headers("alice"),
        json={"recipient_username": "bob", "amount": "1"},
    )
    assert unsupported.status_code == 501
    assert unsupported.json()["error"]["type"] == "endpoint_not_supported"
    assert client.get("/v1/credits/transfers", headers=_headers("alice")).status_code == 501


def test_non_owner_management_key_cannot_send_org_credits(client: TestClient) -> None:
    alice, workspace, bob, _ = _setup_sender_and_recipient()
    _verified_user("carol")
    STORE.add_members(workspace, ["bob@example.com"], role="admin")
    raw, _ = STORE.create_api_key(
        workspace_id=workspace, name="org admin", creator_user_id=bob, management=True,
    )
    before = _available(workspace)
    response = client.post("/v1/credits/transfers", headers={"authorization": f"Bearer {raw}"},
                           json={"recipient_username": "carol", "amount": "1"})
    assert response.status_code == 403
    assert "owner" in response.text
    assert _available(workspace) == before
    owner_raw, _ = STORE.create_api_key(
        workspace_id=workspace, name="owner", creator_user_id=alice, management=True,
    )
    assert client.post("/v1/credits/transfers", headers={"authorization": f"Bearer {owner_raw}"},
                       json={"recipient_username": "carol", "amount": "1"}).status_code == 201


def test_same_workspace_refused_before_store_call(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    from trusted_router.routes import credit_transfers

    _, workspace, _, _ = _setup_sender_and_recipient()
    # Model two ownership snapshots that resolve to the same account ID.
    monkeypatch.setattr(
        credit_transfers, "_owned_credit_workspace",
        lambda user: dataclasses.replace(STORE.get_workspace(workspace), owner_user_id=user.id),
    )
    calls = []
    original = InMemoryStore.transfer_workspace_credits

    def record(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InMemoryStore, "transfer_workspace_credits", record)
    response = client.post("/v1/credits/transfers", headers=_headers("alice"),
                           json={"recipient_username": "bob", "amount": "1"})
    assert response.status_code == 400
    assert not calls
    assert STORE.list_credit_movements(workspace, kinds=["user_transfer_out", "user_transfer_in"]) == []


@pytest.mark.parametrize("username", ["alice", "bob"])
def test_billing_pause_causes_prevent_sending_and_receiving(client: TestClient, username: str) -> None:
    _setup_sender_and_recipient()
    user = STORE.find_user_by_username(username)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    workspace.billing_pause_causes = ["admin"]
    response = client.post("/v1/credits/transfers", headers=_headers("alice"),
                           json={"recipient_username": "bob", "amount": "1"})
    assert response.status_code == 403
    assert STORE.list_credit_movements(workspace.id, kinds=["user_transfer_out", "user_transfer_in"]) == []


@pytest.mark.parametrize("amount", ["1e2", "+5", " 5 ", "5.999", True, 5.999, 100, "01", "1\n"])
def test_transfer_amount_requires_plain_two_decimal_string(amount: object) -> None:
    from pydantic import ValidationError

    from trusted_router.schemas import CreditTransferRequest

    with pytest.raises(ValidationError):
        CreditTransferRequest(recipient_username="bob", amount=amount)


def test_console_preserves_form_key_on_failure_and_replay(client: TestClient) -> None:
    import re

    alice, workspace, bob, _ = _setup_sender_and_recipient()
    session, _ = STORE.create_auth_session(user_id=alice, provider="test", label="test",
                                           ttl_seconds=3600, workspace_id=workspace)
    client.cookies.set("tr_session", session)
    page = client.get("/console/credit-transfers")
    assert page.status_code == 200
    key = re.search(r'name="idempotency_key" value="([^"]+)"', page.text)[1]
    before = _available(workspace)
    form = {"recipient_username": "bob", "amount": "bad", "idempotency_key": key}
    failed = client.post("/console/credit-transfers", data=form)
    assert f'value="{key}"' in failed.text
    form["amount"] = "2.50"
    first = client.post("/console/credit-transfers", data=form)
    second = client.post("/console/credit-transfers", data=form)
    assert first.status_code == second.status_code == 200
    assert f'value="{key}"' in second.text
    assert "$2.50" in second.text
    assert _available(workspace) == before - 2_500_000
    STORE.get_user(bob).username = None
    missing = client.get("/console/credit-transfers")
    assert "Unavailable account" in missing.text
    assert "@None" not in missing.text


def test_history_batches_identity_lookups(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_sender_and_recipient()
    for index in range(3):
        assert client.post("/v1/credits/transfers", headers=_headers("alice"),
                           json={"recipient_username": "bob", "amount": "1", "idempotency_key": f"history-{index}"}).status_code == 201
    calls = []
    batch = InMemoryStore.workspace_owner_usernames

    def record(self, ids):  # type: ignore[no-untyped-def]
        calls.append(ids)
        return batch(self, ids)

    monkeypatch.setattr(InMemoryStore, "workspace_owner_usernames", record)
    response = client.get("/v1/credits/transfers", headers=_headers("alice"))
    assert response.status_code == 200
    assert len(response.json()["data"]) == 3
    assert len(calls) == 1
    assert len(calls[0]) == 2


def test_transfer_endpoint_rejects_bad_amounts_without_movements(client: TestClient) -> None:
    _, workspace, _, _ = _setup_sender_and_recipient()
    for amount in ["1e2", "+5", " 5 ", "5.999", True, 5.999]:
        response = client.post("/v1/credits/transfers", headers=_headers("alice"),
                               json={"recipient_username": "bob", "amount": amount})
        assert response.status_code == 400, response.text
    assert STORE.list_credit_movements(workspace, kinds=["user_transfer_out"]) == []


def test_recipient_owner_is_verified_independently_of_username_row(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    _, workspace, bob, _ = _setup_sender_and_recipient()
    original = InMemoryStore.get_user

    def owner(self, uid):  # type: ignore[no-untyped-def]
        user = original(self, uid)
        return dataclasses.replace(user, identity_status="none") if uid == bob else user

    monkeypatch.setattr(InMemoryStore, "get_user", owner)
    response = client.post("/v1/credits/transfers", headers=_headers("alice"),
                           json={"recipient_username": "bob", "amount": "1"})
    assert response.status_code == 404
    assert STORE.list_credit_movements(workspace, kinds=["user_transfer_out"]) == []


def _daily_cap_workspaces(store: Store, unique: str) -> tuple[str, str, str, str]:
    """Fund two workspaces for one owner and one for an independent sender."""
    workspaces = []
    for name, owner in (("a", "alice"), ("b", "alice"), ("c", "carol"), ("r", "recipient")):
        user = store.ensure_user(
            f"{owner}-{unique}", f"{owner}-{unique}@example.com",
            trial_credit_microdollars=0,
        )
        workspace = store.create_workspace(
            user.id, f"{name}-{unique}", trial_credit_microdollars=0,
        )
        assert store.credit_workspace_once(
            workspace.id, 200_000_000, f"fund-{name}-{unique}",
        )
        workspaces.append(workspace.id)
    return workspaces[0], workspaces[1], workspaces[2], workspaces[3]


def test_user_credit_transfer_daily_cap_shared_across_workspaces() -> None:
    store = InMemoryStore()
    unique = "daily-cap"
    a, b, _, recipient = _daily_cap_workspaces(store, unique)
    when = dt.datetime(2026, 9, 5, 12, tzinfo=dt.UTC)
    assert store.transfer_workspace_credits(
        a, recipient, 60_000_000, f"first-{unique}",
        daily_cap_microdollars=100_000_000, now=when,
    )[0] == "accepted"
    # B has ample funds and no outgoing history of its own: only A's spend blocks this.
    assert store.transfer_workspace_credits(
        b, recipient, 60_000_000, f"blocked-{unique}",
        daily_cap_microdollars=100_000_000, now=when,
    ) == ("daily_limit", None)
    assert store.list_credit_movements(b, kinds=["user_transfer_out"]) == []
    balance = live_credit_summary(b, store=store)
    assert balance is not None and balance["available"] == 200_000_000
    assert store.transfer_workspace_credits(
        b, recipient, 30_000_000, f"smaller-{unique}",
        daily_cap_microdollars=100_000_000, now=when,
    )[0] == "accepted"
    balance = live_credit_summary(b, store=store)
    assert balance is not None and balance["available"] == 170_000_000


def test_user_credit_transfer_daily_cap_independent_between_users() -> None:
    store = InMemoryStore()
    unique = "daily-cap"
    a, _, c, recipient = _daily_cap_workspaces(store, unique)
    when = dt.datetime(2026, 9, 5, 12, tzinfo=dt.UTC)
    for sender in (a, c):
        assert store.transfer_workspace_credits(
            sender, recipient, 60_000_000, f"send-{sender}-{unique}",
            daily_cap_microdollars=100_000_000, now=when,
        )[0] == "accepted"
        balance = live_credit_summary(sender, store=store)
        assert balance is not None and balance["available"] == 140_000_000
    incoming = store.list_credit_movements(recipient, kinds=["user_transfer_in"])
    assert len(incoming) == 2
    assert sum(m.amount_microdollars for m in incoming) == 120_000_000


def test_user_credit_transfer_daily_cap_replay_does_not_double_count() -> None:
    store = InMemoryStore()
    unique = "daily-cap"
    a, b, _, recipient = _daily_cap_workspaces(store, unique)
    when = dt.datetime(2026, 9, 5, 12, tzinfo=dt.UTC)
    outcome, first = store.transfer_workspace_credits(
        a, recipient, 60_000_000, f"first-{unique}",
        daily_cap_microdollars=100_000_000, now=when,
    )
    assert outcome == "accepted" and first is not None
    assert store.transfer_workspace_credits(
        a, recipient, 60_000_000, f"first-{unique}",
        daily_cap_microdollars=100_000_000, now=when,
    ) == ("duplicate", first)
    # A replay must leave exactly $40 of the owner's $100 daily allowance.
    assert store.transfer_workspace_credits(
        b, recipient, 40_000_000, f"remaining-{unique}",
        daily_cap_microdollars=100_000_000, now=when,
    )[0] == "accepted"
    assert store.transfer_workspace_credits(
        b, recipient, 1, f"over-{unique}",
        daily_cap_microdollars=100_000_000, now=when,
    ) == ("daily_limit", None)
    outgoing = [
        movement for sender in (a, b)
        for movement in store.list_credit_movements(sender, kinds=["user_transfer_out"])
    ]
    assert len(outgoing) == 2
    assert sum(m.amount_microdollars for m in outgoing) == -100_000_000
    incoming = store.list_credit_movements(recipient, kinds=["user_transfer_in"])
    assert len(incoming) == 2
    assert sum(m.amount_microdollars for m in incoming) == 100_000_000
