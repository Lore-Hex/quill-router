"""Console actions the API reserves for owners and admins refuse a plain member.

The API gates billing, Broadcast and BYOK with ``ManagementPrincipal`` and
``tests/test_security_contracts.py`` pins its 403s. These console routes used
to check membership only, so a member could, among other things, enable
auto-refill on the owner's saved card or add a Broadcast destination that
receives every prompt. Each case below asserts the 403, that nothing changed,
and that no billing provider call was attempted; owners and admins still
succeed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import AutoRefillSettingsError, check_auto_refill_settings
from trusted_router.routes.console import credits as credits_module
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.store_protocol import Store

OWNER_EMAIL = "gates-owner@example.com"
ADMIN_EMAIL = "gates-admin@example.com"
MEMBER_EMAIL = "gates-member@example.com"
OWNER_CARD = "pm_gates_owner_card"
OWNER_CUSTOMER = "cus_gates_owner"
WEBHOOK_URL = "https://owner-collector.example/otlp"
WEBHOOK_HEADER = "X-Owner-Collector-Token"
# A fake provider key, assembled so no literal looks like a credential.
BYOK_KEY = "-".join(["mistral", "owner", "fake", "9876"])
BYOK_HINT = "mistra...9876"
RECEIPT_URL = "https://pay.stripe.test/receipts/owner-receipt"
FORBIDDEN = "Requires workspace manager role"

_PAYMENT = {
    "id": "pi_gates_owner",
    "created": 1_763_000_000,
    "amount": 2_659,
    "currency": "usd",
    "status": "succeeded",
    "metadata": {"payment_method": "card"},
    "latest_charge": {
        "receipt_url": RECEIPT_URL,
        "payment_method_details": {"card": {"brand": "visa", "last4": "4242"}},
    },
}


@dataclass
class Gates:
    app: Any
    workspace_id: str
    users: dict[str, str]

    def client(self, role: str) -> TestClient:
        client = TestClient(self.app)
        raw_token, _ = STORE.create_auth_session(
            user_id=self.users[role],
            provider="test",
            label=f"gates-{role}",
            ttl_seconds=3600,
            workspace_id=self.workspace_id,
            state="active",
        )
        client.cookies.set("tr_session", raw_token)
        return client


@pytest.fixture
def billing_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every Stripe SDK call the console's billing services can make, plus
    the PayPal and Adyen session creators, recorded instead of sent."""
    calls: list[str] = []

    def record(name: str, result: Any) -> Callable[..., Any]:
        def fake(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            return result

        return fake

    sdk = "trusted_router.services.stripe_billing.stripe"
    for target, result in (
        ("checkout.Session.create", {"id": "cs_gates", "url": "https://checkout.stripe.test/session"}),
        ("Customer.create", {"id": "cus_gates_new"}),
        ("billing_portal.Session.create", {"url": "https://billing.stripe.test/portal"}),
        (
            "PaymentMethod.retrieve",
            {"type": "card", "card": {"brand": "visa", "last4": "4242", "exp_month": 1, "exp_year": 2030}},
        ),
        ("PaymentMethod.detach", {"id": OWNER_CARD}),
        ("PaymentIntent.search", SimpleNamespace(data=[_PAYMENT])),
    ):
        monkeypatch.setattr(f"{sdk}.{target}", record(target, result))
    for name in ("create_paypal_checkout_session", "create_adyen_checkout_session"):
        monkeypatch.setattr(credits_module, name, record(name, {"mode": "mock"}))
    return calls


@pytest.fixture
def gates(billing_calls: list[str]) -> Gates:
    del billing_calls  # installed before any request can reach a provider
    settings = Settings(
        environment="local",
        stripe_secret_key="sk_test_member_role_gates",  # noqa: S106 - test fixture.
        stripe_webhook_secret=None,
    )
    app = create_app(settings, init_observability=False)
    owner = STORE.ensure_user(OWNER_EMAIL, email=OWNER_EMAIL)
    workspace = STORE.list_workspaces_for_user(owner.id)[0]
    STORE.set_stripe_customer(workspace.id, customer_id=OWNER_CUSTOMER, payment_method_id=OWNER_CARD)
    STORE.update_auto_refill_settings(
        workspace.id, enabled=True, threshold_microdollars=10_000_000, amount_microdollars=25_000_000,
    )
    admin = STORE.add_members(workspace.id, [ADMIN_EMAIL], role="admin")[0]
    member = STORE.add_members(workspace.id, [MEMBER_EMAIL], role="member")[0]
    return Gates(
        app=app,
        workspace_id=workspace.id,
        users={"owner": owner.id, "admin": admin.user_id, "member": member.user_id},
    )


def _billing_state(workspace_id: str) -> tuple[Any, ...]:
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    return (
        account.stripe_customer_id,
        account.stripe_payment_method_id,
        account.auto_refill_enabled,
        account.auto_refill_threshold_microdollars,
        account.auto_refill_amount_microdollars,
    )


_MEMBER_BILLING_REQUESTS = [
    ("POST", "/console/credits/auto-refill", {"enabled": "1", "threshold": "500", "amount": "2000"}),
    ("POST", "/console/credits/auto-refill", {"threshold": "10", "amount": "25"}),
    ("POST", "/console/credits/payment-methods/remove", None),
    ("POST", "/console/credits/payment-methods/add", None),
    ("POST", "/console/credits/payment-methods/manage", None),
    ("POST", "/console/credits/checkout", {"amount": "25", "payment_method": "card"}),
    ("POST", "/console/credits/checkout", {"amount": "10000", "payment_method": "auto"}),
    ("POST", "/console/credits/checkout", {"amount": "25", "payment_method": "paypal"}),
    ("POST", "/console/credits/checkout", {"amount": "25", "payment_method": "adyen"}),
    ("GET", "/console/credits/stripe-details", None),
]


@pytest.mark.parametrize(
    "method,path,data",
    _MEMBER_BILLING_REQUESTS,
    ids=[
        "enable-auto-refill", "disable-auto-refill", "remove-card", "add-card",
        "billing-portal", "checkout-card", "checkout-max", "checkout-paypal",
        "checkout-adyen", "stripe-details",
    ],
)
def test_member_cannot_use_billing(
    gates: Gates, billing_calls: list[str], method: str, path: str, data: dict[str, str] | None,
) -> None:
    before = _billing_state(gates.workspace_id)
    response = gates.client("member").request(method, path, data=data, follow_redirects=False)
    assert response.status_code == 403, response.text
    assert FORBIDDEN in response.text
    assert billing_calls == []
    assert _billing_state(gates.workspace_id) == before


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_can_use_billing(gates: Gates, billing_calls: list[str], role: str) -> None:
    client = gates.client(role)

    saved = client.post(
        "/console/credits/auto-refill",
        data={"enabled": "1", "threshold": "20", "amount": "60"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/console/credits?saved=1"
    assert _billing_state(gates.workspace_id)[2:] == (True, 20_000_000, 60_000_000)

    checkout = client.post(
        "/console/credits/checkout",
        data={"amount": "25", "payment_method": "card"},
        follow_redirects=False,
    )
    assert checkout.status_code == 303, checkout.text
    assert checkout.headers["location"] == "https://checkout.stripe.test/session"

    added = client.post("/console/credits/payment-methods/add", follow_redirects=False)
    assert added.status_code == 303
    assert added.headers["location"] == "https://checkout.stripe.test/session"

    portal = client.post("/console/credits/payment-methods/manage", follow_redirects=False)
    assert portal.status_code == 303
    assert portal.headers["location"] == "https://billing.stripe.test/portal"

    details = client.get("/console/credits/stripe-details")
    assert details.status_code == 200
    assert "Visa ····4242" in details.text
    assert RECEIPT_URL in details.text

    removed = client.post("/console/credits/payment-methods/remove", follow_redirects=False)
    assert removed.status_code == 303
    assert removed.headers["location"] == "/console/credits?payment_method=removed"
    assert _billing_state(gates.workspace_id)[1] is None

    # stripe-details makes its two reads concurrently, so compare as a multiset.
    assert sorted(billing_calls) == sorted([
        "checkout.Session.create",  # checkout
        "checkout.Session.create",  # add a card (setup session)
        "billing_portal.Session.create",
        "PaymentMethod.retrieve",
        "PaymentIntent.search",
        "PaymentMethod.detach",
    ])


def test_member_credits_page_shows_the_balance_only(gates: Gates, billing_calls: list[str]) -> None:
    page = gates.client("member").get("/console/credits")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "private, no-store"
    assert "credits-balance-amount" in page.text
    assert "Only workspace owners and admins can manage billing." in page.text
    for hidden in (
        "/console/credits/checkout",
        "/console/credits/payment-methods",
        "/console/credits/auto-refill",
        "/console/credits/stripe-details",
        "/console/credit-transfers",
        "Saved card",
        "Manage in Stripe",
        "Payment history",
        "Auto refill",
        "Receipt",
        OWNER_CARD[-6:],
    ):
        assert hidden not in page.text, hidden
    assert billing_calls == []

    owner_page = gates.client("owner").get("/console/credits")
    assert "Only workspace owners and admins can manage billing." not in owner_page.text
    for shown in (
        'action="/console/credits/checkout"',
        'action="/console/credits/payment-methods/remove"',
        'action="/console/credits/auto-refill"',
        'data-credits-stripe-details-url="/console/credits/stripe-details"',
        "Manage in Stripe",
    ):
        assert shown in owner_page.text, shown


def _owner_broadcast(client: TestClient) -> None:
    created = client.post(
        "/console/broadcast",
        data={
            "name": "Owner collector",
            "destination_type": "webhook",
            "endpoint": WEBHOOK_URL,
            "method": "POST",
            "headers_json": f'{{"{WEBHOOK_HEADER}":"owner-header-secret"}}',
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    assert created.headers["location"] == "/console/broadcast"


def test_member_cannot_create_or_delete_broadcast_destinations(gates: Gates) -> None:
    _owner_broadcast(gates.client("owner"))
    [existing] = STORE.list_broadcast_destinations(gates.workspace_id)
    member = gates.client("member")

    created = member.post(
        "/console/broadcast",
        data={
            "name": "Member exfiltration",
            "destination_type": "webhook",
            "endpoint": "https://member-collector.example/otlp",
            "enabled": "on",
            "include_content": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 403, created.text
    assert FORBIDDEN in created.text
    deleted = member.post(f"/console/broadcast/{existing.id}/delete", follow_redirects=False)
    assert deleted.status_code == 403, deleted.text
    assert [d.id for d in STORE.list_broadcast_destinations(gates.workspace_id)] == [existing.id]

    page = member.get("/console/broadcast")
    assert page.status_code == 200
    assert "Owner collector" in page.text  # that a destination exists
    assert "Only workspace owners and admins can manage Broadcast destinations." in page.text
    for hidden in (WEBHOOK_URL, "owner-collector.example", WEBHOOK_HEADER, existing.id,
                   'action="/console/broadcast"', "/delete", "api key", ">headers<"):
        assert hidden not in page.text, hidden


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_manage_broadcast_destinations(gates: Gates, role: str) -> None:
    client = gates.client(role)
    _owner_broadcast(client)
    [destination] = STORE.list_broadcast_destinations(gates.workspace_id)
    page = client.get("/console/broadcast")
    assert WEBHOOK_URL in page.text
    assert 'action="/console/broadcast"' in page.text
    deleted = client.post(f"/console/broadcast/{destination.id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert STORE.list_broadcast_destinations(gates.workspace_id) == []


def test_member_cannot_save_byok_and_sees_no_key_hint(gates: Gates) -> None:
    owner = gates.client("owner")
    saved = owner.post(
        "/console/byok", data={"provider": "mistral", "api_key": BYOK_KEY}, follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    assert [p.key_hint for p in STORE.list_byok_providers(gates.workspace_id)] == [BYOK_HINT]

    member = gates.client("member")
    refused = member.post(
        "/console/byok",
        data={"provider": "mistral", "api_key": "member-replacement-key-0000"},
        follow_redirects=False,
    )
    assert refused.status_code == 403, refused.text
    assert FORBIDDEN in refused.text
    [provider] = STORE.list_byok_providers(gates.workspace_id)
    assert provider.key_hint == BYOK_HINT

    page = member.get("/console/byok")
    assert page.status_code == 200
    assert "Mistral" in page.text and "Key set" in page.text
    assert "Only workspace owners and admins can manage provider keys." in page.text
    assert BYOK_HINT not in page.text and "9876" not in page.text
    assert 'action="/console/byok"' not in page.text
    assert BYOK_HINT in owner.get("/console/byok").text


def test_admin_can_save_byok(gates: Gates) -> None:
    admin = gates.client("admin")
    saved = admin.post(
        "/console/byok", data={"provider": "mistral", "api_key": BYOK_KEY}, follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    assert BYOK_HINT in admin.get("/console/byok").text


@pytest.mark.parametrize(
    "threshold,amount",
    [("50", "50"), ("60", "50"), ("500", "5")],
    ids=["equal", "above", "far-above"],
)
def test_auto_refill_threshold_at_or_above_amount_is_a_form_error(
    gates: Gates, threshold: str, amount: str,
) -> None:
    client = gates.client("owner")
    before = _billing_state(gates.workspace_id)
    response = client.post(
        "/console/credits/auto-refill",
        data={"enabled": "1", "threshold": threshold, "amount": amount},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/console/credits?error=auto_refill_threshold"
    assert _billing_state(gates.workspace_id) == before
    page = client.get(response.headers["location"])
    assert "Set the refill amount above the threshold" in page.text
    assert "data-auto-refill-error" in page.text


def test_auto_refill_accepts_a_threshold_below_the_amount_and_any_disable(gates: Gates) -> None:
    client = gates.client("owner")
    below = client.post(
        "/console/credits/auto-refill",
        data={"enabled": "1", "threshold": "49", "amount": "50"},
        follow_redirects=False,
    )
    assert below.headers["location"] == "/console/credits?saved=1"
    assert _billing_state(gates.workspace_id)[2:] == (True, 49_000_000, 50_000_000)
    assert "data-auto-refill-error" not in client.get("/console/credits").text

    # Switching off must always work, even with numbers saved before the rule.
    off = client.post(
        "/console/credits/auto-refill",
        data={"threshold": "500", "amount": "5"},
        follow_redirects=False,
    )
    assert off.headers["location"] == "/console/credits?saved=1"
    assert _billing_state(gates.workspace_id)[2] is False


def _memory_store() -> tuple[Store, str]:
    store = InMemoryStore()
    user = store.ensure_user("refill-rule@example.com", email="refill-rule@example.com")
    return store, store.list_workspaces_for_user(user.id)[0].id


def _spanner_store() -> tuple[Store, str]:
    store, _database = make_fake_store()
    user = store.ensure_user("refill-rule@example.com", email="refill-rule@example.com")
    return store, store.list_workspaces_for_user(user.id)[0].id


@pytest.mark.parametrize("factory", [_memory_store, _spanner_store], ids=["memory", "spanner"])
def test_store_refuses_auto_refill_threshold_at_or_above_amount(
    factory: Callable[[], tuple[Store, str]],
) -> None:
    """Another caller of the store cannot save what the form refuses."""
    store, workspace_id = factory()
    assert store.update_auto_refill_settings(
        workspace_id, enabled=True, threshold_microdollars=10_000_000, amount_microdollars=25_000_000,
    ) is not None

    for threshold, amount in ((25_000_000, 25_000_000), (500_000_000, 5_000_000)):
        with pytest.raises(AutoRefillSettingsError):
            store.update_auto_refill_settings(
                workspace_id, enabled=True, threshold_microdollars=threshold, amount_microdollars=amount,
            )
        account = store.get_credit_account(workspace_id)
        assert account is not None
        assert (
            account.auto_refill_enabled,
            account.auto_refill_threshold_microdollars,
            account.auto_refill_amount_microdollars,
        ) == (True, 10_000_000, 25_000_000)

    disabled = store.update_auto_refill_settings(
        workspace_id, enabled=False, threshold_microdollars=500_000_000, amount_microdollars=5_000_000,
    )
    assert disabled is not None and disabled.auto_refill_enabled is False


def test_auto_refill_rule_is_a_value_error_with_a_reason() -> None:
    check_auto_refill_settings(enabled=True, threshold_microdollars=1, amount_microdollars=2)
    check_auto_refill_settings(enabled=False, threshold_microdollars=2, amount_microdollars=1)
    with pytest.raises(ValueError, match="threshold must be below the refill amount"):
        check_auto_refill_settings(enabled=True, threshold_microdollars=2, amount_microdollars=2)


# ── Identity verification, credit transfers, authorized apps, welcome ──────

def test_member_cannot_start_identity_verification_on_the_shared_workspace(gates: Gates) -> None:
    """The $5 fee is debited from the selected workspace, as the API route
    (ManagementPrincipal) already requires a manager for."""
    from trusted_router.storage_models import CreditProvenance
    from trusted_router.typed_balance import live_credit_summary

    member_id = gates.users["member"]
    STORE.mark_user_email_verified(member_id)
    # Meet the member's own prerequisites, so only the role can refuse.
    assert STORE.credit_workspace_typed_direct(
        gates.workspace_id, 25_000_000, f"gates-identity-funding:{member_id}",
        provenance=CreditProvenance.system_grant(), lifetime_topup_user_id=member_id,
    )
    before = live_credit_summary(gates.workspace_id)
    member = gates.client("member")

    started = member.post("/console/account/verification/identity/start", follow_redirects=False)
    assert started.status_code == 403, started.text
    assert FORBIDDEN in started.text
    assert live_credit_summary(gates.workspace_id) == before
    user = STORE.get_user(member_id)
    assert user is not None and not user.identity_verified and user.identity_status == "none"

    page = member.get("/console/account/verification").text
    assert "data-identity-manager-note" in page
    assert "only its owners and admins can start verification here" in page
    assert "owners and managers" not in page
    assert 'class="btn primary verification-start" type="submit" disabled' in page

    admin = gates.client("admin")
    assert "data-identity-manager-note" not in admin.get("/console/account/verification").text


def test_member_cannot_view_credit_transfers(gates: Gates) -> None:
    refused = gates.client("member").get("/console/credit-transfers")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text
    for role in ("owner", "admin"):
        assert gates.client(role).get("/console/credit-transfers").status_code == 200


@pytest.mark.parametrize("action", ["budget", "revoke"])
@pytest.mark.parametrize(
    "headers",
    [{}, {"origin": "null"}, {"origin": "https://evil.example"}],
    ids=["missing", "null", "foreign"],
)
def test_authorized_app_changes_require_same_origin(action: str, headers: dict[str, str]) -> None:
    from tests.test_oauth_authorized_apps_increment_e import APP_ID, _grant

    # The app first: creating it configures the store the grant writes to.
    client = TestClient(create_app(Settings(environment="test"), init_observability=False))
    user_id, workspace_id, _raw_keys = _grant()
    raw_token, _ = STORE.create_auth_session(
        user_id=user_id, provider="test", label="apps", ttl_seconds=3600,
        workspace_id=workspace_id, state="active",
    )
    client.cookies.set("tr_session", raw_token)
    keys = [k for k in STORE.list_keys(workspace_id) if k.app_id == APP_ID]
    assert len(keys) == 2
    before = [(k.disabled, k.limit_monthly_microdollars) for k in keys]

    path = f"/console/authorized-apps/{APP_ID}/{action}"
    data = {"monthly_budget": "1"} if action == "budget" else None
    refused = client.post(path, data=data, headers=headers, follow_redirects=False)
    assert refused.status_code == 403, refused.text
    after = [STORE.get_key_by_hash(k.hash) for k in keys]
    assert [(k.disabled, k.limit_monthly_microdollars) for k in after] == before

    # Control: the same POST from the console's own origin goes through.
    allowed = client.post(
        path, data=data, headers={"origin": "http://testserver"}, follow_redirects=False,
    )
    assert allowed.status_code == 303, allowed.text
    changed = [STORE.get_key_by_hash(k.hash) for k in keys]
    assert [(k.disabled, k.limit_monthly_microdollars) for k in changed] != before


def test_welcome_page_does_not_send_members_to_the_create_key_panel(gates: Gates) -> None:
    member_page = gates.client("member").get("/console/welcome").text
    assert "already been displayed" in member_page
    assert "/console/api-keys#new-api-key" not in member_page
    assert "data-welcome-member-note" in member_page
    owner_page = gates.client("owner").get("/console/welcome").text
    assert 'href="/console/api-keys#new-api-key"' in owner_page
    assert "data-welcome-member-note" not in owner_page


def test_activation_reminders_go_only_to_the_workspace_owner(
    client: TestClient, test_settings: Settings,
) -> None:
    """The reminder links to the create-key panel, so its only recipient is
    someone who can use it: the workspace owner, never a member."""
    from trusted_router.services.activation_reminders import run_activation_reminder_pass

    settings = test_settings
    signup = client.post("/v1/signup", json={"email": "reminded-owner@example.com", "name": "Owner"})
    assert signup.status_code == 201, signup.text
    workspace_id = signup.json()["data"]["workspace_id"]
    STORE.add_members(workspace_id, ["reminded-member@example.com"], role="member")
    STORE.add_members(workspace_id, ["reminded-admin@example.com"], role="admin")

    sent: list[Any] = []
    for task in STORE.list_activation_reminders(limit=10):
        run_activation_reminder_pass(
            settings, now=task.due_at,
            email_service=SimpleNamespace(send=lambda message: sent.append(message) or True),  # type: ignore[arg-type]
        )
    assert len(sent) == 2
    assert {message.to for message in sent} == {"reminded-owner@example.com"}
    assert all("/console/api-keys#new-api-key" in message.text_body for message in sent)


def test_a_members_credit_transfer_post_lands_on_the_credits_note(gates: Gates) -> None:
    """Not on the transfers page, which a member gets 403 for: the Credits
    page tells them to ask a workspace owner or admin."""
    member = gates.client("member")
    response = member.post(
        "/console/credit-transfers",
        data={"recipient_username": "someone", "amount": "1", "idempotency_key": "gates-transfer"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/console/credits"
    page = member.get(response.headers["location"])
    assert page.status_code == 200
    assert "Only workspace owners and admins can manage billing. Ask one of them" in page.text
