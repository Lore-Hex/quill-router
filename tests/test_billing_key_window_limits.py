"""Per-key daily/weekly/monthly spend limits (fixed UTC windows, lazy reset).

Covers the money semantics end to end on the fake Spanner store:
  - settle bumps the window counters in the same release UPDATE (lazy reset
    across a boundary; BYOK gated by include_byok; refund/reaper +0 no-op)
  - authorize rejects when a window is exhausted (approximate: point-read
    before the holds) and passes again after the window rolls over
  - the mirror writes window LIMIT config but never the window USAGE state
    (the #79 ownership-split pin, extended to the new columns)
"""

from __future__ import annotations

import datetime as dt

from tests.fakes.spanner import make_fake_store
from trusted_router.spend_windows import utcnow, window_floors, window_resets_at
from trusted_router.storage import Workspace
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    authorize_atomic,
    check_key_window_limits,
    settle_atomic,
)
from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE


def _floors() -> dict:
    # Recompute at call time, not module import — comparing an import-time
    # capture against settle-time floors flakes across a UTC day/week/month
    # boundary crossed mid-suite (audit finding).
    return window_floors(utcnow())


def _auth(store, ws: str, kh: str, estimate: int, *, is_byok=False):
    return authorize_atomic(
        store._database, store._param_types,
        workspace_id=ws, key_hash=kh, estimate=estimate,
        has_credit_candidate=not is_byok, reservation_usage_type="Credits" if not is_byok else "BYOK",
        idempotency_scope=None, idempotency_fingerprint=None,
        expires_at=utcnow() + dt.timedelta(hours=2),
        build_auth_body=lambda a, r: "{}",
    )


def _check(store, kh: str, estimate: int, window_limits):
    """The lock-free snapshot window check (runs BEFORE authorize_atomic)."""
    return check_key_window_limits(
        store._database, store._param_types,
        key_hash=kh, estimate=estimate, window_limits=window_limits,
    )


def _seed(store, db, ws: str, *, credits: int = 100_000_000, **key_kwargs):
    store._write_entity("workspace", ws, Workspace(id=ws, name="t", owner_user_id="u"))
    db.typed.setdefault("tr_credit_balance", {})[(ws, 0)] = {
        "workspace_id": ws, "shard": 0, "total_credits": credits, "total_usage": 0, "reserved": 0,
    }
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None, **key_kwargs
    )
    return key


def test_settle_bumps_windows_and_authorize_blocks_then_rolls_over() -> None:
    store, db, _ = make_fake_store()
    key = _seed(store, db, "ws_w", limit_daily_microdollars=1_000)

    # Authorize + settle 700 — window counters land in the current windows.
    assert _check(store, key.hash, 700, {"daily": 1_000}) is None
    res = _auth(store, "ws_w", key.hash, 700)
    assert res["outcome"] == AuthorizeOutcome.ACCEPTED
    out = settle_atomic(
        store._database, store._param_types,
        reservation_id=res["reservation_id"], actual_micro=700,
        settled_usage_type="Credits", success=True,
    )
    assert out["outcome"] == "settled"
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["day_usage"] == 700
    assert row["week_usage"] == 700
    assert row["month_usage"] == 700
    assert row["day_start"] == _floors()["daily"]

    # 700 + 400 > 1000 -> the snapshot check blocks, naming WHICH window.
    assert _check(store, key.hash, 400, {"daily": 1_000}) == "daily"

    # Roll the stored day window back a day (simulate yesterday's usage):
    # the lazy math treats it as zero and the same request passes.
    row["day_start"] = _floors()["daily"] - dt.timedelta(days=1)
    assert _check(store, key.hash, 400, {"daily": 1_000}) is None  # stale window = zero
    res3 = _auth(store, "ws_w", key.hash, 400)
    assert res3["outcome"] == AuthorizeOutcome.ACCEPTED
    # ... and the next settle REPLACES the stale day counter instead of adding.
    out3 = settle_atomic(
        store._database, store._param_types,
        reservation_id=res3["reservation_id"], actual_micro=400,
        settled_usage_type="Credits", success=True,
    )
    assert out3["outcome"] == "settled"
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["day_usage"] == 400  # replaced (lazy reset), not 1100
    assert row["day_start"] == _floors()["daily"]
    assert row["week_usage"] == 1_100  # same week -> accumulated


def test_weekly_and_monthly_windows_block_independently() -> None:
    store, db, _ = make_fake_store()
    key = _seed(store, db, "ws_wm", limit_weekly_microdollars=500)
    res = _auth(store, "ws_wm", key.hash, 300)
    assert res["outcome"] == AuthorizeOutcome.ACCEPTED
    settle_atomic(
        store._database, store._param_types,
        reservation_id=res["reservation_id"], actual_micro=300,
        settled_usage_type="Credits", success=True,
    )
    assert _check(store, key.hash, 300, {"weekly": 500}) == "weekly"
    assert _check(store, key.hash, 100, {"weekly": 500}) is None  # under the cap passes


