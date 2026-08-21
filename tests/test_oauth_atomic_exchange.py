"""Atomic in-memory OAuth grant exchange under failures and forced races."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from trusted_router import storage_oauth_codes
from trusted_router.storage import InMemoryStore
from trusted_router.storage_oauth_codes import OAuthWorkspaceUnavailable


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )


def _grant(store: InMemoryStore) -> tuple[str, str, str]:
    user = store.ensure_user("atomic-oauth@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    verifier = "atomic-verifier-" + "a" * 43
    raw_code, _code = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://atomic.example.com/cb",
        key_label="atomic test",
        ttl_seconds=300,
        app_id=10,
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
    )
    return raw_code, verifier, workspace.id


def test_memory_atomic_oauth_exchange_has_one_winner_under_forced_race() -> None:
    store = InMemoryStore()
    raw_code, verifier, workspace_id = _grant(store)
    callers = 16
    ready = threading.Barrier(callers)

    def exchange(_index: int):
        ready.wait(timeout=10)
        return store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    with ThreadPoolExecutor(max_workers=callers) as executor:
        results = list(executor.map(exchange, range(callers)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    winner = winners[0]
    keys = store.list_keys(workspace_id)
    assert len(keys) == 1
    assert keys[0].hash == winner.api_key.hash
    assert keys[0].management is False
    assert raw_code not in json.dumps(store.oauth_code_store.codes, default=vars)
    assert winner.raw_key not in json.dumps(keys[0].__dict__)


def test_memory_key_build_failure_leaves_grant_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    raw_code, verifier, workspace_id = _grant(store)
    original = storage_oauth_codes.new_oauth_delegated_api_key

    def fail_key_build(_code):
        raise RuntimeError("key generator unavailable")

    monkeypatch.setattr(
        storage_oauth_codes,
        "new_oauth_delegated_api_key",
        fail_key_build,
    )
    with pytest.raises(RuntimeError, match="key generator unavailable"):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    assert store.list_keys(workspace_id) == []
    assert store.oauth_code_store.code_ids_by_lookup_hash
    code = next(iter(store.oauth_code_store.codes.values()))
    assert code.consumed_at is None

    monkeypatch.setattr(
        storage_oauth_codes,
        "new_oauth_delegated_api_key",
        original,
    )
    retry = store.exchange_oauth_authorization_code(
        raw_code,
        code_verifier=verifier,
        code_challenge_method="S256",
    )
    assert retry is not None
    assert len(store.list_keys(workspace_id)) == 1


def test_memory_partial_index_failure_rolls_back_key_and_consumption() -> None:
    store = InMemoryStore()
    raw_code, verifier, workspace_id = _grant(store)

    class BrokenIndex(dict[str, str]):
        def __setitem__(self, key: str, value: str) -> None:
            raise RuntimeError("index write failed")

    store.oauth_code_store._api_key_ids_by_lookup_hash = BrokenIndex()  # noqa: SLF001
    with pytest.raises(RuntimeError, match="index write failed"):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    assert store.list_keys(workspace_id) == []
    code = next(iter(store.oauth_code_store.codes.values()))
    assert code.consumed_at is None

    store.oauth_code_store._api_key_ids_by_lookup_hash = (  # noqa: SLF001
        store.api_keys.key_ids_by_lookup_hash
    )
    assert (
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        is not None
    )


def test_memory_missing_workspace_does_not_consume_grant() -> None:
    store = InMemoryStore()
    raw_code, verifier, workspace_id = _grant(store)
    workspace = store.workspaces.pop(workspace_id)

    with pytest.raises(OAuthWorkspaceUnavailable):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    assert store.oauth_code_store.codes
    assert next(iter(store.oauth_code_store.codes.values())).consumed_at is None
    assert store.list_keys(workspace_id) == []

    store.workspaces[workspace_id] = workspace
    assert (
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        is not None
    )
