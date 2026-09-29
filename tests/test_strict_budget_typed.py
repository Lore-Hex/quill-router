from concurrent.futures import ThreadPoolExecutor

from tests.fakes.spanner import make_fake_store
from tests.test_billing_typed_enforcement import _auth_body, _seed_credit, _typed
from trusted_router.spend_windows import utcnow
from trusted_router.storage_gcp_authorize import authorize_atomic, settle_atomic


def setup():
    store, db = make_fake_store()
    _seed_credit(store, "strict-ws", 1000)
    _, key = store.create_api_key(
        workspace_id="strict-ws",
        creator_user_id=None,
        name="strict",
        budget_strict=True,
        limit_daily_microdollars=100,
    )
    return store, db, key


def authorize(store, key, *, amount=60, scope=None):
    return authorize_atomic(
        store._database,
        store._param_types,
        workspace_id="strict-ws",
        key_hash=key.hash,
        estimate=amount,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        idempotency_scope=scope,
        idempotency_fingerprint="fingerprint",
        expires_at=utcnow(),
        build_auth_body=_auth_body,
        strict_budget=True,
    )


def test_strict_typed_reservation_rolls_back_credit_on_rejection_and_replays_once():
    store, db, key = setup()
    first = authorize(store, key, scope="scope")
    assert first["outcome"] == "accepted"
    assert first["outcome"].rate_limit.remaining == 100
    assert authorize(store, key, scope="scope")["outcome"] == "replay"
    assert authorize(store, key)["outcome"] == "key_window_limit_exceeded:daily"
    assert db.typed["tr_key_limit"][(key.hash, 0)]["reserved"] == 60
    assert _typed(db, "strict-ws")["reserved"] == 60


def test_strict_typed_concurrent_holds_are_atomic():
    store, db, key = setup()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: authorize(store, key), range(8)))
    assert sum(result["outcome"] == "accepted" for result in results) == 1
    assert _typed(db, "strict-ws")["reserved"] == 60


def test_strict_typed_settlement_and_refund_release_once():
    store, db, key = setup()
    first = authorize(store, key)
    result = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=first["reservation_id"],
        actual_micro=10,
        settled_usage_type="Credits",
        success=True,
    )
    assert result["outcome"] == "settled"
    again = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=first["reservation_id"],
        actual_micro=10,
        settled_usage_type="Credits",
        success=True,
    )
    assert again["outcome"] == "already_settled"
    assert db.typed["tr_key_limit"][(key.hash, 0)]["reserved"] == 0
    assert authorize(store, key, amount=91)["outcome"] == "key_window_limit_exceeded:daily"
    assert authorize(store, key, amount=90)["outcome"] == "accepted"


def test_strict_failed_generation_releases_hold_without_spend():
    store, db, key = setup()
    first = authorize(store, key)
    for expected in ("settled", "already_settled"):
        result = settle_atomic(
            store._database,
            store._param_types,
            reservation_id=first["reservation_id"],
            actual_micro=0,
            settled_usage_type="Credits",
            success=False,
        )
        assert result["outcome"] == expected
    assert _typed(db, "strict-ws")["reserved"] == 0
    assert db.typed["tr_key_limit"][(key.hash, 0)]["reserved"] == 0
    assert authorize(store, key, amount=100)["outcome"] == "accepted"


def test_strict_old_windows_reset_but_inflight_holds_still_count():
    from datetime import timedelta

    store, db, key = setup()
    row = db.typed["tr_key_limit"][(key.hash, 0)]
    row.update(day_usage=100, day_start=utcnow() - timedelta(days=2), reserved=60)
    assert authorize(store, key, amount=41)["outcome"] == "key_window_limit_exceeded:daily"
    assert authorize(store, key, amount=40)["outcome"] == "accepted"


def test_strict_typed_wrapper_uses_current_limits_even_without_snapshot():
    store, db, key = setup()
    kwargs = dict(
        workspace_id="strict-ws",
        key_hash=key.hash,
        estimate=60,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="m",
        provider="openai",
        requested_model_id=None,
        candidate_model_ids=["m"],
        region="us",
        endpoint_id="e",
        candidate_endpoint_ids=["e"],
        idempotency_key=None,
        idempotency_fingerprint=None,
        expires_at=utcnow(),
        strict_budget=True,
        window_limits=None,
    )
    first, _ = store.authorize_gateway_typed(**kwargs)
    assert first == "accepted"
    second, _ = store.authorize_gateway_typed(**kwargs)
    assert second == "key_window_limit_exceeded:daily"
    assert second.rate_limit.remaining == 40
    assert db.typed["tr_key_limit"][(key.hash, 0)]["reserved"] == 60
    alerted, _ = store.authorize_gateway_typed(**kwargs, strict_budget_alert_only=True)
    assert alerted == "accepted"
