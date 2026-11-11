"""The clients that reach URLs a customer chose must connect only to public
addresses, whatever the name resolves to and whenever it is asked.

These tests use a real listener on this machine as the stand-in for an
internal service, and count what reaches it. Mocking the transport would
skip the connect step, which is the thing under test. Every refusal is
paired with a control that does reach the listener, so a test cannot pass
because the listener was simply unreachable.
"""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import broadcast as broadcast_service
from trusted_router.services.broadcast_adapters import (
    WebhookOTLPBroadcastAdapter,
    _validate_http_endpoint,
    allows_local_destinations,
)
from trusted_router.services.safe_egress import (
    _dial_addresses,
    _PublicOnlyAsyncBackend,
    _PublicOnlyBackend,
    is_public_ip_literal_or_name,
    public_egress_async_client,
    public_egress_client,
)
from trusted_router.storage import BroadcastDestination, Generation

_REAL_GETADDRINFO = socket.getaddrinfo


class _Listener:
    """A local HTTP server that records every request it is sent."""

    def __init__(self, *, redirect_to: str | None = None) -> None:
        seen: list[str] = []
        self.seen = seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                seen.append(f"{self.command} {self.path}")
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                if redirect_to is not None:
                    self.send_response(307)
                    self.send_header("location", redirect_to)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-type", "image/png")
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = do_POST = do_PUT = _answer  # noqa: N815 - http.server's naming.

            def log_message(self, *_args: Any) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def listener() -> Iterator[_Listener]:
    server = _Listener()
    yield server
    server.close()


def _resolve_names_to(
    monkeypatch: pytest.MonkeyPatch, answers: Callable[[str], list[str] | None]
) -> list[str]:
    """Answer lookups for chosen names; everything else resolves for real.

    Returns the list of names that were looked up.
    """
    asked: list[str] = []

    def fake_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        name = host.decode() if isinstance(host, bytes) else str(host)
        mapped = answers(name)
        if mapped is None:
            return _REAL_GETADDRINFO(host, port, *args, **kwargs)
        asked.append(name)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, int(port or 0)))
            for address in mapped
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return asked


def _internal(name: str) -> list[str] | None:
    return ["127.0.0.1"] if name.endswith(".example") else None


def test_clients_carry_the_checking_backend() -> None:
    # The builders reach into httpx to install the backend. If an upgrade
    # moves that attribute the clients would connect unchecked, and this is
    # the test that says so.
    with public_egress_client(timeout=1) as client:
        transport = client._transport
        assert isinstance(transport, httpx.HTTPTransport)
        assert isinstance(transport._pool._network_backend, _PublicOnlyBackend)
        assert client.follow_redirects is False
        assert client._mounts == {}
    async_client = public_egress_async_client(timeout=1)
    async_transport = async_client._transport
    assert isinstance(async_transport, httpx.AsyncHTTPTransport)
    assert isinstance(async_transport._pool._network_backend, _PublicOnlyAsyncBackend)
    assert async_client.follow_redirects is False
    assert async_client._mounts == {}


def test_proxy_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:3128")
    with public_egress_client(timeout=1) as client:
        assert client._mounts == {}
        assert isinstance(client._transport._pool._network_backend, _PublicOnlyBackend)  # type: ignore[attr-defined]


