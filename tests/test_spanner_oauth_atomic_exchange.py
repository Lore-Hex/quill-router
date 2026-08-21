"""Native-Spanner OAuth exchange transactions under aborts and failures."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router import storage_gcp_oauth_codes
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE
from trusted_router.storage_oauth_codes import (
    OAuthWorkspaceBillingPaused,
    OAuthWorkspaceUnavailable,
)


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )


def _grant(
    store: Any,
    *,
    limit_microdollars: int | None = 7_000_000,
) -> tuple[str, str, str]:
    user = store.ensure_user("spanner-atomic-oauth@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    verifier = "spanner-verifier-" + "s" * 43
    raw_code, _code = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://spanner-atomic.example.com/cb",
        key_label="Spanner atomic OAuth",
        ttl_seconds=300,
        app_id=20,
        limit_microdollars=limit_microdollars,
        limit_reset="monthly",
        code_challenge=_challenge(verifier),
        code_challenge_method="S256",
    )
    return raw_code, verifier, workspace.id


def _set_credit_shards(store: Any, workspace_id: str, shard_count: int) -> None:
    account = store.get_credit_account(workspace_id)
    assert account is not None
    account.shard_count = shard_count
    store._write_entity(  # noqa: SLF001
        "credit",
        workspace_id,
        account,
    )


def test_spanner_atomic_oauth_exchange_retries_to_one_winner() -> None:
    store, database, _bigtable = make_fake_store()
    raw_code, verifier, workspace_id = _grant(store)
    callers = 8
    database._ready_barrier = threading.Barrier(callers)  # noqa: SLF001

    def exchange(_index: int):
        return store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    with ThreadPoolExecutor(max_workers=callers) as executor:
        results = list(executor.map(exchange, range(callers)))

    winners = [result for result in results if result is not None]
    assert database.aborts >= 1
    assert len(winners) == 1
    assert len(store.list_keys(workspace_id)) == 1
    assert winners[0].api_key.management is False
    durable_json = json.dumps([row.body for row in database.rows.values()])
    assert raw_code not in durable_json
    assert winners[0].raw_key not in durable_json


def test_spanner_partial_key_write_failure_rolls_back_every_row() -> None:
    store, database, _bigtable = make_fake_store()
    raw_code, verifier, workspace_id = _grant(store)
    original_io = store.oauth_code_store._io  # noqa: SLF001

    def fail_after_key_write(
        transaction: Any,
        kind: str,
        entity_id: str,
        value: Any,
    ) -> None:
        if kind == "api_key_lookup":
            raise RuntimeError("Spanner write unavailable")
        original_io.write_entity_tx(transaction, kind, entity_id, value)

    store.oauth_code_store._io = replace(  # noqa: SLF001
        original_io,
        write_entity_tx=fail_after_key_write,
    )
    with pytest.raises(RuntimeError, match="Spanner write unavailable"):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    assert not any(kind == "api_key" for kind, _entity_id in database.rows)
    assert not any(kind == "api_key_lookup" for kind, _entity_id in database.rows)
    assert not any(kind == "api_key_by_workspace" for kind, _entity_id in database.rows)
    assert database.typed.get("tr_key_limit", {}) == {}
    stored_code = next(
        row for (kind, _entity_id), row in database.rows.items() if kind == "oauth_code"
    )
    assert json.loads(stored_code.body).get("consumed_at") is None

    store.oauth_code_store._io = original_io  # noqa: SLF001
    retry = store.exchange_oauth_authorization_code(
        raw_code,
        code_verifier=verifier,
        code_challenge_method="S256",
    )
    assert retry is not None
    assert len(store.list_keys(workspace_id)) == 1
    assert len(database.typed["tr_key_limit"]) == 1


def test_spanner_paused_workspace_keeps_grant_retryable() -> None:
    store, database, _bigtable = make_fake_store()
    raw_code, verifier, workspace_id = _grant(store)
    store.update_workspace(workspace_id, billing_paused=True)

    with pytest.raises(OAuthWorkspaceBillingPaused):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    assert not any(kind == "api_key" for kind, _entity_id in database.rows)
    assert database.typed.get("tr_key_limit", {}) == {}
    store.update_workspace(workspace_id, billing_paused=False)
    assert (
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        is not None
    )


def test_spanner_missing_workspace_keeps_grant_retryable() -> None:
    store, database, _bigtable = make_fake_store()
    raw_code, verifier, workspace_id = _grant(store)
    workspace = store.get_workspace(workspace_id)
    assert workspace is not None
    store._delete_entities("workspace", [workspace_id])  # noqa: SLF001

    with pytest.raises(OAuthWorkspaceUnavailable):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    stored_code = next(
        row for (kind, _entity_id), row in database.rows.items() if kind == "oauth_code"
    )
    assert json.loads(stored_code.body).get("consumed_at") is None
    assert not any(kind == "api_key" for kind, _entity_id in database.rows)
    store._write_entity("workspace", workspace_id, workspace)  # noqa: SLF001
    assert (
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        is not None
    )


def test_spanner_missing_credit_configuration_keeps_grant_retryable() -> None:
    store, database, _bigtable = make_fake_store()
    raw_code, verifier, workspace_id = _grant(store, limit_microdollars=None)
    credit = store.get_credit_account(workspace_id)
    assert credit is not None
    store._delete_entities("credit", [workspace_id])  # noqa: SLF001

    with pytest.raises(OAuthWorkspaceUnavailable):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    stored_code = next(
        row for (kind, _entity_id), row in database.rows.items() if kind == "oauth_code"
    )
    assert json.loads(stored_code.body).get("consumed_at") is None
    assert not any(kind == "api_key" for kind, _entity_id in database.rows)
    store._write_entity("credit", workspace_id, credit)  # noqa: SLF001
    assert (
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        is not None
    )


def test_spanner_uncapped_oauth_key_preserves_create_api_key_sharding() -> None:
    store, database, _bigtable = make_fake_store()
    raw_code, verifier, workspace_id = _grant(store, limit_microdollars=None)
    _set_credit_shards(store, workspace_id, 8)
    store._credit_shard_counts.invalidate(workspace_id)  # noqa: SLF001

    _baseline_raw, baseline = store.create_api_key(
        workspace_id=workspace_id,
        name="pre-atomic semantics",
        creator_user_id=None,
    )
    exchange = store.exchange_oauth_authorization_code(
        raw_code,
        code_verifier=verifier,
        code_challenge_method="S256",
    )

    assert baseline.usage_shard_count == 8
    assert exchange is not None
    assert exchange.api_key.usage_shard_count == baseline.usage_shard_count
    assert {
        shard
        for key_hash, shard in database.typed[KEY_LIMIT_TABLE]
        if key_hash == exchange.api_key.hash
    } == set(range(8))


def test_spanner_oauth_shard_selection_retries_concurrent_reshard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, _bigtable = make_fake_store()
    raw_code, verifier, workspace_id = _grant(store, limit_microdollars=None)
    _set_credit_shards(store, workspace_id, 4)
    entered = threading.Event()
    resume = threading.Event()
    observed_counts: list[int] = []
    original = storage_gcp_oauth_codes.new_oauth_delegated_api_key

    def pause_first_build(code, *, usage_shard_count: int = 1):
        observed_counts.append(usage_shard_count)
        if len(observed_counts) == 1:
            entered.set()
            assert resume.wait(timeout=10)
        return original(code, usage_shard_count=usage_shard_count)

    monkeypatch.setattr(
        storage_gcp_oauth_codes,
        "new_oauth_delegated_api_key",
        pause_first_build,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            store.exchange_oauth_authorization_code,
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        assert entered.wait(timeout=10)
        _set_credit_shards(store, workspace_id, 8)
        resume.set()
        exchange = future.result(timeout=10)

    assert database.aborts >= 1
    assert observed_counts[0] == 4
    assert observed_counts[-1] == 8
    assert exchange is not None
    assert exchange.api_key.usage_shard_count == 8
    assert {
        shard
        for key_hash, shard in database.typed[KEY_LIMIT_TABLE]
        if key_hash == exchange.api_key.hash
    } == set(range(8))
