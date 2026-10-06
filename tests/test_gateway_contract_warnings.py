from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import sentry_sdk
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.schemas import GatewayContractRejection
from trusted_router.sentry_config import before_send, reset_sentry_floodgate_for_tests
from trusted_router.services.gateway_contract_warnings import (
    PARAMETER_CATEGORIES,
    report_gateway_contract_rejection,
)
from trusted_router.storage import STORE

REQUEST_ID = "rlog_" + "a" * 32


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    reset_sentry_floodgate_for_tests()

    def capture(event: dict[str, Any]) -> None:
        scrubbed = before_send(event)
        if scrubbed is not None:
            events.append(scrubbed)

    monkeypatch.setattr(sentry_sdk, "capture_event", capture)
    yield events
    reset_sentry_floodgate_for_tests()


def _request(key: dict[str, Any], *, parameter: str = "store", status: int = 400) -> dict[str, Any]:
    return {
        "api_key_hash": key["hash"],
        "route_type": "/v1/chat/completions",
        "contract_rejection": {
            "status": status,
            "parameter": parameter,
            "request_id": REQUEST_ID,
        },
    }


def _key(client: TestClient) -> dict[str, Any]:
    created = client.post(
        "/v1/keys",
        headers={"x-trustedrouter-user": "alice@example.com"},
        json={"name": "private-key-label"},
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]


@pytest.mark.parametrize("status", [400, 422, 501])
def test_rejection_warns_with_verified_identity_without_billing(
    client: TestClient,
    warnings: list[dict[str, Any]],
    status: int,
) -> None:
    key = _key(client)
    response = client.post("/v1/internal/gateway/validate", json=_request(key, status=status))
    assert response.status_code == 200, response.text
    [event] = warnings
    assert event["level"] == "warning"
    assert event["fingerprint"] == [
        "gateway-contract-rejection",
        "/v1/chat/completions",
        str(status),
        "store",
    ]
    assert event["tags"]["workspace_id"] == key["workspace_id"]
    assert event["tags"]["credential_id"] == key["hash"]
    assert event["contexts"]["gateway_rejection"]["request_id"] == REQUEST_ID
    assert "private-key-label" not in json.dumps(event)
    assert "alice@example.com" not in json.dumps(event)
    assert STORE.credit_money[key["workspace_id"]].reserved_microdollars == 0
    assert STORE.credit_money[key["workspace_id"]].total_usage_microdollars == 0
    assert not STORE.generation_store.generations


def test_plain_validation_is_not_an_error(
    client: TestClient, warnings: list[dict[str, Any]]
) -> None:
    key = _key(client)
    response = client.post("/v1/internal/gateway/validate", json={"api_key_hash": key["hash"]})
    assert response.status_code == 200
    assert warnings == []


@pytest.mark.parametrize(
    "parameter",
    [
        "plugins", "truncation", "prompt_cache_key", "prompt_cache_options", "service_tier",
        "usage.include", "stream_options.include_usage", "provider.quantizations",
        "provider.max_price.image", "plugins.web-fetch",
        "file", "audio", "video", "content", "input_image", "input_audio", "input_file",
    ],
)
def test_public_parameter_diagnosis_survives_scrubbing(
    client: TestClient, warnings: list[dict[str, Any]], parameter: str
) -> None:
    key = _key(client)
    response = client.post(
        "/v1/internal/gateway/validate", json=_request(key, parameter=parameter)
    )
    assert response.status_code == 200
    [event] = warnings
    assert event["tags"]["parameter"] == parameter
    assert event["fingerprint"][-1] == parameter
    assert event["tags"]["workspace_id"] == key["workspace_id"]
    assert STORE.credit_money[key["workspace_id"]].reserved_microdollars == 0


@pytest.mark.parametrize(
    "path", ["future_option", "usage.future_option", "input[12].future_option", "a" * 100, "usage." + "a" * 94],
)
def test_unknown_rejected_field_path_is_retained_without_value_or_new_fingerprint(
    client: TestClient, warnings: list[dict[str, Any]], path: str
) -> None:
    payload = _request(_key(client), parameter="other")
    payload["contract_rejection"]["parameter_path"] = path
    response = client.post("/v1/internal/gateway/validate", json=payload)
    assert response.status_code == 200
    [event] = warnings
    assert event["contexts"]["gateway_rejection"]["parameter_path"] == path
    assert event["fingerprint"][-1] == "other"


