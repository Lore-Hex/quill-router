from __future__ import annotations

import datetime as dt
import logging

import pytest
from fastapi.testclient import TestClient

import trusted_router.acquisition as acquisition_module
from tests.fakes.spanner import make_fake_store
from trusted_router.acquisition import (
    ATTRIBUTION_COOKIE_NAME,
    AttributionContext,
    decode_attribution_cookie,
    encode_attribution_cookie,
    record_free_credit_exhausted_safely,
    record_successful_api_call,
)
from trusted_router.config import Settings
from trusted_router.storage import STORE
from trusted_router.storage_models import AcquisitionAttribution


def _campaign_landing(client: TestClient, *, click_id: str = "google-click-123") -> None:
    response = client.get(
        "/openrouter-alternative"
        "?utm_source=google&utm_medium=paid_search"
        "&utm_campaign=router_launch&utm_content=privacy_a"
        f"&gclid={click_id}"
    )
    assert response.status_code == 200


def _signup(client: TestClient, email: str = "attributed@example.com") -> dict[str, object]:
    response = client.post("/v1/signup", json={"email": email, "name": "Attributed"})
    assert response.status_code == 201, response.text
    payload = response.json()["data"]
    assert isinstance(payload, dict)
    return payload


def test_cookie_round_trip_and_tamper_rejection() -> None:
    settings = Settings(
        internal_gateway_token="cookie-signing-root"  # noqa: S106 - test fixture secret.
    )
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    touch = {
        "utm_source": "google",
        "utm_medium": "paid_search",
        "utm_campaign": "launch",
        "gclid_fingerprint": "a" * 64,
        "landing_path": "/openrouter-alternative",
        "captured_at": now,
    }
    context = AttributionContext("a" * 32, touch, touch, now)

    encoded = encode_attribution_cookie(context, settings)
    assert decode_attribution_cookie(encoded, settings) == context
    assert decode_attribution_cookie(encoded + "tampered", settings) is None
    assert (
        decode_attribution_cookie(
            encoded,
            Settings(internal_gateway_token="rotated"),  # noqa: S106 - test fixture secret.
        )
        is None
    )


def test_expired_cookie_is_rejected() -> None:
    settings = Settings(
        internal_gateway_token="cookie-signing-root"  # noqa: S106 - test fixture secret.
    )
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=91)).replace(microsecond=0)
    created_at = old.isoformat().replace("+00:00", "Z")
    touch = {
        "utm_source": "x",
        "utm_medium": "paid_social",
        "landing_path": "/",
        "captured_at": created_at,
    }
    encoded = encode_attribution_cookie(
        AttributionContext("b" * 32, touch, touch, created_at), settings
    )
    assert decode_attribution_cookie(encoded, settings) is None


def test_legacy_cookie_with_raw_click_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        internal_gateway_token="cookie-signing-root"  # noqa: S106 - test fixture secret.
    )
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    legacy_touch = {
        "utm_source": "google",
        "utm_medium": "paid_search",
        "gclid": "legacy-raw-click-id",
        "landing_path": "/",
        "captured_at": now,
    }
    context = AttributionContext("c" * 32, legacy_touch, legacy_touch, now)
    monkeypatch.setattr(acquisition_module, "_COOKIE_VERSION", 1)
    encoded = encode_attribution_cookie(context, settings)
    monkeypatch.setattr(acquisition_module, "_COOKIE_VERSION", 2)

    assert decode_attribution_cookie(encoded, settings) is None


def test_paid_landing_sets_signed_httponly_cookie(client: TestClient) -> None:
    response = client.get(
        "/?utm_source=x&utm_medium=paid_social&utm_campaign=privacy&twclid=tw-123"
    )
    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert f"{ATTRIBUTION_COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "tw-123" not in set_cookie

    encoded = client.cookies.get(ATTRIBUTION_COOKIE_NAME)
    context = decode_attribution_cookie(encoded, client.app.state.settings)
    assert context is not None
    assert context.last_touch["utm_source"] == "x"
    assert context.last_touch["utm_medium"] == "paid_social"
    assert context.last_touch["twclid_fingerprint"]
    assert "twclid" not in context.last_touch