def test_refund_and_reaper_do_not_book_window_usage() -> None:
    store, db, _ = make_fake_store()
    key = _seed(store, db, "ws_rf", limit_daily_microdollars=1_000)
    res = _auth(store, "ws_rf", key.hash, 900)
    assert res["outcome"] == AuthorizeOutcome.ACCEPTED
    # Refund (success=False books nothing).
    out = settle_atomic(
        store._database, store._param_types,
        reservation_id=res["reservation_id"], actual_micro=0,
        settled_usage_type="Credits", success=False,
    )
    assert out["outcome"] == "settled"
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["day_usage"] == 0
    assert row["reserved"] == 0


def test_byok_settle_counts_only_when_key_includes_byok() -> None:
    store, db, _ = make_fake_store()
    # include_byok=True: BYOK settles count toward windows.
    key_inc = _seed(store, db, "ws_bi", limit_daily_microdollars=10_000)
    res = _auth(store, "ws_bi", key_inc.hash, 600, is_byok=True)
    assert res["outcome"] == AuthorizeOutcome.ACCEPTED
    settle_atomic(
        store._database, store._param_types,
        reservation_id=res["reservation_id"], actual_micro=600,
        settled_usage_type="BYOK", success=True,
    )
    assert db.typed[KEY_LIMIT_TABLE][(key_inc.hash, 0)]["day_usage"] == 600

    # include_byok=False: BYOK settles bump byok_usage but NOT the windows
    # (and the caller omits window_limits per the authorize contract).
    key_exc = _seed(
        store, db, "ws_be",
        limit_daily_microdollars=10_000, include_byok_in_limit=False,
    )
    res2 = _auth(store, "ws_be", key_exc.hash, 600, is_byok=True)
    assert res2["outcome"] == AuthorizeOutcome.ACCEPTED
    settle_atomic(
        store._database, store._param_types,
        reservation_id=res2["reservation_id"], actual_micro=600,
        settled_usage_type="BYOK", success=True,
    )
    row = db.typed[KEY_LIMIT_TABLE][(key_exc.hash, 0)]
    assert row["byok_usage"] == 600
    assert row["day_usage"] == 0  # excluded from the caps


def test_mirror_writes_window_limits_but_never_window_usage() -> None:
    """The #79 ownership-split pin, extended: a JSON api_key write mirrors the
    window LIMIT config but must not clobber the typed window USAGE state."""
    store, db, _ = make_fake_store()
    key = _seed(store, db, "ws_pin", limit_daily_microdollars=5_000)
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["day_limit_micro"] == 5_000  # config mirrored on create

    # Live typed window state (as if settles happened).
    row["day_usage"] = 4_999
    row["day_start"] = _floors()["daily"]

    # Any JSON write to the key re-fires the mirror (e.g. a rename + new limit).
    store.update_key(key.hash, {"name": "renamed", "limit_daily_microdollars": 7_000})
    row = db.typed[KEY_LIMIT_TABLE][(key.hash, 0)]
    assert row["day_limit_micro"] == 7_000  # config updated
    assert row["day_usage"] == 4_999  # typed-owned state SURVIVES the mirror
    assert row["day_start"] == _floors()["daily"]


def test_window_resets_at_is_the_next_boundary() -> None:
    now = dt.datetime(2026, 6, 27, 15, 30, tzinfo=dt.UTC)  # a Saturday
    assert window_resets_at("daily", now) == dt.datetime(2026, 6, 28, tzinfo=dt.UTC)
    assert window_resets_at("weekly", now) == dt.datetime(2026, 6, 29, tzinfo=dt.UTC)  # Monday
    assert window_resets_at("monthly", now) == dt.datetime(2026, 7, 1, tzinfo=dt.UTC)


def test_key_shape_exposes_window_limits_remaining_and_resets() -> None:
    from trusted_router.serialization import key_shape
    from trusted_router.storage import STORE

    STORE.reset()
    user = STORE.ensure_user("shape@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(
        workspace_id=ws.id, name="k", creator_user_id=user.id,
        limit_daily_microdollars=2_000_000,  # $2/day
    )
    shape = key_shape(key, window_usage={"daily": 500_000, "weekly": 500_000, "monthly": 500_000})
    assert shape["limit_daily"] == 2.0
    assert shape["limit_daily_microdollars"] == 2_000_000
    assert shape["limit_daily_remaining_microdollars"] == 1_500_000
    assert shape["limit_daily_resets_at"].endswith("Z")
    assert shape["usage_daily_microdollars"] == 500_000  # real window value
    # Unset windows expose null limits and no remaining/resets fields.
    assert shape["limit_weekly"] is None
    assert "limit_weekly_resets_at" not in shape


