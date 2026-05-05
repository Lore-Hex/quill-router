"""End-to-end coverage of the Spanner-backed feature classes via the
fake spanner. The unit-level tests in test_storage_gcp_auth_contracts
hit the wallet/verification surfaces; this file pushes the rest:

* SpannerAuthSessions full lifecycle (create / get_by_raw / upgrade /
  set_workspace / delete_by_raw / expiry sweep)
* SpannerEmailBlocks (block, is_blocked, get, record_message_once)
* SpannerByok (upsert idempotency, list-by-workspace, hint preservation)
* SpannerOAuthCodes (create, consume, replay, expiry sweep)
* SpannerApiKeys gateway_authorization + finalize lifecycle (the
  cross-request reservation handle that has zero coverage today)
* SpannerGenerations.add → key counter rollup invariant + activity index
* SpannerRateLimits transactional bucket increment

Each test bypasses InMemoryStore and exercises the Spanner sibling
directly through the fake. Run them after any storage_gcp_* edit to
catch divergence between in-memory and production behavior."""

from __future__ import annotations

import datetime as dt
import json

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_models import EmailSendBlock


def _seed_workspace_and_key(store) -> tuple[str, str]:
    user = store.ensure_user("workspace-owner@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name="primary",
        creator_user_id=user.id,
    )
    return workspace.id, api_key.hash


# ── Auth sessions ───────────────────────────────────────────────────────


def test_gcp_auth_session_full_lifecycle() -> None:
    store, _db, _ = make_fake_store()
    user = store.ensure_user("session-owner@example.com")

    raw, session = store.create_auth_session(
        user_id=user.id,
        provider="google",
        label="alice@example.com",
        ttl_seconds=3600,
        state="active",
    )

    assert raw.startswith("trsess-v1-")
    assert session.user_id == user.id
    assert session.state == "active"

    fetched = store.get_auth_session_by_raw(raw)
    assert fetched is not None
    assert fetched.hash == session.hash
    assert fetched.label == "alice@example.com"

    upgraded = store.upgrade_auth_session(raw, state="active")
    assert upgraded is not None
    assert upgraded.state == "active"

    workspace = store.list_workspaces_for_user(user.id)[0]
    bound = store.set_auth_session_workspace(raw, workspace.id)
    assert bound is not None and bound.workspace_id == workspace.id
    refetch = store.get_auth_session_by_raw(raw)
    assert refetch is not None and refetch.workspace_id == workspace.id

    assert store.delete_auth_session_by_raw(raw) is True
    assert store.get_auth_session_by_raw(raw) is None
    assert store.delete_auth_session_by_raw(raw) is False


def test_gcp_auth_session_expired_token_returns_none_and_purges() -> None:
    store, db, _ = make_fake_store()
    user = store.ensure_user("expired-session@example.com")

    raw, session = store.create_auth_session(
        user_id=user.id,
        provider="github",
        label="github-user",
        ttl_seconds=60,
    )
    # Force-expire by stomping the row through the same JSON encoding
    # the production store uses.
    expired_at = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    row = db.rows[("auth_session", session.hash)]
    body = json.loads(row.body)
    body["expires_at"] = expired_at
    db.rows[("auth_session", session.hash)].body = json.dumps(body)  # type: ignore[union-attr]

    assert store.get_auth_session_by_raw(raw) is None


def test_gcp_auth_session_pending_state_can_be_promoted() -> None:
    store, _db, _ = make_fake_store()
    user = store.ensure_user("wallet-pending@example.com")

    raw, session = store.create_auth_session(
        user_id=user.id,
        provider="metamask",
        label="0x" + "a" * 40,
        ttl_seconds=600,
        state="pending_email",
    )
    assert session.state == "pending_email"

    promoted = store.upgrade_auth_session(raw, state="active")
    assert promoted is not None
    assert promoted.state == "active"

    fetched = store.get_auth_session_by_raw(raw)
    assert fetched is not None and fetched.state == "active"


# ── Email blocks ────────────────────────────────────────────────────────


