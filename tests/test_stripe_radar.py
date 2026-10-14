from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import stripe
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services.stripe_radar import report_workspace_card_fraud
from trusted_router.storage import InMemoryStore, configure_store

_SEARCH = "trusted_router.services.stripe_radar.stripe.PaymentIntent.search"
_MODIFY = "trusted_router.services.stripe_radar.stripe.Charge.modify"
_OPERATOR_HEADERS = {
    "authorization": "Bearer operator-secret",
    "x-trustedrouter-operator-identity": "ops@example.com",
}


def _charge(
    charge_id: str,
    *,
    status: str = "succeeded",
    method: str = "card",
    refunded: bool = False,
    user_report: str | None = None,
) -> dict[str, Any]:
    return {
        "id": charge_id,
        "status": status,
        "refunded": refunded,
        "payment_method_details": {"type": method},
        "fraud_details": {"user_report": user_report} if user_report else {},
    }


class _StripeObject:
    """Shaped like a StripeObject: not a dict, converts recursively."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def _to_dict_recursive(self) -> dict[str, Any]:
        return self._data


class _FakeStripe:
    """Payment search plus charge updates, keeping Stripe's fraud flag."""

    def __init__(self, charges_by_intent: dict[str, dict[str, Any] | None]) -> None:
        self.charges_by_intent = charges_by_intent
        self.searches: list[dict[str, Any]] = []
        self.modified: list[tuple[str, dict[str, Any]]] = []
        self.fail_charges: set[str] = set()
        self.search_error: Exception | None = None
        self.later_page_error: Exception | None = None

    def search(self, **params: Any) -> Any:
        self.searches.append(params)
        if self.search_error is not None:
            raise self.search_error
        intents = [
            _StripeObject({"id": intent_id, "latest_charge": charge})
            for intent_id, charge in self.charges_by_intent.items()
        ]
        later_page_error = self.later_page_error

        class _Page:
            def auto_paging_iter(self) -> Any:
                yield intents[0]
                if later_page_error is not None:
                    raise later_page_error
                yield from intents[1:]

        return _Page()

    def modify(self, charge_id: str, **params: Any) -> dict[str, Any]:
        if charge_id in self.fail_charges:
            raise RuntimeError("stripe unavailable")
        self.modified.append((charge_id, params))
        for charge in self.charges_by_intent.values():
            if charge is not None and charge["id"] == charge_id:
                charge["fraud_details"] = dict(params["fraud_details"])
        return {"id": charge_id}


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> _FakeStripe:
    fake = _FakeStripe(
        {
            "pi_paid": _charge("ch_paid"),
            "pi_refunded": _charge("ch_refunded", refunded=True),
            "pi_reported": _charge("ch_reported", user_report="fraudulent"),
            "pi_declined": _charge("ch_declined", status="failed"),
            "pi_pending": _charge("ch_pending", status="pending"),
            "pi_crypto": _charge("ch_crypto", method="crypto"),
            "pi_unattempted": None,
        }
    )
    monkeypatch.setattr(_SEARCH, fake.search)
    monkeypatch.setattr(_MODIFY, fake.modify)
    return fake


def _settings(**overrides: Any) -> Settings:
    return Settings(environment="test", stripe_secret_key="sk_test_radar", **overrides)  # noqa: S106


def test_report_marks_each_succeeded_card_charge_fraudulent(fake_stripe: _FakeStripe) -> None:
    report = report_workspace_card_fraud(workspace_id="ws-1", settings=_settings())

    assert report == {
        "status": "complete",
        "reported": ["ch_paid", "ch_refunded"],
        "already_reported": ["ch_reported"],
        "failed": [],
    }
    assert fake_stripe.modified == [
        ("ch_paid", {"fraud_details": {"user_report": "fraudulent"}}),
        ("ch_refunded", {"fraud_details": {"user_report": "fraudulent"}}),
    ]
    assert fake_stripe.searches == [
        {
            "query": "metadata['workspace_id']:'ws-1'",
            "limit": 100,
            "expand": ["data.latest_charge"],
        }
    ]


