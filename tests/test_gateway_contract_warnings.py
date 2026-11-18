"""Gateway contract rejections are expected customer 4xx/501 responses.

They used to be Sentry warnings, and every route/status/parameter opened its
own Sentry issue (dozens of "Authenticated gateway request rejected by the
API contract" issues), burying real faults. They are now one structured
operational log line each. These tests read that line, run it through the
same Axiom scrubber production applies, and assert Sentry never sees it.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import sentry_sdk
from fastapi.testclient import TestClient

from trusted_router import storage_keys
from trusted_router.axiom_config import _AxiomScrubFilter
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.schemas import GatewayContractRejection
from trusted_router.sentry_config import (
    _scrub_string,
    before_send,
    reset_sentry_floodgate_for_tests,
)
from trusted_router.services.gateway_contract_warnings import (
    PARAMETER_CATEGORIES,
    report_gateway_contract_rejection,
)
from trusted_router.storage import STORE
from trusted_router.storage_operational_analytics import analytics_surrogate

REQUEST_ID = "rlog_" + "a" * 32


@pytest.mark.parametrize(
    "parameter", ["include_reasoning", "thinking", "enable_thinking", "chat_template_kwargs"]
)
def test_reasoning_has_a_stable_diagnostic_category(parameter: str) -> None:
    assert parameter in PARAMETER_CATEGORIES


@pytest.mark.parametrize("parameter", ["repetition_penalty", "repeat_penalty"])
def test_repetition_penalty_spellings_have_their_own_category(parameter: str) -> None:
    assert parameter in PARAMETER_CATEGORIES


LOGGER = "trusted_router.services.gateway_contract_warnings"
LOG_FIELDS = (
    "contract_route", "http_status", "parameter", "request_id", "workspace_id",
    "workspace_surrogate", "credential_id", "parameter_path", "rejected_field",
    "value_preview", "value_truncated",
)


class _ContractLog:
    """The scrubbed `gateway.contract_rejection` log lines, read on access."""

    def __init__(self, caplog: pytest.LogCaptureFixture) -> None:
        self._caplog = caplog

    def _rows(self) -> list[dict[str, Any]]:
        rows = []
        for record in self._caplog.records:
            if record.name != LOGGER or record.msg != "gateway.contract_rejection":
                continue
            assert record.levelno == logging.INFO
            shipped = copy.copy(record)
            shipped.__dict__ = dict(record.__dict__)
            assert _AxiomScrubFilter().filter(shipped)
            rows.append({k: shipped.__dict__[k] for k in LOG_FIELDS if k in shipped.__dict__})
        return rows

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._rows())

    def __len__(self) -> int:
        return len(self._rows())

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self._rows()[index]

    def __eq__(self, other: object) -> bool:
        return self._rows() == other


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []
    monkeypatch.setattr(sentry_sdk, "capture_event", lambda event, *a, **k: events.append(event))
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda msg, *a, **k: events.append(msg))
    return events


@pytest.fixture
def warnings(
    sentry_events: list[Any], caplog: pytest.LogCaptureFixture
) -> Iterator[_ContractLog]:
    reset_sentry_floodgate_for_tests()
    caplog.set_level(logging.INFO, logger=LOGGER)
    yield _ContractLog(caplog)
    # The point of the change: an expected client error never opens a Sentry issue.
    assert sentry_events == []
    reset_sentry_floodgate_for_tests()


@pytest.fixture(autouse=True)
def deterministic_key_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mint fixed key ids so `credential_id` tag assertions cannot flake.

    Real key ids are `key_` + `secrets.token_urlsafe(18)`. A random draw can
    contain a GitHub token prefix such as `ghu_` (CI saw `key_...QNcghu_`),
    and the Sentry scrubber deliberately filters those prefixes anywhere in a
    string, so the tag would become `[Filtered]`. That substring matching is
    the intended secret-scrubbing behavior; these tests are about rejection
    context, so they pin ids the scrubber is proven to leave intact.
    """
    counter = itertools.count()

    def fixed_key_id(prefix: str = "key") -> str:
        key_id = f"{prefix}_contractwarning{next(counter):09d}"
        assert _scrub_string(key_id) == key_id, key_id
        return key_id

    monkeypatch.setattr(storage_keys, "new_key_id", fixed_key_id)


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
def test_rejection_logs_with_verified_identity_without_billing(
    client: TestClient,
    warnings: _ContractLog,
    status: int,
) -> None:
    key = _key(client)
    response = client.post("/v1/internal/gateway/validate", json=_request(key, status=status))
    assert response.status_code == 200, response.text
    [event] = warnings
    assert event["contract_route"] == "/v1/chat/completions"
    assert event["http_status"] == str(status)
    assert event["parameter"] == "store"
    assert event["workspace_id"] == key["workspace_id"]
    assert event["credential_id"] == key["hash"]
    # A query counts distinct workspaces with the opaque surrogate.
    assert event["workspace_surrogate"] == analytics_surrogate("workspace", key["workspace_id"])
    assert key["workspace_id"] not in event["workspace_surrogate"]
    assert event["request_id"] == REQUEST_ID
    assert "private-key-label" not in json.dumps(event)
    assert "alice@example.com" not in json.dumps(event)
    assert STORE.credit_money[key["workspace_id"]].reserved_microdollars == 0
    assert STORE.credit_money[key["workspace_id"]].total_usage_microdollars == 0
    assert not STORE.generation_store.generations