@pytest.mark.parametrize(
    ("path", "campaign"),
    [
        ("/openrouter-alternative", "exact_openrouter_migration_20260809"),
        ("/private-llm-api", "exact_private_llm_20260809"),
        ("/llm-failover", "exact_provider_failover_20260809"),
        ("/latest-model-apis", "exact_hot_models_20260809"),
    ],
)
def test_exact_intent_landings_keep_distinct_first_party_attribution(
    client: TestClient,
    path: str,
    campaign: str,
) -> None:
    response = client.get(
        f"{path}?utm_source=google&utm_medium=paid_search"
        f"&utm_campaign={campaign}&utm_content=rsa1&gclid=click-{campaign}"
    )

    assert response.status_code == 200
    context = decode_attribution_cookie(
        client.cookies.get(ATTRIBUTION_COOKIE_NAME), client.app.state.settings
    )
    assert context is not None
    assert context.last_touch["utm_source"] == "google"
    assert context.last_touch["utm_medium"] == "paid_search"
    assert context.last_touch["utm_campaign"] == campaign
    assert context.last_touch["utm_content"] == "rsa1"
    assert context.last_touch["landing_path"] == path
    assert context.last_touch["gclid_fingerprint"]
    assert "gclid" not in context.last_touch


def test_first_touch_is_preserved_and_last_touch_updates(client: TestClient) -> None:
    first = client.get("/?utm_source=x&utm_campaign=first&twclid=tw-first")
    assert first.status_code == 200
    second = client.get("/private-llm-api?utm_source=google&utm_campaign=second&gclid=g-second")
    assert second.status_code == 200

    context = decode_attribution_cookie(
        client.cookies.get(ATTRIBUTION_COOKIE_NAME), client.app.state.settings
    )
    assert context is not None
    assert context.first_touch["utm_source"] == "x"
    assert context.first_touch["utm_campaign"] == "first"
    assert context.first_touch["twclid_fingerprint"]
    assert "twclid" not in context.first_touch
    assert context.last_touch["utm_source"] == "google"
    assert context.last_touch["utm_campaign"] == "second"
    assert context.last_touch["gclid_fingerprint"]
    assert "gclid" not in context.last_touch
    assert context.last_touch["landing_path"] == "/private-llm-api"


@pytest.mark.parametrize("header", ["sec-gpc", "dnt"])
def test_privacy_signals_suppress_attribution_cookie(client: TestClient, header: str) -> None:
    response = client.get(
        "/?utm_source=google&gclid=do-not-store",
        headers={header: "1"},
    )
    assert response.status_code == 200
    assert ATTRIBUTION_COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_crawlers_do_not_receive_attribution_cookie(client: TestClient) -> None:
    response = client.get(
        "/?utm_source=google&gclid=bot-click",
        headers={"user-agent": "Googlebot/2.1"},
    )
    assert response.status_code == 200
    assert ATTRIBUTION_COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_status_host_does_not_receive_attribution_cookie(client: TestClient) -> None:
    response = client.get(
        "/",
        headers={"host": "status.trustedrouter.com"},
    )

    assert response.status_code == 200
    assert ATTRIBUTION_COOKIE_NAME not in response.headers.get("set-cookie", "")


@pytest.mark.parametrize(
    "path",
    [
        "/leaderboard",
        "/leaderboard/video",
        "/leaderboard/video.json",
        "/status",
        "/status.json",
        "/status/history?window=24h&format=json",
    ],
)
def test_public_analytics_pages_are_cookie_free_for_shared_edge_cache(
    client: TestClient,
    path: str,
) -> None:
    response = client.get(path, headers={"host": "trustedrouter.com"})

    assert response.status_code == 200
    assert ATTRIBUTION_COOKIE_NAME not in response.headers.get("set-cookie", "")
    assert "public" in response.headers.get("cache-control", "")


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("purpose", "prefetch"),
        ("sec-purpose", "prefetch;prerender"),
        ("x-purpose", "preview"),
    ],
)
def test_prefetches_do_not_receive_attribution_cookie(
    client: TestClient,
    header: str,
    value: str,
) -> None:
    response = client.get(
        "/?utm_source=x&twclid=preview-click",
        headers={header: value, "user-agent": "Mozilla/5.0 Chrome/146.0"},
    )
    assert response.status_code == 200
    assert ATTRIBUTION_COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_invalid_click_id_is_not_persisted(client: TestClient) -> None:
    response = client.get("/?utm_source=google&gclid=bad%20click%0Avalue")
    assert response.status_code == 200
    context = decode_attribution_cookie(
        client.cookies.get(ATTRIBUTION_COOKIE_NAME), client.app.state.settings
    )
    assert context is not None
    assert "gclid" not in context.last_touch
    assert "gclid_fingerprint" not in context.last_touch


