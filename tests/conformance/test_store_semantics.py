"""Behavioural contract every storage backend must satisfy.

Each test names one property and the failure it prevents. They are written
against the `Store` Protocol only, so a new backend (Postgres, DSQL,
CockroachDB) is validated by registering it in `conftest.BACKENDS` — no test
changes.

The properties chosen here are the ones where a backend can diverge *silently*
and expensively: exactly-once credit, single-use secrets, and index ordering.
An implementation that is more permissive than the contract (double-crediting
on retry, letting a token be redeemed twice) passes the existing structural
conformance test and fails these.

Known limits of this suite — read before trusting it
----------------------------------------------------
* Most single-use-secret tests are sequential. Credit reservation is the
  exception: its oversubscription test uses real threads against the same
  store, which means separate pooled connections on server-backed backends.
* The `Store` protocol exposes no backend-neutral balance read: `CreditAccount`
  became metadata-only, and the money snapshot lives on backend-specific
  methods (`credit_money_snapshot`, `typed_credit_snapshot`). The reservation
  tests therefore assert balances through observable capacity: reserve the
  exact expected remainder, then prove one more microdollar is rejected.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from trusted_router.store_protocol import Store
from trusted_router.typed_balance import live_credit_summary

from .conftest import BACKENDS, make_benchmark_sample, make_synthetic_probe_sample

# --------------------------------------------------------------------------
# Exactly-once money
# --------------------------------------------------------------------------


def test_credit_workspace_once_is_idempotent_per_event(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Re-applying the same credit event MUST NOT credit twice.

    This is the retry path for Stripe webhooks and top-ups: the same event_id
    is delivered again after a timeout. A backend that returns True twice has
    given the customer free money. The boolean is the contract — True means
    "applied now", False means "already applied".
    """
    event = f"evt-{unique}-A"
    assert store.credit_workspace_once(workspace_id, 5_000, event) is True
    assert store.credit_workspace_once(workspace_id, 5_000, event) is False


