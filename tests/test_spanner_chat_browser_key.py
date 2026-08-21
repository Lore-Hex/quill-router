"""Native-Spanner browser-key cap under an explicitly forced commit race."""

from __future__ import annotations

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_chat_browser_keys import is_active_chat_browser_key
from trusted_router.storage_models import ApiKey


def test_spanner_chat_browser_cap_retries_concurrent_guard_conflicts() -> None:
    store, database, _bigtable = make_fake_store()
    user = store.ensure_user("spanner-chat-cap@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    cap = 3
    callers = 8
    # Fake Spanner pauses every caller after its first transaction callback but
    # before commit. They all observe the same absent guard; one wins and the
    # others must ABORT/retry against the newly durable count.
    database._ready_barrier = threading.Barrier(callers)  # noqa: SLF001

    def issue(index: int) -> tuple[str, ApiKey] | None:
        return store.issue_chat_browser_key(
            workspace_id=workspace.id,
            name=f"chat-browser-spanner-race-{index}",
            creator_user_id=user.id,
            limit_microdollars=5_000_000,
            expires_at=(dt.datetime.now(dt.UTC) + dt.timedelta(days=30)).isoformat(),
            active_key_cap=cap,
        )

    with ThreadPoolExecutor(max_workers=callers) as executor:
        results = list(executor.map(issue, range(callers)))

    assert database.aborts >= 1
    assert sum(result is not None for result in results) == cap
    assert sum(
        is_active_chat_browser_key(key) for key in store.list_keys(workspace.id)
    ) == cap
