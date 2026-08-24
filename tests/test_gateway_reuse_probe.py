"""gateway_reused_path must measure the warm path real traffic takes.

The probe opens a raw HTTP/1.1-over-TLS connection and checks whether a
second request can ride the same socket. It used to send both requests to
/health on every cloud, and reported a permanent false red on the AWS EU
status page.

Ground truth, measured live against the real gateways (curl --http1.1 and a
two-request raw socket, 2026-08-01):

    api.trustedrouter.com      /health                 200 keep-alive  reuse OK
    api-us-central1…           /health                 200 keep-alive  reuse OK
    api-aws.trustedrouter.com  /health    (no key)     401 Connection: close
    api-aws.trustedrouter.com  /health    (any bearer) 404 Connection: close
    api-aws.trustedrouter.com  /attestation            200 keep-alive  reuse OK

The attested gateway terminates TLS inside the enclave and protects every
route except /attestation, hanging up on everything it does not serve, so
/health can NEVER measure reuse there. Sending the monitor's API key does
not help and makes it worse: /health is not a route on that gateway at all,
so an authenticated request gets 404 + close, which would have dragged the
cold path red too.

/attestation is not a workaround — it is the real warm path. G6 session
binding REQUIRES a pinned client to send its prompt on the same TLS session
whose exporter was attested, so attest-then-reuse is exactly what every AWS
client does.

These tests drive the real probe against a real TLS server, so the raw
request framing, the route selection, and keep-alive handling are all
exercised for real rather than mocked.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import ssl
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from trusted_router.storage_models import SyntheticProbeSample
from trusted_router.synthetic.probes import SyntheticTarget, gateway_latency_phase_probes

OK_BODY = b'{"status":"ok"}'
INVALID_KEY_BODY = b'{"error":{"message":"Invalid API key","status":401}}'
NOT_FOUND_BODY = b'{"error":{"message":"route not found","source":"router","status":404}}'
# A real Nitro attestation document measured 4648 bytes — deliberately over
# the 4 KB bound the /health reader used, which would have rejected it as a
# malformed response and reported a phantom ValueError instead of reuse.
ATTESTATION_DOC = b"\xd2\x84\x45" + b"\xa5" * 4645

# (path, authorization) -> (status, body, connection, content_type)
Responder = Callable[[str, str | None], tuple[int, bytes, str, str]]


def _write_self_signed(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        # CA:TRUE so the non-attested tests can load this same cert as a
        # trust anchor and exercise REAL CA verification, the way the GCP
        # gateway is actually probed.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _FakeGateway:
    """Minimal HTTP/1.1-over-TLS server with real keep-alive semantics."""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.requested_paths: list[str] = []
        self.authorization_headers: list[str | None] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self, cert_path: Path, key_path: Path) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        context.set_alpn_protocols(["http/1.1"])
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=context)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    return
                path = request_line.decode("latin-1").split(" ")[1]
                headers: dict[str, str] = {}
                while True:
                    line = await reader.readline()
                    if line in {b"\r\n", b"\n", b""}:
                        break
                    field, _, value = line.decode("latin-1").partition(":")
                    headers[field.strip().casefold()] = value.strip()
                authorization = headers.get("authorization")
                self.requested_paths.append(path)
                self.authorization_headers.append(authorization)
                status, body, connection, content_type = self._responder(path, authorization)
                writer.write(
                    (
                        f"HTTP/1.1 {status} STATUS\r\n"
                        f"Content-Type: {content_type}\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        f"Connection: {connection}\r\n\r\n"
                    ).encode("ascii")
                    + body
                )
                await writer.drain()
                if connection == "close":
                    return
        except (ConnectionError, ssl.SSLError):
            return
        finally:
            writer.close()


async def _run_probe(
    responder: Responder,
    *,
    cert_path: Path,
    key_path: Path,
    attested: bool,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[dict[str, SyntheticProbeSample], _FakeGateway]:
    gateway = _FakeGateway(responder)
    await gateway.start(cert_path, key_path)
    if not attested:
        # The non-attested path uses ssl.create_default_context(), i.e. real
        # CA verification and hostname checking. Point it at this server's
        # cert as the trust anchor rather than weakening the check.
        assert monkeypatch is not None
        trusting = ssl.create_default_context(cafile=str(cert_path))
        monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: trusting)
    try:
        samples = await gateway_latency_phase_probes(
            SyntheticTarget(
                "canonical",
                f"https://127.0.0.1:{gateway.port}/v1",
                "eu-west-3" if attested else "us-central1",
                attested=attested,
            ),
            monitor_region="eu-west-3" if attested else "us-central1",
            timeout_seconds=10.0,
        )
    finally:
        await gateway.stop()
    return {sample.probe_type: sample for sample in samples}, gateway


@pytest.fixture
def tls_material(tmp_path: Path) -> tuple[Path, Path]:
    return _write_self_signed(tmp_path)


def _nitro_gateway(path: str, authorization: str | None) -> tuple[int, bytes, str, str]:
    """The live AWS Nitro enclave, reproduced exactly as measured.

    /attestation is the only anonymous route and the only one that keeps the
    socket alive. Everything else is 401 (no bearer) or 404 (any bearer),
    both with `Connection: close`.
    """
    if path.startswith("/attestation"):
        return 200, ATTESTATION_DOC, "keep-alive", "application/cbor"
    if authorization is None:
        return 401, INVALID_KEY_BODY, "close", "application/json"
    return 404, NOT_FOUND_BODY, "close", "application/json"


def _gcp_gateway(_path: str, _authorization: str | None) -> tuple[int, bytes, str, str]:
    """GCP answers /health 200 + keep-alive to anyone."""
    return 200, OK_BODY, "keep-alive", "application/json"


@pytest.mark.asyncio
async def test_attested_gateway_reuse_is_measured_on_the_route_it_keeps_warm(
    tls_material: tuple[Path, Path],
) -> None:
    cert_path, key_path = tls_material

    samples, gateway = await _run_probe(
        _nitro_gateway, cert_path=cert_path, key_path=key_path, attested=True
    )

    assert samples["gateway_cold_path"].status == "up"
    assert samples["gateway_reused_path"].status == "up"
    assert samples["gateway_reused_path"].error_type is None
    assert samples["gateway_reused_path"].connection_reused is True
    # Reuse was genuinely exercised: two requests, one socket.
    assert gateway.requested_paths == ["/attestation", "/attestation"]


@pytest.mark.asyncio
async def test_attested_probe_never_asks_the_enclave_for_health(
    tls_material: tuple[Path, Path],
) -> None:
    """Regression pin for the exact defect this fix closes.

    /health on the attested gateway is 401+close anonymously and 404+close
    with a bearer. Probing it there can only ever produce a false red, so
    the probe must not go near it — and must not send a credential either.
    """
    cert_path, key_path = tls_material

    samples, gateway = await _run_probe(
        _nitro_gateway, cert_path=cert_path, key_path=key_path, attested=True
    )

    assert not any(path.startswith("/health") for path in gateway.requested_paths)
    assert gateway.authorization_headers == [None, None]
    assert samples["gateway_cold_path"].target_url.endswith("/attestation")
    assert samples["gateway_reused_path"].target_url.endswith("/attestation")
    # The 404 that authenticating would have produced is unambiguously red,
    # which is why it must never be requested.
    assert samples["gateway_cold_path"].http_status == 200


@pytest.mark.asyncio
async def test_attestation_document_larger_than_the_health_bound_is_read(
    tls_material: tuple[Path, Path],
) -> None:
    """A 4648-byte doc must not trip the 4 KB /health response bound."""
    cert_path, key_path = tls_material

    samples, _ = await _run_probe(
        _nitro_gateway, cert_path=cert_path, key_path=key_path, attested=True
    )

    assert len(ATTESTATION_DOC) > 4096
    assert samples["gateway_reused_path"].status == "up"
    assert samples["gateway_cold_path"].error_type is None


@pytest.mark.asyncio
async def test_gcp_gateway_still_probes_health_and_reports_reuse_up(
    tls_material: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GCP must be completely unaffected: same route, same verdict."""
    cert_path, key_path = tls_material

    samples, gateway = await _run_probe(
        _gcp_gateway,
        cert_path=cert_path,
        key_path=key_path,
        attested=False,
        monkeypatch=monkeypatch,
    )

    assert gateway.requested_paths == ["/health", "/health"]
    assert gateway.authorization_headers == [None, None]
    assert samples["gateway_cold_path"].status == "up"
    assert samples["gateway_reused_path"].status == "up"
    assert samples["gateway_reused_path"].error_type is None
    assert samples["gateway_reused_path"].connection_reused is True