def test_credit_workspace_once_distinguishes_events(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Idempotency is keyed on the event, not the amount or workspace.

    Guards the opposite error from the test above: a backend that dedupes too
    aggressively (e.g. keys on workspace+amount) would silently swallow a
    second, legitimate top-up of the same size. The third call proves a
    rejected duplicate did not poison the dedupe state for later events.
    """
    assert store.credit_workspace_once(workspace_id, 5_000, f"evt-{unique}-A") is True
    assert store.credit_workspace_once(workspace_id, 5_000, f"evt-{unique}-A") is False
    assert store.credit_workspace_once(workspace_id, 5_000, f"evt-{unique}-B") is True


def test_guarded_debit_is_exactly_once_and_writes_one_negative_movement(
    store: Store,
    workspace_id: str,
    unique: str,
) -> None:
    assert store.credit_workspace_once(workspace_id, 100, f"evt-fund-debit-{unique}")
    event_id = f"evt-debit-{unique}"

    assert (
        store.debit_workspace_guarded(
            workspace_id,
            60,
            event_id,
            kind="verification_fee",
            authorization_id=f"auth-{unique}",
        )
        == "accepted"
    )
    assert (
        store.debit_workspace_guarded(
            workspace_id,
            60,
            event_id,
            kind="verification_fee",
        )
        == "duplicate"
    )
    summary = live_credit_summary(workspace_id, store=store)
    assert summary is not None
    assert summary["total_credits"] == 40
    movements = store.list_credit_movements(workspace_id)
    assert [(movement.kind, movement.amount_microdollars) for movement in movements] == [
        ("verification_fee", -60)
    ]
    assert movements[0].authorization_id == f"auth-{unique}"


def test_guarded_debit_never_overdraws_or_records_a_rejected_attempt(
    store: Store,
    workspace_id: str,
    unique: str,
) -> None:
    assert store.credit_workspace_once(workspace_id, 100, f"evt-fund-overdraw-{unique}")
    assert (
        store.debit_workspace_guarded(
            workspace_id,
            101,
            f"evt-overdraw-{unique}",
            kind="adjustment",
        )
        == "insufficient"
    )
    summary = live_credit_summary(workspace_id, store=store)
    assert summary is not None
    assert summary["available"] == 100
    assert store.list_credit_movements(workspace_id) == []


def test_user_earnings_seed_and_payout_are_idempotent(
    store: Store,
    user_id: str,
    unique: str,
) -> None:
    store.ensure_earnings_account(user_id)
    assert store.earnings_summary(user_id) == {
        "total_earned": 0,
        "total_transferred": 0,
        "available": 0,
    }
    event_id = f"evt-payout-{unique}"
    assert store.credit_user_earnings(
        user_id,
        75,
        event_id,
        custom_model_id="model-a",
        payer_workspace_id="ws-payer",
    )
    assert not store.credit_user_earnings(user_id, 75, event_id)
    assert store.earnings_summary(user_id) == {
        "total_earned": 75,
        "total_transferred": 0,
        "available": 75,
    }
    movement = store.list_credit_movements(f"user:{user_id}")[0]
    assert movement.kind == "custom_model_payout"
    assert movement.amount_microdollars == 75
    assert movement.counterparty_account_id == "ws-payer"
    assert movement.custom_model_id == "model-a"


def test_user_earnings_credit_seeds_an_absent_account(
    store: Store,
    user_id: str,
    unique: str,
) -> None:
    assert store.credit_user_earnings(user_id, 9, f"evt-bare-payout-{unique}")
    assert store.earnings_summary(user_id)["available"] == 9


def test_transfer_earnings_is_atomic_idempotent_and_visible_in_workspace(
    store: Store,
    user_id: str,
    workspace_id: str,
    unique: str,
) -> None:
    assert store.credit_user_earnings(user_id, 100, f"evt-transfer-fund-{unique}")
    event_id = f"evt-transfer-{unique}"
    assert store.transfer_earnings_to_workspace(user_id, workspace_id, 60, event_id) == "accepted"
    assert store.transfer_earnings_to_workspace(user_id, workspace_id, 60, event_id) == "duplicate"
    assert store.earnings_summary(user_id) == {
        "total_earned": 100,
        "total_transferred": 60,
        "available": 40,
    }
    summary = live_credit_summary(workspace_id, store=store)
    assert summary is not None
    assert summary["total_credits"] == 60
    user_movements = store.list_credit_movements(f"user:{user_id}", kinds=["earnings_transfer_out"])
    workspace_movements = store.list_credit_movements(workspace_id, kinds=["earnings_transfer_in"])
    assert len(user_movements) == len(workspace_movements) == 1
    assert user_movements[0].amount_microdollars == -60
    assert user_movements[0].counterparty_account_id == workspace_id
    assert workspace_movements[0].amount_microdollars == 60
    assert workspace_movements[0].counterparty_account_id == f"user:{user_id}"


def test_insufficient_earnings_transfer_leaves_both_accounts_unchanged(
    store: Store,
    user_id: str,
    workspace_id: str,
    unique: str,
) -> None:
    assert store.credit_user_earnings(user_id, 20, f"evt-low-fund-{unique}")
    assert (
        store.transfer_earnings_to_workspace(
            user_id,
            workspace_id,
            21,
            f"evt-low-transfer-{unique}",
        )
        == "insufficient"
    )
    assert store.earnings_summary(user_id)["available"] == 20
    summary = live_credit_summary(workspace_id, store=store)
    assert summary is not None
    assert summary["total_credits"] == 0
    assert store.list_credit_movements(f"user:{user_id}", kinds=["earnings_transfer_out"]) == []
    assert store.list_credit_movements(workspace_id, kinds=["earnings_transfer_in"]) == []


def test_custom_model_earnings_aggregate_groups_only_payouts_by_model(
    store: Store,
    user_id: str,
    unique: str,
) -> None:
    assert store.credit_user_earnings(
        user_id,
        30,
        f"evt-model-a1-{unique}",
        custom_model_id="model-a",
    )
    assert store.credit_user_earnings(
        user_id,
        12,
        f"evt-model-a2-{unique}",
        custom_model_id="model-a",
    )
    assert store.credit_user_earnings(
        user_id,
        8,
        f"evt-model-b-{unique}",
        custom_model_id="model-b",
    )
    assert store.custom_model_earnings_by_model(
        user_id,
        since="1970-01-01T00:00:00Z",
    ) == {"model-a": 42, "model-b": 8}


def test_credit_movement_listing_is_newest_first_filterable_and_bounded(
    store: Store,
    user_id: str,
    unique: str,
) -> None:
    first_id = f"evt-list-a-{unique}"
    second_id = f"evt-list-b-{unique}"
    assert store.credit_user_earnings(
        user_id,
        1,
        first_id,
        custom_model_id="model-a",
    )
    assert store.credit_user_earnings(
        user_id,
        2,
        second_id,
        custom_model_id="model-b",
    )

    account_id = f"user:{user_id}"
    movements = store.list_credit_movements(
        account_id,
        kinds=["custom_model_payout"],
        limit=1,
    )
    assert [movement.movement_id for movement in movements] == [second_id]
    assert store.list_credit_movements(account_id, kinds=[]) == []
    all_movements = store.list_credit_movements(account_id)
    assert (
        store.list_credit_movements(
            account_id,
            before=min(movement.created_at for movement in all_movements),
        )
        == []
    )


def test_lifetime_topup_is_part_of_the_grant_claim(
    store: Store,
    workspace_id: str,
    user_id: str,
    unique: str,
) -> None:
    assert store.get_lifetime_topup_microdollars(f"unknown-{unique}") == 0
    event_id = f"evt-lifetime-{unique}"
    assert store.credit_workspace_typed_direct(
        workspace_id,
        25,
        event_id,
        lifetime_topup_user_id=user_id,
    )
    assert not store.credit_workspace_typed_direct(
        workspace_id,
        25,
        event_id,
        lifetime_topup_user_id=user_id,
    )
    assert store.credit_workspace_typed_direct(
        workspace_id,
        10,
        f"{event_id}-second",
        lifetime_topup_user_id=user_id,
    )
    assert store.get_lifetime_topup_microdollars(user_id) == 35


def _credit_and_key(
    store: Store,
    workspace_id: str,
    unique: str,
    amount_microdollars: int,
) -> str:
    assert (
        store.credit_workspace_once(
            workspace_id,
            amount_microdollars,
            f"evt-reservation-{unique}",
        )
        is True
    )
    _raw_key, key = store.create_api_key(
        workspace_id=workspace_id,
        name=f"reservation-{unique}",
        creator_user_id=None,
    )
    return key.hash


def _assert_exact_available_capacity(
    store: Store,
    workspace_id: str,
    key_hash: str,
    amount_microdollars: int,
    unique: str,
) -> None:
    store.reserve(
        workspace_id,
        key_hash,
        amount_microdollars,
        idempotency_key=f"capacity-{unique}",
    )
    with pytest.raises(ValueError, match="insufficient credits"):
        store.reserve(workspace_id, key_hash, 1)


def test_reserve_then_settle_less_releases_unused_hold(
    store: Store, workspace_id: str, unique: str
) -> None:
    """A 60 microdollar hold settled at 20 leaves exactly 80 of 100."""
    key_hash = _credit_and_key(store, workspace_id, unique, 100)
    reservation = store.reserve(workspace_id, key_hash, 60)

    store.settle(reservation.id, 20)

    _assert_exact_available_capacity(store, workspace_id, key_hash, 80, unique)


def test_reserve_then_settle_more_books_full_actual(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Settlement may exceed the hold and can make available credit negative."""
    key_hash = _credit_and_key(store, workspace_id, unique, 100)
    reservation = store.reserve(workspace_id, key_hash, 60)

    store.settle(reservation.id, 120)

    # Correct state is credits=100, usage=120, reserved=0: a 20 top-up only
    # reaches zero, and the next microdollar creates exactly one of capacity.
    assert store.credit_workspace_once(workspace_id, 20, f"evt-overage-zero-{unique}") is True
    with pytest.raises(ValueError, match="insufficient credits"):
        store.reserve(workspace_id, key_hash, 1)
    assert store.credit_workspace_once(workspace_id, 1, f"evt-overage-positive-{unique}") is True
    _assert_exact_available_capacity(store, workspace_id, key_hash, 1, unique)


def test_reserve_then_refund_restores_exact_balance(
    store: Store, workspace_id: str, unique: str
) -> None:
    key_hash = _credit_and_key(store, workspace_id, unique, 100)
    reservation = store.reserve(workspace_id, key_hash, 60)

    store.refund(reservation.id)

    _assert_exact_available_capacity(store, workspace_id, key_hash, 100, unique)


def test_settle_is_idempotent(store: Store, workspace_id: str, unique: str) -> None:
    """A replay releases and charges once, even after the first call returned."""
    key_hash = _credit_and_key(store, workspace_id, unique, 100)
    reservation = store.reserve(workspace_id, key_hash, 60)

    store.settle(reservation.id, 20)
    store.settle(reservation.id, 20)

    _assert_exact_available_capacity(store, workspace_id, key_hash, 80, unique)


def test_refund_is_idempotent(store: Store, workspace_id: str, unique: str) -> None:
    """A refund replay releases the recorded hold exactly once."""
    key_hash = _credit_and_key(store, workspace_id, unique, 100)
    reservation = store.reserve(workspace_id, key_hash, 60)

    store.refund(reservation.id)
    store.refund(reservation.id)

    _assert_exact_available_capacity(store, workspace_id, key_hash, 100, unique)


def test_concurrent_reserves_cannot_oversubscribe(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Two simultaneous full-balance holds cannot both pass the predicate."""
    key_hash = _credit_and_key(store, workspace_id, unique, 100)
    ready = threading.Barrier(3)
    result_lock = threading.Lock()
    reservations: list[object] = []
    errors: list[Exception] = []

    def reserve_once() -> None:
        ready.wait()
        try:
            reservation = store.reserve(workspace_id, key_hash, 100)
        except Exception as exc:
            with result_lock:
                errors.append(exc)
        else:
            with result_lock:
                reservations.append(reservation)

    threads = [threading.Thread(target=reserve_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    ready.wait()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads), "reserve threads hung"
    assert len(reservations) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert str(errors[0]) == "insufficient credits"
    with pytest.raises(ValueError, match="insufficient credits"):
        store.reserve(workspace_id, key_hash, 1)


def test_insufficient_reserve_does_not_mutate_balance(
    store: Store, workspace_id: str, unique: str
) -> None:
    key_hash = _credit_and_key(store, workspace_id, unique, 50)

    with pytest.raises(ValueError, match="insufficient credits"):
        store.reserve(workspace_id, key_hash, 51)

    _assert_exact_available_capacity(store, workspace_id, key_hash, 50, unique)


def test_record_sns_message_once_is_idempotent(store: Store, unique: str) -> None:
    """SNS redelivers; bounce/complaint processing must run once.

    Without this, one bounce notification replayed by SNS can suppress an
    address twice or double-count a complaint.
    """
    assert store.record_sns_message_once(f"msg-{unique}-1") is True
    assert store.record_sns_message_once(f"msg-{unique}-1") is False
    assert store.record_sns_message_once(f"msg-{unique}-2") is True


# --------------------------------------------------------------------------
# Single-use secrets
# --------------------------------------------------------------------------


def test_wallet_challenge_is_single_use(store: Store) -> None:
    """A SIWE nonce must be redeemable exactly once.

    Replayable nonces are a signature-replay authentication bypass.
    """
    raw_nonce, _challenge = store.create_wallet_challenge(
        address="0xabc", message="sign this", ttl_seconds=300
    )
    assert store.consume_wallet_challenge(raw_nonce) is not None
    assert store.consume_wallet_challenge(raw_nonce) is None


def test_unknown_wallet_challenge_returns_none(store: Store, unique: str) -> None:
    """An unissued nonce must not authenticate anything."""
    assert store.consume_wallet_challenge(f"never-issued-{unique}") is None


def test_verification_token_is_single_use(store: Store, user_id: str) -> None:
    """Email-verification links land in inboxes and get clicked twice."""
    raw_token, _token = store.create_verification_token(
        user_id=user_id, purpose="verify_email", ttl_seconds=300
    )
    assert store.consume_verification_token(raw_token, purpose="verify_email") is not None
    assert store.consume_verification_token(raw_token, purpose="verify_email") is None


def test_verification_token_is_scoped_to_its_purpose(store: Store, user_id: str) -> None:
    """A token minted for one purpose must not satisfy another, and a failed
    attempt must not burn it.

    Otherwise a low-value token (email verification) could be redeemed on a
    high-value flow (password reset) — privilege escalation via token reuse.
    The second half matters just as much: a backend that consumes the token
    *while* rejecting the wrong purpose turns any attacker who guesses a token
    into a denial-of-service on the legitimate user's verification link.
    """
    raw_token, _token = store.create_verification_token(
        user_id=user_id, purpose="verify_email", ttl_seconds=300
    )
    assert store.consume_verification_token(raw_token, purpose="password_reset") is None
    # Still redeemable for what it was actually minted for.
    assert store.consume_verification_token(raw_token, purpose="verify_email") is not None


def test_oauth_authorization_code_is_single_use(store: Store, workspace_id: str) -> None:
    """OAuth codes are single-use by RFC 6749; replay must fail."""
    raw_code, _code = store.create_oauth_authorization_code(
        workspace_id=workspace_id,
        user_id=None,
        callback_url="https://example.com/cb",
        key_label="conformance",
        ttl_seconds=300,
        app_id=1,
    )
    assert store.consume_oauth_authorization_code(raw_code) is not None
    assert store.consume_oauth_authorization_code(raw_code) is None


# --------------------------------------------------------------------------
# Read-your-writes and lifecycle
# --------------------------------------------------------------------------


def test_workspace_is_readable_immediately_after_creation(store: Store, workspace_id: str) -> None:
    """No backend may require a settling delay for its own write.

    Route code creates a workspace and immediately reads it back in the same
    request. An eventually-consistent backend would 404 intermittently.
    """
    fetched = store.get_workspace(workspace_id)
    assert fetched is not None
    assert str(fetched.id) == workspace_id


def test_api_key_lookups_agree_and_delete_revokes(store: Store, workspace_id: str) -> None:
    """Every key lookup path must resolve to the same key, and delete must
    actually revoke it.

    The three lookups are separate code paths (and on the typed backend,
    separate tables). If they disagree, authentication and revocation
    disagree — a deleted key that still authenticates is the worst case.
    """
    raw_key, created = store.create_api_key(
        workspace_id=workspace_id, name="conformance", creator_user_id=None
    )
    by_raw = store.get_key_by_raw(raw_key)
    by_hash = store.get_key_by_hash(created.hash)
    by_lookup = store.get_key_by_lookup_hash(created.lookup_hash)
    assert by_raw is not None
    assert by_hash is not None
    assert by_lookup is not None
    assert by_raw.hash == by_hash.hash == by_lookup.hash == created.hash

    assert store.delete_key(created.hash) is True
    assert store.get_key_by_raw(raw_key) is None
    assert store.get_key_by_hash(created.hash) is None
    assert store.get_key_by_lookup_hash(created.lookup_hash) is None


def test_auth_session_lifecycle(store: Store, user_id: str) -> None:
    """Create -> read -> delete -> gone. Logout must actually invalidate."""
    raw_token, _session = store.create_auth_session(
        user_id=user_id, provider="google", label="conformance", ttl_seconds=300
    )
    assert store.get_auth_session_by_raw(raw_token) is not None
    assert store.delete_auth_session_by_raw(raw_token) is True
    assert store.get_auth_session_by_raw(raw_token) is None


# --------------------------------------------------------------------------
# Index / scan semantics
# --------------------------------------------------------------------------


def test_benchmark_samples_return_newest_first(store: Store, unique: str) -> None:
    """The index contract is reverse-chronological order.

    Route-health and the leaderboard both read "the newest N samples" and
    treat element 0 as current state. On Bigtable this ordering is a property
    of the reverse-timestamp row key; on SQL it must come from an explicit
    ORDER BY. A backend that returns insertion order breaks freshness logic
    without any error.
    """
    provider, model = f"acme-{unique}", f"acme-{unique}/m1"
    for idx, created_at in enumerate(
        ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"]
    ):
        store.record_provider_benchmark(
            make_benchmark_sample(
                sample_id=f"{unique}-s{idx}",
                provider=provider,
                model=model,
                created_at=created_at,
            )
        )
    samples = store.provider_benchmark_samples(provider=provider, model=model, limit=10)
    assert len(samples) == 3
    timestamps = [s.created_at for s in samples]
    assert timestamps == sorted(timestamps, reverse=True), (
        f"expected newest-first, got {timestamps}"
    )


def test_benchmark_samples_respect_limit(store: Store, unique: str) -> None:
    """`limit` must cap the result set.

    Route-health sizes its statistical window with this argument; a backend
    that ignores it silently changes the alert threshold.
    """
    provider, model = f"acme-{unique}", f"acme-{unique}/m1"
    for idx in range(5):
        store.record_provider_benchmark(
            make_benchmark_sample(
                sample_id=f"{unique}-s{idx}",
                provider=provider,
                model=model,
                created_at=f"2026-01-0{idx + 1}T00:00:00+00:00",
            )
        )
    assert len(store.provider_benchmark_samples(provider=provider, model=model, limit=2)) == 2


def test_benchmark_samples_filter_by_route(store: Store, unique: str) -> None:
    """Provider/model filtering must not leak other routes' samples.

    A leak here silently mixes another model's health into a route's failure
    rate — which is exactly how a false alert (or a missed one) is born.
    """
    provider, model = f"acme-{unique}", f"acme-{unique}/m1"
    store.record_provider_benchmark(
        make_benchmark_sample(sample_id=f"{unique}-a", provider=provider, model=model)
    )
    store.record_provider_benchmark(
        make_benchmark_sample(
            sample_id=f"{unique}-b", provider=f"other-{unique}", model=f"other-{unique}/m2"
        )
    )
    samples = store.provider_benchmark_samples(provider=provider, model=model, limit=10)
    assert [s.id for s in samples] == [f"{unique}-a"]


# --------------------------------------------------------------------------
# Public-status synthetic samples + rollups
# --------------------------------------------------------------------------


def test_synthetic_probe_samples_return_newest_first_and_respect_limit(
    store: Store, unique: str
) -> None:
    """The public hot path asks for the newest bounded live-sample window."""
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    target = f"status-{unique}"
    probe_type = f"probe-{unique}"
    monitor_region = f"monitor-{unique}"
    for idx, minutes_ago in enumerate((3, 2, 1)):
        store.record_synthetic_probe_sample(
            make_synthetic_probe_sample(
                sample_id=f"{unique}-synthetic-{idx}",
                target=target,
                probe_type=probe_type,
                monitor_region=monitor_region,
                created_at=_iso_utc(now - dt.timedelta(minutes=minutes_ago)),
            )
        )

    samples = store.synthetic_probe_samples(
        target=target,
        probe_type=probe_type,
        monitor_region=monitor_region,
        limit=2,
    )

    assert [sample.id for sample in samples] == [
        f"{unique}-synthetic-2",
        f"{unique}-synthetic-1",
    ]


def test_synthetic_probe_samples_apply_status_reader_filters(store: Store, unique: str) -> None:
    """Date and route dimensions must not leak unrelated deployment checks."""
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    date = now.date().isoformat()
    target = f"status-{unique}"
    probe_type = f"probe-{unique}"
    monitor_region = f"monitor-{unique}"
    dimensions = (
        ("match", target, probe_type, monitor_region, now),
        ("wrong-target", f"other-{unique}", probe_type, monitor_region, now),
        ("wrong-probe", target, f"other-probe-{unique}", monitor_region, now),
        ("wrong-monitor", target, probe_type, f"other-monitor-{unique}", now),
        ("wrong-date", target, probe_type, monitor_region, now - dt.timedelta(days=1)),
    )
    for suffix, sample_target, sample_probe, sample_monitor, created_at in dimensions:
        store.record_synthetic_probe_sample(
            make_synthetic_probe_sample(
                sample_id=f"{unique}-{suffix}",
                target=sample_target,
                probe_type=sample_probe,
                monitor_region=sample_monitor,
                created_at=_iso_utc(created_at),
            )
        )

    samples = store.synthetic_probe_samples(
        date=date,
        target=target,
        probe_type=probe_type,
        monitor_region=monitor_region,
        limit=10,
    )

    assert [sample.id for sample in samples] == [f"{unique}-match"]


def test_synthetic_rollups_apply_ranges_order_limit_and_histogram_option(
    store: Store, unique: str
) -> None:
    """Status history uses inclusive period ranges and newest-N ordering."""
    # The window must be in the PAST and inside ROLLUP_RETENTION_MONTHS.
    # An earlier version made it "practically unique" by basing it at
    # 2100-01-01 (+ up to ~5,700 years of offset) — and a run pointed at
    # a live store wrote year-7748 rows into production, where they
    # sorted first in every newest-first read and permanently pinned the
    # staleness detector's "latest sample". Future-dated samples are now
    # rejected at ingest and filtered at read, so a future-based range
    # would fail those guards anyway. Uniqueness against persistent
    # conformance databases comes from filtering assertions to this
    # test's own target below, not from an exclusive time range.
    # Stay well inside ROLLUP_RETENTION_MONTHS (24). An earlier version of
    # this fix used a ~17,000 hour spread (~23 months), which could place
    # the window a month or two outside retention depending on `unique`
    # and the day of the month — rollup_is_within_retention then filtered
    # every row and the assertions saw an empty list. 4,000 hours (~166
    # days) keeps every generated window recent, and uniqueness comes from
    # filtering to this test's own target below, not from the time range.
    now = dt.datetime.now(dt.UTC).replace(minute=10, second=0, microsecond=0) - dt.timedelta(
        hours=3 + int(unique, 16) % 4_000
    )
    target = f"status-{unique}"
    probe_type = f"probe-{unique}"
    monitor_region = f"monitor-{unique}"
    samples = [
        make_synthetic_probe_sample(
            sample_id=f"{unique}-rollup-{idx}",
            target=target,
            probe_type=probe_type,
            monitor_region=monitor_region,
            created_at=_iso_utc(now - dt.timedelta(hours=hours_ago)),
            latency_milliseconds=40 + idx,
            ttfb_milliseconds=20 + idx,
        )
        for idx, hours_ago in enumerate((2, 1, 0))
    ]
    for sample in samples:
        store.record_synthetic_probe_sample(sample)
    # Re-delivery must not increment the aggregate twice.
    store.record_synthetic_probe_sample(samples[-1])

    oldest_start = _iso_utc((now - dt.timedelta(hours=2)).replace(minute=0))
    newest_start = _iso_utc(now.replace(minute=0))
    middle_start = _iso_utc((now - dt.timedelta(hours=1)).replace(minute=0))
    # A persistent conformance database may hold foreign rows in the same
    # hours, so exact-membership assertions go through this test's own
    # target; the limit clause is asserted as a pure cap + newest-first
    # prefix property, which holds regardless of what else is present.
    full = store.synthetic_rollups(
        period="hour",
        since=oldest_start,
        until=newest_start,
        limit=1000,
    )
    own_full = [row for row in full if row.target == target]
    assert [row.period_start for row in own_full] == [newest_start, middle_start, oldest_start]
    capped = store.synthetic_rollups(
        period="hour",
        since=oldest_start,
        until=newest_start,
        limit=2,
    )
    assert len(capped) == 2
    assert [row.id for row in capped] == [row.id for row in full[:2]]

    ranged = store.synthetic_rollups(
        period="hour",
        since=middle_start,
        until=newest_start,
        include_histograms=False,
        limit=1000,
    )

    assert [row.period_start for row in ranged] == sorted(
        (row.period_start for row in ranged),
        reverse=True,
    )
    assert all(row.period == "hour" for row in ranged)
    assert all(middle_start <= row.period_start <= newest_start for row in ranged)
    assert all(row.latency_histogram == {} for row in ranged)
    assert all(row.ttfb_histogram == {} for row in ranged)
    assert all(row.dns_histogram == {} for row in ranged)
    assert all(row.tcp_connect_histogram == {} for row in ranged)
    assert all(row.tls_handshake_histogram == {} for row in ranged)
    assert all(row.gateway_processing_histogram == {} for row in ranged)
    own_rows = [
        row
        for row in ranged
        if row.target == target
        and row.probe_type == probe_type
        and row.monitor_region == monitor_region
    ]
    assert [row.period_start for row in own_rows] == [newest_start, middle_start]
    assert own_rows[0].sample_count == 1


# --------------------------------------------------------------------------
# Coverage guard
# --------------------------------------------------------------------------


def test_memory_backend_is_always_runnable() -> None:
    """Tripwire: a suite where every backend skipped proves nothing.

    This deliberately does NOT take the `store` fixture. A guard that depends
    on the parametrized fixture is itself skipped when every backend skips,
    so pytest would exit 0 having exercised no backend at all — the guard
    would be part of the illusion it exists to prevent.
    """
    assert "memory" in BACKENDS
    store = BACKENDS["memory"]()
    assert isinstance(store, Store)


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Schema shape the key-limit call family depends on
# --------------------------------------------------------------------------


def test_key_limit_window_columns_and_index_exist(store: Store, unique: str) -> None:
    """tr_key_limit must carry the lazy rolling-window counters.

    reserve/settle/refund_key_limit enforce day/week/month caps by reading
    *_usage against a *_start floor. If the columns are missing the call
    family cannot be implemented at all — and because CREATE TABLE IF NOT
    EXISTS is a no-op on an existing table, a store created before they
    were added would silently lack them forever without the ALTERs.

    Skips on backends that do not expose an introspectable SQL schema
    (the in-memory store has no DDL); the point is to pin Postgres/DSQL.
    """
    pool = getattr(store, "_pool", None)
    if pool is None:
        pytest.skip("backend has no SQL schema to introspect")

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tr_key_limit'"
        ).fetchall()
    columns = {str(row[0]) for row in rows}

    for window in ("day", "week", "month"):
        assert f"{window}_usage" in columns, f"missing {window}_usage"
        assert f"{window}_start" in columns, f"missing {window}_start"
        # The limit columns predate this change; assert them too so a
        # regression in either half is caught by one test.
        assert f"{window}_limit_micro" in columns, f"missing {window}_limit_micro"


# --------------------------------------------------------------------------
# Per-key spend caps (reserve/settle/refund_key_limit)
#
# Seeded through direct SQL because update_key is still _not_implemented on
# PostgresStore; these skip on backends with no SQL pool. The semantic
# reference is InMemoryApiKeys.reserve_limit — same rules, different engine.
# --------------------------------------------------------------------------


def _seed_key_limit(store: Store, key_hash: str, **columns: object) -> None:
    pool = getattr(store, "_pool", None)
    if pool is None:
        pytest.skip("backend has no SQL schema to seed")
    cols = {"workspace_id": "ws-keylimit", "key_hash": key_hash, "shard": 0, **columns}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with pool.connection() as conn:
        conn.execute("DELETE FROM tr_key_limit WHERE key_hash = %s", (key_hash,), prepare=False)
        # noqa justified: column names come from this function's own
        # keyword arguments, never from test data or user input; values are
        # still bound as parameters.
        conn.execute(
            f"INSERT INTO tr_key_limit ({names}) VALUES ({marks})",  # noqa: S608
            tuple(cols.values()),
            prepare=False,
        )


def _key_limit_row(store: Store, key_hash: str) -> dict[str, object]:
    with store._pool.connection() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT reserved, day_usage, day_start FROM tr_key_limit"
            " WHERE key_hash = %s AND shard = 0",
            (key_hash,),
            prepare=False,
        ).fetchone()
    return {"reserved": row[0], "day_usage": row[1], "day_start": row[2]}


def test_reserve_key_limit_enforces_lifetime_cap(store: Store, unique: str) -> None:
    kh = f"kl-cap-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=100, usage=0, byok_usage=0, reserved=0, include_byok=True
    )

    store.reserve_key_limit(kh, 60, usage_type="Credits")
    assert _key_limit_row(store, kh)["reserved"] == 60

    # 60 held + 60 requested > 100: the predicate must reject, not oversubscribe.
    with pytest.raises(ValueError):
        store.reserve_key_limit(kh, 60, usage_type="Credits")
    assert _key_limit_row(store, kh)["reserved"] == 60


def test_reserve_key_limit_uncapped_is_noop(store: Store, unique: str) -> None:
    kh = f"kl-uncapped-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=None, usage=0, byok_usage=0, reserved=0, include_byok=True
    )
    store.reserve_key_limit(kh, 10_000_000, usage_type="Credits")
    assert _key_limit_row(store, kh)["reserved"] == 0


def test_reserve_key_limit_byok_excluded_is_noop(store: Store, unique: str) -> None:
    """A BYOK request against a key that excludes BYOK must not consume the
    key's cap — the customer is paying the provider directly."""
    kh = f"kl-byok-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=100, usage=0, byok_usage=0, reserved=0, include_byok=False
    )
    store.reserve_key_limit(kh, 5_000, usage_type="BYOK")
    assert _key_limit_row(store, kh)["reserved"] == 0


def test_reserve_key_limit_counts_byok_when_included(store: Store, unique: str) -> None:
    kh = f"kl-byok-in-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=100, usage=0, byok_usage=90, reserved=0, include_byok=True
    )
    # 90 BYOK already used against a 100 cap leaves 10.
    with pytest.raises(ValueError):
        store.reserve_key_limit(kh, 20, usage_type="Credits")
    store.reserve_key_limit(kh, 10, usage_type="Credits")


