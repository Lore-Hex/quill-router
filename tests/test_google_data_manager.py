from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.google_ads_conversions import (
    GOOGLE_ADS_ACTIVATED_ACTION,
    GOOGLE_ADS_PURCHASE_ACTION,
    GOOGLE_ADS_SIGNUP_ACTION,
)
from trusted_router.services.google_data_manager import (
    DATA_MANAGER_INGEST_URL,
    DATA_MANAGER_SCOPE,
    GoogleDataManagerClient,
    GoogleDataManagerConfig,
    GoogleDataManagerIngestResult,
    GoogleDataManagerUploadError,
    MetadataAccessTokenProvider,
    encode_google_data_manager_request,
    run_google_data_manager_once,
)
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import AcquisitionAttribution, GoogleAdsConversion


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _config() -> GoogleDataManagerConfig:
    return GoogleDataManagerConfig(
        account_id="1234567890",
        signup_action_id="111",
        purchase_action_id="222",
        login_account_id="9998887776",
    )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "google_data_manager_enabled": True,
        "google_data_manager_account_id": "123-456-7890",
        "google_data_manager_signup_action_id": "111",
        "google_data_manager_purchase_action_id": "222",
        "google_data_manager_batch_size": 50,
        "google_data_manager_lease_seconds": 60,
        "google_data_manager_max_attempts": 3,
    }
    values.update(overrides)
    return Settings(**values)


def _conversion(
    *,
    action: str = GOOGLE_ADS_PURCHASE_ACTION,
    order_id: str = "a" * 64,
    value_microdollars: int = 12_345_678,
    gclid: str | None = "google-click",
    gbraid: str | None = None,
    wbraid: str | None = None,
) -> GoogleAdsConversion:
    now = _now()
    return GoogleAdsConversion(
        order_id=order_id,
        conversion_action=action,
        occurred_at=now,
        gclid=gclid,
        gbraid=gbraid,
        wbraid=wbraid,
        value_microdollars=value_microdollars,
        delivery_status="pending",
        next_attempt_at=now,
    )


def _attribution(*, workspace_id: str = "ws-google") -> AcquisitionAttribution:
    now = _now()
    touch = {
        "utm_source": "google",
        "gclid": "google-click",
        "captured_at": now,
    }
    return AcquisitionAttribution(
        workspace_id=workspace_id,
        anonymous_id="anonymous-random-id",
        first_touch=touch,
        last_touch=touch,
        signup_provider="email",
        signup_at=now,
    )


def test_request_is_exact_metadata_only_json() -> None:
    signup = _conversion(
        action=GOOGLE_ADS_SIGNUP_ACTION,
        order_id="s" * 64,
        value_microdollars=0,
    )
    purchase = _conversion(
        order_id="p" * 64,
        value_microdollars=12_345_678,
    )

    encoded = encode_google_data_manager_request(
        [signup, purchase],
        config=_config(),
    )
    payload = json.loads(encoded, parse_float=Decimal)

    assert payload["destinations"] == [
        {
            "loginAccount": {
                "accountId": "9998887776",
                "accountType": "GOOGLE_ADS",
            },
            "operatingAccount": {
                "accountId": "1234567890",
                "accountType": "GOOGLE_ADS",
            },
            "productDestinationId": "111",
            "reference": "signup",
        },
        {
            "loginAccount": {
                "accountId": "9998887776",
                "accountType": "GOOGLE_ADS",
            },
            "operatingAccount": {
                "accountId": "1234567890",
                "accountType": "GOOGLE_ADS",
            },
            "productDestinationId": "222",
            "reference": "purchase",
        },
    ]
    assert payload["events"][0]["conversionValue"] == 0
    assert payload["events"][1]["conversionValue"] == Decimal("12.345678")
    assert payload["events"][1]["adIdentifiers"] == {"gclid": "google-click"}
    assert payload["events"][1]["eventSource"] == "WEB"
    assert payload["events"][1]["transactionId"] == "p" * 64

    lowered = encoded.decode().lower()
    for forbidden in (
        "email",
        "workspace",
        "prompt",
        "output",
        "api_key",
        "request_body",
        "user_id",
    ):
        assert forbidden not in lowered


@pytest.mark.parametrize(
    ("value_microdollars", "expected"),
    [
        (0, 0),
        (1, Decimal("0.000001")),
        (10, Decimal("0.00001")),
        (1_000_000, 1),
        (1_000_001, Decimal("1.000001")),
        (999_999_999_999, Decimal("999999.999999")),
    ],
)
def test_request_never_rounds_microdollars(
    value_microdollars: int,
    expected: int | Decimal,
) -> None:
    payload = json.loads(
        encode_google_data_manager_request(
            [_conversion(value_microdollars=value_microdollars)],
            config=_config(),
        ),
        parse_float=Decimal,
    )
    assert payload["events"][0]["conversionValue"] == expected


