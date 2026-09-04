from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from trusted_router.config import Settings
from trusted_router.synthetic import cli


class _SSEStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_streaming_probe_requires_first_byte_and_valid_terminal() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "rlog_00112233445566778899aabbccddeeff",
            },
            stream=_SSEStream(
                [
                    b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n',
                    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                    b"data: [DONE]\n\n",
                ]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway_request_id = await cli.streaming_chat_completion_probe(
            client,
            api_base_url="https://api.trustedrouter.com/v1",
            api_key="sk-test",  # noqa: S106 - test placeholder
            model="trustedrouter/monitor",
            idempotency_key="probe-once",
        )

    assert requests[0]["stream"] is True
    assert gateway_request_id == "rlog_00112233445566778899aabbccddeeff"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [
        [b"event: message\n", b"data: [DONE]\n\n"],
        [b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'],
        [b"data: not-json\n\n"],
    ],
)
async def test_streaming_probe_rejects_invalid_sse(chunks: list[bytes]) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_SSEStream(chunks),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await cli.streaming_chat_completion_probe(
                client,
                api_base_url="https://api.trustedrouter.com/v1",
                api_key="sk-test",  # noqa: S106 - test placeholder
                model="trustedrouter/monitor",
                idempotency_key="probe-invalid",
            )


@pytest.mark.asyncio
async def test_stage_d_probe_uses_authorization_lookup_and_checks_binding() -> None:
    gateway_request_id = "rlog_00112233445566778899aabbccddeeff"
    evidence = {
        "data": {
            "authorization_id": "gwa-0123456789abcdef0123456789abcdef",
            "gateway_request_id": gateway_request_id,
            "workspace_id": "workspace",
            "authorization_kind": "local_typed",
            "settled": True,
            "disposition": "finalized",
            "stage_d_boot_kid": "boot-expected",
            "heartbeat_seq": 1,
        }
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=evidence)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await cli.assert_stage_d_authorization(
            client,
            control_plane_base_url="https://trustedrouter.com",
            internal_gateway_token="gateway-token",  # noqa: S106
            gateway_request_id=gateway_request_id,
            expected_boot_kid="boot-expected",
            timeout_seconds=0,
        )
        evidence["data"]["stage_d_boot_kid"] = "boot-other"
        with pytest.raises(RuntimeError, match="unexpected boot kid"):
            await cli.assert_stage_d_authorization(
                client,
                control_plane_base_url="https://trustedrouter.com",
                internal_gateway_token="gateway-token",  # noqa: S106
                gateway_request_id=gateway_request_id,
                expected_boot_kid="boot-expected",
                timeout_seconds=0,
            )

    assert requests[0].url.path.endswith(
        f"/authorizations/by-gateway-request-id/{gateway_request_id}"
    )
    assert requests[0].headers["authorization"] == "Bearer gateway-token"


@pytest.mark.asyncio
async def test_expect_stage_d_runs_only_dedicated_cheap_stream_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def stream_probe(_client: httpx.AsyncClient, **kwargs: Any) -> str:
        calls["stream"] = kwargs
        return "rlog_00112233445566778899aabbccddeeff"

    async def evidence(_client: httpx.AsyncClient, **kwargs: Any) -> None:
        calls["evidence"] = kwargs

    async def ordinary_pass(**_kwargs: Any) -> tuple[list[Any], list[Any]]:
        raise AssertionError("Stage D job ran the ordinary monitor suite")

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(
            environment="test",
            internal_gateway_token="gateway-token",  # noqa: S106
        ),
    )
    monkeypatch.setattr(cli, "streaming_chat_completion_probe", stream_probe)
    monkeypatch.setattr(cli, "assert_stage_d_authorization", evidence)
    monkeypatch.setattr(cli, "_probe_and_rotation_pass", ordinary_pass)
    monkeypatch.setenv("TR_STAGE_D_PROBE_API_KEY", "sk-stage-d")
    monkeypatch.delenv("TR_SYNTHETIC_THROUGHPUT_ONLY", raising=False)
    monkeypatch.delenv("TR_SYNTHETIC_START_DELAY_SECONDS", raising=False)
    monkeypatch.delenv("TR_STAGE_D_PROBE_LOOKUP_TIMEOUT_SECONDS", raising=False)

    assert await cli.run(expect_stage_d=True) == 0
    assert calls["stream"]["model"] == "trustedrouter/cheap"
    assert calls["stream"]["api_key"] == "sk-stage-d"
    assert calls["evidence"]["gateway_request_id"] == (
        "rlog_00112233445566778899aabbccddeeff"
    )
    assert calls["evidence"]["internal_gateway_token"] == "gateway-token"  # noqa: S105
    assert calls["evidence"]["control_plane_base_url"] == "https://trustedrouter.com"
    assert calls["evidence"]["timeout_seconds"] == 60.0


def test_monitor_help_documents_stage_d_workspace_requirements(capsys: Any) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "heartbeat-capable local-typed key" in help_text
    assert "outside the regional-quota pilot" in help_text
    assert "path." in help_text