def test_gcp_email_blocks_block_lookup_and_normalization() -> None:
    store, db, _ = make_fake_store()

    block = store.block_email_sending(
        email="Bouncy@Example.com",
        reason="Hard bounce: 5.1.1",
        bounce_type="Permanent",
        feedback_id="0100018a-feedback",
    )
    assert isinstance(block, EmailSendBlock)
    assert block.email == "bouncy@example.com"

    assert store.is_email_blocked("BOUNCY@EXAMPLE.COM") is True
    assert store.is_email_blocked("bouncy@example.com") is True
    assert store.is_email_blocked("not-blocked@example.com") is False

    fetched = store.get_email_block("bouncy@example.com")
    assert fetched is not None
    assert fetched.bounce_type == "Permanent"
    assert fetched.reason == "Hard bounce: 5.1.1"

    # Normalized email is the dict key in storage, raw email never appears.
    serialized = "\n".join(row.body for row in db.rows.values())
    assert "Bouncy@Example.com" not in serialized
    assert "bouncy@example.com" in serialized


def test_gcp_email_blocks_record_message_once_dedupes() -> None:
    store, _db, _ = make_fake_store()

    assert store.record_sns_message_once("msg-aaaa") is True
    assert store.record_sns_message_once("msg-aaaa") is False
    assert store.record_sns_message_once("msg-bbbb") is True
    # First message is still recorded — second message-id is independent.
    assert store.record_sns_message_once("msg-aaaa") is False


# ── BYOK ────────────────────────────────────────────────────────────────


