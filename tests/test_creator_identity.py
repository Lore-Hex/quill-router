from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.creator_identity import (
    local_creator_username,
    validate_creator_username,
)
from trusted_router.storage import STORE


@pytest.mark.parametrize(
    "username",
    ["a", "ab", "-alice", "alice-", "alice_name", "a" * 33, "trustedrouter"],
)
def test_creator_username_rejects_invalid_and_reserved_values(username: str) -> None:
    with pytest.raises(ValueError):
        validate_creator_username(username)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ada", "ada"),
        (" legal-ai ", "legal-ai"),
        ("a1b", "a1b"),
        ("a" * 32, "a" * 32),
    ],
)
def test_creator_username_normalizes_valid_values(raw: str, expected: str) -> None:
    assert validate_creator_username(raw) == expected


def test_username_claim_requires_identity_and_is_unique_and_immutable(
    client: TestClient,
) -> None:
    alice_headers = {"x-trustedrouter-user": "alice@example.com"}
    bob_headers = {"x-trustedrouter-user": "bob@example.com"}
    client.get("/v1/auth/verification-status", headers=alice_headers)
    alice = STORE.find_user_by_email("alice@example.com")
    assert alice is not None

    unverified = client.put(
        "/v1/auth/username",
        headers=alice_headers,
        json={"username": "ada-builder"},
    )
    assert unverified.status_code == 403
    assert unverified.json()["error"]["type"] == "verification_required"

    STORE.set_user_identity_status(
        alice.id,
        status="approved",
        verified_name="Alice Creator",
    )
    claimed = client.put(
        "/v1/auth/username",
        headers=alice_headers,
        json={"username": "Ada-Builder"},
    )
    replay = client.put(
        "/v1/auth/username",
        headers=alice_headers,
        json={"username": "ada-builder"},
    )
    changed = client.put(
        "/v1/auth/username",
        headers=alice_headers,
        json={"username": "alice-new"},
    )
    assert claimed.status_code == replay.status_code == 200
    assert claimed.json()["data"]["username"] == "ada-builder"
    assert changed.status_code == 409
    assert changed.json()["error"]["type"] == "conflict"

    client.get("/v1/auth/verification-status", headers=bob_headers)
    bob = STORE.find_user_by_email("bob@example.com")
    assert bob is not None
    STORE.set_user_identity_status(
        bob.id,
        status="approved",
        verified_name="Bob Creator",
    )
    duplicate = client.put(
        "/v1/auth/username",
        headers=bob_headers,
        json={"username": "ada-builder"},
    )
    assert duplicate.status_code == 409
    assert STORE.find_user_by_username("ADA-BUILDER").id == alice.id


def test_spanner_username_index_enforces_global_ownership() -> None:
    store, _database, _bigtable = make_fake_store()
    alice = store.ensure_user("alice@example.com")
    bob = store.ensure_user("bob@example.com")

    assert store.claim_user_username(alice.id, "ada-builder").username == "ada-builder"
    assert store.claim_user_username(alice.id, "ADA-BUILDER").username == "ada-builder"
    with pytest.raises(ValueError, match="creator_username_taken"):
        store.claim_user_username(bob.id, "ada-builder")
    with pytest.raises(ValueError, match="creator_username_immutable"):
        store.claim_user_username(alice.id, "another-name")
    assert store.find_user_by_username("ada-builder").id == alice.id


def test_local_creator_username_is_stable_and_never_reserved() -> None:
    user = STORE.ensure_user("TrustedRouter@example.com")
    first = local_creator_username(user)
    second = local_creator_username(user)
    assert first == second
    assert first.startswith("dev-")
    assert validate_creator_username(first) == first
