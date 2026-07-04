"""Durable settle outbox — Increment 2: reaper guard wiring.

Mirrors the typed-billing reaper helpers from test_billing_typed_enforcement and
the outbox row builder style from test_settle_outbox_storage.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from tests.test_billing_typed_enforcement import (
    _NOW,
    _authorize,
    _make_key,
    _seed_credit,
    _typed,
)
from trusted_router import storage_gcp_authorize as authorize_mod
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    SettleOutcome,
    reap_expired_reservations,
    settle_atomic,
)
from trusted_router.storage_gcp_settle_outbox import GUARD_COUNT_SQL, SpannerSettleOutbox
from trusted_router.storage_models import SettleOutboxRow


def _outbox(store: Any) -> SpannerSettleOutbox:
    return SpannerSettleOutbox(store._database, store._param_types)


def _row(aid: str, rid: str, *, cost: int = 900_000) -> SettleOutboxRow:
    return SettleOutboxRow(
        authorization_id=aid,
        intent_kind="settle",
        settle_origin="typed",
        actual_cost_micro=cost,
        reservation_id=rid,
        selected_endpoint_id="openai/gpt-4o@openai",
        model_id="openai/gpt-4o",
        selected_usage_type="Credits",
        settle_body=f'{{"authorization_id":"{aid}","reservation_id":"{rid}"}}',
    )


def _expired_authorization(store: Any, *, ws: str, estimate: int = 1_000_000) -> dict[str, Any]:
    _seed_credit(store, ws, 5_000_000)
    key = _make_key(store, ws, limit=5_000_000)
    auth = _authorize(store, ws=ws, key_hash=key.hash, estimate=estimate)
    assert auth["outcome"] == AuthorizeOutcome.ACCEPTED
    return auth


def _assert_frozen(db: Any, ws: str, rid: str) -> None:
    assert _typed(db, ws)["reserved"] == 1_000_000
    assert db.reservations[rid]["settled"] is False


def _assert_free_released(db: Any, ws: str, rid: str) -> None:
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 0
    assert db.reservations[rid]["settled"] is True
    assert db.reservations[rid]["actual_micro"] == 0


def test_pending_row_freezes_hold() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_pending"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    _outbox(store).enqueue(_row(aid, rid))

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0
    _assert_frozen(db, ws, rid)
    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0
    _assert_frozen(db, ws, rid)


def test_dead_row_freezes_hold() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_dead"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    ob = _outbox(store)
    ob.enqueue(_row(aid, rid))
    [job] = ob.claim()
    assert ob.mark(aid, "settle", done=False, lease_owner=job.lease_owner, max_attempts=1) == "dead"

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0
    _assert_frozen(db, ws, rid)


def test_release_approved_row_is_reaped() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_release_approved"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    _outbox(store).enqueue(_row(aid, rid))
    db.settle_outbox[(aid, "settle")]["status"] = "release_approved"

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 1
    _assert_free_released(db, ws, rid)


def test_done_row_is_reaped() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_done"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    ob = _outbox(store)
    ob.enqueue(_row(aid, rid))
    assert ob.mark(aid, "settle", done=True) == "done"

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 1
    _assert_free_released(db, ws, rid)


def test_empty_table_reaper_unchanged() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_empty"
    auth = _expired_authorization(store, ws=ws)
    rid = auth["reservation_id"]

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 1
    _assert_free_released(db, ws, rid)


def test_in_txn_guard_beats_stale_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_mf2"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    _outbox(store).enqueue(_row(aid, rid))
    monkeypatch.setattr(authorize_mod, "_outbox_has_intent", lambda *_args, **_kwargs: False)

    assert reap_expired_reservations(store._database, store._param_types, now=_NOW) == 0
    _assert_frozen(db, ws, rid)


def test_settle_atomic_guard_outbox_semantics() -> None:
    store, db, _ = make_fake_store()
    ws = "ws_guard_settle_atomic"
    auth = _expired_authorization(store, ws=ws)
    rid, aid = auth["reservation_id"], auth["authorization_id"]
    _outbox(store).enqueue(_row(aid, rid))

    guarded = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=rid,
        actual_micro=0,
        settled_usage_type="Credits",
        success=False,
        guard_outbox=True,
    )
    assert guarded["outcome"] == SettleOutcome.OUTBOX_GUARDED
    _assert_frozen(db, ws, rid)

    inline = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=rid,
        actual_micro=900_000,
        settled_usage_type="Credits",
        success=True,
        guard_outbox=False,
    )
    assert inline["outcome"] == SettleOutcome.SETTLED
    assert _typed(db, ws)["reserved"] == 0
    assert _typed(db, ws)["total_usage"] == 900_000


def test_probe_falls_back_when_table_missing() -> None:
    class NotFound(Exception):
        pass

    class MissingOutboxSnapshot:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self._snapshot: Any = None

        def __enter__(self) -> MissingOutboxSnapshot:
            self._snapshot = self._inner.__enter__()
            return self

        def __exit__(self, *args: Any) -> None:
            return self._inner.__exit__(*args)

        def execute_sql(
            self,
            sql: str,
            *,
            params: dict[str, Any] | None = None,
            param_types: Any = None,
        ) -> list[list[Any]]:
            if sql == GUARD_COUNT_SQL:
                raise NotFound("tr_settle_outbox table not found")
            return self._snapshot.execute_sql(sql, params=params, param_types=param_types)

    class MissingOutboxDatabase:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def snapshot(self, **kwargs: Any) -> MissingOutboxSnapshot:
            return MissingOutboxSnapshot(self._inner.snapshot(**kwargs))

        def run_in_transaction(self, fn: Any) -> Any:
            return self._inner.run_in_transaction(fn)

    store, db, _ = make_fake_store()
    ws = "ws_guard_missing_table"
    auth = _expired_authorization(store, ws=ws)
    rid = auth["reservation_id"]

    assert reap_expired_reservations(MissingOutboxDatabase(store._database), store._param_types, now=_NOW) == 1
    _assert_free_released(db, ws, rid)


def test_probe_transient_notfound_fails_closed() -> None:
    class NotFound(Exception):
        pass

    class MissingOutboxSnapshot:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self._snapshot: Any = None

        def __enter__(self) -> MissingOutboxSnapshot:
            self._snapshot = self._inner.__enter__()
            return self

        def __exit__(self, *args: Any) -> None:
            return self._inner.__exit__(*args)

        def execute_sql(
            self,
            sql: str,
            *,
            params: dict[str, Any] | None = None,
            param_types: Any = None,
        ) -> list[list[Any]]:
            if sql == GUARD_COUNT_SQL:
                raise NotFound("Session not found")
            return self._snapshot.execute_sql(sql, params=params, param_types=param_types)

    class MissingOutboxDatabase:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def snapshot(self, **kwargs: Any) -> MissingOutboxSnapshot:
            return MissingOutboxSnapshot(self._inner.snapshot(**kwargs))

        def run_in_transaction(self, fn: Any) -> Any:
            return self._inner.run_in_transaction(fn)

    store, db, _ = make_fake_store()
    ws = "ws_guard_transient_notfound"
    auth = _expired_authorization(store, ws=ws)
    rid = auth["reservation_id"]

    with pytest.raises(NotFound):
        reap_expired_reservations(MissingOutboxDatabase(store._database), store._param_types, now=_NOW)
    _assert_frozen(db, ws, rid)