def test_reserve_key_limit_window_blocks_before_lifetime(store: Store, unique: str) -> None:
    """Window caps are independent of the lifetime cap and raise a typed
    error carrying the window, so the gateway can send Retry-After."""
    from trusted_router.spend_windows import KeyWindowLimitExceeded, window_floors
    from trusted_router.storage_models import utcnow

    kh = f"kl-window-{unique}"
    floors = window_floors(utcnow())
    _seed_key_limit(
        store,
        kh,
        limit_micro=1_000_000,
        usage=0,
        byok_usage=0,
        reserved=0,
        include_byok=True,
        day_limit_micro=100,
        day_usage=95,
        day_start=floors["daily"],
    )
    with pytest.raises(KeyWindowLimitExceeded) as excinfo:
        store.reserve_key_limit(kh, 10, usage_type="Credits")
    assert excinfo.value.window == "daily"
    # Under the window cap still succeeds against the same row.
    store.reserve_key_limit(kh, 5, usage_type="Credits")


def test_stale_window_start_reads_as_zero(store: Store, unique: str) -> None:
    """The lazy-window rule: a *_start older than the current floor means the
    window has not started this period, so usage reads ZERO. Without this a
    key that hit its daily cap once would be blocked forever — there is no
    reset job, by design."""
    from trusted_router.spend_windows import window_floors
    from trusted_router.storage_models import utcnow

    kh = f"kl-stale-{unique}"
    floors = window_floors(utcnow())
    _seed_key_limit(
        store,
        kh,
        limit_micro=1_000_000,
        usage=0,
        byok_usage=0,
        reserved=0,
        include_byok=True,
        day_limit_micro=100,
        day_usage=999_999,
        day_start=floors["daily"] - dt.timedelta(days=3),
    )
    store.reserve_key_limit(kh, 50, usage_type="Credits")


