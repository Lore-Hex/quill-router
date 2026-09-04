from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import CreditProvenance


def _console(settings: Settings | None = None) -> tuple[TestClient, Any, Any]:
    client = TestClient(
        create_app(settings or Settings(environment="test"), init_observability=False)
    )
    user = STORE.ensure_user("verification-console@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_session, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="test",
        label="verification console",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    client.cookies.set("tr_session", raw_session)
    return client, user, workspace


def _fund_and_verify_phone(user: Any, workspace: Any) -> None:
    assert STORE.credit_workspace_typed_direct(
        workspace.id,
        VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
        f"console-verification-funding:{user.id}",
        provenance=CreditProvenance.system_grant(),
        lifetime_topup_user_id=user.id,
    )
    started = STORE.begin_phone_verification(user.id, "+13059511381", "voice")
    assert started is not None
    code, _updated = started
    assert STORE.confirm_phone_verification(user.id, code)[0] == "ok"


def test_console_verification_renders_four_live_steps_and_actions() -> None:
    client, user, workspace = _console()

    initial = client.get("/console/account/verification?veriff=done")

    assert initial.status_code == 200
    assert initial.text.count('class="verification-step ') == 4
    assert 'data-step="email" data-step-state="incomplete"' in initial.text
    assert 'data-step="funding" data-step-state="incomplete"' in initial.text
    assert 'data-step="phone" data-step-state="incomplete"' in initial.text
    assert 'data-step="identity" data-step-state="none"' in initial.text
    assert 'href="/console/account/preferences"' in initial.text
    assert 'href="/console/credits?purpose=identity_verification"' in initial.text
    assert 'href="/console/settings#phone"' in initial.text
    assert "$1+ unlocks phone verification; $25 total unlocks identity verification." in initial.text
    assert "Each new verification attempt costs $5.00." in initial.text
    assert "Identity submitted — the decision arrives automatically." in initial.text
    assert 'href="/console/account/verification" class="sidebar-link active"' in initial.text

    STORE.mark_user_email_verified(user.id)
    _fund_and_verify_phone(user, workspace)
    STORE.set_user_identity_status(
        user.id,
        status="approved",
        verified_name="Ada Lovelace",
    )
    complete = client.get("/console/account/verification")

    assert complete.text.count('data-step-state="complete"') == 3
    assert 'data-step="identity" data-step-state="approved"' in complete.text
    assert "Ada Lovelace" in complete.text
    assert complete.text.count('class="verification-step complete"') == 4


def test_console_identity_start_uses_shared_dev_flow_and_redirects() -> None:
    client, user, workspace = _console()
    _fund_and_verify_phone(user, workspace)

    response = client.post(
        "/console/account/verification/identity/start",
        follow_redirects=False,
    )
    updated = STORE.get_user(user.id)

    assert response.status_code == 303
    assert response.headers["location"] == "/console/account/verification?dev=1"
    assert updated is not None
    assert updated.identity_verified
    assert updated.identity_verified_name == "Dev User"


def test_console_identity_start_maps_prerequisite_and_credit_errors() -> None:
    client, user, workspace = _console()

    prereqs = client.post(
        "/console/account/verification/identity/start",
        follow_redirects=False,
    )
    _fund_and_verify_phone(user, workspace)
    available = VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS + (
        10 * MICRODOLLARS_PER_DOLLAR
    )
    assert STORE.debit_workspace_guarded(
        workspace.id,
        available,
        "drain-console-verification",
        kind="test_drain",
    ) == "accepted"
    insufficient = client.post(
        "/console/account/verification/identity/start",
        follow_redirects=False,
    )

    assert prereqs.status_code == 303
    assert prereqs.headers["location"].endswith("?error=prereqs")
    assert insufficient.status_code == 303
    assert insufficient.headers["location"].endswith("?error=insufficient")


def test_console_identity_start_maps_disabled_provider_to_unavailable() -> None:
    settings = Settings(
        environment="test",
        veriff_api_key="configured-api-key",
        veriff_shared_secret_key="configured-shared-secret",  # noqa: S106
    )
    client, user, workspace = _console(settings)
    _fund_and_verify_phone(user, workspace)

    response = client.post(
        "/console/account/verification/identity/start",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("?error=veriff_unavailable")