@pytest.mark.parametrize(
    "path",
    ["alice@example.com", "sk-tr-v1-secret", "sk_private_secret", "private customer text", "usage=secret", "x\nsecret"],
)
def test_unsafe_rejected_field_path_is_dropped_not_forwarded(
    warnings: list[dict[str, Any]], path: str
) -> None:
    report_gateway_contract_rejection(
        GatewayContractRejection(status=400, parameter="other", request_id=REQUEST_ID, parameter_path=path),
        route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
    )
    assert "parameter_path" not in warnings[0]["contexts"]["gateway_rejection"]


@pytest.mark.parametrize("path", ["a" * 101, "usage." + "a" * 95, "a" * 128, "a" * 129, "private_prompt_" * 100])
def test_oversized_path_is_dropped_without_losing_rejection_context(
    client: TestClient, warnings: list[dict[str, Any]], path: str,
) -> None:
    key = _key(client)
    payload = _request(key, parameter="other")
    payload["contract_rejection"]["parameter_path"] = path
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 200
    [event] = warnings
    assert event["contexts"]["gateway_rejection"] == {"request_id": REQUEST_ID}
    assert event["tags"]["workspace_id"] == key["workspace_id"]
    assert event["tags"]["credential_id"] == key["hash"]
    assert event["tags"]["route"] == "/v1/chat/completions"
    assert event["tags"]["http_status"] == "400"
    assert event["fingerprint"][-1] == "other"
    assert path[:100] not in json.dumps(event)
    assert STORE.credit_money[key["workspace_id"]].reserved_microdollars == 0


def test_diagnostic_sink_rechecks_path_limit(warnings: list[dict[str, Any]]) -> None:
    rejection = GatewayContractRejection.model_construct(
        status=400, parameter="other", request_id=REQUEST_ID, parameter_path="a" * 101,
    )
    report_gateway_contract_rejection(
        rejection, route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
    )
    assert warnings[0]["contexts"]["gateway_rejection"] == {"request_id": REQUEST_ID}


@pytest.mark.parametrize(("path", "raw", "expected"), [
    ("prompt_cache_retention", '"24h"', '"24h"'),
    ("usage.include", '"false"', '"false"'),
    ("temperature", "0.7", "0.7"),
    ("usage", '{"include":true}', '{"include":true}'),
    ("future", '"private customer content"', '"[redacted:string]"'),
    ("messages", '[{"content":"private customer content"}]', '"[redacted:array]"'),
])
def test_safe_value_preview_survives_sentry_scrubbing(
    client: TestClient, warnings: list[dict[str, Any]], path: str, raw: str, expected: str,
) -> None:
    key = _key(client)
    payload = _request(key, parameter="other")
    payload["contract_rejection"].update(parameter_path=path, value_preview=raw, value_truncated=True)
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 200
    [event] = warnings
    context = event["contexts"]["gateway_rejection"]
    assert context["value_preview"] == expected
    assert context["value_truncated"] is True
    assert context["request_id"] == REQUEST_ID
    assert event["tags"]["workspace_id"] == key["workspace_id"]
    assert event["fingerprint"][-1] == "other"
    assert "private customer content" not in json.dumps(event)
    assert STORE.credit_money[key["workspace_id"]].reserved_microdollars == 0


def test_oversized_value_preserves_warning_not_a_prompt_prefix(
    client: TestClient, warnings: list[dict[str, Any]],
) -> None:
    payload = _request(_key(client))
    payload["contract_rejection"].update(parameter_path="store", value_preview="private " * 100)
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 200
    assert warnings[0]["contexts"]["gateway_rejection"] == {
        "request_id": REQUEST_ID, "parameter_path": "store",
    }


def test_value_previews_do_not_create_new_sentry_groups(warnings: list[dict[str, Any]]) -> None:
    for value in ("0.1", "0.7", "10", '"false"'):
        report_gateway_contract_rejection(
            GatewayContractRejection(
                status=400, parameter="temperature", request_id=REQUEST_ID,
                parameter_path="temperature", value_preview=value,
            ),
            route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
        )
    assert len(warnings) == 1
    assert warnings[0]["contexts"]["gateway_rejection"]["value_preview"] == "0.1"
    assert warnings[0]["fingerprint"][-1] == "temperature"


def test_arbitrary_rejected_paths_do_not_exhaust_sentry_budget(
    warnings: list[dict[str, Any]],
) -> None:
    for i in range(100):
        report_gateway_contract_rejection(
            GatewayContractRejection(
                status=400, parameter="other", request_id=REQUEST_ID,
                parameter_path=f"future_option_{i}",
            ),
            route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
        )
    assert len(warnings) == 1
    assert warnings[0]["contexts"]["gateway_rejection"]["parameter_path"] == "future_option_0"


