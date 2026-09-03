from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import subprocess
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
    encrypt_google_ads_click_id,
    google_ads_conversion_kind,
    google_ads_conversion_kinds_since,
    google_ads_key_wrapper_config,
)
from trusted_router.services.google_data_manager import (
    DATA_MANAGER_INGEST_URL,
    DATA_MANAGER_SCOPE,
    DATA_MANAGER_STATUS_URL,
    GoogleDataManagerClient,
    GoogleDataManagerConfig,
    GoogleDataManagerIngestResult,
    GoogleDataManagerUploadError,
    MetadataAccessTokenProvider,
    encode_google_data_manager_request,
    run_google_data_manager_once,
)
from trusted_router.storage import InMemoryStore
from trusted_router.storage_gcp_google_ads import SpannerGoogleAdsDeliveryStore
from trusted_router.storage_models import (
    AcquisitionAttribution,
    EncryptedGoogleClickEnvelope,
    GoogleAdsConversion,
)


def _now() -> str:
    return (
        dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def _later(days: int) -> str:
    return (
        (dt.datetime.now(dt.UTC).replace(microsecond=0) + dt.timedelta(days=days))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "gcp_project_id": "test-project",
        "google_data_manager_enabled": True,
        "google_data_manager_account_id": "123-456-7890",
        "google_data_manager_login_account_id": "999-888-7776",
        "google_data_manager_signup_action_id": "111",
        "google_data_manager_activated_action_id": "333",
        "google_data_manager_purchase_action_id": "222",
        "google_data_manager_batch_size": 50,
        "google_data_manager_lease_seconds": 60,
        "google_data_manager_max_attempts": 3,
        "google_data_manager_status_poll_attempts": 3,
        "google_data_manager_status_poll_seconds": 0.1,
    }
    values.update(overrides)
    return Settings(**values)


def _config() -> GoogleDataManagerConfig:
    return GoogleDataManagerConfig(
        project_id="test-project",
        account_id="1234567890",
        signup_action_id="111",
        activated_action_id="333",
        purchase_action_id="222",
        login_account_id="9998887776",
    )


def _conversion(
    *,
    settings: Settings | None = None,
    action: str = GOOGLE_ADS_PURCHASE_ACTION,
    order_id: str = "a" * 64,
    value_microdollars: int = 12_345_678,
    click_id_kind: str = "gclid",
    click_id: str = "google-click",
    attribution_id: str = "b" * 32,
) -> GoogleAdsConversion:
    settings = settings or _settings()
    return GoogleAdsConversion(
        order_id=order_id,
        conversion_action=action,
        occurred_at=_now(),
        attribution_id=attribution_id,
        click_id_kind=click_id_kind,
        encrypted_click_id=encrypt_google_ads_click_id(
            click_id,
            settings,
            attribution_id=attribution_id,
        ),
        click_expires_at=_later(90),
        value_microdollars=value_microdollars,
    )


def _attribution(
    *,
    settings: Settings | None = None,
    workspace_id: str = "ws-google",
) -> AcquisitionAttribution:
    settings = settings or _settings()
    now = _now()
    attribution_id = "c" * 32
    touch = {
        "utm_source": "google",
        "gclid_fingerprint": "d" * 64,
        "captured_at": now,
    }
    return AcquisitionAttribution(
        workspace_id=workspace_id,
        anonymous_id=attribution_id,
        first_touch=touch,
        last_touch=touch,
        signup_provider="email",
        signup_at=now,
        google_click_id_kind="gclid",
        encrypted_google_click_id=encrypt_google_ads_click_id(
            "google-click",
            settings,
            attribution_id=attribution_id,
        ),
        google_click_expires_at=_later(90),
    )


def _client(
    handler: Any,
    *,
    settings: Settings | None = None,
    sleeps: list[float] | None = None,
) -> tuple[httpx.Client, GoogleDataManagerClient]:
    settings = settings or _settings()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleDataManagerClient(
        config=_config(),
        settings=settings,
        client=http,
        token_provider=lambda: "scoped-access-token",
        sleep=(sleeps.append if sleeps is not None else lambda _seconds: None),
    )
    return http, client


def test_request_is_exact_metadata_only_json() -> None:
    settings = _settings()
    conversions = [
        _conversion(
            settings=settings,
            action=GOOGLE_ADS_SIGNUP_ACTION,
            order_id="s" * 64,
            value_microdollars=0,
        ),
        _conversion(
            settings=settings,
            action=GOOGLE_ADS_ACTIVATED_ACTION,
            order_id="a" * 64,
            value_microdollars=0,
        ),
        _conversion(settings=settings, order_id="p" * 64),
    ]

    encoded = encode_google_data_manager_request(
        conversions,
        config=_config(),
        settings=settings,
    )
    payload = json.loads(encoded, parse_float=Decimal)

    assert [item["reference"] for item in payload["destinations"]] == [
        "signup",
        "activation",
        "purchase",
    ]
    assert payload["events"][0]["conversionValue"] == 0
    assert payload["events"][1]["conversionValue"] == 0
    assert payload["events"][2]["conversionValue"] == Decimal("12.345678")
    assert payload["events"][2]["adIdentifiers"] == {"gclid": "google-click"}
    assert payload["events"][2]["eventSource"] == "WEB"
    assert payload["events"][2]["transactionId"] == "p" * 64

    lowered = encoded.decode().lower()
    for forbidden in (
        "email",
        "workspace",
        "prompt",
        "output",
        "api_key",
        "request_body",
        "user_id",
        "provider",
        "model",
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
    settings = _settings()
    payload = json.loads(
        encode_google_data_manager_request(
            [_conversion(settings=settings, value_microdollars=value_microdollars)],
            config=_config(),
            settings=settings,
        ),
        parse_float=Decimal,
    )
    assert payload["events"][0]["conversionValue"] == expected


@pytest.mark.parametrize("click_id_kind", ["gclid", "gbraid", "wbraid"])
def test_request_supports_each_google_click_identifier(click_id_kind: str) -> None:
    settings = _settings()
    payload = json.loads(
        encode_google_data_manager_request(
            [
                _conversion(
                    settings=settings,
                    click_id_kind=click_id_kind,
                    click_id=f"{click_id_kind}-value",
                )
            ],
            config=_config(),
            settings=settings,
        )
    )
    assert payload["events"][0]["adIdentifiers"] == {click_id_kind: f"{click_id_kind}-value"}


def test_durable_rows_never_contain_plaintext_click_or_identity_data() -> None:
    click_id = "private-google-click-123"
    conversion = _conversion(click_id=click_id)
    durable_json = json.dumps(dataclasses.asdict(conversion), sort_keys=True)

    assert click_id not in durable_json
    for forbidden in ("email", "workspace_id", "prompt", "output", "api_key"):
        assert forbidden not in durable_json.lower()


def test_google_click_kms_key_is_separate_from_byok_key() -> None:
    settings = _settings(
        byok_kms_key_name="projects/p/locations/l/keyRings/r/cryptoKeys/byok",
        google_data_manager_kms_key_name=(
            "projects/p/locations/l/keyRings/r/cryptoKeys/google-clicks"
        ),
    )
    wrapper = google_ads_key_wrapper_config(settings)

    assert wrapper.byok_kms_key_name == settings.google_data_manager_kms_key_name
    assert wrapper.byok_kms_key_name != settings.byok_kms_key_name


def test_config_normalizes_google_ads_ids() -> None:
    assert GoogleDataManagerConfig.from_settings(_settings()) == _config()


def test_config_fails_closed_when_enabled_without_destination_ids() -> None:
    with pytest.raises(ValueError, match="SIGNUP_ACTION_ID"):
        Settings(
            environment="test",
            google_data_manager_enabled=True,
            google_data_manager_account_id="123",
            google_data_manager_activated_action_id="333",
            google_data_manager_purchase_action_id="222",
        )


@pytest.mark.parametrize("environment", ("worker", "canary", "production"))
def test_deployed_config_forbids_outbound_sharing_outside_local_test(
    environment: str,
) -> None:
    with pytest.raises(ValueError, match="no-sharing policy"):
        _settings(environment=environment, service_surface="control")


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


def test_client_waits_for_asynchronous_success_before_accepting() -> None:
    requests: list[httpx.Request] = []
    statuses = iter(["PROCESSING", "SUCCESS"])
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == DATA_MANAGER_INGEST_URL:
            return httpx.Response(200, json={"requestId": "request-123"})
        assert str(request.url).startswith(DATA_MANAGER_STATUS_URL)
        return httpx.Response(
            200,
            json={"requestStatusPerDestination": [{"requestStatus": next(statuses)}]},
        )

    http, client = _client(handler, sleeps=sleeps)
    with http:
        result = client.ingest([_conversion()])

    assert result == GoogleDataManagerIngestResult("request-123", 0)
    assert [request.method for request in requests] == ["POST", "GET", "GET"]
    assert sleeps == [0.1]
    assert requests[0].headers["authorization"] == "Bearer scoped-access-token"
    assert requests[0].headers["x-goog-user-project"] == "test-project"


@pytest.mark.parametrize("final_status", ["FAILED", "PARTIAL_SUCCESS"])
def test_client_rejects_failed_asynchronous_processing(final_status: str) -> None:
    private_click = "must-never-enter-error"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == DATA_MANAGER_INGEST_URL:
            return httpx.Response(200, json={"requestId": "request-failed"})
        return httpx.Response(
            200,
            json={
                "requestStatusPerDestination": [
                    {
                        "requestStatus": final_status,
                        "errorInfo": {
                            "errorCounts": [{"reason": "INVALID_CONVERSION_ACTION", "count": "1"}]
                        },
                    }
                ]
            },
        )

    http, client = _client(handler)
    with http, pytest.raises(GoogleDataManagerUploadError) as raised:
        client.ingest([_conversion(click_id=private_click)])

    assert raised.value.retryable is True
    assert raised.value.request_id == "request-failed"
    assert "INVALID_CONVERSION_ACTION" in str(raised.value)
    assert private_click not in str(raised.value)


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(400, False), (403, True), (408, True), (429, True), (500, True), (503, True)],
)
def test_http_failure_classification_does_not_echo_response_body(
    status_code: int,
    retryable: bool,
) -> None:
    private_response_body = "private-click-id-that-must-not-reach-errors"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=private_response_body)

    http, client = _client(handler)
    with http, pytest.raises(GoogleDataManagerUploadError) as raised:
        client.ingest([_conversion()])

    assert raised.value.retryable is retryable
    assert private_response_body not in str(raised.value)


