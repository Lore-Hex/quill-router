from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from trusted_router.storage import InMemoryStore


def _memory() -> Any:
    return InMemoryStore()


def _spanner() -> Any:
    from tests.fakes.spanner import make_fake_store

    return make_fake_store()[0]


def _postgres() -> Any:
    from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn

    return postgres_store_on(sqlite_postgres_conn())


@pytest.mark.parametrize("make_store", [_memory, _spanner, _postgres], ids=["memory", "spanner", "postgres"])
def test_set_user_github_identity_round_trips(make_store: Callable[[], Any]) -> None:
    store = make_store()
    user = store.ensure_user("gh@example.com", trial_credit_microdollars=0)

    updated = store.set_user_github_identity(user.id, "alice-gh", "2026-10-13T09:00:00Z")

    assert updated is not None
    reread = store.get_user(user.id)
    assert reread.github_login == "alice-gh"
    assert reread.github_account_created_at == "2026-10-13T09:00:00Z"
    assert store.set_user_github_identity("missing-user", "x", None) is None