def test_invalid_key_cannot_create_a_warning(
    client: TestClient, warnings: list[dict[str, Any]]
) -> None:
    response = client.post("/v1/internal/gateway/validate", json=_request({"hash": "invalid"}))
    assert response.status_code == 401
    assert warnings == []


def test_internal_auth_is_required_before_warning(warnings: list[dict[str, Any]]) -> None:
    client = TestClient(
        create_app(
            Settings(environment="test", internal_gateway_token="private-internal"),  # noqa: S106 - test-only credential
            init_observability=False,
        )
    )
    key = _key(client)
    response = client.post("/v1/internal/gateway/validate", json=_request(key))
    assert response.status_code == 401
    assert warnings == []


def test_unknown_parameter_names_are_coalesced_and_not_exported(
    client: TestClient,
    warnings: list[dict[str, Any]],
) -> None:
    key = _key(client)
    for parameter in ["private-customer-text", "sk-tr-v1-sensitive", "alice@example.com"]:
        response = client.post(
            "/v1/internal/gateway/validate", json=_request(key, parameter=parameter)
        )
        assert response.status_code == 200
    assert len(warnings) == 1
    assert warnings[0]["fingerprint"][-1] == "other"
    serialized = json.dumps(warnings)
    for secret in ["private-customer-text", "sk-tr-v1-sensitive", "alice@example.com"]:
        assert secret not in serialized


def test_repeats_are_capped_but_a_different_parameter_warns(
    client: TestClient,
    warnings: list[dict[str, Any]],
) -> None:
    key = _key(client)
    for _ in range(20):
        assert client.post("/v1/internal/gateway/validate", json=_request(key)).status_code == 200
    assert len(warnings) == 1
    assert (
        client.post(
            "/v1/internal/gateway/validate", json=_request(key, parameter="tools")
        ).status_code
        == 200
    )
    assert len(warnings) == 2


def test_warning_failure_cannot_break_key_validation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(_event: Any) -> None:
        raise RuntimeError("private transport detail")

    monkeypatch.setattr(sentry_sdk, "capture_event", broken)
    response = client.post("/v1/internal/gateway/validate", json=_request(_key(client)))
    assert response.status_code == 200
    assert "private transport detail" not in caplog.text


@pytest.mark.parametrize("status", [200, 401, 402, 429, 500, 502])
def test_non_contract_status_cannot_be_reported(
    client: TestClient,
    warnings: list[dict[str, Any]],
    status: int,
) -> None:
    response = client.post(
        "/v1/internal/gateway/validate", json=_request(_key(client), status=status)
    )
    assert response.status_code == 400
    assert warnings == []


def test_rejection_payload_cannot_contain_content(
    client: TestClient, warnings: list[dict[str, Any]]
) -> None:
    payload = _request(_key(client))
    payload["contract_rejection"]["prompt"] = "private content"
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 400
    assert warnings == []


def test_unrecognized_route_is_not_exported(
    client: TestClient, warnings: list[dict[str, Any]]
) -> None:
    payload = _request(_key(client))
    payload["route_type"] = "/v1/private-content"
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 200
    assert warnings == []


def _report(parameter: str = "store") -> None:
    report_gateway_contract_rejection(
        GatewayContractRejection(status=400, parameter=parameter, request_id=REQUEST_ID),
        route="/v1/chat/completions",
        workspace_id="ws_verified",
        credential_id="key_verified",
    )


def test_warning_budget_leaves_room_for_server_errors_and_recovers(
    warnings: list[dict[str, Any]],
) -> None:
    now = [0.0]
    reset_sentry_floodgate_for_tests(clock=lambda: now[0])
    for parameter in sorted(PARAMETER_CATEGORIES):
        _report(parameter)
    assert len(warnings) == 10
    # Warnings use at most ten of the normal fifty-event hourly budget.
    for i in range(40):
        assert before_send({"level": "error", "fingerprint": ["real-outage", str(i)]}) is not None
    assert before_send({"level": "error", "fingerprint": ["over-budget"]}) is None
    now[0] = 3601.0
    _report()
    assert len(warnings) == 11


def test_simultaneous_rejections_emit_one_warning(warnings: list[dict[str, Any]]) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: _report(), range(100)))
    assert len(warnings) == 1


def test_invalid_request_id_is_not_exported(
    client: TestClient, warnings: list[dict[str, Any]]
) -> None:
    payload = _request(_key(client))
    payload["contract_rejection"]["request_id"] = "private-content"
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 400
    assert warnings == []