def test_plain_validation_is_not_an_error(
    client: TestClient, warnings: _ContractLog
) -> None:
    key = _key(client)
    response = client.post("/v1/internal/gateway/validate", json={"api_key_hash": key["hash"]})
    assert response.status_code == 200
    assert warnings == []


@pytest.mark.parametrize(
    "parameter",
    [
        "deidentify", "plugins", "truncation", "prompt_cache_key", "prompt_cache_options", "service_tier",
        "usage.include", "stream_options.include_usage", "provider.quantizations",
        "provider.max_price.image", "plugins.web-fetch",
        "file", "audio", "video", "content", "input_image", "input_audio", "input_file",
    ],
)
def test_public_parameter_diagnosis_survives_scrubbing(
    client: TestClient, warnings: _ContractLog, parameter: str
) -> None:
    key = _key(client)
    response = client.post(
        "/v1/internal/gateway/validate", json=_request(key, parameter=parameter)
    )
    assert response.status_code == 200
    [event] = warnings
    assert event["parameter"] == parameter
    assert event["workspace_id"] == key["workspace_id"]
    assert STORE.credit_money[key["workspace_id"]].reserved_microdollars == 0


@pytest.mark.parametrize(
    "path", ["future_option", "usage.future_option", "input[12].future_option", "a" * 100, "usage." + "a" * 94],
)
def test_unknown_rejected_field_path_is_retained_under_the_other_category(
    client: TestClient, warnings: _ContractLog, path: str
) -> None:
    payload = _request(_key(client), parameter="other")
    payload["contract_rejection"]["parameter_path"] = path
    response = client.post("/v1/internal/gateway/validate", json=payload)
    assert response.status_code == 200
    [event] = warnings
    assert event["parameter_path"] == path
    assert event["parameter"] == "other"


@pytest.mark.parametrize(
    ("parameter", "path", "field"),
    [
        ("input", "input[148].content", "input[].content"),
        ("input", "input[0].content", "input[].content"),
        ("input", "input[5].namespace", "input[].namespace"),
        ("tools", "tools[3].type", "tools[].type"),
        ("other", "client_metadata", "client_metadata"),
        ("other", "token_budget", "token_budget"),
        ("thinking", "thinking", "thinking"),
        ("other", "a[1].b[22].c[333]", "a[].b[].c[]"),
    ],
)
def test_rejected_field_aggregates_paths_without_indices(
    warnings: _ContractLog, parameter: str, path: str, field: str
) -> None:
    """The field without item indices, so a log query can tell a widely sent
    field (our gap) from one client's stray field."""
    report_gateway_contract_rejection(
        GatewayContractRejection(
            status=400, parameter=parameter, request_id=REQUEST_ID, parameter_path=path
        ),
        route="/v1/responses", workspace_id="ws_verified", credential_id="key_verified",
    )
    [event] = warnings
    assert event["rejected_field"] == field
    assert event["parameter_path"] == path
    assert (event["contract_route"], event["http_status"], event["parameter"]) == (
        "/v1/responses", "400", parameter,
    )


