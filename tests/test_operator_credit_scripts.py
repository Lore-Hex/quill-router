from __future__ import annotations

from typing import Any

import pytest

from scripts import grant_credit
from tests.fakes.spanner import make_fake_store
from trusted_router.typed_balance import live_credit_summary


def _user_and_workspace(store: Any, email: str) -> tuple[Any, Any]:
    user = store.ensure_user(email)
    workspace = store.list_workspaces_for_user(user.id)[0]
    return user, workspace


def test_grant_credit_dry_run_does_not_mutate() -> None:
    store, _db, _ = make_fake_store()
    _user, workspace = _user_and_workspace(store, "operator@example.com")
    before = live_credit_summary(workspace.id, store=store)

    rc = grant_credit.main(
        [
            "--email",
            "operator@example.com",
            "--amount",
            "10.000001",
            "--event-id",
            "evt_dry",
        ],
        store=store,
    )

    assert rc == 0
    assert live_credit_summary(workspace.id, store=store) == before


def test_grant_credit_applies_once_and_reports_authoritative_balance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, _db, _ = make_fake_store()
    _user, workspace = _user_and_workspace(store, "operator@example.com")
    argv = [
        "--email",
        "operator@example.com",
        "--amount",
        "10.000001",
        "--event-id",
        "evt_apply_once",
        "--apply",
    ]

    assert grant_credit.main(argv, store=store) == 0
    assert grant_credit.main(argv, store=store) == 0

    summary = live_credit_summary(workspace.id, store=store)
    assert summary is not None
    assert summary["total_credits"] == 10_000_001
    assert summary["available"] == 10_000_001


def test_grant_credit_requires_workspace_when_selection_is_ambiguous() -> None:
    store, _db, _ = make_fake_store()
    user, first = _user_and_workspace(store, "operator@example.com")
    store.update_workspace(first.id, name="Team One")
    store.create_workspace(user.id, "Team Two")

    rc = grant_credit.main(
        [
            "--email",
            "operator@example.com",
            "--amount",
            "10",
            "--event-id",
            "evt_ambiguous",
        ],
        store=store,
    )

    assert rc == 1
