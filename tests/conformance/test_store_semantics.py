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


def _credit_and_key(
    store: Store,
    workspace_id: str,
    unique: str,
    amount_microdollars: int,
) -> str:
    assert store.credit_workspace_once(
        workspace_id,
        amount_microdollars,
        f"evt-reservation-{unique}",
    ) is True
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
    assert store.credit_workspace_once(
        workspace_id, 20, f"evt-overage-zero-{unique}"
    ) is True
    with pytest.raises(ValueError, match="insufficient credits"):
        store.reserve(workspace_id, key_hash, 1)
    assert store.credit_workspace_once(
        workspace_id, 1, f"evt-overage-positive-{unique}"
    ) is True
    _assert_exact_available_capacity(store, workspace_id, key_hash, 1, unique)


def test_reserve_then_refund_restores_exact_balance(
    store: Store, workspace_id: str, unique: str
) -> None:
    key_hash = _credit_and_key(store, workspace_id, unique, 100)
    reservation = store.reserve(workspace_id, key_hash, 60)

    store.refund(reservation.id)

    _assert_exact_available_capacity(store, workspace_id, key_hash, 100, unique)


def test_settle_is_idempotent(
    store: Store, workspace_id: str, unique: str
) -> None:
    """A replay releases and charges once, even after the first call returned."""
    key_hash = _credit_and_key(store, workspace_id, unique, 100)
    reservation = store.reserve(workspace_id, key_hash, 60)

    store.settle(reservation.id, 20)
    store.settle(reservation.id, 20)

    _assert_exact_available_capacity(store, workspace_id, key_hash, 80, unique)


def test_refund_is_idempotent(
    store: Store, workspace_id: str, unique: str
) -> None:
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


def test_workspace_is_readable_immediately_after_creation(
    store: Store, workspace_id: str
) -> None:
    """No backend may require a settling delay for its own write.

    Route code creates a workspace and immediately reads it back in the same
    request. An eventually-consistent backend would 404 intermittently.
    """
    fetched = store.get_workspace(workspace_id)
    assert fetched is not None
    assert str(fetched.id) == workspace_id


def test_api_key_lookups_agree_and_delete_revokes(
    store: Store, workspace_id: str
) -> None:
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


def test_synthetic_probe_samples_apply_status_reader_filters(
    store: Store, unique: str
) -> None:
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
    now = dt.datetime.now(dt.UTC).replace(
        minute=10, second=0, microsecond=0
    ) - dt.timedelta(hours=3 + int(unique, 16) % 17_000)
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

    oldest_start = _iso_utc(
        (now - dt.timedelta(hours=2)).replace(minute=0)
    )
    newest_start = _iso_utc(now.replace(minute=0))
    middle_start = _iso_utc(
        (now - dt.timedelta(hours=1)).replace(minute=0)
    )
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
