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
* **Sequential, not concurrent.** "Single use" is really a claim about an
  atomic take, and these tests exercise it one caller at a time. A backend
  that implements consume as a non-atomic read-then-write would pass here and
  still double-redeem under two concurrent callers. Concurrent conformance
  needs real backends (an emulator or container), so it lands with that
  wiring rather than being faked against the in-memory store.
* **Credit is asserted through its return contract, not a balance.** The
  `Store` protocol exposes no backend-neutral balance read: `CreditAccount`
  became metadata-only, and the money snapshot lives on backend-specific
  methods (`credit_money_snapshot`, `typed_credit_snapshot`). So a backend
  that returns `(True, False)` while crediting the wrong amount would pass.
  Closing that hole means putting a neutral money read on the protocol — a
  real portability gap, tracked in docs/storage-portability/README.md rather
  than papered over here.
"""

from __future__ import annotations

from trusted_router.store_protocol import Store

from .conftest import BACKENDS, make_benchmark_sample

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