def test_each_workspace_has_its_own_surrogate(warnings: _ContractLog) -> None:
    for workspace in ("ws_first", "ws_second"):
        report_gateway_contract_rejection(
            GatewayContractRejection(status=400, parameter="thinking", request_id=REQUEST_ID),
            route="/v1/chat/completions", workspace_id=workspace, credential_id="key_verified",
        )
    assert [event["workspace_surrogate"] for event in warnings] == [
        analytics_surrogate("workspace", "ws_first"),
        analytics_surrogate("workspace", "ws_second"),
    ]
    assert warnings[0]["parameter"] == warnings[1]["parameter"] == "thinking"


@pytest.mark.parametrize(
    "path",
    ["alice@example.com", "sk-tr-v1-secret", "sk_private_secret", "private customer text", "usage=secret", "x\nsecret"],
)
def test_unsafe_rejected_field_path_is_dropped_not_forwarded(
    warnings: _ContractLog, path: str
) -> None:
    report_gateway_contract_rejection(
        GatewayContractRejection(status=400, parameter="other", request_id=REQUEST_ID, parameter_path=path),
        route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
    )
    assert "parameter_path" not in warnings[0]
    assert "rejected_field" not in warnings[0]
    assert path not in json.dumps(list(warnings))


@pytest.mark.parametrize("path", ["a" * 101, "usage." + "a" * 95, "a" * 128, "a" * 129, "private_prompt_" * 100])
def test_oversized_path_is_dropped_without_losing_rejection_context(
    client: TestClient, warnings: _ContractLog, path: str,
) -> None:
    key = _key(client)
    payload = _request(key, parameter="other")
    payload["contract_rejection"]["parameter_path"] = path
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 200
    [event] = warnings
    assert event["request_id"] == REQUEST_ID
    assert not {"parameter_path", "rejected_field", "value_preview"} & set(event)
    assert event["workspace_id"] == key["workspace_id"]
    assert event["credential_id"] == key["hash"]
    assert event["contract_route"] == "/v1/chat/completions"
    assert event["http_status"] == "400"
    assert event["parameter"] == "other"
    assert path[:100] not in json.dumps(event)
    assert STORE.credit_money[key["workspace_id"]].reserved_microdollars == 0


def test_diagnostic_sink_rechecks_path_limit(warnings: _ContractLog) -> None:
    rejection = GatewayContractRejection.model_construct(
        status=400, parameter="other", request_id=REQUEST_ID, parameter_path="a" * 101,
    )
    report_gateway_contract_rejection(
        rejection, route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
    )
    assert warnings[0]["request_id"] == REQUEST_ID
    assert "parameter_path" not in warnings[0]


@pytest.mark.parametrize("parameter", ["output_config", "output_config.effort"])
def test_output_config_has_named_warning_category(
    warnings: _ContractLog, parameter: str,
) -> None:
    report_gateway_contract_rejection(
        GatewayContractRejection(
            status=400, parameter=parameter, request_id=REQUEST_ID,
            parameter_path="output_config.effort", value_preview='"high"',
        ),
        route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
    )
    [event] = warnings
    assert event["parameter"] == parameter
    assert event["value_preview"] == '"high"'


@pytest.mark.parametrize(("parameter", "path", "raw", "expected"), [
    ("thinking", "thinking", '{"budget_tokens":8000,"type":"auto"}', '{"budget_tokens":8000,"type":"auto"}'),
    ("thinking", "thinking", '{"type":"enabled","display":"private customer content"}',
     '{"_redacted":true,"type":"enabled"}'),
    ("chat_template_kwargs", "chat_template_kwargs", '{"enable_thinking":false,"thinking":true}',
     '{"enable_thinking":false,"thinking":true}'),
    ("chat_template_kwargs", "chat_template_kwargs.add_generation_prompt", "true", '"[redacted:boolean]"'),
    ("chat_template_kwargs", "chat_template_kwargs.documents", '"private customer content"', '"[redacted:string]"'),
    ("client_metadata", "client_metadata", '"[redacted:string]"', '"[redacted:string]"'),
    ("client_metadata", "client_metadata", '{"x-codex-installation-id":"private customer content","type":"enabled"}',
     '"[redacted:object]"'),
    ("repeat_penalty", "repeat_penalty", "-1", "-1"),
    ("repeat_penalty", "repeat_penalty", '"private customer content"', '"[redacted:string]"'),
    ("repetition_penalty", "repetition_penalty", "1.1", "1.1"),
])
def test_compatibility_fields_warn_under_their_own_name(
    warnings: _ContractLog, parameter: str, path: str, raw: str, expected: str,
) -> None:
    report_gateway_contract_rejection(
        GatewayContractRejection(
            status=400, parameter=parameter, request_id=REQUEST_ID, parameter_path=path, value_preview=raw,
        ),
        route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
    )
    [event] = warnings
    assert event["parameter"] == parameter
    assert event["parameter_path"] == path
    assert event["value_preview"] == expected
    assert "private customer content" not in json.dumps(event)