def test_report_retry_completes_only_what_failed(fake_stripe: _FakeStripe) -> None:
    fake_stripe.fail_charges.add("ch_paid")

    first = report_workspace_card_fraud(workspace_id="ws-1", settings=_settings())
    assert first["status"] == "incomplete"
    assert first["reported"] == ["ch_refunded"]
    assert first["failed"] == ["ch_paid"]

    fake_stripe.fail_charges.clear()
    second = report_workspace_card_fraud(workspace_id="ws-1", settings=_settings())
    assert second == {
        "status": "complete",
        "reported": ["ch_paid"],
        "already_reported": ["ch_refunded", "ch_reported"],
        "failed": [],
    }


def test_report_says_when_the_payment_search_failed(fake_stripe: _FakeStripe) -> None:
    fake_stripe.search_error = RuntimeError("search down")

    report = report_workspace_card_fraud(workspace_id="ws-1", settings=_settings())

    assert report["status"] == "incomplete"
    assert report["error"] == "payment search failed: RuntimeError"
    assert report["reported"] == []
    assert fake_stripe.modified == []


def test_report_reports_nothing_when_a_later_page_fails(fake_stripe: _FakeStripe) -> None:
    fake_stripe.later_page_error = RuntimeError("page 2 failed")

    report = report_workspace_card_fraud(workspace_id="ws-1", settings=_settings())

    assert report["status"] == "incomplete"
    assert report["error"] == "payment search failed: RuntimeError"
    assert fake_stripe.modified == []


class _StripeTransport(stripe.HTTPClient):
    """Answers the real SDK's HTTP requests: two search pages, then updates."""

    name = "test-transport"

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Any,
        post_data: Any = None,
        *,
        _usage: Any = None,
    ) -> tuple[str, int, dict[str, str]]:
        parsed = urlsplit(url)
        params = parse_qs(parsed.query if method == "get" else (post_data or ""))
        self.requests.append((method, parsed.path, params))
        if parsed.path == "/v1/payment_intents/search":
            page = params.get("page", [""])[0]
            charge_id, more = ("ch_page2", False) if page == "next-page" else ("ch_page1", True)
            body = {
                "object": "search_result",
                "url": "/v1/payment_intents/search",
                "has_more": more,
                "next_page": "next-page" if more else None,
                "data": [
                    {
                        "id": f"pi_{charge_id}",
                        "object": "payment_intent",
                        "latest_charge": _charge(charge_id) | {"object": "charge"},
                    }
                ],
            }
            return json.dumps(body), 200, {}
        charge_id = parsed.path.rsplit("/", 1)[-1]
        return json.dumps({"id": charge_id, "object": "charge"}), 200, {}

    def close(self) -> None:
        return None


def test_report_follows_every_search_page_through_the_stripe_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _StripeTransport()
    monkeypatch.setattr(stripe, "default_http_client", transport)

    report = report_workspace_card_fraud(workspace_id="ws-9", settings=_settings())

    assert report["reported"] == ["ch_page1", "ch_page2"]
    searches = [params for method, path, params in transport.requests if "search" in path]
    assert len(searches) == 2
    for params in searches:
        assert params["query"] == ["metadata['workspace_id']:'ws-9'"]
        assert params["expand[0]"] == ["data.latest_charge"]
    assert searches[1]["page"] == ["next-page"]
    updates = [(path, params) for method, path, params in transport.requests if method == "post"]
    assert updates == [
        ("/v1/charges/ch_page1", {"fraud_details[user_report]": ["fraudulent"]}),
        ("/v1/charges/ch_page2", {"fraud_details[user_report]": ["fraudulent"]}),
    ]


def test_report_without_a_stripe_key_is_unavailable(fake_stripe: _FakeStripe) -> None:
    report = report_workspace_card_fraud(
        workspace_id="ws-1", settings=Settings(environment="test")
    )

    assert report == {"status": "unavailable", "reason": "this service has no Stripe key"}
    assert fake_stripe.searches == []


def _operator_client(**settings_overrides: Any) -> tuple[TestClient, InMemoryStore, str]:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("radar@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    settings = Settings(
        environment="test",
        service_surface="combined",
        operator_token="operator-secret",  # noqa: S106 - test credential.
        operator_identities="ops@example.com",
        **settings_overrides,
    )
    client = TestClient(create_app(settings, configure_store_arg=False, init_observability=False))
    return client, store, workspace.id