def test_inmemory_window_enforcement_and_snapshot() -> None:
    from trusted_router.spend_windows import KeyWindowLimitExceeded
    from trusted_router.storage import STORE

    STORE.reset()
    user = STORE.ensure_user("inmem@example.com")
    ws = STORE.list_workspaces_for_user(user.id)[0]
    _raw, key = STORE.create_api_key(
        workspace_id=ws.id, name="k", creator_user_id=user.id,
        limit_daily_microdollars=1_000,
    )
    # Book usage via the InMemory settle path, then the window blocks.
    STORE.api_keys.add_usage(key.hash, 800, is_byok=False)
    assert STORE.typed_key_usage(key.hash)["windows"]["daily"] == 800
    try:
        STORE.reserve_key_limit(key.hash, 300, usage_type="Credits")
        raise AssertionError("expected KeyWindowLimitExceeded")
    except KeyWindowLimitExceeded as exc:
        assert exc.window == "daily"
    # Under the limit passes (window check is approximate: usage only).
    STORE.reserve_key_limit(key.hash, 100, usage_type="Credits")


def test_window_check_passes_through_idempotent_replay() -> None:
    """A retry of an ALREADY-COMMITTED authorize must replay, never 429 — the
    snapshot check defers to the txn when a same-fingerprint reservation exists."""
    store, db, _ = make_fake_store()
    key = _seed(store, db, "ws_replay", limit_daily_microdollars=1_000)
    # First authorize commits with an idempotency scope.
    res = authorize_atomic(
        store._database, store._param_types,
        workspace_id="ws_replay", key_hash=key.hash, estimate=900,
        has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope="scope-1", idempotency_fingerprint="fp-1",
        expires_at=utcnow() + dt.timedelta(hours=2),
        build_auth_body=lambda a, r: "{}",
    )
    assert res["outcome"] == AuthorizeOutcome.ACCEPTED
    # The window is now effectively exhausted for a NEW request...
    blocked = check_key_window_limits(
        store._database, store._param_types,
        key_hash=key.hash, estimate=900, window_limits={"daily": 1_000},
    )
    assert blocked is None  # reserved isn't counted; settle first
    settle_atomic(
        store._database, store._param_types,
        reservation_id=res["reservation_id"], actual_micro=900,
        settled_usage_type="Credits", success=True,
    )
    assert check_key_window_limits(
        store._database, store._param_types,
        key_hash=key.hash, estimate=900, window_limits={"daily": 1_000},
    ) == "daily"  # a new request IS blocked
    # ...but the same-scope same-fingerprint retry passes through to replay.
    assert check_key_window_limits(
        store._database, store._param_types,
        key_hash=key.hash, estimate=900, window_limits={"daily": 1_000},
        idempotency_scope="scope-1", idempotency_fingerprint="fp-1",
    ) is None


def test_drift_comparator_covers_window_limit_config() -> None:
    """The exact-mirror gate must catch a broken window-limit mirror (codex #93)."""
    from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE as KLT
    from trusted_router.storage_gcp_counters import key_drift

    store, db, _ = make_fake_store()
    key = _seed(store, db, "ws_drift", limit_daily_microdollars=5_000)
    json_body = {"limit_daily_microdollars": 5_000, "include_byok_in_limit": True}
    typed_row = dict(db.typed[KLT][(key.hash, 0)])
    assert key_drift(json_body, typed_row) == {}  # mirrored -> no drift
    typed_row["day_limit_micro"] = 999  # simulate a broken mirror
    assert "day_limit_micro" in key_drift(json_body, typed_row)


def test_flip_seed_carries_window_limit_config() -> None:
    """codex #93 round 2: reconcile_for_flip's seed must include the window
    config columns, or a seeded key with a daily limit drifts immediately."""
    from trusted_router.storage import CreditAccount, Workspace
    from trusted_router.storage_gcp_counter_reconcile import reconcile_for_flip
    from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE as KLT

    store, db, _ = make_fake_store()
    ws = "ws_seed"
    store._write_entity(
        "workspace", ws, Workspace(id=ws, name="t", owner_user_id="u", billing_paused=True)
    )
    store._write_entity(
        "credit", ws,
        CreditAccount(workspace_id=ws, total_credits_microdollars=1_000_000),
    )
    _raw, key = store.api_keys.create(
        workspace_id=ws, name="k", creator_user_id=None,
        limit_daily_microdollars=4_000, limit_monthly_microdollars=90_000,
    )
    del db.typed[KLT][(key.hash, 0)]  # simulate a never-typed key (no mirror row)

    res = reconcile_for_flip(store, ws, apply=True)
    assert res.applied, res.reasons
    row = db.typed[KLT][(key.hash, 0)]
    assert row["day_limit_micro"] == 4_000
    assert row["week_limit_micro"] is None
    assert row["month_limit_micro"] == 90_000