@pytest.mark.asyncio
async def test_connection_close_on_a_served_request_is_a_real_failure(
    tls_material: tuple[Path, Path],
) -> None:
    """The trap this fix must not fall into.

    Tolerating `Connection: close` outright would make the check green
    forever. A gateway that SERVES the request and still refuses to keep the
    connection has genuinely broken reuse, and must stay red.
    """
    cert_path, key_path = tls_material

    def never_reuses(_path: str, _auth: str | None) -> tuple[int, bytes, str, str]:
        return 200, ATTESTATION_DOC, "close", "application/cbor"

    samples, _ = await _run_probe(
        never_reuses, cert_path=cert_path, key_path=key_path, attested=True
    )

    assert samples["gateway_cold_path"].status == "up"
    assert samples["gateway_reused_path"].status == "down"
    assert samples["gateway_reused_path"].error_type == "connection_not_reusable"
    assert samples["gateway_reused_path"].connection_reused is False


@pytest.mark.asyncio
async def test_server_that_hangs_up_mid_stream_is_red(
    tls_material: tuple[Path, Path],
) -> None:
    """Advertising keep-alive and then closing is broken reuse, not green."""
    cert_path, key_path = tls_material

    calls = {"n": 0}

    def lies_about_keep_alive(_path: str, _auth: str | None) -> tuple[int, bytes, str, str]:
        calls["n"] += 1
        # Says keep-alive, but the harness closes after the first response
        # because the SECOND response claims close before any body arrives.
        if calls["n"] == 1:
            return 200, ATTESTATION_DOC, "keep-alive", "application/cbor"
        return 503, b'{"error":{"message":"gone"}}', "close", "application/json"

    samples, _ = await _run_probe(
        lies_about_keep_alive, cert_path=cert_path, key_path=key_path, attested=True
    )

    assert samples["gateway_reused_path"].status == "down"