def _abuse_url(workspace_id: str) -> str:
    return f"/v1/internal/admin/workspaces/{workspace_id}/abuse"


def test_abuse_latch_reports_card_fraud_and_replays_the_report(
    fake_stripe: _FakeStripe,
) -> None:
    client, store, workspace_id = _operator_client(stripe_secret_key="sk_test_radar")  # noqa: S106
    body = {"abuse_ref": "carding-1", "reason": "stolen cards", "report_card_fraud": True}

    first = client.post(_abuse_url(workspace_id), headers=_OPERATOR_HEADERS, json=body)

    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["applied"] is True
    assert data["stripe_card_fraud_report"]["status"] == "complete"
    assert data["stripe_card_fraud_report"]["reported"] == ["ch_paid", "ch_refunded"]
    assert fake_stripe.searches[0]["query"] == f"metadata['workspace_id']:'{workspace_id}'"
    workspace = store.get_workspace(workspace_id)
    assert workspace is not None and "abuse" in workspace.billing_pause_causes

    replay = client.post(_abuse_url(workspace_id), headers=_OPERATOR_HEADERS, json=body)

    assert replay.status_code == 200, replay.text
    replayed = replay.json()["data"]
    assert replayed["replayed"] is True
    assert replayed["stripe_card_fraud_report"]["reported"] == []
    assert replayed["stripe_card_fraud_report"]["already_reported"] == [
        "ch_paid",
        "ch_refunded",
        "ch_reported",
    ]
    assert [charge_id for charge_id, _ in fake_stripe.modified] == ["ch_paid", "ch_refunded"]


def test_abuse_latch_without_the_flag_leaves_stripe_alone(fake_stripe: _FakeStripe) -> None:
    client, _store, workspace_id = _operator_client(stripe_secret_key="sk_test_radar")  # noqa: S106

    response = client.post(
        _abuse_url(workspace_id),
        headers=_OPERATOR_HEADERS,
        json={"abuse_ref": "spam-1", "reason": "terms violation"},
    )

    assert response.status_code == 200, response.text
    assert "stripe_card_fraud_report" not in response.json()["data"]
    assert fake_stripe.searches == []


@pytest.mark.parametrize(
    ("extra", "detail"),
    [
        ({"report_card_fraud": "true"}, "report_card_fraud must be a boolean"),
        (
            {"report_card_fraud": True, "action": "clear"},
            "report_card_fraud applies only when latching abuse",
        ),
    ],
)
def test_abuse_rejects_a_bad_fraud_flag_before_changing_anything(
    fake_stripe: _FakeStripe, extra: dict[str, Any], detail: str
) -> None:
    client, store, workspace_id = _operator_client(stripe_secret_key="sk_test_radar")  # noqa: S106

    response = client.post(
        _abuse_url(workspace_id),
        headers=_OPERATOR_HEADERS,
        json={"abuse_ref": "case-2", "reason": "check", **extra},
    )

    assert response.status_code == 400
    assert detail in response.text
    workspace = store.get_workspace(workspace_id)
    assert workspace is not None and workspace.billing_pause_causes == []
    assert fake_stripe.searches == []


def test_abuse_fraud_report_on_an_unknown_workspace_never_reaches_stripe(
    fake_stripe: _FakeStripe,
) -> None:
    client, _store, _workspace_id = _operator_client(stripe_secret_key="sk_test_radar")  # noqa: S106

    response = client.post(
        _abuse_url("ws-missing"),
        headers=_OPERATOR_HEADERS,
        json={"abuse_ref": "case-3", "reason": "check", "report_card_fraud": True},
    )

    assert response.status_code == 404
    assert fake_stripe.searches == []


def test_abuse_fraud_report_is_unavailable_where_no_stripe_key_is_mounted(
    fake_stripe: _FakeStripe,
) -> None:
    client, store, workspace_id = _operator_client()

    response = client.post(
        _abuse_url(workspace_id),
        headers=_OPERATOR_HEADERS,
        json={"abuse_ref": "case-4", "reason": "stolen cards", "report_card_fraud": True},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["applied"] is True
    assert data["stripe_card_fraud_report"]["status"] == "unavailable"
    workspace = store.get_workspace(workspace_id)
    assert workspace is not None and "abuse" in workspace.billing_pause_causes
    assert fake_stripe.searches == []
