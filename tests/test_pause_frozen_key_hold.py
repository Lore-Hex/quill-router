"""Pause rejection must release only the hold returned by reserve on every backend."""

from typing import Any

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.storage import InMemoryStore
from trusted_router.storage_legacy_trust import BillingPausedError
from trusted_router.types import UsageType


@pytest.mark.parametrize("backend", ["memory", "postgres", "spanner"])
@pytest.mark.parametrize("capped", [False, True], ids=["uncapped", "capped"])
@pytest.mark.parametrize("sibling", [False, True], ids=["alone", "sibling"])
@pytest.mark.parametrize("byok", [False, True], ids=["credits", "byok"])
def test_pause_reject_releases_frozen_key_hold(
    backend: str, capped: bool, sibling: bool, byok: bool
) -> None:
    store: Any
    db: Any = None
    if backend == "spanner":
        store, db, _ = make_fake_store(request_record_write_mode="legacy")
    elif backend == "postgres":
        store = postgres_store_on(sqlite_postgres_conn())
    else:
        store = InMemoryStore()
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=True)
    ws = store.create_workspace("owner", "pause-hold", trial_credit_microdollars=1000)
    _, key = store.create_api_key(
        workspace_id=ws.id,
        name="key",
        creator_user_id="owner",
        limit_microdollars=1000 if capped else None,
        include_byok_in_limit=True,
    )
    usage = UsageType.BYOK if byok else UsageType.CREDITS
    hold = store.reserve_key_limit(key.hash, 100, usage_type=usage).reserved_microdollars
    assert hold == (100 if capped else 0)

    def reserved() -> int:
        if backend == "postgres":
            return store._run_transaction(
                lambda conn: conn.execute(
                    "SELECT reserved FROM tr_key_limit WHERE key_hash = %s AND shard = 0",
                    (key.hash,),
                ).fetchone()[0]
            )
        return store.api_keys.get_by_hash(key.hash).reserved_microdollars

    assert reserved() == hold
    if sibling:
        # A zero-hold request must not consume headroom owned by a newer request.
        store.update_key(key.hash, {"limit_microdollars": 1000})
        assert store.reserve_key_limit(
            key.hash, 70, usage_type=usage
        ).reserved_microdollars == 70
    if capped:
        # Removing the cap (or excluding BYOK) must not strand an existing hold.
        store.update_key(key.hash, {"limit_microdollars": None, "include_byok_in_limit": False})

    def pause(paused: bool) -> None:
        causes = ["abuse"] if paused else []
        if backend == "memory":
            store.workspaces[ws.id].billing_pause_causes = causes
        elif backend == "postgres":
            store._run_transaction(
                lambda conn: conn.execute(
                    "UPDATE tr_credit_balance SET billing_pause_causes = %s WHERE workspace_id = %s",
                    ('["abuse"]' if paused else "[]", ws.id),
                )
            )
        else:
            for (workspace_id, _), row in db.typed["tr_credit_balance"].items():
                if workspace_id == ws.id:
                    row["billing_pause_causes"] = causes

    pause(True)
    args = dict(
        workspace_id=ws.id,
        key_hash=key.hash,
        model_id="m",
        provider="p",
        usage_type=usage,
        estimated_microdollars=100,
        key_reserved_microdollars=hold,
        credit_reservation_id=None,
        idempotency_key="paused-hold",
    )
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(**args)
    assert reserved() == (70 if sibling else 0)
    pause(False)
    # The terminal marker prevents a replay from releasing the sibling's hold.
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(**args)
    assert reserved() == (70 if sibling else 0)
    with pytest.raises(BillingPausedError):
        store.get_gateway_authorization_by_idempotency_key(ws.id, key.hash, "paused-hold")


def test_spanner_paused_pointer_replay_raises_billing_paused() -> None:
    store, db, _ = make_fake_store(request_record_write_mode="legacy")
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=True)
    ws = store.create_workspace("owner", "paused-replay", trial_credit_microdollars=1000)
    _, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    for (workspace_id, _), row in db.typed["tr_credit_balance"].items():
        if workspace_id == ws.id:
            row["billing_pause_causes"] = ["abuse"]

    args: dict[str, Any] = dict(
        workspace_id=ws.id,
        key_hash=key.hash,
        model_id="m",
        provider="p",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=100,
        key_reserved_microdollars=0,
        credit_reservation_id=None,
        idempotency_key="paused-replay",
    )
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(**args)

    # Resume billing so only the durable terminal pointer can reject the replay.
    for (workspace_id, _), row in db.typed["tr_credit_balance"].items():
        if workspace_id == ws.id:
            row["billing_pause_causes"] = []
    with pytest.raises(BillingPausedError):
        store.create_gateway_authorization(**args)


@pytest.mark.parametrize("backend", ["memory", "postgres", "spanner"])
def test_unarmed_pause_is_inert(backend: str) -> None:
    store: Any
    db: Any = None
    if backend == "spanner":
        store, db, _ = make_fake_store(request_record_write_mode="legacy")
    elif backend == "postgres":
        store = postgres_store_on(sqlite_postgres_conn())
    else:
        store = InMemoryStore()
    store.trust_settings = Settings(environment="test", spend_lease_trust_eligibility_enabled=False)
    ws = store.create_workspace("owner", "unarmed-pause", trial_credit_microdollars=1000)
    _, key = store.create_api_key(
        workspace_id=ws.id, name="key", creator_user_id="owner", limit_microdollars=1000
    )
    hold = store.reserve_key_limit(
        key.hash, 100, usage_type=UsageType.CREDITS
    ).reserved_microdollars
    assert hold == 100
    if backend == "memory":
        store.workspaces[ws.id].billing_pause_causes = ["abuse"]
    elif backend == "postgres":
        store._run_transaction(
            lambda conn: conn.execute(
                "UPDATE tr_credit_balance SET billing_pause_causes = %s WHERE workspace_id = %s",
                ('["abuse"]', ws.id),
            )
        )
    else:
        for (workspace_id, _), row in db.typed["tr_credit_balance"].items():
            if workspace_id == ws.id:
                row["billing_pause_causes"] = ["abuse"]
    authorization = store.create_gateway_authorization(
        workspace_id=ws.id,
        key_hash=key.hash,
        model_id="m",
        provider="p",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=100,
        key_reserved_microdollars=hold,
        credit_reservation_id=None,
        idempotency_key="unarmed-pause",
    )
    assert authorization is not None
    replay = store.get_gateway_authorization_by_idempotency_key(ws.id, key.hash, "unarmed-pause")
    assert replay is not None and replay.id == authorization.id
    if backend == "postgres":
        reserved = store._run_transaction(
            lambda conn: conn.execute(
                "SELECT reserved FROM tr_key_limit WHERE key_hash = %s AND shard = 0",
                (key.hash,),
            ).fetchone()[0]
        )
    else:
        reserved = store.api_keys.get_by_hash(key.hash).reserved_microdollars
    assert reserved == hold