def test_settle_key_limit_releases_hold_and_rolls_window(store: Store, unique: str) -> None:
    kh = f"kl-settle-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=1_000, usage=0, byok_usage=0, reserved=0, include_byok=True
    )
    store.reserve_key_limit(kh, 60, usage_type="Credits")
    store.settle_key_limit(kh, 60, 20, usage_type="Credits")

    row = _key_limit_row(store, kh)
    assert row["reserved"] == 0, "hold must be released"
    assert row["day_usage"] == 20, "window counter books the ACTUAL cost"
    assert row["day_start"] is not None


def test_settle_cannot_drive_reserved_negative(store: Store, unique: str) -> None:
    """A duplicate settle must not hand the key free headroom."""
    kh = f"kl-dup-settle-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=1_000, usage=0, byok_usage=0, reserved=0, include_byok=True
    )
    store.reserve_key_limit(kh, 50, usage_type="Credits")
    store.settle_key_limit(kh, 50, 10, usage_type="Credits")
    store.settle_key_limit(kh, 50, 10, usage_type="Credits")
    assert _key_limit_row(store, kh)["reserved"] == 0


def test_refund_key_limit_releases_without_booking_usage(store: Store, unique: str) -> None:
    kh = f"kl-refund-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=1_000, usage=0, byok_usage=0, reserved=0, include_byok=True
    )
    store.reserve_key_limit(kh, 60, usage_type="Credits")
    store.refund_key_limit(kh, 60, usage_type="Credits")

    row = _key_limit_row(store, kh)
    assert row["reserved"] == 0
    assert (row["day_usage"] or 0) == 0, "a refunded request spent nothing"


