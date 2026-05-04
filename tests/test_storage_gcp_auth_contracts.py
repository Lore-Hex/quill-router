from __future__ import annotations

from tests.fakes.spanner import make_fake_store


def test_gcp_wallet_challenge_and_verification_tokens_are_one_shot() -> None:
    store, _db, _bt = make_fake_store()

    nonce, challenge = store.create_wallet_challenge(
        address="0x" + "a" * 40,
        message="trustedrouter.com wants you to sign in",
        ttl_seconds=600,
        raw_nonce="nonce-for-test",
    )
    assert nonce == "nonce-for-test"
    assert challenge.address == "0x" + "a" * 40

    consumed = store.consume_wallet_challenge("nonce-for-test")
    replayed = store.consume_wallet_challenge("nonce-for-test")
    assert consumed is not None
    assert consumed.consumed_at is not None
    assert replayed is None

    user = store.create_wallet_user("0x" + "b" * 40)
    raw_token, token = store.create_verification_token(
        user_id=user.id,
        purpose="signup",
        ttl_seconds=600,
    )
    assert token.user_id == user.id
    assert store.consume_verification_token(raw_token, purpose="login") is None
    consumed_token = store.consume_verification_token(raw_token, purpose="signup")
    replayed_token = store.consume_verification_token(raw_token, purpose="signup")
    assert consumed_token is not None
    assert consumed_token.consumed_at is not None
    assert replayed_token is None


def test_gcp_wallet_user_email_and_membership_are_uuid_keyed() -> None:
    store, _db, _bt = make_fake_store()

    wallet_user = store.create_wallet_user("0x" + "c" * 40)
    same_wallet = store.create_wallet_user("0x" + "C" * 40)
    assert same_wallet.id == wallet_user.id
    assert wallet_user.email is None

    attached = store.set_user_email(wallet_user.id, "Wallet@Example.com")
    assert attached is not None
    assert attached.email == "wallet@example.com"
    assert attached.email_verified is False
    assert store.mark_user_email_verified(wallet_user.id).email_verified is True
    changed = store.set_user_email(wallet_user.id, "new-wallet@example.com")
    assert changed is not None
    assert changed.email == "new-wallet@example.com"
    assert changed.email_verified is False
    assert store.find_user_by_email("wallet@example.com") is None
    assert store.find_user_by_email("new-wallet@example.com").id == wallet_user.id

    workspace = store.list_workspaces_for_user(wallet_user.id)[0]
    credit = store.get_credit_account(workspace.id)
    assert credit is not None
    assert credit.total_credits_microdollars == 0
    invited = store.add_members(workspace.id, ["friend@example.com"], role="member")[0]
    store.remove_members(workspace.id, ["friend@example.com"])
    assert not store.user_is_member(invited.user_id, workspace.id)

    readded = store.add_members(workspace.id, ["friend@example.com"], role="member")[0]
    assert readded.user_id == invited.user_id
    assert store.user_is_member(readded.user_id, workspace.id)
