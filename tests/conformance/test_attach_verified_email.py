"""Contract for Store.attach_verified_email (wallet email attach).

The attach must be one conditional write: it never moves an address between
users and never replaces an email the account has already verified.
"""

from __future__ import annotations

import uuid

from trusted_router.storage_models import (
    EMAIL_ATTACH_ALREADY_VERIFIED,
    EMAIL_ATTACH_ATTACHED,
    EMAIL_ATTACH_MISSING_USER,
    EMAIL_ATTACH_TAKEN,
)
from trusted_router.store_protocol import Store


def _address() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _email(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:12]}@example.com"


def test_attach_sets_email_verified_and_indexes_it(store: Store) -> None:
    user = store.create_wallet_user(_address())
    email = _email("attach")

    assert store.attach_verified_email(user.id, email.upper()) == EMAIL_ATTACH_ATTACHED

    refreshed = store.get_user(user.id)
    assert refreshed is not None
    assert refreshed.email == email and refreshed.email_verified is True
    owner = store.find_user_by_email(email)
    assert owner is not None and owner.id == user.id
    # Same address again is an idempotent success.
    assert store.attach_verified_email(user.id, email) == EMAIL_ATTACH_ATTACHED


def test_attach_refuses_an_address_another_user_owns(store: Store) -> None:
    email = _email("owned")
    other = store.ensure_user(email, email=email, email_verified=True)
    user = store.create_wallet_user(_address())

    assert store.attach_verified_email(user.id, email) == EMAIL_ATTACH_TAKEN

    refreshed = store.get_user(user.id)
    assert refreshed is not None and refreshed.email is None
    owner = store.find_user_by_email(email)
    assert owner is not None and owner.id == other.id


def test_attach_never_replaces_a_verified_email(store: Store) -> None:
    user = store.create_wallet_user(_address())
    first, second = _email("first"), _email("second")
    assert store.attach_verified_email(user.id, first) == EMAIL_ATTACH_ATTACHED

    assert store.attach_verified_email(user.id, second) == EMAIL_ATTACH_ALREADY_VERIFIED

    refreshed = store.get_user(user.id)
    assert refreshed is not None and refreshed.email == first
    assert store.find_user_by_email(second) is None


def test_attach_for_a_missing_user_writes_nothing(store: Store) -> None:
    email = _email("missing")

    assert store.attach_verified_email(str(uuid.uuid4()), email) == EMAIL_ATTACH_MISSING_USER
    assert store.find_user_by_email(email) is None
