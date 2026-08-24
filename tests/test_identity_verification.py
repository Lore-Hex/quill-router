from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    VERIFF_ATTEMPT_FEE_MICRODOLLARS,
    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
)
from trusted_router.routes import identity_verify as identity_route
from trusted_router.services.veriff import VeriffSession
from trusted_router.storage import STORE, InMemoryStore

HEADERS = {"x-trustedrouter-user": "identity-user@example.com"}


def _client(settings: Settings | None = None) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings or Settings(environment="test"), init_observability=False)
    ) as client:
        yield client


def _user(client: TestClient) -> Any:
    client.get("/v1/auth/verification-status", headers=HEADERS)
    user = STORE.find_user_by_email("identity-user@example.com")
    assert user is not None
    return user


def _fund(user: Any, amount: int = VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS) -> None:
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    assert STORE.credit_workspace_typed_direct(
        workspace.id,
        amount,
        f"identity-topup:{user.id}:{amount}",
        lifetime_topup_user_id=user.id,
    )


def _verify_phone(user: Any) -> None:
    started = STORE.begin_phone_verification(user.id, "+13059511381", "voice")
    assert started is not None
    code, _updated = started
    assert STORE.confirm_phone_verification(user.id, code)[0] == "ok"


def _eligible_user(client: TestClient) -> Any:
    user = _user(client)
    _fund(user)
    _verify_phone(user)
    return user


@pytest.mark.parametrize(
    ("missing_email", "phone_verified", "funded", "expected"),
    [
        (True, True, True, ["email"]),
        (False, False, True, ["phone_verified"]),
        (False, True, False, ["funding"]),
        (True, False, False, ["email", "phone_verified", "funding"]),
    ],
)
def test_identity_session_reports_exact_missing_requirements(
    missing_email: bool,
    phone_verified: bool,
    funded: bool,
    expected: list[str],
) -> None:
    for client in _client():
        if missing_email:
            user = STORE.create_wallet_user("0x" + "4" * 40)
            workspace = STORE.list_workspaces_for_user(user.id)[0]
            raw_session, _session = STORE.create_auth_session(
                user_id=user.id,
                provider="wallet",
                label="wallet",
                ttl_seconds=3600,
                workspace_id=workspace.id,
            )
            client.cookies.set("tr_session", raw_session)
            headers: dict[str, str] = {}
        else:
            user = _user(client)
            headers = HEADERS
        if phone_verified:
            _verify_phone(user)
        if funded:
            _fund(user)

        response = client.post("/v1/auth/identity/session", headers=headers)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["type"] == "verification_required"
    assert error["missing_requirements"] == expected
    assert error["verification_url"] == "/console/account/verification"


def test_identity_session_disabled_without_dev_fallback() -> None:
    settings = Settings(
        environment="test",
        veriff_api_key="configured-api-key",
        veriff_shared_secret_key="configured-shared-secret",  # noqa: S106
    )
    for client in _client(settings):
        response = client.post("/v1/auth/identity/session", headers=HEADERS)
    assert response.status_code == 503


def test_identity_session_dev_fallback_auto_approves() -> None:
    for client in _client():
        user = _eligible_user(client)
        response = client.post("/v1/auth/identity/session", headers=HEADERS)
        updated = STORE.get_user(user.id)

    assert response.status_code == 201
    assert response.json()["data"] == {
        "url": "/console/account/verification?dev=1",
        "status": "approved",
    }
    assert updated is not None
    assert updated.identity_verified
    assert updated.identity_verified_name == "Dev User"
    assert updated.veriff_attempt_count == 1