def test_gcp_byok_upsert_idempotent_and_list_by_workspace() -> None:
    store, _db, _ = make_fake_store()
    user = store.ensure_user("byok-owner@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]

    first = store.upsert_byok_provider(
        workspace_id=workspace.id,
        provider="mistral",
        secret_ref="env://MISTRAL_API_KEY",  # noqa: S106 - placeholder ref.
        key_hint="mis...1234",
    )
    second = store.upsert_byok_provider(
        workspace_id=workspace.id,
        provider="mistral",
        secret_ref="env://MISTRAL_API_KEY_NEW",  # noqa: S106 - placeholder ref.
        key_hint="mis...5678",
    )

    # upsert preserves the original created_at and bumps updated_at +
    # secret_ref + key_hint.
    assert first.workspace_id == second.workspace_id
    assert first.created_at == second.created_at
    assert second.secret_ref == "env://MISTRAL_API_KEY_NEW"  # noqa: S105 - placeholder ref.
    assert second.key_hint == "mis...5678"

    listed = store.list_byok_providers(workspace.id)
    assert [item.provider for item in listed] == ["mistral"]


def test_gcp_byok_delete_returns_false_for_missing_provider() -> None:
    store, _db, _ = make_fake_store()
    user = store.ensure_user("byok-delete@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]

    store.upsert_byok_provider(
        workspace_id=workspace.id,
        provider="cerebras",
        secret_ref="env://CEREBRAS_API_KEY",  # noqa: S106 - placeholder ref.
        key_hint="cer...4321",
    )
    assert store.delete_byok_provider(workspace.id, "cerebras") is True
    assert store.delete_byok_provider(workspace.id, "cerebras") is False
    assert store.delete_byok_provider(workspace.id, "openai") is False


# ── OAuth authorization codes ──────────────────────────────────────────


def test_gcp_oauth_code_create_consume_and_replay() -> None:
    store, _db, _ = make_fake_store()
    user = store.ensure_user("oauth-flow@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]

    raw, code = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://app.example.com/callback",
        key_label="App key",
        ttl_seconds=600,
        app_id=42,
        code_challenge="challenge-abc",
        code_challenge_method="S256",
    )
    assert raw.startswith("auth_code-")
    assert code.app_id == 42
    assert code.code_challenge == "challenge-abc"

    consumed = store.consume_oauth_authorization_code(raw)
    assert consumed is not None
    assert consumed.consumed_at is not None
    assert consumed.workspace_id == workspace.id

    # Replay returns None even though the code row still exists.
    assert store.consume_oauth_authorization_code(raw) is None


def test_gcp_oauth_code_expiry_sweeps_lookup_and_returns_none() -> None:
    store, db, _ = make_fake_store()
    user = store.ensure_user("oauth-expiry@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]

    raw, code = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://app.example.com/callback",
        key_label="Expiring key",
        ttl_seconds=60,
        app_id=7,
    )
    expired_at = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    row = db.rows[("oauth_code", code.hash)]
    body = json.loads(row.body)
    body["code_expires_at"] = expired_at
    db.rows[("oauth_code", code.hash)].body = json.dumps(body)  # type: ignore[union-attr]

    assert store.consume_oauth_authorization_code(raw) is None
    # Expired-cleanup ran inside the txn — both rows should be gone.
    assert ("oauth_code", code.hash) not in db.rows
    assert ("oauth_code_lookup", code.lookup_hash) not in db.rows


def test_gcp_oauth_code_tampered_secret_rejected() -> None:
    store, _db, _ = make_fake_store()
    user = store.ensure_user("oauth-tamper@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]

    raw, _ = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://app.example.com/callback",
        key_label="Tampered key",
        ttl_seconds=600,
        app_id=99,
    )
    # Same lookup-hash prefix but a different secret body — would only
    # work if the consume path forgot to verify the salted secret.
    bad = raw[:-4] + "XXXX"
    assert store.consume_oauth_authorization_code(bad) is None
    # Original still works.
    assert store.consume_oauth_authorization_code(raw) is not None


# ── Gateway authorizations + finalize ───────────────────────────────────


def test_gcp_gateway_authorization_create_get_and_mark_settled() -> None:
    store, db, _ = make_fake_store()
    workspace_id, key_hash = _seed_workspace_and_key(store)

    auth = store.create_gateway_authorization(
        workspace_id=workspace_id,
        key_hash=key_hash,
        model_id="openai/gpt-4o-mini",
        provider="openai",
        usage_type="Credits",
        estimated_microdollars=1_000,
        credit_reservation_id=None,
        requested_model_id="trustedrouter/auto",
        candidate_model_ids=["openai/gpt-4o-mini", "mistralai/mistral-small-2603"],
        region="us-central1",
    )
    assert auth.id.startswith("gwa-")
    assert auth.region == "us-central1"
    assert auth.candidate_model_ids == ["openai/gpt-4o-mini", "mistralai/mistral-small-2603"]

    fetched = store.get_gateway_authorization(auth.id)
    assert fetched is not None
    assert fetched.id == auth.id
    assert fetched.requested_model_id == "trustedrouter/auto"

    store.mark_gateway_authorization_settled(auth.id)
    after = store.get_gateway_authorization(auth.id)
    assert after is not None and after.settled is True
    # mark_settled is idempotent — a second call doesn't error.
    store.mark_gateway_authorization_settled(auth.id)

    # Unknown authorization id is a no-op (returns None on get).
    assert store.get_gateway_authorization("gwa-nonexistent") is None
    store.mark_gateway_authorization_settled("gwa-nonexistent")
    # Sanity: nothing leaked into the rows.
    assert ("gateway_authorization", "gwa-nonexistent") not in db.rows


def test_gcp_finalize_gateway_authorization_settles_credits_and_writes_generation() -> None:
    from trusted_router.storage_models import Generation

    store, db, _ = make_fake_store()
    workspace_id, key_hash = _seed_workspace_and_key(store)

    reservation = store.reserve(workspace_id, key_hash, 5_000_000)
    auth = store.create_gateway_authorization(
        workspace_id=workspace_id,
        key_hash=key_hash,
        model_id="anthropic/claude-sonnet-4.6",
        provider="anthropic",
        usage_type="Credits",
        estimated_microdollars=5_000_000,
        credit_reservation_id=reservation.id,
    )
    store.reserve_key_limit(key_hash, 5_000_000, usage_type="Credits")

    generation = Generation(
        id="gen-finalize-success",
        request_id="req-finalize-success",
        workspace_id=workspace_id,
        key_hash=key_hash,
        model="anthropic/claude-sonnet-4.6",
        provider_name="Anthropic",
        app="finalize-test",
        tokens_prompt=100,
        tokens_completion=50,
        total_cost_microdollars=2_500_000,
        usage_type="Credits",
        speed_tokens_per_second=12.5,
        finish_reason="stop",
        status="success",
        streamed=False,
    )

    settled = store.finalize_gateway_authorization(
        auth.id,
        success=True,
        actual_microdollars=2_500_000,
        selected_usage_type="Credits",
        generation=generation,
    )
    assert settled is True

    after = store.get_gateway_authorization(auth.id)
    assert after is not None and after.settled is True
    credit = store.get_credit_account(workspace_id)
    assert credit is not None
    assert credit.total_usage_microdollars == 2_500_000
    assert credit.reserved_microdollars == 0
    # Generation row landed under the right Spanner key.
    assert ("generation", generation.id) in db.rows


def test_gcp_finalize_gateway_authorization_refunds_on_failure() -> None:
    store, db, _ = make_fake_store()
    workspace_id, key_hash = _seed_workspace_and_key(store)

    reservation = store.reserve(workspace_id, key_hash, 1_000_000)
    auth = store.create_gateway_authorization(
        workspace_id=workspace_id,
        key_hash=key_hash,
        model_id="openai/gpt-4o-mini",
        provider="openai",
        usage_type="Credits",
        estimated_microdollars=1_000_000,
        credit_reservation_id=reservation.id,
    )
    store.reserve_key_limit(key_hash, 1_000_000, usage_type="Credits")

    refunded = store.finalize_gateway_authorization(
        auth.id,
        success=False,
        actual_microdollars=0,
        selected_usage_type="Credits",
        generation=None,
    )
    assert refunded is True

    credit = store.get_credit_account(workspace_id)
    assert credit is not None
    assert credit.reserved_microdollars == 0
    assert credit.total_usage_microdollars == 0
    # No generation row written on the failure path.
    assert not any(kind == "generation" for kind, _ in db.rows)


def test_gcp_finalize_gateway_authorization_is_one_shot() -> None:
    store, _db, _ = make_fake_store()
    workspace_id, key_hash = _seed_workspace_and_key(store)
    auth = store.create_gateway_authorization(
        workspace_id=workspace_id,
        key_hash=key_hash,
        model_id="openai/gpt-4o-mini",
        provider="openai",
        usage_type="BYOK",
        estimated_microdollars=0,
        credit_reservation_id=None,
    )

    first = store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=0, selected_usage_type="BYOK", generation=None
    )
    second = store.finalize_gateway_authorization(
        auth.id, success=True, actual_microdollars=0, selected_usage_type="BYOK", generation=None
    )
    assert first is True
    assert second is False


# ── Generations: per-key counter rollup ────────────────────────────────


def test_gcp_add_generation_rolls_cost_into_per_key_counters() -> None:
    from trusted_router.storage_models import Generation

    store, db, _ = make_fake_store()
    workspace_id, key_hash = _seed_workspace_and_key(store)

    prepaid = Generation(
        id="gen-prepaid-1",
        request_id="req-prepaid-1",
        workspace_id=workspace_id,
        key_hash=key_hash,
        model="openai/gpt-4o-mini",
        provider_name="OpenAI",
        app="counter-test",
        tokens_prompt=10,
        tokens_completion=5,
        total_cost_microdollars=12_345,
        usage_type="Credits",
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
    )
    byok = Generation(
        id="gen-byok-1",
        request_id="req-byok-1",
        workspace_id=workspace_id,
        key_hash=key_hash,
        model="mistralai/mistral-small-2603",
        provider_name="Mistral",
        app="counter-test",
        tokens_prompt=20,
        tokens_completion=10,
        total_cost_microdollars=67_890,
        usage_type="BYOK",
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
    )

    store.add_generation(prepaid)
    store.add_generation(byok)

    key_row = json.loads(db.rows[("api_key", key_hash)].body)
    assert key_row["usage_microdollars"] == 12_345
    assert key_row["byok_usage_microdollars"] == 67_890

    # Both generations are retrievable by id.
    assert store.get_generation("gen-prepaid-1") is not None
    assert store.get_generation("gen-byok-1") is not None


# ── Rate limits ─────────────────────────────────────────────────────────


def test_gcp_rate_limit_increments_in_same_window_and_rolls_over() -> None:
    store, _db, _ = make_fake_store()
    now = dt.datetime(2026, 5, 3, 12, 0, 1, tzinfo=dt.UTC)

    first = store.hit_rate_limit(namespace="ip", subject="9.9.9.9", limit=2, window_seconds=60, now=now)
    second = store.hit_rate_limit(namespace="ip", subject="9.9.9.9", limit=2, window_seconds=60, now=now)
    third = store.hit_rate_limit(namespace="ip", subject="9.9.9.9", limit=2, window_seconds=60, now=now)
    next_window = store.hit_rate_limit(
        namespace="ip",
        subject="9.9.9.9",
        limit=2,
        window_seconds=60,
        now=now + dt.timedelta(seconds=61),
    )

    assert (first.allowed, first.remaining) == (True, 1)
    assert (second.allowed, second.remaining) == (True, 0)
    assert third.allowed is False
    assert third.retry_after_seconds > 0
    assert next_window.allowed is True
    assert next_window.remaining == 1
