"""Provider error code/type/param and request ID reach probe samples and alerts.

Sentry QUILL-ROUTER-J1 (a 400 "That request was rejected") kept only the
provider's human message, so the rejected field was unknowable without a
credentialed call. The gateway already forwards ``error.type``, ``error.code``
and ``error.param`` and stamps ``x-request-id``; these tests pin that the probe
keeps them and the route alerts surface them, without touching grouping.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from trusted_router.provider_error_detail import (
    ProviderErrorDetail,
    detail_from_error,
    format_error_message,
    parse_error_message,
)
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    provider_rotation_probe,
    provider_throughput_probe,
)
from trusted_router.synthetic.route_health import (
    RouteHealthFlag,
    evaluate_route_health,
    report_route_health,
)

TARGET = SyntheticTarget("rotation", "https://api.trustedrouter.com/v1", "us-central1")
REJECTED = (
    "That request was rejected. Check it against the model's supported inputs."
)
STRUCTURED_400 = {
    "error": {
        "message": REJECTED,
        "type": "invalid_request_error",
        "code": "unsupported_parameter",
        "param": "temperature",
        "source": "provider",
        "status": 400,
        "metadata": {"provider_name": "boundless", "raw": '{"prompt": "never stored"}'},
    }
}
PREFIX = (
    "{type=invalid_request_error code=unsupported_parameter param=temperature "
    "request_id=req_0123abcd}"
)


async def _probe(handler: Any) -> ProviderBenchmarkSample:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await provider_rotation_probe(
            client,
            TARGET,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="boundless",
            model="openai/gpt-oss-120b",
        )


@pytest.mark.asyncio
async def test_structured_400_keeps_code_type_param_and_request_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=STRUCTURED_400, headers={"x-request-id": "req_0123abcd"})

    sample = await _probe(handler)

    # Classification and status are unchanged.
    assert sample.error_type == "probe_config_error"
    assert sample.error_status == 400
    assert sample.status == "unsupported"
    assert sample.error_message == f"{PREFIX} {REJECTED}"
    # The gateway's raw provider body is never stored.
    assert "never stored" not in str(sample.error_message)
    detail, rest = parse_error_message(sample.error_message)
    assert detail == ProviderErrorDetail(
        type="invalid_request_error",
        code="unsupported_parameter",
        param="temperature",
        request_id="req_0123abcd",
    )
    assert rest == REJECTED


@pytest.mark.asyncio
async def test_unstructured_error_still_records_message_and_request_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad gateway text", headers={"request-id": "gw-77"})

    sample = await _probe(handler)

    assert sample.error_type == "http_400"
    assert sample.error_status == 400
    assert sample.error_message == "{request_id=gw-77}"


@pytest.mark.asyncio
async def test_unstructured_error_without_request_id_has_no_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="not json")

    sample = await _probe(handler)

    assert sample.error_type == "http_400"
    assert sample.error_message is None


@pytest.mark.asyncio
async def test_sse_error_frame_keeps_message_and_request_id() -> None:
    # The SSE observer is mirrored byte for byte by the public provider-check
    # contract, so a mid-stream error frame keeps its message and the request
    # ID (which finds the gateway log line naming code and param), not the
    # frame's own code/type/param.
    body = (
        b'data: {"error":{"message":"rejected","type":"invalid_request_error",'
        b'"code":"unsupported_parameter","param":"top_p","status":400}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"x-request-id": "req-sse"})

    sample = await _probe(handler)

    assert sample.error_status == 400
    assert sample.error_type == "probe_config_error"
    assert sample.error_message == "{request_id=req-sse} rejected"


@pytest.mark.asyncio
async def test_throughput_probe_keeps_code_type_param() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=STRUCTURED_400, headers={"x-request-id": "req_0123abcd"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sample = await provider_throughput_probe(
            client,
            TARGET,
            monitor_region="us-central1",
            api_key="sk-test",  # noqa: S106 - test placeholder.
            provider="boundless",
            model="openai/gpt-oss-120b",
        )

    assert sample.source == "synthetic_throughput"
    assert sample.error_message == f"{PREFIX} {REJECTED}"


def _streak(message: str | None) -> list[ProviderBenchmarkSample]:
    now = dt.datetime.now(dt.UTC)
    return [
        ProviderBenchmarkSample(
            id=f"config-{i}",
            model="openai/gpt-oss-120b",
            provider="boundless",
            provider_name="boundless",
            status="unsupported",
            usage_type="Credits",
            streamed=True,
            error_type="probe_config_error",
            error_status=400,
            error_message=message,
            source="synthetic",
            created_at=(now - dt.timedelta(hours=i)).isoformat(),
        )
        for i in range(6)
    ]


class _Store:
    def __init__(self, samples: list[ProviderBenchmarkSample]) -> None:
        self.samples = samples

    def _newest_first(self) -> list[ProviderBenchmarkSample]:
        return sorted(self.samples, key=lambda sample: sample.created_at, reverse=True)

    def provider_benchmark_samples(self, **_kwargs: Any) -> list[ProviderBenchmarkSample]:
        return self._newest_first()

    def provider_route_benchmark_samples(self, **_kwargs: Any) -> list[ProviderBenchmarkSample]:
        return self._newest_first()

    def provider_account_refusal_samples(self, **_kwargs: Any) -> list[ProviderBenchmarkSample]:
        # Every sample here is a 400; none is an account refusal.
        return []


def _capture_ops_alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    from trusted_router.synthetic import alerts

    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        alerts, "ops_alert", lambda message, **kwargs: captured.append((message, kwargs)),
    )
    return captured


def test_configuration_alert_surfaces_code_type_param(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_ops_alerts(monkeypatch)
    flags = evaluate_route_health(  # type: ignore[arg-type]
        _Store(_streak(f"{PREFIX} {REJECTED}")),
        routes=[("boundless", "openai/gpt-oss-120b")],
    )
    assert [flag.kind for flag in flags] == ["configuration"]

    report_route_health(flags)

    assert len(captured) == 1
    message, kwargs = captured[0]
    assert message.endswith(f"; latest: {PREFIX} {REJECTED}")
    # Grouping is unchanged: route only, never the error code.
    assert kwargs["fingerprint"] == ["route-configuration", "boundless", "openai/gpt-oss-120b"]
    assert kwargs["tags"] == {
        "route_provider": "boundless",
        "route_model": "openai/gpt-oss-120b",
        "upstream_error_type": "invalid_request_error",
        "upstream_error_code": "unsupported_parameter",
        "upstream_error_param": "temperature",
    }
    assert kwargs["extra"] == {"upstream_request_id": "req_0123abcd"}


def test_configuration_alert_without_detail_keeps_route_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_ops_alerts(monkeypatch)
    flags = evaluate_route_health(  # type: ignore[arg-type]
        _Store(_streak(REJECTED)), routes=[("boundless", "openai/gpt-oss-120b")],
    )

    report_route_health(flags)

    message, kwargs = captured[0]
    assert message.endswith(f"; latest: {REJECTED}")
    assert kwargs["tags"] == {"route_provider": "boundless", "route_model": "openai/gpt-oss-120b"}
    assert kwargs["extra"] is None


def test_structural_alert_tags_code_type_param(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    class Scope:
        def __init__(self) -> None:
            self.fingerprint: list[str] = []
            self.tags: dict[str, str] = {}
            self.extra: dict[str, str] = {}

        def set_tag(self, key: str, value: str) -> None:
            self.tags[key] = value

        def set_extra(self, key: str, value: str) -> None:
            self.extra[key] = value

        def __enter__(self) -> Scope:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    scopes: list[Scope] = []
    messages: list[str] = []

    def push_scope() -> Scope:
        scopes.append(Scope())
        return scopes[-1]

    monkeypatch.setattr(sentry_sdk, "push_scope", push_scope)
    monkeypatch.setattr(
        sentry_sdk, "capture_message", lambda message, *, level: messages.append(message),
    )
    report_route_health([
        RouteHealthFlag(
            "boundless", "openai/gpt-oss-120b", 6, 6, 1.0, "provider_error",
            f"{PREFIX} {REJECTED}",
        ),
    ])

    assert messages == [
        "route-health: boundless/openai/gpt-oss-120b 100% failure over 6 samples "
        f"(latest: provider_error {PREFIX} {REJECTED})"
    ]
    assert scopes[0].fingerprint == ["route-health", "boundless", "openai/gpt-oss-120b"]
    assert scopes[0].tags["upstream_error_code"] == "unsupported_parameter"
    assert scopes[0].tags["upstream_error_param"] == "temperature"
    assert scopes[0].tags["upstream_error_type"] == "invalid_request_error"
    assert scopes[0].extra == {"upstream_request_id": "req_0123abcd"}


def test_ops_alert_sets_extra_outside_tags_and_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentry_sdk

    from trusted_router.synthetic import alerts

    class Scope:
        def __init__(self) -> None:
            self.fingerprint: list[str] = []
            self.tags: dict[str, str] = {}
            self.extra: dict[str, str] = {}

        def set_tag(self, key: str, value: str) -> None:
            self.tags[key] = value

        def set_extra(self, key: str, value: str) -> None:
            self.extra[key] = value

        def __enter__(self) -> Scope:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    scope = Scope()
    monkeypatch.setattr(alerts, "alert_claimed", lambda _key: True)
    monkeypatch.setattr(sentry_sdk, "new_scope", lambda: scope)
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda *_a, **_k: None)

    assert alerts.ops_alert(
        "x", fingerprint=["route-configuration", "p", "m"],
        tags={"route_provider": "p"}, extra={"upstream_request_id": "req-1"},
    )
    assert scope.fingerprint == ["ops-alert", "route-configuration", "p", "m"]
    assert scope.tags == {"route_provider": "p"}
    assert scope.extra == {"upstream_request_id": "req-1"}


@pytest.mark.parametrize(
    ("error", "headers", "expected"),
    [
        # Generic gateway type and a numeric code equal to the status add nothing.
        ({"type": "provider_error", "code": 400}, {}, ProviderErrorDetail()),
        ({"code": 1003, "param": "messages[0].content"}, {},
         ProviderErrorDetail(code="1003", param="messages[0].content")),
        # Not plain identifier-shaped strings: dropped, never escaped.
        ({"type": "a b", "code": {"nested": 1}, "param": ["temperature"]}, {},
         ProviderErrorDetail()),
        ({"code": True, "param": "x" * 65}, {}, ProviderErrorDetail()),
        ({"code": "sk-" + "a1B2c3D4e5F6g7H8"}, {}, ProviderErrorDetail()),
        ({"code": "bad}code"}, {"x-request-id": "id with space"}, ProviderErrorDetail()),
        ({"metadata": {"provider_request_id": "chatcmpl-req-9"}}, {"x-request-id": "gw-1"},
         ProviderErrorDetail(request_id="gw-1", provider_request_id="chatcmpl-req-9")),
    ],
)
def test_detail_from_error_admits_only_bounded_identifiers(
    error: dict[str, Any], headers: dict[str, str], expected: ProviderErrorDetail,
) -> None:
    assert detail_from_error(error, headers, status=400) == expected


def test_format_bounds_total_length_and_keeps_detail_first() -> None:
    detail = ProviderErrorDetail(
        type="t" * 64, code="c" * 64, param="p" * 64, request_id="r" * 96,
        provider_request_id="q" * 96,
    )
    message = format_error_message("m" * 400, detail)
    assert message is not None
    assert len(message) == 300
    parsed, rest = parse_error_message(message)
    # The prefix budget keeps the highest-priority fields that fit.
    assert parsed.type == "t" * 64
    assert parsed.code == "c" * 64
    assert parsed.param == "p" * 64
    assert rest is not None and set(rest) == {"m"}


def test_format_scrubs_message_and_passes_plain_messages_through() -> None:
    secret = "sk-" + "Zz9Yy8Xx7Ww6"
    assert format_error_message(f"bad key {secret}") == "bad key sk-***"
    assert format_error_message(None) is None
    assert format_error_message("") is None


@pytest.mark.parametrize(
    "message",
    [
        "plain provider message",
        "{not a prefix} message",
        "{type=ok unknown=field} message",
        "{type=ok type=twice} message",
        "{code=sk-***} message",
    ],
)
def test_parse_leaves_messages_without_a_valid_prefix_alone(message: str) -> None:
    assert parse_error_message(message) == (ProviderErrorDetail(), message)