def test_key_limit_calls_on_missing_row_are_noops(store: Store, unique: str) -> None:
    """No typed row means nothing to enforce — must not raise. The gateway's
    own KEY_MISSING path handles the authorize-side decision."""
    _seed_key_limit(store, f"kl-present-{unique}", limit_micro=100)
    absent = f"kl-absent-{unique}"
    store.reserve_key_limit(absent, 10, usage_type="Credits")
    store.settle_key_limit(absent, 10, 5, usage_type="Credits")
    store.refund_key_limit(absent, 10, usage_type="Credits")


def test_concurrent_key_limit_reserves_cannot_oversubscribe(store: Store, unique: str) -> None:
    """Two simultaneous full-cap holds: exactly one may win."""
    kh = f"kl-race-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=100, usage=0, byok_usage=0, reserved=0, include_byok=True
    )
    ready = threading.Barrier(3)
    lock = threading.Lock()
    wins: list[int] = []
    losses: list[Exception] = []

    def reserve_once() -> None:
        ready.wait()
        try:
            store.reserve_key_limit(kh, 100, usage_type="Credits")
        except Exception as exc:
            with lock:
                losses.append(exc)
        else:
            with lock:
                wins.append(1)

    threads = [threading.Thread(target=reserve_once) for _ in range(2)]
    for t in threads:
        t.start()
    ready.wait()
    for t in threads:
        t.join()

    assert len(wins) == 1, f"expected exactly one winner, got {len(wins)} (losses={losses})"
    assert _key_limit_row(store, kh)["reserved"] == 100