def test_request_sends_exactly_one_click_identifier() -> None:
    payload = json.loads(
        encode_google_data_manager_request(
            [
                _conversion(
                    gclid="gclid",
                    gbraid="gbraid",
                    wbraid="wbraid",
                )
            ],
            config=_config(),
        )
    )
    assert payload["events"][0]["adIdentifiers"] == {"gclid": "gclid"}


def test_request_rejects_non_direct_events_and_missing_click_ids() -> None:
    with pytest.raises(ValueError, match="not eligible"):
        encode_google_data_manager_request(
            [_conversion(action=GOOGLE_ADS_ACTIVATED_ACTION)],
            config=_config(),
        )
    with pytest.raises(ValueError, match="no click identifier"):
        encode_google_data_manager_request(
            [_conversion(gclid=None)],
            config=_config(),
        )


def test_request_enforces_google_batch_limit() -> None:
    conversion = _conversion()
    with pytest.raises(ValueError, match="at most 2000"):
        encode_google_data_manager_request(
            [conversion] * 2_001,
            config=_config(),
        )


def test_config_normalizes_google_ads_ids() -> None:
    settings = _settings(
        google_data_manager_account_id="123-456-7890",
        google_data_manager_login_account_id="999-888-7776",
    )
    assert GoogleDataManagerConfig.from_settings(settings) == _config()


def test_config_fails_closed_when_enabled_without_destination_ids() -> None:
    with pytest.raises(ValueError, match="SIGNUP_ACTION_ID"):
        Settings(
            environment="test",
            google_data_manager_enabled=True,
            google_data_manager_account_id="123",
            google_data_manager_purchase_action_id="222",
        )


def test_raw_http_client_posts_to_data_manager_without_google_sdk() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "requestId": "request-123",
                "fieldWarnings": [{"fieldPath": "events[0]"}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = GoogleDataManagerClient(
            config=_config(),
            client=http,
            token_provider=lambda: "scoped-access-token",
        ).ingest([_conversion()])

    assert result == GoogleDataManagerIngestResult(
        request_id="request-123",
        warning_count=1,
    )
    assert len(requests) == 1
    assert str(requests[0].url) == DATA_MANAGER_INGEST_URL
    assert requests[0].headers["authorization"] == "Bearer scoped-access-token"
    assert requests[0].headers["content-type"] == "application/json"


def test_metadata_identity_token_is_scoped_and_cached() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "metadata-token", "expires_in": 3600},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = MetadataAccessTokenProvider(http)
        assert provider() == "metadata-token"
        assert provider() == "metadata-token"

    assert len(requests) == 1
    assert requests[0].headers["metadata-flavor"] == "Google"
    assert requests[0].url.params["scopes"] == DATA_MANAGER_SCOPE
    assert requests[0].url.params["enforce_scopes"] == "true"


def test_deploy_worker_does_not_receive_application_secrets() -> None:
    deploy_script = (
        Path(__file__).parents[1] / "scripts/deploy/google_data_manager.sh"
    ).read_text()

    assert "TR_ENVIRONMENT=worker" in deploy_script
    assert "tr-google-data-manager@" in deploy_script
    assert "TR_GOOGLE_DATA_MANAGER_MAX_ATTEMPTS=20" in deploy_script
    assert "TR_STRIPE_SECRET_KEY=" not in deploy_script
    assert "TR_STRIPE_WEBHOOK_SECRET=" not in deploy_script
    assert "TR_INTERNAL_GATEWAY_TOKEN=" not in deploy_script
    assert "TR_SENTRY_DSN=" not in deploy_script
    assert "TR_BIGTABLE_" not in deploy_script
    assert "TR_BYOK_" not in deploy_script
    assert "--update-secrets" not in deploy_script


def test_uploader_uses_raw_https_without_google_client_library() -> None:
    source = (
        Path(__file__).parents[1]
        / "src/trusted_router/services/google_data_manager.py"
    ).read_text()

    assert "import google" not in source
    assert "from google" not in source
    assert DATA_MANAGER_INGEST_URL in source


