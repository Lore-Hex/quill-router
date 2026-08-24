from __future__ import annotations

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn


def _message(domain: str, address: str, nonce: str) -> str:
    return f"{domain} wants you to sign in with your Ethereum account:\n{address}\n\nNonce: {nonce}"


def test_postgres_wallet_challenge_reuse_is_bounded_and_one_shot() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    address = "0x" + "a" * 40

    first_nonce, first = store.create_wallet_challenge(
        address=address,
        message=_message("trusted.example", address, "first-nonce"),
        ttl_seconds=300,
        raw_nonce="first-nonce",
    )
    returned_nonce, returned = store.create_wallet_challenge(
        address=address.upper(),
        message=_message("trusted.example", address, "attacker-proposal"),
        ttl_seconds=300,
        raw_nonce="attacker-proposal",
    )

    assert first_nonce == "first-nonce"
    assert returned_nonce == first_nonce
    assert returned.hash == first.hash
    assert conn.count_entities("wallet_challenge") == 1
    assert conn.count_entities("wallet_challenge_lookup") == 1
    assert conn.count_entities("wallet_challenge_by_scope") == 1
    assert store.consume_wallet_challenge("attacker-proposal") is None
    assert store.consume_wallet_challenge(first_nonce) is not None
    assert store.consume_wallet_challenge(first_nonce) is None

    fresh_nonce, fresh = store.create_wallet_challenge(
        address=address,
        message=_message("trusted.example", address, "after-consume"),
        ttl_seconds=300,
        raw_nonce="after-consume",
    )
    assert fresh_nonce == "after-consume"
    assert fresh.hash != first.hash
    assert conn.count_entities("wallet_challenge") == 1
    assert conn.count_entities("wallet_challenge_lookup") == 1
    assert conn.count_entities("wallet_challenge_by_scope") == 1


def test_postgres_wallet_challenge_does_not_reuse_across_domains() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    address = "0x" + "b" * 40

    trusted_nonce, trusted = store.create_wallet_challenge(
        address=address,
        message=_message("trusted.example", address, "trusted-nonce"),
        ttl_seconds=300,
        raw_nonce="trusted-nonce",
    )
    ally_nonce, ally = store.create_wallet_challenge(
        address=address,
        message=_message("ally.example", address, "ally-nonce"),
        ttl_seconds=300,
        raw_nonce="ally-nonce",
    )

    assert trusted_nonce != ally_nonce
    assert trusted.hash != ally.hash
    assert conn.count_entities("wallet_challenge") == 2
    assert conn.count_entities("wallet_challenge_lookup") == 2
    assert conn.count_entities("wallet_challenge_by_scope") == 2
    assert store.consume_wallet_challenge(trusted_nonce) is not None
    assert store.consume_wallet_challenge(ally_nonce) is not None


def test_postgres_wallet_challenge_rejects_message_nonce_mismatch_without_write() -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    address = "0x" + "c" * 40

    with pytest.raises(ValueError, match="does not match"):
        store.create_wallet_challenge(
            address=address,
            message=_message("trusted.example", address, "message-nonce"),
            ttl_seconds=300,
            raw_nonce="different-nonce",
        )

    assert conn.count_entities("wallet_challenge") == 0
    assert conn.count_entities("wallet_challenge_lookup") == 0
    assert conn.count_entities("wallet_challenge_by_scope") == 0