@pytest.mark.parametrize(("path", "raw", "expected"), [
    ("prompt_cache_retention", '"24h"', '"24h"'),
    ("usage.include", '"false"', '"false"'),
    ("output_config.effort", '"high"', '"high"'),
    ("output_config.effort", '"private customer content"', '"[redacted:string]"'),
    ("temperature", "0.7", "0.7"),
    ("usage", '{"include":true}', '{"include":true}'),
    ("future", '"private customer content"', '"[redacted:string]"'),
    ("messages", '[{"content":"private customer content"}]', '"[redacted:array]"'),
    ("include", '["reasoning.encrypted_content"]', '["reasoning.encrypted_content"]'),
    ("include", '["reasoning.encrypted_content","private customer content"]',
     '["reasoning.encrypted_content","[redacted:string]"]'),
    ("include", '["reasoning.encrypted_content","reasoning.encrypted_content","reasoning.encrypted_content"]',
     '["reasoning.encrypted_content","reasoning.encrypted_content","reasoning.encrypted_content"]'),
    ("modalities", '["text","audio"]', '["text","audio"]'),
    ("tools[0].type", '"custom"', '"custom"'),
    ("tools[12].type", '"web_search"', '"web_search"'),
    ("tools[0].type", '"mcp"', '"mcp"'),
    ("tools[0].type", '"private customer content"', '"[redacted:string]"'),
    ("tools[0].function.name", '"private customer content"', '"[redacted:string]"'),
    ("tools[0].type", '{"secret":"private customer content"}', '"[redacted:object]"'),
])
@pytest.mark.parametrize(("route", "status"), [("/v1/chat/completions", 400), ("/v1/responses", 501)])
def test_safe_value_preview_survives_log_scrubbing(
    client: TestClient, warnings: _ContractLog, path: str, raw: str, expected: str,
    route: str, status: int,
) -> None:
    key = _key(client)
    payload = _request(key, parameter="other")
    payload["route_type"] = route
    payload["contract_rejection"].update(status=status, parameter_path=path, value_preview=raw, value_truncated=True)
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 200
    [event] = warnings
    assert event["value_preview"] == expected
    assert event["value_truncated"] is True
    assert event["request_id"] == REQUEST_ID
    assert event["workspace_id"] == key["workspace_id"]
    assert event["contract_route"] == route
    assert event["http_status"] == str(status)
    assert event["parameter"] == "other"
    assert "private customer content" not in json.dumps(event)
    assert STORE.credit_money[key["workspace_id"]].reserved_microdollars == 0


def test_oversized_value_preserves_warning_not_a_prompt_prefix(
    client: TestClient, warnings: _ContractLog,
) -> None:
    payload = _request(_key(client))
    payload["contract_rejection"].update(parameter_path="store", value_preview="private " * 100)
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 200
    assert warnings[0]["parameter_path"] == "store"
    assert "value_preview" not in warnings[0]
    assert "private" not in json.dumps(list(warnings))


def test_every_rejection_is_logged_with_its_own_preview(warnings: _ContractLog) -> None:
    for value in ("0.1", "0.7", "10", '"false"'):
        report_gateway_contract_rejection(
            GatewayContractRejection(
                status=400, parameter="temperature", request_id=REQUEST_ID,
                parameter_path="temperature", value_preview=value,
            ),
            route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
        )
    assert [event["value_preview"] for event in warnings] == ["0.1", "0.7", "10", '"false"']
    assert {event["parameter"] for event in warnings} == {"temperature"}