def test_worker_uses_spanner_only_outbox_store() -> None:
    root = Path(__file__).parents[1]
    cli_source = (
        root / "src/trusted_router/google_data_manager_cli.py"
    ).read_text()
    store_source = (
        root / "src/trusted_router/storage_gcp_google_ads.py"
    ).read_text()

    assert "create_google_ads_delivery_store" in cli_source
    assert "create_store" not in cli_source
    assert "google.cloud import spanner" in store_source
    assert "google.cloud import bigtable" not in store_source
    assert "bigtable.Client" not in store_source


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (400, False),
        (403, True),
        (408, True),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_http_failure_classification_does_not_echo_response_body(
    status_code: int,
    retryable: bool,
) -> None:
    private_response_body = "private-click-id-that-must-not-reach-errors"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=private_response_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = GoogleDataManagerClient(
            config=_config(),
            client=http,
            token_provider=lambda: "token",
        )
        with pytest.raises(GoogleDataManagerUploadError) as raised:
            client.ingest([_conversion()])
    assert raised.value.retryable is retryable
    assert private_response_body not in str(raised.value)


def test_google_error_codes_are_logged_without_raw_message() -> None:
    private_message = "click-id-secret-must-not-be-logged"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "status": "PERMISSION_DENIED",
                    "message": private_message,
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "SERVICE_DISABLED",
                        },
                        {
                            "@type": "type.googleapis.com/google.rpc.RequestInfo",
                            "requestId": "google-request-123",
                        },
                    ],
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = GoogleDataManagerClient(
            config=_config(),
            client=http,
            token_provider=lambda: "token",
        )
        with pytest.raises(GoogleDataManagerUploadError) as raised:
            client.ingest([_conversion()])

    message = str(raised.value)
    assert "status=PERMISSION_DENIED" in message
    assert "reason=SERVICE_DISABLED" in message
    assert "request_id=google-request-123" in message
    assert private_message not in message
    assert raised.value.retryable is True


def test_success_without_request_id_is_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = GoogleDataManagerClient(
            config=_config(),
            client=http,
            token_provider=lambda: "token",
        )
        with pytest.raises(GoogleDataManagerUploadError) as raised:
            client.ingest([_conversion()])
    assert raised.value.retryable is True


class _SuccessfulClient:
    def ingest(
        self,
        conversions: list[GoogleAdsConversion],
    ) -> GoogleDataManagerIngestResult:
        assert {
            item.conversion_action for item in conversions
        } == {
            GOOGLE_ADS_SIGNUP_ACTION,
            GOOGLE_ADS_PURCHASE_ACTION,
        }
        return GoogleDataManagerIngestResult(
            request_id="accepted-request",
            warning_count=0,
        )


class _FailingClient:
    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable

    def ingest(
        self,
        conversions: list[GoogleAdsConversion],
    ) -> GoogleDataManagerIngestResult:
        raise GoogleDataManagerUploadError(
            "sanitized failure",
            retryable=self.retryable,
        )


def test_worker_submits_signup_and_settled_purchase_only() -> None:
    store = InMemoryStore()
    record = _attribution()
    assert store.create_acquisition_attribution(record)
    store.claim_acquisition_milestones(
        record.workspace_id,
        ["first_successful_api_call"],
        occurred_at=_now(),
    )
    store.record_acquisition_purchase(
        record.workspace_id,
        amount_microdollars=25_000_001,
        occurred_at=_now(),
    )

    result = run_google_data_manager_once(
        store=store,
        settings=_settings(),
        client=_SuccessfulClient(),  # type: ignore[arg-type]
    )

    assert result.claimed == 2
    assert result.submitted == 2
    conversions = store.list_google_ads_conversions(
        since="2000-01-01T00:00:00Z",
        limit=10,
    )
    by_action = {item.conversion_action: item for item in conversions}
    assert by_action[GOOGLE_ADS_SIGNUP_ACTION].delivery_status == "submitted"
    assert by_action[GOOGLE_ADS_PURCHASE_ACTION].delivery_status == "submitted"
    assert by_action[GOOGLE_ADS_ACTIVATED_ACTION].delivery_status == "not_scheduled"
    assert {
        by_action[GOOGLE_ADS_SIGNUP_ACTION].google_request_id,
        by_action[GOOGLE_ADS_PURCHASE_ACTION].google_request_id,
    } == {"accepted-request"}
    assert store.claim_google_ads_deliveries(limit=10, lease_seconds=60) == []