def test_identity_session_debits_once_with_session_scoped_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        environment="test",
        veriff_enabled=True,
        veriff_api_key="api-key",
        veriff_shared_secret_key="shared-secret",  # noqa: S106
    )
    session = VeriffSession("session-exact", "https://verify.example/session-exact")
    monkeypatch.setattr(identity_route, "create_veriff_session", lambda **_kwargs: session)
    original = InMemoryStore.debit_workspace_guarded
    calls: list[tuple[str, int, str, str]] = []

    def debit(
        self: InMemoryStore,
        workspace_id: str,
        amount_microdollars: int,
        event_id: str,
        *,
        kind: str,
        custom_model_id: str | None = None,
        authorization_id: str | None = None,
    ) -> str:
        calls.append((workspace_id, amount_microdollars, event_id, kind))
        return original(
            self,
            workspace_id,
            amount_microdollars,
            event_id,
            kind=kind,
            custom_model_id=custom_model_id,
            authorization_id=authorization_id,
        )

    monkeypatch.setattr(InMemoryStore, "debit_workspace_guarded", debit)
    for client in _client(settings):
        user = _eligible_user(client)
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        response = client.post("/v1/auth/identity/session", headers=HEADERS)

    assert response.status_code == 201
    assert calls == [
        (
            workspace.id,
            VERIFF_ATTEMPT_FEE_MICRODOLLARS,
            f"veriff_fee:{user.id}:session-exact",
            "verification_fee",
        )
    ]


def test_identity_session_insufficient_discards_unpersisted_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        environment="test",
        veriff_enabled=True,
        veriff_api_key="api-key",
        veriff_shared_secret_key="shared-secret",  # noqa: S106
    )
    monkeypatch.setattr(
        identity_route,
        "create_veriff_session",
        lambda **_kwargs: VeriffSession("discarded", "https://verify.example/discarded"),
    )
    for client in _client(settings):
        user = _eligible_user(client)
        workspace = STORE.list_workspaces_for_user(user.id)[0]
        available = VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS + (
            10 * MICRODOLLARS_PER_DOLLAR
        )
        assert STORE.debit_workspace_guarded(
            workspace.id,
            available,
            "drain-before-veriff",
            kind="test_drain",
        ) == "accepted"

        response = client.post("/v1/auth/identity/session", headers=HEADERS)
        updated = STORE.get_user(user.id)

    assert response.status_code == 402
    assert updated is not None
    assert updated.identity_status == "none"
    assert updated.veriff_session_id is None
    assert updated.veriff_attempt_count == 0


@pytest.mark.parametrize("status", ["pending", "resubmission_requested"])
def test_young_reusable_session_returns_same_url_without_second_debit(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    settings = Settings(
        environment="test",
        veriff_enabled=True,
        veriff_api_key="api-key",
        veriff_shared_secret_key="shared-secret",  # noqa: S106
    )

    def unexpected_debit(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("a reusable session must not be charged again")

    monkeypatch.setattr(InMemoryStore, "debit_workspace_guarded", unexpected_debit)
    for client in _client(settings):
        user = _eligible_user(client)
        STORE.set_user_identity_status(
            user.id,
            status=status,
            session_id="young-session",
            session_url="https://verify.example/young-session",
            increment_attempts=True,
        )
        response = client.post("/v1/auth/identity/session", headers=HEADERS)

    assert response.status_code == 201
    assert response.json()["data"] == {
        "url": "https://verify.example/young-session",
        "status": status,
    }


@pytest.mark.parametrize("old_status", ["declined", "pending"])
def test_declined_or_stale_pending_creates_and_charges_new_session(
    monkeypatch: pytest.MonkeyPatch,
    old_status: str,
) -> None:
    settings = Settings(
        environment="test",
        veriff_enabled=True,
        veriff_api_key="api-key",
        veriff_shared_secret_key="shared-secret",  # noqa: S106
    )
    monkeypatch.setattr(
        identity_route,
        "create_veriff_session",
        lambda **_kwargs: VeriffSession("new-session", "https://verify.example/new-session"),
    )
    for client in _client(settings):
        user = _eligible_user(client)
        old = STORE.set_user_identity_status(
            user.id,
            status=old_status,
            session_id="old-session",
            session_url="https://verify.example/old-session",
            increment_attempts=True,
        )
        assert old is not None
        if old_status == "pending":
            old.veriff_session_created_at = (
                dt.datetime.now(dt.UTC) - dt.timedelta(days=8)
            ).isoformat()
        response = client.post("/v1/auth/identity/session", headers=HEADERS)
        updated = STORE.get_user(user.id)

    assert response.status_code == 201
    assert updated is not None
    assert updated.veriff_session_id == "new-session"
    assert updated.veriff_attempt_count == 2
    workspace_id = STORE.list_workspaces_for_user(user.id)[0].id
    movements = STORE.list_credit_movements(workspace_id)
    assert any(
        movement.movement_id == f"veriff_fee:{user.id}:new-session"
        for movement in movements
    )
