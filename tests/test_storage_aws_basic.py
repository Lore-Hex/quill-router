"""Phase 1 day 1 smoke tests for the AWS PostgreSQL backend.

Validates:
  1. `create_store(settings)` routes `TR_STORAGE_BACKEND=aws-postgres`
     to PostgresStore (no actual Postgres connection — type-check only).
  2. PostgresStore raises a clear error if instantiated without
     `TR_AWS_POSTGRES_URL`.
  3. Stubbed methods raise NotImplementedError with the documented
     "port from storage_gcp" pointer message.
  4. The `__getattr__` shim covers unknown methods too.

The end-to-end ensure_user roundtrip test against actual Docker
Postgres is gated behind the `TR_AWS_POSTGRES_TEST_URL` env var —
present in dev (when docker compose is up), absent in CI. Phase 1
day 2 will add a Postgres service container to CI and remove the
skip.
"""

from __future__ import annotations

import os

import pytest

from trusted_router.config import Settings
from trusted_router.storage import create_store


def test_create_store_routes_aws_postgres_backend_to_postgres_store() -> None:
    """`TR_STORAGE_BACKEND=aws-postgres` + a URL routes to PostgresStore.
    This is the wiring test — if create_store ever stops routing here
    correctly, the trustedrouter.eu deploy silently degrades to memory."""
    settings = Settings(
        environment="local",
        storage_backend="aws-postgres",
        # Use a dummy URL — PostgresStore lazy-creates the engine but
        # doesn't connect at construction time, so the type-check
        # works without a live Postgres.
        aws_postgres_url="postgresql://tr:tr@localhost:5433/trustedrouter",
    )
    store = create_store(settings)
    # The lazy sqlalchemy import has happened at this point. We can't
    # import PostgresStore at module-top because it's the lazy import
    # path that we're verifying.
    from trusted_router.storage_aws import PostgresStore

    assert isinstance(store, PostgresStore)


def test_aws_postgres_backend_requires_url() -> None:
    """A misconfigured deployment (backend set but URL missing) must
    fail at create_store, not at first-DB-call time. Failing fast
    surfaces config errors at process start instead of mid-request."""
    settings = Settings(
        environment="local",
        storage_backend="aws-postgres",
        # aws_postgres_url left as default (None)
    )
    with pytest.raises(ValueError, match="TR_AWS_POSTGRES_URL"):
        create_store(settings)


def test_aws_postgres_backend_stubbed_method_has_helpful_error() -> None:
    """A method that's not yet ported must raise a NotImplementedError
    that tells the next operator (a) it's intentionally not yet ported
    and (b) where the GCP reference implementation lives. Without this
    pointer, mid-port debugging burns time on 'is this a bug or a stub?'"""
    settings = Settings(
        environment="local",
        storage_backend="aws-postgres",
        aws_postgres_url="postgresql://tr:tr@localhost:5433/trustedrouter",
    )
    store = create_store(settings)
    with pytest.raises(NotImplementedError) as excinfo:
        store.find_user_by_email("alice@example.com")
    msg = str(excinfo.value)
    assert "not yet ported" in msg
    assert "storage_gcp" in msg, (
        "stub error must point to the GCP reference implementation so "
        "the porter knows where to look"
    )


def test_aws_postgres_backend_unknown_method_via_getattr_shim() -> None:
    """The class only spells out a few stubs explicitly; the rest are
    handled by __getattr__. That shim must produce the same not-yet-
    ported error — otherwise un-ported methods would fail with the much
    less-informative AttributeError."""
    settings = Settings(
        environment="local",
        storage_backend="aws-postgres",
        aws_postgres_url="postgresql://tr:tr@localhost:5433/trustedrouter",
    )
    store = create_store(settings)
    with pytest.raises(NotImplementedError) as excinfo:
        store.update_workspace(workspace_id="w1", name="new")
    msg = str(excinfo.value)
    assert "not yet ported" in msg
    assert "update_workspace" in msg


def test_aws_postgres_backend_private_attribute_raises_attribute_error() -> None:
    """Distinguish "method not yet ported" (NotImplementedError) from
    "this attribute doesn't exist on this class" (AttributeError).
    Internal attributes that aren't found are real bugs, not port-
    status messages."""
    settings = Settings(
        environment="local",
        storage_backend="aws-postgres",
        aws_postgres_url="postgresql://tr:tr@localhost:5433/trustedrouter",
    )
    store = create_store(settings)
    with pytest.raises(AttributeError):
        store._this_attribute_does_not_exist  # noqa: B018


# ── End-to-end (gated on local Docker Postgres being up) ────────────────


@pytest.mark.skipif(
    not os.environ.get("TR_AWS_POSTGRES_TEST_URL"),
    reason=(
        "TR_AWS_POSTGRES_TEST_URL not set — run "
        "`docker compose up -d postgres` and "
        "`export TR_AWS_POSTGRES_TEST_URL=postgresql://tr:tr@localhost:5433/trustedrouter` "
        "to exercise this test against real Postgres."
    ),
)
def test_ensure_user_roundtrip_against_real_postgres() -> None:
    """The Phase 1 day 1 deliverable: one method working end-to-end
    against real Postgres proves the entire wiring (config → factory →
    sqlalchemy → schema → insert/upsert/select → User dataclass). Once
    this passes against Docker, the remaining ~80 methods are
    mechanical ports against the same proven scaffolding."""
    settings = Settings(
        environment="local",
        storage_backend="aws-postgres",
        aws_postgres_url=os.environ["TR_AWS_POSTGRES_TEST_URL"],
    )
    store = create_store(settings)
    store.reset()  # Clean slate

    # First call: creates a fresh user with the given id.
    user = store.ensure_user("test-user-123", email="alice@example.com")
    assert user.id == "test-user-123"
    assert user.email == "alice@example.com"
    assert user.email_verified is False

    # Second call: idempotent — returns the SAME user, doesn't recreate.
    again = store.ensure_user("test-user-123")
    assert again.id == "test-user-123"
    assert again.email == "alice@example.com"  # preserved
    assert again.created_at == user.created_at, (
        "ensure_user must be idempotent — second call must return the "
        "existing user, not recreate (which would reset created_at)"
    )

    # Third call: passing an email when one's already set must NOT
    # overwrite the existing email — that would be data destruction
    # via an unrelated code path. Reference: storage_gcp.py contract.
    later = store.ensure_user("test-user-123", email="malicious@example.com")
    assert later.email == "alice@example.com"

    # Different user_id: creates a separate user.
    other = store.ensure_user("test-user-456", email="bob@example.com")
    assert other.id == "test-user-456"
    assert other.id != user.id