@pytest.mark.parametrize("click_field", ["gbraid", "wbraid"])
def test_google_browser_click_ids_are_fingerprinted_not_retained(
    client: TestClient,
    click_field: str,
) -> None:
    click_id = f"{click_field}-private-123"
    landing = client.get(f"/?utm_source=google&utm_medium=paid_search&{click_field}={click_id}")
    assert landing.status_code == 200
    context = decode_attribution_cookie(
        client.cookies.get(ATTRIBUTION_COOKIE_NAME), client.app.state.settings
    )
    assert context is not None
    assert click_field not in context.last_touch
    fingerprint = context.last_touch[f"{click_field}_fingerprint"]
    assert len(fingerprint) == 64
    assert click_id not in client.cookies.get(ATTRIBUTION_COOKIE_NAME)

    payload = _signup(client, f"{click_field}@example.com")
    record = STORE.get_acquisition_attribution(str(payload["workspace_id"]))
    assert record is not None
    assert click_field not in record.last_touch
    assert record.last_touch[f"{click_field}_fingerprint"] == fingerprint


def test_signup_persists_attribution_and_emits_no_raw_click_id(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_click_id = "gclid-secret-987"
    _campaign_landing(client, click_id=raw_click_id)
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    payload = _signup(client)

    workspace_id = str(payload["workspace_id"])
    record = STORE.get_acquisition_attribution(workspace_id)
    assert record is not None
    assert "gclid" not in record.first_touch
    assert len(record.first_touch["gclid_fingerprint"]) == 64
    assert record.last_touch["utm_campaign"] == "router_launch"
    assert record.signup_provider == "email"
    assert record.starter_credit_microdollars == 300_000
    assert set(record.milestones) == {"signup_completed", "api_key_created"}
    assert "acquisition.signup_completed" in caplog.text
    assert "acquisition.api_key_created" in caplog.text
    assert raw_click_id not in caplog.text
    assert str(payload["key"]) not in caplog.text


def test_attribution_failure_never_blocks_signup(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _campaign_landing(client)

    def fail_write(_record: object) -> bool:
        raise RuntimeError("spanner unavailable")

    monkeypatch.setattr(STORE.target, "create_acquisition_attribution", fail_write)
    caplog.set_level(logging.WARNING, logger="trusted_router.acquisition")
    payload = _signup(client, "analytics-failure@example.com")
    assert payload["workspace_id"]
    assert "acquisition.signup_write_failed" in caplog.text
    assert "spanner unavailable" not in caplog.text


def test_signin_open_event_is_campaign_attributed_without_click_id_leak(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_click_id = "tw-private-123"
    landing = client.get(f"/?utm_source=x&utm_campaign=modal-test&twclid={raw_click_id}")
    assert landing.status_code == 200
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    response = client.post("/analytics/events", json={"event": "sign_in_opened"})
    assert response.status_code == 204
    assert "acquisition.sign_in_opened" in caplog.text
    assert raw_click_id not in caplog.text


def test_engaged_landing_has_stable_click_fingerprint_without_raw_id(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_click_id = "tw-private-engaged-123"
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")

    assert client.get(f"/?utm_source=x&twclid={raw_click_id}").status_code == 200
    first = client.post("/analytics/events", json={"event": "landing_engaged"})
    assert first.status_code == 204
    first_record = next(
        item for item in caplog.records if item.getMessage() == "acquisition.landing_engaged"
    )

    caplog.clear()
    client.cookies.clear()
    assert client.get(f"/?utm_source=x&twclid={raw_click_id}").status_code == 200
    second = client.post("/analytics/events", json={"event": "landing_engaged"})
    assert second.status_code == 204
    second_record = next(
        item for item in caplog.records if item.getMessage() == "acquisition.landing_engaged"
    )

    assert first_record.twclid_fingerprint == second_record.twclid_fingerprint
    assert first_record.anonymous_fingerprint != second_record.anonymous_fingerprint
    assert raw_click_id not in caplog.text


def test_unknown_browser_funnel_event_is_rejected(client: TestClient) -> None:
    response = client.post("/analytics/events", json={"event": "prompt_submitted"})
    assert response.status_code == 400


@pytest.mark.parametrize("event", ["first_call_started", "first_call_failed"])
def test_first_call_browser_events_are_accepted_without_payload(
    client: TestClient,
    event: str,
) -> None:
    _campaign_landing(client)
    response = client.post("/analytics/events", json={"event": event})
    assert response.status_code == 204


def test_successful_usage_milestones_are_once_only(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _campaign_landing(client)
    payload = _signup(client)
    workspace_id = str(payload["workspace_id"])
    record = STORE.get_acquisition_attribution(workspace_id)
    assert record is not None
    record.signup_at = (
        (dt.datetime.now(dt.UTC) - dt.timedelta(days=8))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    caplog.clear()
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    record_successful_api_call(workspace_id, model="test/model", provider="test-provider")
    record_successful_api_call(workspace_id, model="test/model", provider="test-provider")

    record = STORE.get_acquisition_attribution(workspace_id)
    assert record is not None
    assert "first_successful_api_call" in record.milestones
    assert "retained_api_usage_7d" in record.milestones
    messages = [item.getMessage() for item in caplog.records]
    assert messages.count("acquisition.first_successful_api_call") == 1
    assert messages.count("acquisition.retained_api_usage_7d") == 1


def test_free_credit_exhaustion_uses_typed_usage_and_is_once_only(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _campaign_landing(client)
    payload = _signup(client, "starter-exhausted@example.com")
    workspace_id = str(payload["workspace_id"])
    starter_credit = int(payload["trial_credit_microdollars"])
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")

    STORE.credit_money[workspace_id].total_usage_microdollars = starter_credit - 1
    assert record_free_credit_exhausted_safely(workspace_id) is False

    STORE.credit_money[workspace_id].total_usage_microdollars = starter_credit
    assert record_free_credit_exhausted_safely(workspace_id) is True
    assert record_free_credit_exhausted_safely(workspace_id) is False

    record = STORE.get_acquisition_attribution(workspace_id)
    assert record is not None
    assert "free_credit_exhausted" in record.milestones
    messages = [item.getMessage() for item in caplog.records]
    assert messages.count("acquisition.free_credit_exhausted") == 1
    assert str(payload["key"]) not in caplog.text


def test_checkout_and_saved_payment_method_milestones_are_once_only(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _campaign_landing(client)
    payload = _signup(client, "checkout-funnel@example.com")
    workspace_id = str(payload["workspace_id"])
    headers = {"authorization": f"Bearer {payload['key']}"}
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")

    for _ in range(2):
        checkout = client.post(
            "/v1/billing/checkout",
            headers=headers,
            json={"amount": 20},
        )
        assert checkout.status_code == 201, checkout.text
    for _ in range(2):
        setup = client.post(
            "/v1/billing/payment-methods/setup",
            headers=headers,
        )
        assert setup.status_code == 201, setup.text

    record = STORE.get_acquisition_attribution(workspace_id)
    assert record is not None
    assert "checkout_started" in record.milestones
    assert "payment_method_saved" in record.milestones
    messages = [item.getMessage() for item in caplog.records]
    assert messages.count("acquisition.checkout_started") == 1
    assert messages.count("acquisition.payment_method_saved") == 1
    assert str(payload["key"]) not in caplog.text


def test_stripe_webhook_records_saved_payment_method_for_attributed_signup(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _campaign_landing(client)
    payload = _signup(client, "saved-card-webhook@example.com")
    workspace_id = str(payload["workspace_id"])
    event = {
        "id": "evt_attributed_setup",
        "type": "setup_intent.succeeded",
        "data": {
            "object": {
                "customer": "cus_attributed",
                "payment_method": "pm_attributed",
                "metadata": {"workspace_id": workspace_id},
            }
        },
    }
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")

    first = client.post("/v1/internal/stripe/webhook", json=event)
    second = client.post("/v1/internal/stripe/webhook", json=event)

    assert first.status_code == 200
    assert second.status_code == 200
    record = STORE.get_acquisition_attribution(workspace_id)
    assert record is not None
    assert "payment_method_saved" in record.milestones
    messages = [item.getMessage() for item in caplog.records]
    assert messages.count("acquisition.payment_method_saved") == 1


def test_stripe_purchase_attribution_follows_ledger_idempotency(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _campaign_landing(client)
    payload = _signup(client)
    workspace_id = str(payload["workspace_id"])
    event = {
        "id": "evt_attributed_purchase",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 2500,
                "payment_status": "paid",
                "metadata": {"workspace_id": workspace_id},
            }
        },
    }
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    first = client.post("/v1/internal/stripe/webhook", json=event)
    second = client.post("/v1/internal/stripe/webhook", json=event)

    assert first.json()["data"]["credited"] is True
    assert second.json()["data"]["credited"] is False
    record = STORE.get_acquisition_attribution(workspace_id)
    assert record is not None
    assert record.purchase_count == 1
    assert record.purchase_microdollars == 25_000_000
    messages = [item.getMessage() for item in caplog.records]
    assert messages.count("acquisition.credit_purchase_completed") == 1


def test_public_pageviews_cover_marketing_but_not_console(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.middleware")
    assert client.get("/private-llm-api?utm_source=google").status_code == 200
    client.get("/console/api-keys", follow_redirects=False)
    records = [item for item in caplog.records if item.getMessage() == "public.page_view"]
    assert len(records) == 1
    assert records[0].path == "/private-llm-api"
    assert records[0].page_kind == "marketing"
    assert records[0].automated_request is False
    assert records[0].measurement_tier == "server_request"


def test_prefetch_pageview_is_classified_and_has_no_attribution(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_click_id = "tw-prefetch-do-not-attribute"
    caplog.set_level(logging.INFO, logger="trusted_router.middleware")
    response = client.get(
        f"/?utm_source=x&twclid={raw_click_id}",
        headers={
            "purpose": "prefetch",
            "user-agent": "Mozilla/5.0 Chrome/146.0",
        },
    )
    assert response.status_code == 200
    record = next(item for item in caplog.records if item.getMessage() == "public.page_view")
    assert record.automated_request is True
    assert not hasattr(record, "anonymous_fingerprint")
    assert not hasattr(record, "twclid_fingerprint")
    assert raw_click_id not in caplog.text


def test_record_signup_helper_uses_direct_fallback_without_cookie(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/signup",
        json={"email": "direct-route@example.com", "name": "Direct"},
    )
    assert response.status_code == 201
    workspace_id = response.json()["data"]["workspace_id"]
    record = STORE.get_acquisition_attribution(workspace_id)
    assert record is not None
    assert record.first_touch["utm_source"] == "direct"
    assert record.first_touch["landing_path"] == "/v1/signup"


def test_google_ads_export_routes_do_not_exist(client: TestClient) -> None:
    export = client.get("/v1/internal/marketing/google-ads-conversions.csv")
    backfill = client.post("/v1/internal/marketing/google-ads-conversions/backfill")

    assert export.status_code == 404
    assert backfill.status_code == 404


def test_google_reporting_configuration_is_absent() -> None:
    settings = Settings()

    assert not hasattr(settings, "google_ads_conversion_feed_password")
    assert not hasattr(settings, "google_data_manager_enabled")


def test_spanner_attribution_adapter_is_atomic_and_persistent() -> None:
    store, _, _ = make_fake_store()
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    touch = {
        "utm_source": "google",
        "utm_medium": "paid_search",
        "utm_campaign": "launch",
        "gclid_fingerprint": "d" * 64,
        "landing_path": "/private-llm-api",
        "captured_at": now,
    }
    original = AcquisitionAttribution(
        workspace_id="ws-attribution",
        anonymous_id="c" * 32,
        first_touch=touch,
        last_touch=touch,
        signup_provider="google",
        starter_credit_microdollars=300_000,
        signup_at=now,
    )

    assert store.create_acquisition_attribution(original) is True
    assert store.create_acquisition_attribution(original) is False
    stored = store.get_acquisition_attribution(original.workspace_id)
    assert stored is not None
    assert stored.first_touch["gclid_fingerprint"] == "d" * 64
    assert "gclid" not in stored.first_touch
    assert stored.starter_credit_microdollars == 300_000

    stored, claimed = store.claim_acquisition_milestones(
        original.workspace_id,
        ["first_successful_api_call", "first_successful_api_call"],
        occurred_at=now,
    )
    assert stored is not None
    assert claimed == ["first_successful_api_call"]
    _, replay_claimed = store.claim_acquisition_milestones(
        original.workspace_id,
        ["first_successful_api_call"],
        occurred_at=now,
    )
    assert replay_claimed == []

    purchased = store.record_acquisition_purchase(
        original.workspace_id,
        amount_microdollars=12_345_678,
        occurred_at=now,
    )
    assert purchased is not None
    assert purchased.purchase_count == 1
    assert purchased.purchase_microdollars == 12_345_678