def test_arbitrary_rejected_paths_are_all_logged_and_never_reach_sentry(
    warnings: _ContractLog,
) -> None:
    for i in range(100):
        report_gateway_contract_rejection(
            GatewayContractRejection(
                status=400, parameter="other", request_id=REQUEST_ID,
                parameter_path=f"future_option_{i}",
            ),
            route="/v1/chat/completions", workspace_id="ws_verified", credential_id="key_verified",
        )
    assert [event["parameter_path"] for event in warnings] == [f"future_option_{i}" for i in range(100)]


def test_invalid_key_cannot_create_a_warning(
    client: TestClient, warnings: _ContractLog
) -> None:
    response = client.post("/v1/internal/gateway/validate", json=_request({"hash": "invalid"}))
    assert response.status_code == 401
    assert warnings == []


def test_internal_auth_is_required_before_warning(warnings: _ContractLog) -> None:
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
    warnings: _ContractLog,
) -> None:
    key = _key(client)
    for parameter in ["private-customer-text", "sk-tr-v1-sensitive", "alice@example.com"]:
        response = client.post(
            "/v1/internal/gateway/validate", json=_request(key, parameter=parameter)
        )
        assert response.status_code == 200
    assert len(warnings) == 3
    assert {event["parameter"] for event in warnings} == {"other"}
    serialized = json.dumps(list(warnings))
    for secret in ["private-customer-text", "sk-tr-v1-sensitive", "alice@example.com"]:
        assert secret not in serialized


def test_every_repeat_is_logged_for_demand_counts(
    client: TestClient,
    warnings: _ContractLog,
) -> None:
    key = _key(client)
    for _ in range(20):
        assert client.post("/v1/internal/gateway/validate", json=_request(key)).status_code == 200
    assert len(warnings) == 20
    assert (
        client.post(
            "/v1/internal/gateway/validate", json=_request(key, parameter="tools")
        ).status_code
        == 200
    )
    assert [event["parameter"] for event in warnings].count("tools") == 1


def test_log_failure_cannot_break_key_validation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    warnings: _ContractLog,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*_args: Any) -> str:
        raise RuntimeError("private transport detail")

    monkeypatch.setattr(
        "trusted_router.services.gateway_contract_warnings.analytics_surrogate", broken
    )
    response = client.post("/v1/internal/gateway/validate", json=_request(_key(client)))
    assert response.status_code == 200
    assert "private transport detail" not in caplog.text
    assert "gateway contract rejection log failed" in caplog.text
    assert warnings == []


@pytest.mark.parametrize("status", [200, 401, 402, 429, 500, 502])
def test_non_contract_status_cannot_be_reported(
    client: TestClient,
    warnings: _ContractLog,
    status: int,
) -> None:
    response = client.post(
        "/v1/internal/gateway/validate", json=_request(_key(client), status=status)
    )
    assert response.status_code == 400
    assert warnings == []


def test_rejection_payload_cannot_contain_content(
    client: TestClient, warnings: _ContractLog
) -> None:
    payload = _request(_key(client))
    payload["contract_rejection"]["prompt"] = "private content"
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 400
    assert warnings == []


def test_unrecognized_route_is_not_exported(
    client: TestClient, warnings: _ContractLog
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


def test_rejections_leave_the_whole_sentry_budget_to_real_server_errors(
    warnings: _ContractLog,
) -> None:
    for parameter in sorted(PARAMETER_CATEGORIES):
        _report(parameter)
    assert len(warnings) == len(PARAMETER_CATEGORIES)
    # None of the normal fifty-event hourly Sentry budget went to rejections.
    for i in range(50):
        assert before_send({"level": "error", "fingerprint": ["real-outage", str(i)]}) is not None
    assert before_send({"level": "error", "fingerprint": ["over-budget"]}) is None


def test_simultaneous_rejections_are_each_logged(warnings: _ContractLog) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: _report(), range(100)))
    assert len(warnings) == 100


def test_invalid_request_id_is_not_exported(
    client: TestClient, warnings: _ContractLog
) -> None:
    payload = _request(_key(client))
    payload["contract_rejection"]["request_id"] = "private-content"
    assert client.post("/v1/internal/gateway/validate", json=payload).status_code == 400
    assert warnings == []
