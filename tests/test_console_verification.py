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


def _fund(user: Any, workspace: Any) -> None:
    assert STORE.credit_workspace_typed_direct(
        workspace.id,
        VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
        f"console-verification-funding-only:{user.id}",
        provenance=CreditProvenance.system_grant(),
        lifetime_topup_user_id=user.id,
    )


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
    assert (
        "$1+ unlocks direct phone verification for US, Canadian, and most European numbers; "
        "$25 total unlocks identity verification."
    ) in initial.text
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
    _fund(user, workspace)

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
    assert not updated.phone_verified


def test_console_identity_start_maps_prerequisite_and_credit_errors() -> None:
    client, user, workspace = _console()

    prereqs = client.post(
        "/console/account/verification/identity/start",
        follow_redirects=False,
    )
    _fund(user, workspace)
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
    _fund(user, workspace)

    response = client.post(
        "/console/account/verification/identity/start",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("?error=veriff_unavailable")


def test_console_shows_regional_phone_block_and_startable_identity() -> None:
    client, user, workspace = _console()
    _fund(user, workspace)
    STORE.set_user_identity_status(user.id, status="approved")
    started = STORE.begin_phone_verification(user.id, "+18761234567", "voice")
    assert started is not None
    STORE.set_user_identity_status(user.id, status="declined")

    page = client.get("/console/account/verification")

    assert page.status_code == 200
    assert 'data-step="phone" data-step-state="blocked-until-identity"' in page.text
    assert "blocked until identity" in page.text
    assert 'href="#identity"' in page.text
    assert 'class="btn primary verification-start" type="submit" >' in page.text


def test_first_rejected_number_carries_block_reason_to_checklist() -> None:
    client, user, workspace = _console()
    _fund(user, workspace)
    page = client.post(
        "/console/settings/phone/start",
        data={"phone": "+18761234567", "channel": "voice", "sms_consent": "yes"},
        follow_redirects=True,
    )
    assert page.status_code == 200
    assert "This number is blocked until you complete" in page.text
    link = '/console/account/verification?error=identity_required'
    assert f'href="{link}"' in page.text
    checklist = client.get(link)
    assert 'data-step="phone" data-step-state="blocked-until-identity"' in checklist.text
    assert 'href="#identity"' in checklist.text
    assert 'href="/console/settings#phone">Verify phone with a different number</a>' in checklist.text
    updated = STORE.get_user(user.id)
    assert updated is not None and not updated.phone_verified


def test_stale_foreign_pending_still_offers_different_phone() -> None:
    client, user, workspace = _console()
    _fund(user, workspace)
    STORE.set_user_identity_status(user.id, status="approved")
    STORE.begin_phone_verification(user.id, "+18761234567", "voice")
    STORE.set_user_identity_status(user.id, status="declined")
    page = client.get("/console/account/verification")
    assert 'href="/console/settings#phone">Verify phone with a different number</a>' in page.text


def test_verification_copy_describes_independent_checks() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    public = (root / "src/trusted_router/templates/public/user_models_docs.html").read_text()
    assert "available without a verified phone once email and funding requirements are met" in public
    assert "other regions require identity verification first" in public
    assert 'class="pill">in order' not in public
    client, _, _ = _console()
    credits = client.get("/console/credits?purpose=identity_verification").text
    assert "US, Canadian, and most European numbers; other regions require identity verification first" in credits
    assert "Identity verification does not require a verified phone" in credits
    docs = (root / "docs/sign-in-with-trustedrouter.md").read_text()
    assert "identity does not imply a verified phone" in docs
    assert "other regions require identity verification first" in docs


def test_round3_refusal_survives_navigation_and_identity_approval() -> None:
    client, user, workspace = _console()
    _fund(user, workspace)
    client.post(
        "/console/settings/phone/start",
        data={"phone": "+18765550123", "sms_consent": "yes"},
    )
    assert 'data-step="phone" data-step-state="blocked-until-identity"' in client.get(
        "/console/account/verification"
    ).text
    STORE.set_user_identity_status(user.id, status="approved")
    assert 'data-step="phone" data-step-state="incomplete"' in client.get(
        "/console/account/verification?error=identity_required"
    ).text
    assert "This number is blocked" not in client.get(
        "/console/settings?error=identity_required"
    ).text


# Copy is policy: every public surface must state the qualified region rule and
# let a reader inspect the exact allowlist. Pin each file independently.
_RULE_URL = "https://github.com/Lore-Hex/quill-router/blob/main/src/trusted_router/phone_verification.py"


def test_round3_regional_copy_and_links() -> None:
    from pathlib import Path

    from bs4 import BeautifulSoup

    root = Path(__file__).resolve().parents[1]
    paths = [
        "src/trusted_router/templates/console/settings.html",
        "src/trusted_router/templates/console/credits.html",
        "src/trusted_router/templates/console/account/verification.html",
        "src/trusted_router/templates/public/user_models_docs.html",
        "src/trusted_router/templates/public/notify.html",
        "src/trusted_router/content/blog.py",
        "docs/sign-in-with-trustedrouter.md",
    ]
    for path in paths:
        source = (root / path).read_text()
        assert "most European numbers" in source, path
        assert "and European numbers" not in source, path
        assert "or European number" not in source, path
        assert _RULE_URL in source, path
        if path.endswith(".html"):
            soup = BeautifulSoup(source, "html.parser")
            for paragraph in soup.find_all(["p", "li"]):
                if "European" in paragraph.get_text():
                    assert "most European numbers" in paragraph.get_text(), path
                    assert paragraph.find("a", href=_RULE_URL), path


def test_round3_notify_example_is_directly_eligible() -> None:
    import re
    from pathlib import Path

    from trusted_router import phone_verification as pv

    root = Path(__file__).resolve().parents[1]
    source = (root / "src/trusted_router/templates/public/notify.html").read_text()
    example = re.search(r'"phone": "([+0-9]+)"', source)
    assert example is not None
    assert not pv.phone_verification_requires_identity_first(example[1])
    assert "other regions require identity verification first" in source


def test_round3_blog_does_not_require_phone_before_identity() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "src/trusted_router/content/blog.py").read_text()
    assert "which is what unlocks phone verification" not in source
    assert "and then a government ID check" not in source
    assert "Identity verification does not require a verified phone" in source


def test_round3_oauth_design_warns_against_inferring_lower_tiers() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "docs/design/oauth-scopes-and-app-economy.md").read_text()
    intro, rest = source.split("## Design decisions", 1)
    verification = rest.split("### Verification level:", 1)[1].split("### Registry", 1)[0]
    for section in (intro, verification):
        plain = " ".join(section.split())
        assert "highest tier achieved" in plain
        assert "identity > phone > email > none" in plain
        assert "Apps must not infer lower tiers" in plain
        assert "identity_verified does not imply phone_verified" in plain
    assert "The ladder is ordered; apps can gate on" not in source