def test_retryable_failure_requeues_and_permanent_failure_dead_letters() -> None:
    retry_store = InMemoryStore()
    assert retry_store.create_acquisition_attribution(_attribution())
    retry_result = run_google_data_manager_once(
        store=retry_store,
        settings=_settings(),
        client=_FailingClient(retryable=True),  # type: ignore[arg-type]
    )
    retry_conversion = retry_store.list_google_ads_conversions(
        since="2000-01-01T00:00:00Z",
        limit=10,
    )[0]
    assert retry_result.failed == 1
    assert retry_conversion.delivery_status == "pending"
    assert retry_conversion.delivery_attempts == 1
    assert retry_conversion.lease_owner is None

    dead_store = InMemoryStore()
    assert dead_store.create_acquisition_attribution(
        _attribution(workspace_id="ws-dead")
    )
    dead_result = run_google_data_manager_once(
        store=dead_store,
        settings=_settings(),
        client=_FailingClient(retryable=False),  # type: ignore[arg-type]
    )
    dead_conversion = dead_store.list_google_ads_conversions(
        since="2000-01-01T00:00:00Z",
        limit=10,
    )[0]
    assert dead_result.failed == 1
    assert dead_conversion.delivery_status == "dead"
    assert dead_conversion.delivery_attempts == 1


def test_delivery_lease_prevents_double_claim() -> None:
    store = InMemoryStore()
    assert store.create_acquisition_attribution(_attribution())
    first = store.claim_google_ads_deliveries(limit=10, lease_seconds=60)
    second = store.claim_google_ads_deliveries(limit=10, lease_seconds=60)
    assert len(first) == 1
    assert second == []


def test_legacy_direct_conversion_is_repaired_once() -> None:
    store = InMemoryStore()
    conversion = _conversion()
    conversion.delivery_status = "not_scheduled"
    store.acquisition_store.google_ads_conversions[conversion.order_id] = conversion

    assert store.repair_google_ads_delivery_queue(
        since="2000-01-01T00:00:00Z",
        limit=10,
    ) == 1
    assert store.repair_google_ads_delivery_queue(
        since="2000-01-01T00:00:00Z",
        limit=10,
    ) == 0
    assert len(
        store.claim_google_ads_deliveries(limit=10, lease_seconds=60)
    ) == 1


def test_first_production_403_dead_letter_is_repaired_once() -> None:
    store = InMemoryStore()
    conversion = _conversion()
    conversion.delivery_status = "dead"
    conversion.delivery_attempts = 1
    conversion.last_error = "Google Data Manager returned HTTP 403"
    store.acquisition_store.google_ads_conversions[conversion.order_id] = conversion

    assert store.repair_google_ads_delivery_queue(
        since="2000-01-01T00:00:00Z",
        limit=10,
    ) == 1
    assert conversion.delivery_status == "pending"
    assert conversion.last_error is None
    assert store.repair_google_ads_delivery_queue(
        since="2000-01-01T00:00:00Z",
        limit=10,
    ) == 0


def test_spanner_repairs_first_production_403_dead_letter() -> None:
    store, _database, _table = make_fake_store()
    assert store.create_acquisition_attribution(
        _attribution(workspace_id="ws-spanner-repair")
    )
    claimed = store.claim_google_ads_deliveries(limit=10, lease_seconds=60)
    assert len(claimed) == 1
    conversion = claimed[0]
    assert conversion.lease_owner

    dead = store.mark_google_ads_delivery_failed(
        order_id=conversion.order_id,
        occurred_at=conversion.occurred_at,
        lease_owner=conversion.lease_owner,
        error="Google Data Manager returned HTTP 403",
        retryable=False,
        max_attempts=3,
    )
    assert dead is not None
    assert dead.delivery_status == "dead"

    assert store.repair_google_ads_delivery_queue(
        since="2000-01-01T00:00:00Z",
        limit=10,
    ) == 1
    retried = store.claim_google_ads_deliveries(limit=10, lease_seconds=60)
    assert len(retried) == 1
    assert retried[0].last_error is None


def test_spanner_conversion_and_due_pointer_commit_together() -> None:
    store, _database, _table = make_fake_store()
    record = _attribution(workspace_id="ws-spanner-google")
    assert store.create_acquisition_attribution(record)

    claimed = store.claim_google_ads_deliveries(limit=10, lease_seconds=60)
    assert len(claimed) == 1
    conversion = claimed[0]
    assert conversion.lease_owner
    marked = store.mark_google_ads_delivery_submitted(
        order_id=conversion.order_id,
        occurred_at=conversion.occurred_at,
        lease_owner=conversion.lease_owner,
        request_id="spanner-request",
    )
    assert marked is not None
    assert marked.delivery_status == "submitted"
    assert store.claim_google_ads_deliveries(limit=10, lease_seconds=60) == []