@pytest.mark.asyncio
async def test_rejected_request_is_not_measurable_rather_than_red_or_green(
    tls_material: tuple[Path, Path],
) -> None:
    """A request the gateway refuses cannot prove anything about reuse.

    Calling it `down` is a false red; calling it `up` is a false green. It
    gets an explicit not-measurable state instead.
    """
    cert_path, key_path = tls_material

    def rejects_everything(_path: str, _auth: str | None) -> tuple[int, bytes, str, str]:
        return 401, INVALID_KEY_BODY, "close", "application/json"

    samples, _ = await _run_probe(
        rejects_everything, cert_path=cert_path, key_path=key_path, attested=True
    )

    cold = samples["gateway_cold_path"]
    reused = samples["gateway_reused_path"]
    # 401 tolerance is intact for reachability: DNS/TCP/TLS/TTFB all worked.
    assert cold.status == "up"
    assert cold.http_status == 401
    assert reused.status == "unknown"
    assert reused.error_type == "reuse_not_measurable_request_rejected"
    assert reused.latency_milliseconds is None


@pytest.mark.asyncio
async def test_reuse_is_not_green_when_the_reused_request_is_rejected(
    tls_material: tuple[Path, Path],
) -> None:
    """False-green guard.

    The socket was reused, but the gateway refused the request that rode it.
    Reachability tolerates a 401; the REUSE verdict must not — otherwise the
    page asserts the warm path works on a request nobody served.
    """
    cert_path, key_path = tls_material

    calls = {"n": 0}

    def rejects_the_second(_path: str, _auth: str | None) -> tuple[int, bytes, str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, ATTESTATION_DOC, "keep-alive", "application/cbor"
        return 401, INVALID_KEY_BODY, "keep-alive", "application/json"

    samples, _ = await _run_probe(
        rejects_the_second, cert_path=cert_path, key_path=key_path, attested=True
    )

    reused = samples["gateway_reused_path"]
    assert reused.status == "down"
    assert reused.error_type == "bad_health_response"
    assert reused.connection_reused is True


@pytest.mark.asyncio
async def test_not_measurable_reuse_sample_is_ignored_by_public_rollups() -> None:
    """Structural proof that `unknown` here cannot page or paint anything.

    Both halves matter: the sample maps to no component and no SLO class,
    AND its hourly rollup does not render a public event row. The second
    half is not free — component-less rollups used to surface as
    "Uncategorized — Major outage" on the public page.
    """
    from trusted_router.synthetic.components import (
        sample_component_ids,
        sample_slo_class_ids,
    )
    from trusted_router.synthetic.rollups import new_rollup_for_sample, sample_rollup_ids
    from trusted_router.synthetic.status import _recent_events

    sample = SyntheticProbeSample(
        id="syn-reuse-na",
        probe_type="gateway_reused_path",
        target="canonical",
        target_url="https://api-aws.trustedrouter.com/attestation",
        monitor_region="eu-west-3",
        status="unknown",
        error_type="reuse_not_measurable_request_rejected",
        created_at="2026-08-01T12:00:00Z",
    )

    assert sample_component_ids(sample) == []
    assert sample_slo_class_ids(sample) == []

    rollups = [
        new_rollup_for_sample(sample, period=period, component=component)
        for period, component in sample_rollup_ids(sample)
    ]
    # It really does land in the uncategorized bucket — that is the thing
    # that used to get published.
    assert {rollup.component for rollup in rollups} == {"uncategorized"}

    now = dt.datetime(2026, 8, 1, 12, 30, tzinfo=dt.UTC)
    events = _recent_events([sample], rollups=rollups, now=now)
    assert events == []


@pytest.mark.asyncio
async def test_a_real_reuse_failure_is_still_recorded_even_though_unlabelled() -> None:
    """Suppressing the public row must not suppress the measurement.

    connection_not_reusable is a genuine failure. It maps to no public
    component, so it does not paint an "Uncategorized — Major outage" row —
    but the sample and its rollup still carry the red, which is what the
    latency-anatomy table and any future alerting read.
    """
    from trusted_router.synthetic.rollups import new_rollup_for_sample, sample_rollup_ids
    from trusted_router.synthetic.status import _recent_events

    sample = SyntheticProbeSample(
        id="syn-reuse-down",
        probe_type="gateway_reused_path",
        target="canonical",
        target_url="https://api-aws.trustedrouter.com/attestation",
        monitor_region="eu-west-3",
        status="down",
        error_type="connection_not_reusable",
        created_at="2026-08-01T12:00:00Z",
    )

    rollups = [
        new_rollup_for_sample(sample, period=period, component=component)
        for period, component in sample_rollup_ids(sample)
    ]
    hourly = next(rollup for rollup in rollups if rollup.period == "hour")
    assert hourly.down_count == 1
    assert hourly.error_counts == {"connection_not_reusable": 1}

    now = dt.datetime(2026, 8, 1, 12, 30, tzinfo=dt.UTC)
    assert _recent_events([sample], rollups=rollups, now=now) == []


@pytest.mark.asyncio
async def test_cold_path_still_times_every_connection_phase(
    tls_material: tuple[Path, Path],
) -> None:
    """Route selection must not cost the diagnostic timing this probe exists for."""
    cert_path, key_path = tls_material

    samples, _ = await _run_probe(
        _nitro_gateway, cert_path=cert_path, key_path=key_path, attested=True
    )

    cold = samples["gateway_cold_path"]
    assert cold.dns_milliseconds is not None
    assert cold.tcp_connect_milliseconds is not None
    assert cold.tls_handshake_milliseconds is not None
    assert cold.ttfb_milliseconds is not None
    assert cold.connection_reused is False
    assert cold.protocol == "http/1.1"


@pytest.mark.asyncio
async def test_unreachable_gateway_does_not_blame_connection_reuse(
    tls_material: tuple[Path, Path],
) -> None:
    """A 500 is a request failure, not a reuse failure — report it as one."""
    cert_path, key_path = tls_material

    def broken(_path: str, _auth: str | None) -> tuple[int, bytes, str, str]:
        return 500, b'{"error":{"message":"boom"}}', "keep-alive", "application/json"

    samples, _ = await _run_probe(
        broken, cert_path=cert_path, key_path=key_path, attested=True
    )

    assert samples["gateway_cold_path"].status == "down"
    assert samples["gateway_reused_path"].status == "down"
    assert samples["gateway_reused_path"].error_type == "bad_health_response"