class _SuccessfulClient:
    def ingest(
        self,
        conversions: list[GoogleAdsConversion],
    ) -> GoogleDataManagerIngestResult:
        assert {item.conversion_action for item in conversions} == {
            GOOGLE_ADS_SIGNUP_ACTION,
            GOOGLE_ADS_ACTIVATED_ACTION,
            GOOGLE_ADS_PURCHASE_ACTION,
        }
        return GoogleDataManagerIngestResult("accepted-request", 0)


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


def test_worker_submits_signup_activation_and_settled_purchase_once() -> None:
    store = InMemoryStore()
    record = _attribution()
    assert store.create_acquisition_attribution(record)
    store.claim_acquisition_milestones(
        record.workspace_id,
        ["first_successful_api_call", "first_successful_api_call"],
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

    assert result.claimed == 3
    assert result.submitted == 3
    rows = list(store.acquisition_store.google_ads_conversions.values())
    assert len(rows) == 3
    assert all(row.delivery_status == "submitted" for row in rows)
    assert all(row.encrypted_click_id is None for row in rows)
    assert {row.google_request_id for row in rows} == {"accepted-request"}
    assert store.claim_google_ads_deliveries(limit=10, lease_seconds=60) == []


def test_retryable_failure_requeues_and_permanent_failure_dead_letters() -> None:
    retry_store = InMemoryStore()
    assert retry_store.create_acquisition_attribution(_attribution())
    retry_result = run_google_data_manager_once(
        store=retry_store,
        settings=_settings(),
        client=_FailingClient(retryable=True),  # type: ignore[arg-type]
    )
    retry_conversion = next(iter(retry_store.acquisition_store.google_ads_conversions.values()))
    assert retry_result.failed == 1
    assert retry_conversion.delivery_status == "pending"
    assert retry_conversion.delivery_attempts == 1
    assert retry_conversion.lease_owner is None

    dead_store = InMemoryStore()
    assert dead_store.create_acquisition_attribution(_attribution(workspace_id="ws-dead"))
    dead_result = run_google_data_manager_once(
        store=dead_store,
        settings=_settings(),
        client=_FailingClient(retryable=False),  # type: ignore[arg-type]
    )
    dead_conversion = next(iter(dead_store.acquisition_store.google_ads_conversions.values()))
    assert dead_result.failed == 1
    assert dead_conversion.delivery_status == "dead"
    assert dead_conversion.delivery_attempts == 1
    assert dead_conversion.encrypted_click_id is None


def test_expired_click_is_purged_and_cannot_create_later_conversions() -> None:
    store = InMemoryStore()
    record = _attribution()
    record.google_click_expires_at = _later(-1)
    assert store.create_acquisition_attribution(record)

    assert store.purge_expired_google_ads_click_ids(before=_now(), limit=10) == 1
    stored = store.get_acquisition_attribution(record.workspace_id)
    assert stored is not None
    assert stored.google_click_id_kind is None
    assert stored.encrypted_google_click_id is None
    assert stored.google_click_expires_at is None

    store.claim_acquisition_milestones(
        record.workspace_id,
        ["first_successful_api_call"],
        occurred_at=_now(),
    )
    rows = list(store.acquisition_store.google_ads_conversions.values())
    assert rows == []


def test_expired_pending_delivery_is_dead_lettered_without_decryption() -> None:
    store = InMemoryStore()
    conversion = _conversion()
    conversion.click_expires_at = _later(-1)
    store.acquisition_store.google_ads_conversions[conversion.order_id] = conversion

    assert store.claim_google_ads_deliveries(limit=10, lease_seconds=60) == []
    assert conversion.delivery_status == "dead"
    assert conversion.encrypted_click_id is None


def test_delivery_lease_prevents_double_claim() -> None:
    store = InMemoryStore()
    assert store.create_acquisition_attribution(_attribution())
    first = store.claim_google_ads_deliveries(limit=10, lease_seconds=60)
    second = store.claim_google_ads_deliveries(limit=10, lease_seconds=60)
    assert len(first) == 1
    assert second == []


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


def test_spanner_worker_rehydrates_encrypted_click_envelope() -> None:
    conversion = _conversion()
    decoded = SpannerGoogleAdsDeliveryStore._decode_entity(
        json.dumps(dataclasses.asdict(conversion)),
        GoogleAdsConversion,
    )

    assert isinstance(decoded.encrypted_click_id, EncryptedGoogleClickEnvelope)
    assert decoded.encrypted_click_id == conversion.encrypted_click_id


def test_conversion_storage_is_versioned_away_from_retired_plaintext_rows() -> None:
    occurred_at = "2026-08-18T12:34:56Z"
    assert google_ads_conversion_kind(occurred_at) == "google_ads_conversion_v2_202608"
    assert google_ads_conversion_kinds_since(
        dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
        now=dt.datetime(2026, 8, 18, tzinfo=dt.UTC),
    ) == [
        "google_ads_conversion_v2_202607",
        "google_ads_conversion_v2_202608",
    ]


def test_deploy_enforces_google_data_manager_is_disabled() -> None:
    root = Path(__file__).parents[1]
    deploy_script = (root / "scripts/deploy/google_data_manager.sh").read_text()
    workflow = (root / ".github/workflows/deploy.yml").read_text()

    assert "scheduler jobs pause" in deploy_script
    assert "TR_GOOGLE_DATA_MANAGER_ENABLED=false" in deploy_script
    assert "scheduler is not paused" in deploy_script
    assert "job is not disabled" in deploy_script
    assert "scheduler jobs create" not in deploy_script
    assert "scheduler jobs update http" not in deploy_script
    assert "TR_GOOGLE_DATA_MANAGER_ENABLED=true" not in deploy_script
    assert "Enforce Google Data Manager sharing disabled" in workflow
    assert "steps.optional.outputs.deploy_google_data_manager" not in workflow


def _run_disable_script(tmp_path: Path, *, reported_enabled: str) -> subprocess.CompletedProcess[str]:
    argv_log = tmp_path / "gcloud-argv.log"
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$HARNESS_ARGV_LOG"
if [[ " $* " == *" projects describe "* ]]; then
  printf '123456789\\n'
elif [[ " $* " == *" scheduler jobs describe "* ]]; then
  printf 'PAUSED\\n'
elif [[ " $* " == *" run jobs describe "* ]] && [[ " $* " == *" --format=json "* ]]; then
  printf '{"spec":{"template":{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"TR_GOOGLE_DATA_MANAGER_ENABLED","value":"%s"}]}]}}}}}}\\n' "$HARNESS_JOB_ENABLED"
fi
""",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "HARNESS_ARGV_LOG": str(argv_log),
        "HARNESS_JOB_ENABLED": reported_enabled,
    }
    return subprocess.run(  # noqa: S603 - fixed repository script under a stub CLI PATH
        [str(Path(__file__).parents[1] / "scripts/deploy/google_data_manager.sh")],
        cwd=Path(__file__).parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_disable_deploy_pauses_scheduler_before_disabling_job(tmp_path: Path) -> None:
    result = _run_disable_script(tmp_path, reported_enabled="false")

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "gcloud-argv.log").read_text(encoding="utf-8").splitlines()
    pause_index = next(i for i, call in enumerate(calls) if "scheduler jobs pause" in call)
    update_index = next(i for i, call in enumerate(calls) if "run jobs update" in call)
    assert pause_index < update_index
    assert "--update-env-vars=TR_GOOGLE_DATA_MANAGER_ENABLED=false" in calls[update_index]


def test_disable_deploy_fails_if_job_still_reports_enabled(tmp_path: Path) -> None:
    result = _run_disable_script(tmp_path, reported_enabled="true")

    assert result.returncode != 0
    assert "Google Data Manager job is not disabled" in result.stderr


def test_control_plane_can_encrypt_click_ids_but_worker_alone_can_decrypt() -> None:
    root = Path(__file__).parents[1]
    shared = (root / "scripts/deploy/_lib.sh").read_text()
    infra = (root / "scripts/deploy/infra.sh").read_text()
    click_key_section = infra.split(
        "# Google Ads click identifiers use a separate envelope key.",
        1,
    )[1].split("# Runtime-SA project-level role grants.", 1)[0]
    worker_section = infra.split(
        "# Metadata-only Google Ads conversion worker",
        1,
    )[1]

    assert "CONTROL_RUN_SERVICE_ACCOUNT=" in shared
    assert "serviceAccount:${CONTROL_RUN_SERVICE_ACCOUNT}" in click_key_section
    assert "roles/cloudkms.cryptoKeyEncrypter" in click_key_section
    assert "roles/cloudkms.cryptoKeyDecrypter" not in click_key_section
    assert "serviceAccount:${GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT}" in worker_section
    assert "roles/cloudkms.cryptoKeyDecrypter" in worker_section
    assert "serviceAccount:${CONTROL_RUN_SERVICE_ACCOUNT}" not in worker_section


def test_worker_uses_spanner_only_and_raw_https() -> None:
    root = Path(__file__).parents[1]
    cli_source = (root / "src/trusted_router/google_data_manager_cli.py").read_text()
    store_source = (root / "src/trusted_router/storage_gcp_google_ads.py").read_text()
    service_source = (root / "src/trusted_router/services/google_data_manager.py").read_text()

    assert "create_google_ads_delivery_store" in cli_source
    assert "create_store" not in cli_source
    assert "google.cloud import spanner" in store_source
    assert "google.cloud import bigtable" not in store_source
    assert "import google" not in service_source
    assert "from google" not in service_source
    assert DATA_MANAGER_INGEST_URL in service_source