# --------------------------------------------------------------------------
# Gateway authorization lifecycle (create -> finalize), exactly once
# --------------------------------------------------------------------------


def _authorize(store: Store, workspace_id: str, key_hash: str, **kw: object) -> object:
    params: dict[str, object] = dict(
        workspace_id=workspace_id,
        key_hash=key_hash,
        model_id="anthropic/claude-opus-4.7",
        provider="anthropic",
        usage_type="Credits",
        estimated_microdollars=100,
        credit_reservation_id=None,
    )
    params.update(kw)
    return store.create_gateway_authorization(**params)  # type: ignore[arg-type]


def test_gateway_authorization_round_trips(store: Store, workspace_id: str, unique: str) -> None:
    auth = _authorize(store, workspace_id, f"gw-{unique}")
    fetched = store.get_gateway_authorization(auth.id)  # type: ignore[attr-defined]
    assert fetched is not None
    assert fetched.id == auth.id  # type: ignore[attr-defined]
    assert fetched.settled is False


def test_gateway_authorization_idempotency_key_dedupes(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Two identical requests must yield ONE authorization.

    An authorization holds a credit reservation; creating two would
    double-hold the customer's money for a single request.
    """
    kh = f"gw-idem-{unique}"
    first = _authorize(store, workspace_id, kh, idempotency_key=f"idem-{unique}")
    second = _authorize(store, workspace_id, kh, idempotency_key=f"idem-{unique}")
    assert first.id == second.id  # type: ignore[attr-defined]

    found = store.get_gateway_authorization_by_idempotency_key(workspace_id, kh, f"idem-{unique}")
    assert found is not None and found.id == first.id  # type: ignore[attr-defined]


def test_gateway_idempotency_is_scoped_per_key(
    store: Store, workspace_id: str, unique: str
) -> None:
    """The same idempotency key under a DIFFERENT api key is a different
    request — otherwise one customer's authorization leaks to another."""
    shared = f"idem-shared-{unique}"
    a = _authorize(store, workspace_id, f"gw-a-{unique}", idempotency_key=shared)
    b = _authorize(store, workspace_id, f"gw-b-{unique}", idempotency_key=shared)
    assert a.id != b.id  # type: ignore[attr-defined]


def test_unknown_idempotency_key_returns_none(store: Store, workspace_id: str, unique: str) -> None:
    assert (
        store.get_gateway_authorization_by_idempotency_key(
            workspace_id, f"gw-none-{unique}", f"never-{unique}"
        )
        is None
    )


def test_finalize_gateway_authorization_is_exactly_once(
    store: Store, workspace_id: str, unique: str
) -> None:
    """True on the first finalize, False on every replay.

    A settle retried after a timeout must not release the hold twice.
    """
    auth = _authorize(store, workspace_id, f"gw-fin-{unique}")
    first = store.finalize_gateway_authorization(
        auth.id,
        success=True,
        actual_microdollars=40,
        selected_usage_type="Credits",  # type: ignore[attr-defined]
    )
    second = store.finalize_gateway_authorization(
        auth.id,
        success=True,
        actual_microdollars=40,
        selected_usage_type="Credits",  # type: ignore[attr-defined]
    )
    assert first is True
    assert second is False, "replayed settle must be a no-op"

    settled = store.get_gateway_authorization(auth.id)  # type: ignore[attr-defined]
    assert settled is not None and settled.settled is True
    assert settled.finalization_outcome == "settled"
    assert settled.finalized_cost_microdollars == 40
    assert settled.finalized_usage_type == "Credits"


def test_finalize_unknown_authorization_is_false_not_error(store: Store, unique: str) -> None:
    assert (
        store.finalize_gateway_authorization(
            f"gwauth-missing-{unique}",
            success=True,
            actual_microdollars=10,
            selected_usage_type="Credits",
        )
        is False
    )


def test_finalize_failure_books_no_usage(store: Store, workspace_id: str, unique: str) -> None:
    """A failed request releases its holds but charges nothing."""
    kh = f"gw-fail-{unique}"
    _seed_key_limit(
        store, kh, limit_micro=1_000, usage=0, byok_usage=0, reserved=0, include_byok=True
    )
    auth = _authorize(store, workspace_id, kh, estimated_microdollars=60)
    store.reserve_key_limit(kh, 60, usage_type="Credits")

    assert (
        store.finalize_gateway_authorization(
            auth.id,
            success=False,
            actual_microdollars=0,
            selected_usage_type="Credits",  # type: ignore[attr-defined]
        )
        is True
    )

    row = _key_limit_row(store, kh)
    assert row["reserved"] == 0, "hold released even on failure"
    assert (row["day_usage"] or 0) == 0, "a failed request spent nothing"
    settled = store.get_gateway_authorization(auth.id)  # type: ignore[attr-defined]
    assert settled is not None
    assert settled.finalization_outcome == "refunded"
    assert settled.finalized_cost_microdollars == 0


def test_federated_key_is_resolvable_on_a_LATER_request(store: Store, unique: str) -> None:
    """A federated key must resolve by lookup hash on every SUBSEQUENT request.

    This is the regression that matters. The first federated request never
    touches this path — the resolve returns the record directly — so a
    lookup-index written under the wrong field name still passes any smoke
    test that makes one call. It fails on request number two, in production,
    for a user who is already authenticated.
    """
    record = {
        "lookup_hash": f"lh-{unique}",
        "key_hash": f"kh-{unique}",
        "workspace_id": f"ws-{unique}",
        "name": "federated",
        "disabled": False,
        "limit_microdollars": 1_000,
        "include_byok_in_limit": True,
        "revision": "2026-08-02T00:00:00Z",
    }
    try:
        stored = store.upsert_federated_api_key(record)
    except NotImplementedError:
        pytest.skip("backend is a federation HOME plane; it does not import keys")

    assert stored.hash == f"kh-{unique}"

    # The second request's path: resolve purely from the lookup index.
    resolved = store.get_key_by_lookup_hash(f"lh-{unique}")
    assert resolved is not None, "federated key vanished on the second lookup"
    assert resolved.hash == f"kh-{unique}"
    assert resolved.workspace_id == f"ws-{unique}"


def test_federated_key_carries_no_secret_material(store: Store, unique: str) -> None:
    """A peer holds no home-issued key material, so the raw-bearer path
    (which verifies secret_hash) can never authenticate a federated key."""
    try:
        store.upsert_federated_api_key(
            {
                "lookup_hash": f"lh2-{unique}",
                "key_hash": f"kh2-{unique}",
                "workspace_id": f"ws2-{unique}",
                "name": "federated",
            }
        )
    except NotImplementedError:
        pytest.skip("backend is a federation HOME plane")

    resolved = store.get_key_by_lookup_hash(f"lh2-{unique}")
    assert resolved is not None
    assert resolved.secret_hash == ""
    assert resolved.salt == ""


def test_federating_a_key_materializes_its_workspace(store: Store, unique: str) -> None:
    """A federated key without a workspace 403s on EVERY request.

    The authorize path reads the workspace before it reads credits, so a
    backend that writes the key but not its workspace produces a key that
    resolves perfectly and can never spend — a failure that looks like a
    billing problem and is not.
    """
    workspace_id = f"ws3-{unique}"
    try:
        store.upsert_federated_api_key(
            {
                "lookup_hash": f"lh3-{unique}",
                "key_hash": f"kh3-{unique}",
                "workspace_id": workspace_id,
                "name": "federated",
            }
        )
    except NotImplementedError:
        pytest.skip("backend is a federation HOME plane; it does not import keys")

    workspace = store.get_workspace(workspace_id)
    assert workspace is not None, "federated key has no workspace to authorize against"
    assert workspace.federated_home, "a shadow must be distinguishable from a real workspace"
    assert workspace.owner_user_id == "", "this plane has no user directory to own it"


# --------------------------------------------------------------------------
# Cross-plane credit transfer
# --------------------------------------------------------------------------
#
# The conservation law, at the backend contract level. These use the same
# observable-capacity trick as the reservation tests above: the Store protocol
# exposes no backend-neutral balance read, so a balance is asserted by proving
# exactly how much can still be moved and that one more microdollar cannot.


def _transferable(store: Store, workspace_id: str, unique: str, amount: int) -> None:
    """Assert exactly `amount` is still transferable, and not one more."""
    store.open_credit_transfer(
        transfer_id=f"cap-{unique}",
        workspace_id=workspace_id,
        amount_microdollars=amount,
        destination="peer",
    )
    with pytest.raises(ValueError, match="insufficient credits"):
        store.open_credit_transfer(
            transfer_id=f"cap-over-{unique}",
            workspace_id=workspace_id,
            amount_microdollars=1,
            destination="peer",
        )


def _fund(store: Store, workspace_id: str, unique: str, amount: int) -> None:
    assert store.credit_workspace_once(workspace_id, amount, f"evt-xfer-{unique}") is True


def test_opening_a_transfer_debits_the_source(store: Store, workspace_id: str, unique: str) -> None:
    """Escrow must leave the source's SPENDABLE balance immediately.

    A backend that recorded the transfer without debiting would let the same
    microdollars be spent locally and moved to another plane.
    """
    _fund(store, workspace_id, unique, 100)
    try:
        store.open_credit_transfer(
            transfer_id=f"t-{unique}",
            workspace_id=workspace_id,
            amount_microdollars=60,
            destination="peer",
        )
    except NotImplementedError:
        pytest.skip("backend does not implement cross-plane credit transfer")

    _transferable(store, workspace_id, unique, 40)


def test_opening_the_same_transfer_twice_debits_once(
    store: Store, workspace_id: str, unique: str
) -> None:
    """The retry path. A second debit for one transfer id destroys value."""
    _fund(store, workspace_id, unique, 100)
    try:
        for _ in range(3):
            store.open_credit_transfer(
                transfer_id=f"t-dup-{unique}",
                workspace_id=workspace_id,
                amount_microdollars=60,
                destination="peer",
            )
    except NotImplementedError:
        pytest.skip("backend does not implement cross-plane credit transfer")

    _transferable(store, workspace_id, unique, 40)


def test_an_overdrawing_transfer_is_refused_and_leaves_no_record(
    store: Store, workspace_id: str, unique: str
) -> None:
    """The id must stay usable after a top-up. A backend that recorded the
    failed attempt would return that phantom record on the retry and move
    nothing, while reporting success."""
    _fund(store, workspace_id, unique, 100)
    try:
        with pytest.raises(ValueError, match="insufficient credits"):
            store.open_credit_transfer(
                transfer_id=f"t-over-{unique}",
                workspace_id=workspace_id,
                amount_microdollars=101,
                destination="peer",
            )
    except NotImplementedError:
        pytest.skip("backend does not implement cross-plane credit transfer")

    assert store.get_credit_transfer(f"t-over-{unique}") is None
    _transferable(store, workspace_id, unique, 100)


def test_a_rejected_transfer_returns_the_escrow_exactly_once(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Repeating the destination's verdict must not refund twice."""
    _fund(store, workspace_id, unique, 100)
    try:
        store.open_credit_transfer(
            transfer_id=f"t-ret-{unique}",
            workspace_id=workspace_id,
            amount_microdollars=60,
            destination="peer",
        )
    except NotImplementedError:
        pytest.skip("backend does not implement cross-plane credit transfer")
    for _ in range(3):
        store.resolve_credit_transfer(transfer_id=f"t-ret-{unique}", outcome="rejected")

    _transferable(store, workspace_id, unique, 100)


def test_claiming_the_same_transfer_twice_credits_once(
    store: Store, workspace_id: str, unique: str
) -> None:
    """The DESTINATION side of the retry path, and the one that MINTS money if
    a backend gets it wrong: a duplicate delivery that credits twice creates
    value out of nothing."""
    try:
        for _ in range(3):
            outcome = store.claim_credit_transfer(
                transfer_id=f"t-claim-{unique}",
                workspace_id=workspace_id,
                amount_microdollars=100,
                source="home",
                accept=True,
            )
            assert outcome == "accepted"
    except NotImplementedError:
        pytest.skip("backend does not implement cross-plane credit transfer")

    _transferable(store, workspace_id, unique, 100)


def test_a_rejected_claim_tombstones_the_transfer_id(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Once rejected, an id can never credit — that immutability is what makes
    cancellation safe against an in-flight accept."""
    try:
        assert (
            store.claim_credit_transfer(
                transfer_id=f"t-tomb-{unique}",
                workspace_id=workspace_id,
                amount_microdollars=100,
                source="home",
                accept=False,
            )
            == "rejected"
        )
    except NotImplementedError:
        pytest.skip("backend does not implement cross-plane credit transfer")

    assert (
        store.claim_credit_transfer(
            transfer_id=f"t-tomb-{unique}",
            workspace_id=workspace_id,
            amount_microdollars=100,
            source="home",
            accept=True,
        )
        == "rejected"
    )
    with pytest.raises(ValueError, match="insufficient credits"):
        store.open_credit_transfer(
            transfer_id=f"t-tomb-check-{unique}",
            workspace_id=workspace_id,
            amount_microdollars=1,
            destination="peer",
        )


def test_concurrent_transfers_cannot_overdraw(store: Store, workspace_id: str, unique: str) -> None:
    """Real threads, separate pooled connections on a server-backed backend.

    Four transfers with DIFFERENT ids each try to move the whole balance, so
    idempotency cannot save the backend — only a conditional debit can.
    """
    _fund(store, workspace_id, unique, 100)
    try:
        store.open_credit_transfer(
            transfer_id=f"t-probe-{unique}",
            workspace_id=workspace_id,
            amount_microdollars=1,
            destination="peer",
        )
    except NotImplementedError:
        pytest.skip("backend does not implement cross-plane credit transfer")

    moved: list[bool] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            store.open_credit_transfer(
                transfer_id=f"t-race-{unique}-{index}",
                workspace_id=workspace_id,
                amount_microdollars=99,
                destination="peer",
            )
            outcome = True
        except ValueError:
            outcome = False
        with lock:
            moved.append(outcome)

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert moved.count(True) == 1, f"oversubscribed the balance: {moved}"
