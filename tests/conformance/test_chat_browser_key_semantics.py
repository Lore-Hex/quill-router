"""Atomic browser-key cap semantics shared by every storage backend."""

from __future__ import annotations

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor

from trusted_router.storage_chat_browser_keys import is_active_chat_browser_key
from trusted_router.storage_models import ApiKey
from trusted_router.store_protocol import Store

_ACTIVE_CAP = 3
_LIMIT_MICRODOLLARS = 5_000_000


def _future_expiry() -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(days=30)).isoformat()


def _issue(
    store: Store,
    *,
    workspace_id: str,
    user_id: str,
    name: str,
) -> tuple[str, ApiKey] | None:
    return store.issue_chat_browser_key(
        workspace_id=workspace_id,
        name=name,
        creator_user_id=user_id,
        limit_microdollars=_LIMIT_MICRODOLLARS,
        expires_at=_future_expiry(),
        active_key_cap=_ACTIVE_CAP,
    )


def test_chat_browser_key_cap_refuses_without_persistent_create_work(
    store: Store,
    workspace_id: str,
    user_id: str,
) -> None:
    issued = [
        _issue(
            store,
            workspace_id=workspace_id,
            user_id=user_id,
            name=f"chat-browser-cap-{index}",
        )
        for index in range(_ACTIVE_CAP)
    ]
    assert all(result is not None for result in issued)
    before = store.list_api_keys_with_usage(workspace_id)

    refused = _issue(
        store,
        workspace_id=workspace_id,
        user_id=user_id,
        name="chat-browser-cap-refused",
    )

    assert refused is None
    assert store.list_api_keys_with_usage(workspace_id) == before
    assert sum(
        is_active_chat_browser_key(snapshot.api_key) for snapshot in before
    ) == _ACTIVE_CAP


def test_expired_legacy_browser_key_does_not_consume_cap(
    store: Store,
    workspace_id: str,
    user_id: str,
) -> None:
    store.create_api_key(
        workspace_id=workspace_id,
        name="chat-browser-legacy-expired",
        creator_user_id=user_id,
        expires_at="2000-01-01T00:00:00Z",
    )

    result = store.issue_chat_browser_key(
        workspace_id=workspace_id,
        name="chat-browser-current",
        creator_user_id=user_id,
        limit_microdollars=_LIMIT_MICRODOLLARS,
        expires_at=_future_expiry(),
        active_key_cap=1,
    )

    assert result is not None
    raw, key = result
    assert key.management is False
    assert store.get_key_by_raw(raw) is not None
    assert raw not in {key.secret_hash, key.lookup_hash, key.label}


def test_chat_browser_key_cap_is_isolated_per_workspace(
    store: Store,
    workspace_id: str,
    user_id: str,
    unique: str,
) -> None:
    first = store.issue_chat_browser_key(
        workspace_id=workspace_id,
        name="chat-browser-first-workspace",
        creator_user_id=user_id,
        limit_microdollars=_LIMIT_MICRODOLLARS,
        expires_at=_future_expiry(),
        active_key_cap=1,
    )
    assert first is not None
    assert (
        store.issue_chat_browser_key(
            workspace_id=workspace_id,
            name="chat-browser-first-workspace-refused",
            creator_user_id=user_id,
            limit_microdollars=_LIMIT_MICRODOLLARS,
            expires_at=_future_expiry(),
            active_key_cap=1,
        )
        is None
    )
    other = store.create_workspace(
        user_id,
        f"chat-browser-other-{unique}",
        trial_credit_microdollars=0,
    )

    second = store.issue_chat_browser_key(
        workspace_id=other.id,
        name="chat-browser-other-workspace",
        creator_user_id=user_id,
        limit_microdollars=_LIMIT_MICRODOLLARS,
        expires_at=_future_expiry(),
        active_key_cap=1,
    )

    assert second is not None


def test_concurrent_chat_browser_key_issuance_is_capped(
    store: Store,
    workspace_id: str,
    user_id: str,
) -> None:
    callers = _ACTIVE_CAP * 3
    barrier = threading.Barrier(callers)

    def issue(index: int) -> tuple[str, ApiKey] | None:
        barrier.wait(timeout=10)
        return _issue(
            store,
            workspace_id=workspace_id,
            user_id=user_id,
            name=f"chat-browser-concurrent-{index}",
        )

    with ThreadPoolExecutor(max_workers=callers) as executor:
        results = list(executor.map(issue, range(callers)))

    assert sum(result is not None for result in results) == _ACTIVE_CAP
    stored = store.list_api_keys_with_usage(workspace_id)
    assert sum(
        is_active_chat_browser_key(snapshot.api_key) for snapshot in stored
    ) == _ACTIVE_CAP