def test_name_resolving_to_an_internal_address_is_refused(
    listener: _Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _resolve_names_to(monkeypatch, _internal)
    url = f"http://collector.example:{listener.port}/v1/traces"

    # Control: an ordinary client follows the name to the listener.
    with httpx.Client(trust_env=False, timeout=5) as plain:
        assert plain.post(url, json={}).status_code == 200
    assert listener.seen == ["POST /v1/traces"]

    with public_egress_client(timeout=5) as client, pytest.raises(httpx.ConnectError):
        client.post(url, json={})
    assert listener.seen == ["POST /v1/traces"], "the refused request reached the listener"
    assert asked.count("collector.example") == 2


@pytest.mark.asyncio
async def test_async_name_resolving_to_an_internal_address_is_refused(
    listener: _Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolve_names_to(monkeypatch, _internal)
    url = f"http://collector.example:{listener.port}/v1/traces"

    async with httpx.AsyncClient(trust_env=False, timeout=5) as plain:
        assert (await plain.post(url, json={})).status_code == 200
    assert listener.seen == ["POST /v1/traces"]

    async with public_egress_async_client(timeout=5) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(url, json={})
    assert listener.seen == ["POST /v1/traces"]


@pytest.mark.parametrize(
    "answers",
    (
        ["8.8.8.8", "127.0.0.1"],
        ["10.0.0.5"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["::ffff:127.0.0.1"],
        ["0.0.0.0"],  # noqa: S104 - an address to refuse, not to bind.
    ),
)
def test_any_internal_answer_refuses_the_name(
    answers: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolve_names_to(monkeypatch, lambda name: answers if name.endswith(".example") else None)
    # The message is matched so the test cannot pass because the address
    # happened to be unreachable from where the test runs.
    with (
        public_egress_client(timeout=2) as client,
        pytest.raises(httpx.ConnectError, match="not public"),
    ):
        client.get("https://mixed.example/")


def _info(address: str) -> tuple[Any, ...]:
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))


@pytest.mark.parametrize(
    "answers",
    (
        ["8.8.8.8", "127.0.0.1"],
        ["127.0.0.1", "8.8.8.8"],
        ["8.8.8.8", "10.0.0.5", "1.1.1.1"],
    ),
)
def test_every_answer_is_checked(answers: list[str]) -> None:
    # Control: the same call returns public answers in the resolver's order.
    public = [_info("8.8.8.8"), _info("1.1.1.1")]
    assert _dial_addresses(public, allow_loopback=False) == ["8.8.8.8", "1.1.1.1"]
    with pytest.raises(httpcore.ConnectError, match="not public"):
        _dial_addresses([_info(address) for address in answers], allow_loopback=False)


def test_mixed_answers_never_reach_the_listener(
    listener: _Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Loopback is allowed here, so the first answer alone would connect. The
    # second answer is internal and must stop the request before any dial.
    _resolve_names_to(
        monkeypatch,
        lambda name: ["127.0.0.1", "10.0.0.5"] if name.endswith(".example") else None,
    )
    with (
        public_egress_client(timeout=1, allow_loopback=True) as client,
        pytest.raises(httpx.ConnectError, match="not public"),
    ):
        client.get(f"http://mixed.example:{listener.port}/")
    assert listener.seen == []


def test_sync_client_dials_the_checked_address_not_the_name(
    listener: _Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _resolve_names_to(monkeypatch, _internal)
    with public_egress_client(timeout=5, allow_loopback=True) as client:
        assert client.get(f"http://once.example:{listener.port}/").status_code == 200
    # One lookup: a second one, at connect, is where a changed answer gets in.
    assert asked == ["once.example"]
    assert listener.seen == ["GET /"]


@pytest.mark.asyncio
async def test_async_client_dials_the_checked_address_not_the_name(
    listener: _Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _resolve_names_to(monkeypatch, _internal)
    async with public_egress_async_client(timeout=5, allow_loopback=True) as client:
        response = await client.get(f"http://once.example:{listener.port}/")
    assert response.status_code == 200
    assert asked == ["once.example"]
    assert listener.seen == ["GET /"]


def test_address_written_in_the_url_is_refused(listener: _Listener) -> None:
    url = f"http://127.0.0.1:{listener.port}/"
    with public_egress_client(timeout=5) as client, pytest.raises(httpx.ConnectError):
        client.get(url)
    assert listener.seen == []
    # Control, and the development allowance: the same URL connects when
    # loopback is allowed.
    with public_egress_client(timeout=5, allow_loopback=True) as client:
        assert client.get(url).status_code == 200
    assert listener.seen == ["GET /"]


def test_loopback_allowance_does_not_admit_other_internal_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolve_names_to(monkeypatch, lambda name: ["10.0.0.5"] if name.endswith(".example") else None)
    with (
        public_egress_client(timeout=2, allow_loopback=True) as client,
        pytest.raises(httpx.ConnectError),
    ):
        client.get("https://internal.example/")


def test_redirect_is_returned_not_followed(listener: _Listener) -> None:
    hop = _Listener(redirect_to=f"http://127.0.0.1:{listener.port}/inner")
    try:
        url = f"http://127.0.0.1:{hop.port}/outer"
        # Control: a client told to follow redirects lands on the listener.
        with httpx.Client(trust_env=False, follow_redirects=True, timeout=5) as plain:
            assert plain.post(url, json={}).status_code == 200
        assert listener.seen == ["POST /inner"]

        with public_egress_client(timeout=5, allow_loopback=True) as client:
            response = client.post(url, json={})
        assert response.status_code == 307
        assert listener.seen == ["POST /inner"], "the redirect was followed"
    finally:
        hop.close()


def test_unresolvable_name_is_a_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_such_host(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", no_such_host)
    with public_egress_client(timeout=2) as client, pytest.raises(httpx.ConnectError):
        client.get("https://nowhere.example/")


@pytest.mark.parametrize(
    ("host", "allow_loopback", "expected"),
    (
        ("collector.example", False, True),
        ("8.8.8.8", False, True),
        ("[2606:4700:4700::1111]", False, True),
        ("10.0.0.5", False, False),
        ("169.254.169.254", False, False),
        ("127.0.0.1", False, False),
        ("127.0.0.1", True, True),
        ("::1", True, True),
        ("10.0.0.5", True, False),
    ),
)
def test_ip_literal_check(host: str, allow_loopback: bool, expected: bool) -> None:
    assert is_public_ip_literal_or_name(host, allow_loopback=allow_loopback) is expected


@pytest.mark.parametrize(
    ("endpoint", "allow_local", "expected"),
    (
        ("https://collector.example/otlp", False, None),
        ("https://8.8.8.8/otlp", False, None),
        ("http://collector.example/otlp", False, "endpoint must use https"),
        ("http://collector.example/otlp", True, "endpoint must use https"),
        ("http://127.0.0.1:4318/v1/traces", True, None),
        ("http://localhost:4318/v1/traces", True, None),
        ("http://127.0.0.1:4318/v1/traces", False, "endpoint must use https"),
        ("http://localhost:4318/v1/traces", False, "endpoint must use https"),
        ("https://127.0.0.1/otlp", False, "endpoint must be a public address"),
        ("https://[::1]/otlp", False, "endpoint must be a public address"),
        ("https://10.0.0.5:8123/", False, "endpoint must be a public address"),
        ("https://10.0.0.5:8123/", True, "endpoint must be a public address"),
        ("https://169.254.169.254/latest", False, "endpoint must be a public address"),
        ("https://[fd00::1]/", False, "endpoint must be a public address"),
        ("ftp://collector.example/", False, "endpoint must be an HTTP URL"),
        ("https://", False, "endpoint must be an HTTP URL"),
    ),
)
def test_endpoint_validation(endpoint: str, allow_local: bool, expected: str | None) -> None:
    assert _validate_http_endpoint(endpoint, allow_local=allow_local) == expected


def test_local_destinations_are_a_development_allowance() -> None:
    assert allows_local_destinations(Settings(environment="test")) is True
    assert allows_local_destinations(Settings(environment="local")) is True
    production = Settings(environment="test").model_copy(update={"environment": "production"})
    assert allows_local_destinations(production) is False


@pytest.mark.parametrize(
    "environment", ("production", "canary", "worker", "staging", "Production", "")
)
def test_deployed_environments_do_not_allow_local(environment: str) -> None:
    settings = Settings(environment="test").model_copy(update={"environment": environment})
    assert allows_local_destinations(settings) is False


def _webhook_destination(endpoint: str) -> BroadcastDestination:
    return BroadcastDestination(
        id="bdst_test",
        workspace_id="ws_test",
        type="webhook",
        name="collector",
        endpoint=endpoint,
        method="POST",
    )


@pytest.mark.asyncio
async def test_connection_test_cannot_reach_an_internal_listener_in_production(
    listener: _Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolve_names_to(monkeypatch, _internal)
    destination = _webhook_destination(f"http://collector.example:{listener.port}/otlp")
    adapter = WebhookOTLPBroadcastAdapter()

    # Control: in the test environment the loopback collector answers.
    ok, message = await broadcast_service.test_destination(destination, Settings(environment="test"))
    assert (ok, message) == (True, "ok")
    assert listener.seen == ["POST /otlp"]

    production = Settings(environment="test").model_copy(update={"environment": "production"})
    ok, message = await broadcast_service.test_destination(destination, production)
    assert (ok, message) == (False, "connection failed")
    assert listener.seen == ["POST /otlp"]

    # Delivery goes through the same door.
    with pytest.raises(httpx.ConnectError):
        adapter.deliver_metadata(destination, _generation(), settle_body={}, settings=production)
    assert listener.seen == ["POST /otlp"]


def _generation() -> Generation:
    return Generation(
        id="gen_test",
        request_id="req_test",
        workspace_id="ws_test",
        key_hash="hash",
        model="test/model",
        provider_name="test",
        app="",
        tokens_prompt=1,
        tokens_completion=1,
        total_cost_microdollars=1,
        usage_type="Credits",
        speed_tokens_per_second=1.0,
        finish_reason="stop",
        status="success",
        streamed=False,
    )


@pytest.mark.asyncio
async def test_connection_test_messages_do_not_describe_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _webhook_destination("https://collector.example/otlp")
    settings = Settings(environment="test")

    class Raises:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        async def test(self, *_args: Any) -> tuple[bool, str]:
            raise self.exc

    cases: list[tuple[Exception, str]] = [
        (httpx.ConnectError("[Errno 61] Connection refused 10.0.0.5:8123"), "connection failed"),
        (httpx.ConnectTimeout("timed out reaching 10.0.0.5"), "connection timed out"),
        (httpx.ReadTimeout("read timed out"), "connection timed out"),
        (httpx.RemoteProtocolError("peer sent: 220 internal-smtp ready"), "connection failed"),
        (ValueError("could not decrypt headers for key kms/internal"), "connection test failed"),
    ]
    for exc, expected in cases:
        monkeypatch.setattr(broadcast_service, "adapter_for", lambda _type, exc=exc: Raises(exc))
        ok, message = await broadcast_service.test_destination(destination, settings)
        assert (ok, message) == (False, expected)


def test_image_fetch_refuses_a_name_that_changes_its_answer(
    listener: _Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route checks the name, then the client connects. A name that is
    public for the check and internal for the connection must not connect."""
    lookups = {"count": 0}

    def changing(name: str) -> list[str] | None:
        if not name.endswith(".example"):
            return None
        lookups["count"] += 1
        return ["8.8.8.8"] if lookups["count"] == 1 else ["127.0.0.1"]

    _resolve_names_to(monkeypatch, changing)
    settings = Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token="test-internal-secret",  # noqa: S106 - fixture token.
    )
    client = TestClient(create_app(settings, init_observability=False))
    url = f"http://image.example:{listener.port}/pixel.png"

    # Control: the second answer does lead to the listener.
    with httpx.Client(trust_env=False, timeout=5) as plain:
        lookups["count"] = 1
        assert plain.get(url).status_code == 200
    assert listener.seen == ["GET /pixel.png"]

    lookups["count"] = 0
    response = client.post(
        "/internal/gateway/fetch-image",
        headers={"x-trustedrouter-internal-token": "test-internal-secret"},
        json={"url": url},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"] == "image fetch: fetch failed"
    assert lookups["count"] == 2, "the check and the connection each looked the name up once"
    assert listener.seen == ["GET /pixel.png"], "the image fetch reached the listener"
