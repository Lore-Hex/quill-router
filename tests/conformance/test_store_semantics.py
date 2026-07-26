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
"""

from __future__ import annotations

from trusted_router.store_protocol import Store

from .conftest import make_benchmark_sample

# --------------------------------------------------------------------------
# Exactly-once money
# --------------------------------------------------------------------------


def test_credit_workspace_once_is_idempotent_per_event(
    store: Store, workspace_id: str
) -> None:
    """Re-applying the same credit event MUST NOT credit twice.

    This is the retry path for Stripe webhooks and top-ups: the same event_id
    is delivered again after a timeout. A backend that returns True twice has
    given the customer free money. The boolean is the contract — True means
    "applied now", False means "already applied".
    """
    assert store.credit_workspace_once(workspace_id, 5_000, "evt-A") is True
    assert store.credit_workspace_once(workspace_id, 5_000, "evt-A") is False


def test_credit_workspace_once_distinguishes_events(
    store: Store, workspace_id: str
) -> None:
    """Idempotency is keyed on the event, not the amount or workspace.

    Guards the opposite error from the test above: a backend that dedupes too
    aggressively (e.g. keys on workspace+amount) would silently swallow a
    second, legitimate top-up of the same size.
    """
    assert store.credit_workspace_once(workspace_id, 5_000, "evt-A") is True
    assert store.credit_workspace_once(workspace_id, 5_000, "evt-B") is True


def test_record_sns_message_once_is_idempotent(store: Store) -> None:
    """SNS redelivers; bounce/complaint processing must run once.

    Without this, one bounce notification replayed by SNS can suppress an
    address twice or double-count a complaint.
    """
    assert store.record_sns_message_once("msg-1") is True
    assert store.record_sns_message_once("msg-1") is False
    assert store.record_sns_message_once("msg-2") is True


# --------------------------------------------------------------------------
# Single-use secrets
# --------------------------------------------------------------------------


def test_wallet_challenge_is_single_use(store: Store) -> None:
    """A SIWE nonce must be redeemable exactly once.

    Replayable nonces are a signature-replay authentication bypass, so
    "consume" has to be an atomic take, not a read.
    """
    raw_nonce, _challenge = store.create_wallet_challenge(
        address="0xabc", message="sign this", ttl_seconds=300
    )
    assert store.consume_wallet_challenge(raw_nonce) is not None
    assert store.consume_wallet_challenge(raw_nonce) is None


def test_unknown_wallet_challenge_returns_none(store: Store) -> None:
    """An unissued nonce must not authenticate anything."""
    assert store.consume_wallet_challenge("never-issued") is None


def test_verification_token_is_single_use(store: Store) -> None:
    """Email-verification links land in inboxes and get clicked twice."""
    raw_token, _token = store.create_verification_token(
        user_id="u1", purpose="verify_email", ttl_seconds=300
    )
    assert store.consume_verification_token(raw_token, purpose="verify_email") is not None
    assert store.consume_verification_token(raw_token, purpose="verify_email") is None


def test_verification_token_is_scoped_to_its_purpose(store: Store) -> None:
    """A token minted for one purpose must not satisfy another.

    Otherwise a low-value token (email verification) could be redeemed on a
    high-value flow (password reset) — privilege escalation via token reuse.
    """
    raw_token, _token = store.create_verification_token(
        user_id="u1", purpose="verify_email", ttl_seconds=300
    )
    assert store.consume_verification_token(raw_token, purpose="password_reset") is None


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

    `get_key_by_raw` and `get_key_by_hash` are separate code paths (and on the
    typed backend, separate tables). If they disagree, authentication and
    revocation disagree — a deleted key that still authenticates is the worst
    case.
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


def test_auth_session_lifecycle(store: Store) -> None:
    """Create -> read -> delete -> gone. Logout must actually invalidate."""
    raw_token, _session = store.create_auth_session(
        user_id="u1", provider="google", label="conformance", ttl_seconds=300
    )
    assert store.get_auth_session_by_raw(raw_token) is not None
    assert store.delete_auth_session_by_raw(raw_token) is True
    assert store.get_auth_session_by_raw(raw_token) is None


# --------------------------------------------------------------------------
# Index / scan semantics
# --------------------------------------------------------------------------


def test_benchmark_samples_return_newest_first(store: Store) -> None:
    """The index contract is reverse-chronological order.

    Route-health and the leaderboard both read "the newest N samples" and
    treat element 0 as current state. On Bigtable this ordering is a property
    of the reverse-timestamp row key; on SQL it must come from an explicit
    ORDER BY. A backend that returns insertion order breaks freshness logic
    without any error.
    """
    for idx, created_at in enumerate(
        ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"]
    ):
        store.record_provider_benchmark(
            make_benchmark_sample(
                sample_id=f"s{idx}",
                provider="acme",
                model="acme/m1",
                created_at=created_at,
            )
        )
    samples = store.provider_benchmark_samples(provider="acme", model="acme/m1", limit=10)
    assert len(samples) == 3
    timestamps = [s.created_at for s in samples]
    assert timestamps == sorted(timestamps, reverse=True), (
        f"expected newest-first, got {timestamps}"
    )


def test_benchmark_samples_respect_limit(store: Store) -> None:
    """`limit` must cap the result set.

    Route-health sizes its statistical window with this argument; a backend
    that ignores it silently changes the alert threshold.
    """
    for idx in range(5):
        store.record_provider_benchmark(
            make_benchmark_sample(
                sample_id=f"s{idx}",
                provider="acme",
                model="acme/m1",
                created_at=f"2026-01-0{idx + 1}T00:00:00+00:00",
            )
        )
    assert len(store.provider_benchmark_samples(provider="acme", model="acme/m1", limit=2)) == 2


def test_benchmark_samples_filter_by_route(store: Store) -> None:
    """Provider/model filtering must not leak other routes' samples.

    A leak here silently mixes another model's health into a route's failure
    rate — which is exactly how a false alert (or a missed one) is born.
    """
    store.record_provider_benchmark(
        make_benchmark_sample(sample_id="a", provider="acme", model="acme/m1")
    )
    store.record_provider_benchmark(
        make_benchmark_sample(sample_id="b", provider="other", model="other/m2")
    )
    samples = store.provider_benchmark_samples(provider="acme", model="acme/m1", limit=10)
    assert [s.id for s in samples] == ["a"]


# --------------------------------------------------------------------------
# Coverage guard
# --------------------------------------------------------------------------


def test_at_least_one_backend_actually_ran(store: Store) -> None:
    """Tripwire: a suite where every backend skipped proves nothing.

    `memory` has no external dependency, so it must always run. If this is
    ever reported as skipped, the harness itself is broken and every other
    result in this package is meaningless.
    """
    assert store is not None
