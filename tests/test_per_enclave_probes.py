"""Per-enclave health checks behind one anycast hostname.

api-aws.trustedrouter.com is an AWS Global Accelerator record fronting two
Nitro enclave stacks (eu-west-1 and eu-west-3). Every probe sent to that name
lands on whichever region the accelerator picks, so the status page could not
tell "both enclaves healthy" from "one enclave dead".

The enclave mints its TLS certificate INSIDE the TEE with exactly one SAN,
DNS:api-aws.trustedrouter.com — measured live:

    [FAIL] TLS certificate hostname mismatch for
           quill-enclave-nlb-....elb.eu-west-1.amazonaws.com:
           no matching SubjectAlternativeName in ['DNS:api-aws.trustedrouter.com']

That check is correct and stays. A per-enclave probe therefore CONNECTS to
one region's load balancer while SNI and the Host header keep naming the
canonical hostname (SyntheticTarget.connect_host), exactly as
tools/verify-attestation.py does with --api-host X --connect-ip Y.

These tests drive the real probes against a real TLS server that records the
SNI its client presented and the Host header it received, so "connects there,
presents this name" is asserted on the actual socket and TLS handshake rather
than on a mock having been called.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
import ssl
from pathlib import Path
from typing import Any

import cbor2
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.x509.oid import NameOID

from trusted_router.config import Settings, parse_gateway_region_targets
from trusted_router.storage_models import SyntheticProbeSample, utcnow
from trusted_router.synthetic.components import (
    GATEWAY_REGION_TARGET_NAMES,
    applicable_component_definitions,
    sample_component_ids,
)
from trusted_router.synthetic.probes import (
    SyntheticTarget,
    _attested_ssl_context,
    _connect_host_request,
    attestation_nonce_probe,
    configured_targets,
    gateway_latency_phase_probes,
    run_synthetic_once,
    tls_health_probe,
)
from trusted_router.synthetic.status import status_snapshot

API_HOST = "api-aws.trustedrouter.com"
PCR0 = bytes(range(48))
# The two NLBs scripts/deploy/aws_eu_control_plane.sh pins, verbatim.
IRELAND_NLB = "quill-enclave-nlb-6ed55aa238055cfc.elb.eu-west-1.amazonaws.com"
PARIS_NLB = "quill-enclave-nlb-aa2d3be423fa9027.elb.eu-west-3.amazonaws.com"
REGION_TARGETS = f"eu-west-1={IRELAND_NLB},eu-west-3={PARIS_NLB}"

INVALID_KEY_BODY = b'{"error":{"message":"Invalid API key","status":401}}'


# ---------------------------------------------------------------------------
# A real TLS enclave stand-in
# ---------------------------------------------------------------------------


def _write_enclave_cert(directory: Path) -> tuple[Path, Path, bytes]:
    """Mint the single-SAN cert shape the enclave really serves.

    The ONLY SAN is the canonical API hostname — deliberately not 127.0.0.1
    and not the load-balancer name — so a probe that presented the connect
    host as SNI/Host would be talking to a certificate that does not name it.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, API_HOST)])
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
            x509.SubjectAlternativeName([x509.DNSName(API_HOST)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "enclave-cert.pem"
    key_path = directory / "enclave-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path, cert.public_bytes(serialization.Encoding.DER)


def _attestation_document(cert_der: bytes, nonce_hex: str) -> bytes:
    """A Nitro-shaped COSE document binding this connection's cert + nonce."""
    spki = (
        x509.load_der_x509_certificate(cert_der)
        .public_key()
        .public_bytes(encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo)
    )
    payload = {
        "module_id": "i-test-enc0",
        "digest": "SHA384",
        "pcrs": {0: PCR0},
        "nonce": bytes.fromhex(nonce_hex),
        "public_key": spki,
        "user_data": hashlib.sha256(cert_der).digest(),
    }
    return cbor2.dumps([b"\xa1\x01\x38\x22", {}, cbor2.dumps(payload), b"sig" * 32])


class _FakeEnclave:
    """HTTP/1.1-over-TLS server that records SNI and Host per request."""

    def __init__(self, cert_der: bytes) -> None:
        self._cert_der = cert_der
        self.sni_names: list[str | None] = []
        self.host_headers: list[str | None] = []
        self.authorizations: list[str | None] = []
        self.requested_paths: list[str] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self, cert_path: Path, key_path: Path) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        context.set_alpn_protocols(["http/1.1"])

        def record_sni(
            sslobject: ssl.SSLObject, server_name: str | None, _context: ssl.SSLContext
        ) -> None:
            self.sni_names.append(server_name)

        context.sni_callback = record_sni
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
                self.requested_paths.append(path)
                self.host_headers.append(headers.get("host"))
                self.authorizations.append(headers.get("authorization"))
                status, body, content_type = self._respond(path)
                writer.write(
                    (
                        f"HTTP/1.1 {status} STATUS\r\n"
                        f"Content-Type: {content_type}\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Connection: keep-alive\r\n\r\n"
                    ).encode("ascii")
                    + body
                )
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            return
        finally:
            writer.close()

    def _respond(self, path: str) -> tuple[int, bytes, str]:
        if path.startswith("/attestation"):
            _, _, query = path.partition("?")
            nonce = dict(
                part.split("=", 1) for part in query.split("&") if "=" in part
            ).get("nonce", "")
            return 200, _attestation_document(self._cert_der, nonce), "application/cbor"
        # The live enclave protects every route but /attestation.
        return 401, INVALID_KEY_BODY, "application/json"


@pytest.fixture
async def enclave(tmp_path: Path) -> Any:
    cert_path, key_path, cert_der = _write_enclave_cert(tmp_path)
    server = _FakeEnclave(cert_der)
    await server.start(cert_path, key_path)
    try:
        yield server
    finally:
        await server.stop()


def _pinned_target(port: int, *, name: str = "eu-west-1") -> SyntheticTarget:
    """A per-enclave target: canonical URL, one region's endpoint."""
    return SyntheticTarget(
        name,
        f"https://{API_HOST}:{port}/v1",
        name,
        attested=True,
        expected_pcr0=PCR0.hex(),
        connect_host="127.0.0.1",
        paid_probes=False,
    )


# ---------------------------------------------------------------------------
# STEP 2 — the connection goes to connect_host, the name presented does not
# ---------------------------------------------------------------------------


async def test_raw_socket_probe_dials_connect_host_and_presents_api_sni(
    enclave: _FakeEnclave, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latency-phase probe: real resolve, real socket, real handshake."""
    loop = asyncio.get_running_loop()
    resolved: list[str] = []
    real_getaddrinfo = loop.getaddrinfo

    async def recording_getaddrinfo(host: str, port: int, **kwargs: Any) -> Any:
        resolved.append(host)
        return await real_getaddrinfo(host, port, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", recording_getaddrinfo)

    samples = await gateway_latency_phase_probes(
        _pinned_target(enclave.port), monitor_region="eu-west-3", timeout_seconds=10.0
    )

    # Resolved (and therefore connected to) the pinned endpoint...
    assert resolved == ["127.0.0.1"]
    # ...while the TLS handshake and the request both claimed the canonical
    # hostname, which is the only name the enclave certificate carries.
    assert enclave.sni_names == [API_HOST]
    assert [host.split(":")[0] for host in enclave.host_headers if host] == [
        API_HOST,
        API_HOST,
    ]
    assert {sample.status for sample in samples} == {"up"}


async def test_tls_health_probe_honours_connect_host(enclave: _FakeEnclave) -> None:
    async with httpx.AsyncClient(verify=_attested_ssl_context(), timeout=10.0) as client:
        sample = await tls_health_probe(
            client, _pinned_target(enclave.port), monitor_region="eu-west-3"
        )

    assert enclave.sni_names == [API_HOST]
    assert enclave.host_headers == [f"{API_HOST}:{enclave.port}"]
    assert enclave.requested_paths == ["/health"]
    # 401 on /health is what the real enclave answers and still proves TLS
    # termination — the pre-existing tolerance, unchanged.
    assert sample.status == "up"
    assert sample.target == "eu-west-1"


async def test_attestation_probe_honours_connect_host(enclave: _FakeEnclave) -> None:
    """The trap: a pinned TLS probe with an unpinned attestation probe.

    That combination publishes a per-enclave component that LOOKS measured
    and is not — the attestation half would follow anycast and could report
    a healthy neighbour as proof that this enclave is attesting.
    """
    async with httpx.AsyncClient(verify=_attested_ssl_context(), timeout=10.0) as client:
        sample = await attestation_nonce_probe(
            client, _pinned_target(enclave.port), monitor_region="eu-west-3"
        )

    assert enclave.sni_names == [API_HOST]
    assert enclave.host_headers == [f"{API_HOST}:{enclave.port}"]
    assert enclave.requested_paths[0].startswith("/attestation?nonce=")
    # And it verified for real over that pinned connection: fresh nonce,
    # cert binding to the cert THIS socket served, and the PCR0 pin.
    assert sample.status == "up"
    assert sample.error_type is None
    assert sample.attestation_digest == PCR0.hex()


async def test_every_probe_that_opens_a_connection_is_pinned(
    enclave: _FakeEnclave,
) -> None:
    """One dead region must fail BOTH halves of its component.

    Whole-probe coverage, asserted by observation: the enclave sees one
    connection per probe and every one of them presented the canonical name
    while landing on the pinned endpoint.
    """
    target = _pinned_target(enclave.port)
    async with httpx.AsyncClient(verify=_attested_ssl_context(), timeout=10.0) as client:
        await tls_health_probe(client, target, monitor_region="eu-west-3")
        await attestation_nonce_probe(client, target, monitor_region="eu-west-3")
    await gateway_latency_phase_probes(
        target, monitor_region="eu-west-3", timeout_seconds=10.0
    )

    # Every TLS handshake the enclave saw presented the canonical name (two
    # handshakes: httpx pools the health + attestation requests onto one
    # connection, the latency probe opens its own raw socket).
    assert set(enclave.sni_names) == {API_HOST}
    assert {host.split(":")[0] for host in enclave.host_headers if host} == {API_HOST}
    # ...and every request really arrived: /health once, /attestation from
    # the attestation probe plus the latency probe's cold+reused pair.
    assert enclave.requested_paths[0] == "/health"
    assert sum(
        1 for path in enclave.requested_paths if path.startswith("/attestation")
    ) == 3


async def test_a_pinned_target_is_never_sent_the_monitor_api_key(
    enclave: _FakeEnclave,
) -> None:
    """Credentials never travel to an endpoint named by an env var.

    An attested target is dialled with CERT_NONE (the enclave serves a
    self-signed TEE-minted cert, so CA verification can never pass), and the
    paid probes are the only ones carrying the monitor's live API key.
    Sending that key down an unverified connection to a configured hostname
    must not be one flag-flip away, so the guard is on connect_host itself
    and not only on the cost flag.
    """
    from trusted_router.synthetic import probes as probe_module

    target = _pinned_target(enclave.port)
    # Force the cost decision the WRONG way: only the connect_host guard is
    # left to stop the key.
    paid = SyntheticTarget(
        target.name,
        target.api_base_url,
        target.region,
        attested=True,
        expected_pcr0=target.expected_pcr0,
        connect_host=target.connect_host,
        paid_probes=True,
    )
    sent: list[str | None] = []

    async def recording_pong(
        _client: httpx.AsyncClient,
        probe_target: SyntheticTarget,
        *,
        api_key: str | None = None,
        **_kwargs: Any,
    ) -> SyntheticProbeSample:
        # RECORD, never raise. Raising here propagates out of the probe
        # asyncio.gather and leaves the fake enclave's keep-alive handler
        # waiting on a socket nobody closes, so the guard's own failure
        # would hang the suite instead of reporting.
        sent.append(api_key)
        return SyntheticProbeSample(
            id="syn-paid",
            probe_type="openai_sdk_pong",
            target=probe_target.name,
            target_url=probe_target.api_base_url,
            monitor_region="eu-west-3",
            target_region=probe_target.region,
            status="up",
        )

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(probe_module, "openai_chat_pong_probe", recording_pong)
        monkeypatch.setattr(probe_module, "responses_pong_probe", recording_pong)
        async with httpx.AsyncClient(verify=_attested_ssl_context(), timeout=10.0) as client:
            samples = await probe_module._run_target_synthetic_probes(
                client,
                paid,
                monitor_region="eu-west-3",
                api_key="sk-tr-LIVE-MONITOR-KEY",
                model="test-model",
                billing_semaphore=asyncio.Semaphore(1),
            )
    finally:
        monkeypatch.undo()

    assert sent == [], f"monitor API key handed to a pinned target: {sent!r}"
    probe_types = {sample.probe_type for sample in samples}
    assert probe_types & {"openai_sdk_pong", "responses_pong"} == set()
    # The free health/trust probes still ran — this refuses credentials, it
    # does not silently stop measuring the region.
    assert probe_types >= {"tls_health", "attestation_nonce"}
    # And the enclave never saw an Authorization header on any request.
    assert enclave.authorizations
    assert enclave.authorizations == [None] * len(enclave.authorizations)


async def test_attested_target_fails_when_the_cert_binding_is_unverifiable(
    enclave: _FakeEnclave,
) -> None:
    """"Binding unverifiable" is a failure for an attested target, not a pass.

    _response_peer_cert_der documents exactly this, and the evidence helper
    cannot enforce it (non-attested targets legitimately pass None), so the
    probe must. Without it, an attested target — dialled with CERT_NONE —
    would accept a relayed document as verified whenever the peer cert
    could not be read.
    """
    from trusted_router.synthetic import probes as probe_module

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(probe_module, "_response_peer_cert_der", lambda _response: None)
        async with httpx.AsyncClient(verify=_attested_ssl_context(), timeout=10.0) as client:
            sample = await attestation_nonce_probe(
                client, _pinned_target(enclave.port), monitor_region="eu-west-3"
            )
    finally:
        monkeypatch.undo()

    assert sample.status == "trust_degraded"
    assert sample.error_type == "cert_binding_unverifiable"


async def test_unreachable_region_reports_down_not_trust_degraded() -> None:
    """A region that answers nothing is unreachable, not untrustworthy.

    Reported as trust_degraded, the public summary read "Trust Verification
    Degraded: inference may still work" during a total region outage, and
    quill-cloud-proxy's watchdog — whose severity map is up/degraded/down —
    dropped the check entirely, so the outage could not trigger a rollback.
    """
    # Port 1 on loopback: nothing listens, so the connection is refused.
    target = SyntheticTarget(
        "eu-west-1",
        f"https://{API_HOST}:1/v1",
        "eu-west-1",
        attested=True,
        expected_pcr0=PCR0.hex(),
        connect_host="127.0.0.1",
        paid_probes=False,
    )
    async with httpx.AsyncClient(verify=_attested_ssl_context(), timeout=5.0) as client:
        attestation = await attestation_nonce_probe(client, target, monitor_region="eu-west-3")
        tls = await tls_health_probe(client, target, monitor_region="eu-west-3")

    assert tls.status == "down"
    assert attestation.status == "down"
    # The transport error is still recorded, so triage keeps the cause.
    assert attestation.error_type
    assert attestation.error_type != "cert_binding_unverifiable"


async def test_a_trust_failure_is_still_trust_degraded(enclave: _FakeEnclave) -> None:
    """The counterpart: a live gateway serving a bad measurement.

    Down is for "nothing answered". A document that fails the PCR0 pin was
    answered by a running gateway and is a different failure entirely — the
    two must not collapse into one status.
    """
    target = SyntheticTarget(
        "eu-west-1",
        f"https://{API_HOST}:{enclave.port}/v1",
        "eu-west-1",
        attested=True,
        expected_pcr0="ee" * 48,  # not the measurement the enclave reports
        connect_host="127.0.0.1",
        paid_probes=False,
    )
    async with httpx.AsyncClient(verify=_attested_ssl_context(), timeout=10.0) as client:
        sample = await attestation_nonce_probe(client, target, monitor_region="eu-west-3")

    assert sample.status == "trust_degraded"
    assert sample.error_type == "pcr0_mismatch"


async def test_target_without_connect_host_is_untouched(enclave: _FakeEnclave) -> None:
    """Default None must preserve today's behaviour exactly.

    Same server, same probe, but addressed the ordinary way: the request
    names the host it dialled and carries no rewritten Host header.
    """
    target = SyntheticTarget(
        "canonical", f"https://127.0.0.1:{enclave.port}/v1", "eu-west-3", attested=True
    )
    async with httpx.AsyncClient(verify=_attested_ssl_context(), timeout=10.0) as client:
        sample = await tls_health_probe(client, target, monitor_region="eu-west-3")

    # No SNI at all, because an IP literal never gets one — proof the request
    # went out exactly as it always did, with nothing rewritten.
    assert enclave.sni_names == [None]
    assert enclave.host_headers == [f"127.0.0.1:{enclave.port}"]
    assert sample.status == "up"


def test_connect_host_rewrites_only_the_gateway_origin() -> None:
    """connect_host pins ONE origin, not every URL a probe touches.

    A control-plane URL or an off-gateway signed asset URL fetched through
    the same client keeps resolving normally; pinning those to a gateway
    endpoint would measure the wrong service entirely.
    """
    target = _pinned_target(443)
    gateway_url = f"https://{API_HOST}:443/attestation?nonce=ab"
    request_url, headers, extensions = _connect_host_request(target, gateway_url)
    assert request_url == "https://127.0.0.1:443/attestation?nonce=ab"
    assert headers == {"Host": f"{API_HOST}:443"}
    assert extensions == {"sni_hostname": API_HOST}

    for foreign in (
        "https://aws.trustedrouter.com/health",
        "https://storage.example.com/videos/abc.mp4",
    ):
        assert _connect_host_request(target, foreign) == (foreign, {}, {})


def test_no_connect_host_returns_the_request_unchanged() -> None:
    plain = SyntheticTarget("canonical", f"https://{API_HOST}/v1")
    url = f"https://{API_HOST}/health"
    assert _connect_host_request(plain, url) == (url, {}, {})


# ---------------------------------------------------------------------------
# STEP 3 — configuration-driven targets
# ---------------------------------------------------------------------------


def _aws_settings(**overrides: Any) -> Settings:
    """The AWS EU cloud, as scripts/deploy/aws_eu_control_plane.sh deploys it."""
    return Settings(
        environment="test",
        sentry_dsn=None,
        api_base_url=f"https://{API_HOST}/v1",
        primary_region="eu-west-3",
        regions="eu-west-3",
        synthetic_regional_probes_enabled=False,
        synthetic_image_probe_enabled=False,
        synthetic_canonical_attested=True,
        attestation_expected_pcr0="2c" * 48,
        synthetic_control_plane_health_url="https://aws.trustedrouter.com",
        **overrides,
    )


def _gcp_settings() -> Settings:
    return Settings(environment="test", sentry_dsn=None)


def test_unset_configuration_is_exactly_todays_target_list() -> None:
    """The regression fence: no config, no change, on either cloud."""
    aws = configured_targets(_aws_settings())
    assert [target.name for target in aws] == ["canonical"]
    assert aws[0].connect_host is None

    gcp = configured_targets(_gcp_settings())
    assert [target.name for target in gcp] == [
        "canonical",
        "us-central1",
        "us-east4",
        "europe-west4",
    ]
    assert all(target.connect_host is None for target in gcp)
    assert all(target.paid_probes is True for target in gcp)


def test_configured_entries_become_pinned_targets() -> None:
    targets = configured_targets(
        _aws_settings(synthetic_gateway_region_targets=REGION_TARGETS)
    )

    assert [target.name for target in targets] == ["canonical", "eu-west-1", "eu-west-3"]
    canonical, ireland, paris = targets
    assert canonical.connect_host is None
    assert (ireland.connect_host, paris.connect_host) == (IRELAND_NLB, PARIS_NLB)
    for target in (ireland, paris):
        # Same gateway identity, reached at one endpoint: SNI/Host stay
        # correct because api_base_url is the canonical one, and the trust
        # checks are the canonical target's.
        assert target.api_base_url == canonical.api_base_url
        assert target.attested is True
        assert target.expected_pcr0 == canonical.expected_pcr0
        assert target.region == target.name
        # Health and trust, not paid inference: duplicating the pong probes
        # per region per minute would multiply synthetic spend for samples
        # that map to no public component.
        assert target.paid_probes is False


async def test_a_pass_runs_health_and_trust_per_enclave_and_pays_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Which probes each target actually runs, observed end to end.

    Both halves of a per-enclave component (TLS + attestation) must run for
    every configured region — a component measured by only one of them would
    go green on half the evidence. The paid pong pair must NOT: it maps to no
    public component, so per-region copies would multiply synthetic spend
    every minute for a signal nobody reads.
    """
    from trusted_router.synthetic import probes as probe_module

    def fake_probe(probe_type: str) -> Any:
        async def run(
            _client: httpx.AsyncClient,
            target: SyntheticTarget,
            *,
            monitor_region: str,
            **_kwargs: Any,
        ) -> SyntheticProbeSample:
            return SyntheticProbeSample(
                id=f"{probe_type}-{target.name}",
                probe_type=probe_type,
                target=target.name,
                target_url=target.api_base_url,
                monitor_region=monitor_region,
                target_region=target.region,
                status="up",
            )

        return run

    async def no_phase_probes(
        _target: SyntheticTarget, **_kwargs: Any
    ) -> list[SyntheticProbeSample]:
        return []

    monkeypatch.setattr(probe_module, "tls_health_probe", fake_probe("tls_health"))
    monkeypatch.setattr(
        probe_module, "attestation_nonce_probe", fake_probe("attestation_nonce")
    )
    monkeypatch.setattr(probe_module, "gateway_latency_phase_probes", no_phase_probes)
    monkeypatch.setattr(
        probe_module, "control_plane_health_probe", fake_probe("control_plane_health")
    )
    monkeypatch.setattr(probe_module, "openai_chat_pong_probe", fake_probe("openai_sdk_pong"))
    monkeypatch.setattr(probe_module, "responses_pong_probe", fake_probe("responses_pong"))

    samples = await run_synthetic_once(
        _aws_settings(synthetic_gateway_region_targets=REGION_TARGETS),
        monitor_region="eu-west-3",
        api_key="sk-monitor-test",
    )

    by_target: dict[str, set[str]] = {}
    for sample in samples:
        by_target.setdefault(sample.target, set()).add(sample.probe_type)

    assert by_target["eu-west-1"] == {"tls_health", "attestation_nonce"}
    assert by_target["eu-west-3"] == {"tls_health", "attestation_nonce"}
    assert {"openai_sdk_pong", "responses_pong"} <= by_target["canonical"]


def test_gcp_is_untouched_by_the_new_setting() -> None:
    assert configured_targets(_gcp_settings()) == configured_targets(
        Settings(environment="test", sentry_dsn=None, synthetic_gateway_region_targets="")
    )


@pytest.mark.parametrize(
    "raw",
    [
        "eu-west-1",  # no '=' at all
        "eu-west-1=",  # no connect host
        "=nlb.example.com",  # no name
        "eu-west-1=nlb.example.com,",  # empty trailing entry
        "eu-west-1=nlb.example.com,eu-west-1=other.example.com",  # duplicate
        "canonical=nlb.example.com",  # reserved target name
        "control-plane=nlb.example.com",  # reserved target name
        "eu-west-1=https://nlb.example.com",  # scheme would be dropped
        "eu-west-1=nlb.example.com:8443",  # port would be dropped
        "eu-west-1=nlb.example.com/health",  # path would be dropped
    ],
)
def test_malformed_configuration_raises(raw: str) -> None:
    """Loud, not silent.

    Dropping a bad entry would remove that enclave's probe target, which
    UNPUBLISHES its component rather than turning it red — the page would go
    back to being unable to see a dead region, which is the whole defect.
    """
    with pytest.raises(ValueError):
        parse_gateway_region_targets(raw)
    with pytest.raises(ValueError):
        _aws_settings(synthetic_gateway_region_targets=raw)


def test_blank_configuration_is_not_an_error() -> None:
    assert parse_gateway_region_targets("") == ()
    assert parse_gateway_region_targets("   ") == ()


#: Every cloud that pins per-region gateway probes. One entry per control
#: plane; the AWS and Azure scripts each own their own cloud's names, and the
#: components module owns the union.
REGION_TARGET_DEPLOY_SCRIPTS = (
    "scripts/deploy/aws_eu_control_plane.sh",
    "scripts/deploy/azure_control_plane.sh",
)


def _deploy_script_region_targets(
    script_path: str = "scripts/deploy/aws_eu_control_plane.sh",
) -> str:
    """The GATEWAY_REGION_TARGETS default a deploy script really ships.

    Read out of the script rather than restated here. The previous version of
    this test asserted the two hostnames appeared SOMEWHERE in the file and
    then parsed a constant defined in this module, which is not the same
    claim: transposing the two hostnames in the script left every test green
    while the deployment published Ireland's health under Paris's name.
    """
    script = Path(__file__).resolve().parents[1] / script_path
    text = script.read_text()
    # The shell variable must still be the one wired into the setting, or the
    # default below is dead text that configures nothing. Two spellings,
    # because the clouds take env differently: App Runner is handed a JSON
    # document, Container Apps a KEY=VALUE list.
    wired = (
        '"TR_SYNTHETIC_GATEWAY_REGION_TARGETS": "${GATEWAY_REGION_TARGETS}"' in text
        or '"TR_SYNTHETIC_GATEWAY_REGION_TARGETS=${GATEWAY_REGION_TARGETS}"' in text
    )
    assert wired, f"{script_path} does not wire GATEWAY_REGION_TARGETS into the setting"
    match = re.search(
        r'^GATEWAY_REGION_TARGETS="\$\{GATEWAY_REGION_TARGETS:-([^}]*)\}"$',
        text,
        re.MULTILINE,
    )
    assert match is not None, "deploy script no longer defines a GATEWAY_REGION_TARGETS default"
    return match.group(1)


def test_deploy_script_value_parses_to_the_two_regions() -> None:
    """The name->endpoint binding, taken from the script itself.

    This is the invariant the whole feature rests on: `name` becomes the
    target name, its target_region, and the public component; nothing
    downstream ever re-derives it from the endpoint. The two NLB hostnames
    differ only in a 16-hex-char middle segment.
    """
    assert parse_gateway_region_targets(_deploy_script_region_targets()) == (
        ("eu-west-1", IRELAND_NLB),
        ("eu-west-3", PARIS_NLB),
    )


def test_deploy_script_names_are_exactly_the_published_components() -> None:
    """Every configured name must own a component, and vice versa.

    A configured name with no component is a probe whose samples appear on
    no public row at all — the enclave would be measured and the result
    thrown away, which looks identical to not measuring it. A component with
    no configured name is the mirror failure: a public row nothing can ever
    populate.

    The union is across CLOUDS. Each control plane configures only its own
    cloud's endpoints (an AWS plane cannot reach an Azure container group),
    so the assertion has to gather every deploy script rather than assume
    one — which is exactly what it assumed while AWS was the only peer.
    """
    configured: set[str] = set()
    for script in REGION_TARGET_DEPLOY_SCRIPTS:
        entries = parse_gateway_region_targets(_deploy_script_region_targets(script))
        names = {name for name, _ in entries}
        assert names, f"{script} configures no region targets"
        overlap = configured & names
        assert not overlap, f"{script} reuses target name(s) {overlap} from another cloud"
        configured |= names
    assert configured == set(GATEWAY_REGION_TARGET_NAMES)


def test_a_name_that_contradicts_its_endpoints_region_is_rejected() -> None:
    """Transposing two sibling NLB hostnames must not parse.

    Undetected, this publishes Ireland's health as Paris and Paris's as
    Ireland: during an Ireland outage the page tells the operator Paris is
    down and they evacuate the region that is actually healthy.
    """
    with pytest.raises(ValueError, match="does not match its connect host's region"):
        parse_gateway_region_targets(f"eu-west-1={PARIS_NLB},eu-west-3={IRELAND_NLB}")
    # Per-AZ zonal NLB names are the supported way to get finer granularity,
    # so an AZ-suffixed name for the same region still parses.
    assert parse_gateway_region_targets(f"eu-west-1a=eu-west-1a.{IRELAND_NLB}") == (
        ("eu-west-1a", f"eu-west-1a.{IRELAND_NLB}"),
    )
    # A name that is not region-shaped has nothing to contradict.
    assert parse_gateway_region_targets(f"ireland={IRELAND_NLB}") == (("ireland", IRELAND_NLB),)


# ---------------------------------------------------------------------------
# STEP 4 — the components
# ---------------------------------------------------------------------------

REGION_COMPONENT_IDS = ("eu_west_1_gateway", "eu_west_3_gateway")


def _published_ids(settings: Settings) -> tuple[str, ...]:
    return tuple(
        str(definition["id"]) for definition in applicable_component_definitions(settings)
    )


def test_configured_aws_publishes_a_component_per_region() -> None:
    ids = _published_ids(_aws_settings(synthetic_gateway_region_targets=REGION_TARGETS))

    assert ids == (
        "canonical_api",
        "eu_west_1_gateway",
        "eu_west_3_gateway",
        "attestation",
        "billing_settlement",
        "provider_fallback",
    )


def test_published_rows_do_not_claim_to_measure_one_enclave() -> None:
    """The rows are named for what connect_host actually addresses.

    connect_host is a REGION'S NLB, and that NLB fronts an Auto Scaling group
    (quill-cloud-proxy tools/deploy-aws-nitro.sh: target group with an ELB
    health check, ASG across every AZ subnet, max 50). The probe reaches one
    arbitrary healthy member, so a crash-looping AZ-1a enclave is dropped
    from the target group, every probe lands on 1b, and a row claiming to
    measure "the Ireland enclave" would sit green at half capacity — the
    same blind spot this feature exists to remove, one level down.
    """
    published = {
        str(definition["id"]): definition
        for definition in applicable_component_definitions(
            _aws_settings(synthetic_gateway_region_targets=REGION_TARGETS)
        )
    }
    for component_id in REGION_COMPONENT_IDS:
        definition = published[component_id]
        name = str(definition["name"])
        description = str(definition["description"])
        # "Gateway", not "Enclave": the row must not assert singular
        # enclave health it cannot observe.
        assert "Enclave" not in name, name
        assert "Gateway" in name
        # And the description has to say what green does NOT prove.
        assert "not that every" in description, description


def test_unconfigured_deployments_publish_none_of_them() -> None:
    assert not set(REGION_COMPONENT_IDS) & set(_published_ids(_aws_settings()))
    gcp_ids = _published_ids(_gcp_settings())
    assert not set(REGION_COMPONENT_IDS) & set(gcp_ids)
    # GCP's list is byte-identical to today's, in today's order.
    assert gcp_ids == (
        "canonical_api",
        "us_central1_regional_api",
        "us_east4_regional_api",
        "eu_regional_api",
        "attestation",
        "billing_settlement",
        "provider_fallback",
        "image_generation",
    )


def test_region_samples_map_to_their_own_component_only() -> None:
    """Including the attestation half.

    "Attestation" is a shared, service-wide row scoped to the address
    customers resolve. Folding the pinned probes into it averaged a public
    number over targets carrying no traffic: with one region dead and the
    other serving 100% of requests fully attested, the row read
    "Trust degraded, 66.67% (24h)", and a third region would have made the
    same single-region outage read 50%.
    """
    for target, component in (
        ("eu-west-1", "eu_west_1_gateway"),
        ("eu-west-3", "eu_west_3_gateway"),
    ):
        for probe_type in ("tls_health", "attestation_nonce"):
            sample = SyntheticProbeSample(
                id=f"syn-{target}-{probe_type}",
                probe_type=probe_type,
                target=target,
                target_url=f"https://{API_HOST}/health",
                monitor_region="eu-west-3",
                target_region=target,
                status="up",
            )
            assert sample_component_ids(sample) == [component]

    # The canonical target still feeds it, on both clouds — this narrowed
    # the shared row to the served path, it did not empty it.
    canonical = SyntheticProbeSample(
        id="syn-canonical-attestation",
        probe_type="attestation_nonce",
        target="canonical",
        target_url=f"https://{API_HOST}/attestation",
        monitor_region="eu-west-3",
        target_region="eu-west-3",
        status="up",
    )
    assert sample_component_ids(canonical) == ["canonical_api", "attestation"]


def test_a_dead_region_does_not_degrade_the_shared_attestation_row() -> None:
    """Measured end to end over a 24h window, not just at the mapping."""
    now = utcnow()
    samples = []
    for minutes in range(0, 24 * 60, 30):
        created_at = _iso(now, minutes * 60 + 10)
        for probe_type in ("tls_health", "attestation_nonce"):
            samples.append(
                _sample(target="canonical", probe_type=probe_type, status="up",
                        created_at=created_at)
            )
            samples.append(
                _sample(target="eu-west-3", probe_type=probe_type, status="up",
                        created_at=created_at)
            )
            samples.append(
                _sample(target="eu-west-1", probe_type=probe_type, status="down",
                        created_at=created_at)
            )

    snapshot = status_snapshot(
        samples,
        now=now,
        settings=_aws_settings(synthetic_gateway_region_targets=REGION_TARGETS),
    )
    rows = {str(row["id"]): row for row in snapshot["components"]}

    # Every attestation a customer's request could have performed succeeded.
    assert rows["attestation"]["status"] == "up"
    assert rows["attestation"]["uptime_24h_percent"] == 100.0
    # And the dead region is still reported, on its own row.
    assert rows["eu_west_1_gateway"]["uptime_24h_percent"] == 0.0
    assert rows["eu_west_3_gateway"]["uptime_24h_percent"] == 100.0


def _iso(now: dt.datetime, seconds_ago: float) -> str:
    return (now - dt.timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _sample(
    *,
    target: str,
    probe_type: str,
    status: str,
    created_at: str,
    latency: int | None = 21,
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=f"syn-{target}-{probe_type}-{status}-{created_at}",
        probe_type=probe_type,
        target=target,
        target_url=f"https://{API_HOST}/health",
        monitor_region="eu-west-3",
        # Every real target carries its region (probes.py `_sample` copies
        # SyntheticTarget.region), canonical included.
        target_region="eu-west-3" if target == "canonical" else target,
        status=status,
        latency_milliseconds=latency if status == "up" else None,
        error_type=None if status == "up" else "bad_health_response",
        created_at=created_at,
    )


def _dead_region_snapshot(now: dt.datetime) -> dict[str, Any]:
    """One region unreachable, exactly as the real probes report it.

    tls_health answers "down" and attestation_nonce answers "down" too (its
    transport-failure branch) — asserting on a hand-built pair the probes
    cannot emit would prove nothing about the deployed system, so this
    mirrors probes.py precisely.
    """
    created_at = _iso(now, 10)
    samples = [
        _sample(target=target, probe_type=probe_type, status=status, created_at=created_at)
        for target, status in (
            ("canonical", "up"),
            ("eu-west-1", "up"),
            ("eu-west-3", "down"),
        )
        for probe_type in ("tls_health", "attestation_nonce")
    ]
    return status_snapshot(
        samples,
        now=now,
        settings=_aws_settings(synthetic_gateway_region_targets=REGION_TARGETS),
    )


def test_one_dead_region_reddens_exactly_one_component() -> None:
    """The entire point of the feature.

    Anycast keeps answering — Canonical API is green because the accelerator
    routed the canonical probe to the surviving region — and the page still
    says, specifically, that Paris is down.
    """
    snapshot = _dead_region_snapshot(utcnow())
    published = {str(row["id"]): row["status"] for row in snapshot["components"]}

    assert published["eu_west_3_gateway"] == "down"
    assert published["eu_west_1_gateway"] == "up"
    assert published["canonical_api"] == "up"
    # Neither region row is "unknown": both are genuinely measured, which is
    # the bar a public status page has to clear before it may claim anything.
    assert published["eu_west_1_gateway"] != "unknown"
    assert published["eu_west_3_gateway"] != "unknown"


def test_a_dead_region_is_not_reported_as_all_systems_operational() -> None:
    """The banner may not contradict the table underneath it.

    router_core measures the hostname customers resolve, which Global
    Accelerator keeps answering from the surviving region, so it stays green
    through a total single-region outage. Publishing "All Systems
    Operational" above a red region row is how an operator learns to stop
    reading the page.
    """
    snapshot = _dead_region_snapshot(utcnow())

    assert snapshot["overall_status"] == "down"
    assert snapshot["overall_status_label"] == "Major outage"
    assert snapshot["summary"]["headline"] != "All Systems Operational"


def test_machine_consumers_can_see_the_dead_region_in_current_checks() -> None:
    """current.checks is an API, not a second copy of the components table.

    quill-cloud-proxy's watchdog decides per-region rollback from
    checks[].target_region; scoped to router_core (canonical-only) there was
    no eu-west-1 row at ALL, so a region could be flat down with no
    automation able to observe it and nobody paged.

    Deliberately NOT claimed here: tools/synthetic_gate_status.py also reads
    this array but requires openai_sdk_pong and responses_pong, which pinned
    targets do not run (they carry no credentials — see
    test_a_pinned_target_is_never_sent_the_monitor_api_key). A pinned-only
    region therefore still evaluates to "waiting" at that gate, not "down".
    """
    snapshot = _dead_region_snapshot(utcnow())
    checks = snapshot["current"]["checks"]

    by_region: dict[str, set[str]] = {}
    for row in checks:
        by_region.setdefault(str(row["target_region"]), set()).add(str(row["effective_status"]))

    assert by_region["eu-west-3"] >= {"down"}
    assert by_region["eu-west-1"] == {"up"}
    # The statuses the watchdog understands are up/degraded/down; a region
    # outage it silently drops cannot trigger a rollback.
    assert {str(row["effective_status"]) for row in checks} <= {"up", "degraded", "down"}


def test_pinned_probes_do_not_move_the_published_gateway_latency() -> None:
    """The headline numbers describe the path customers take.

    A pinned probe deliberately bypasses Global Accelerator by dialling one
    load balancer directly, so pooling its latency changed a public number
    with no change in what any customer experienced.
    """
    now = utcnow()
    canonical = [
        _sample(target="canonical", probe_type="tls_health", status="up",
                created_at=_iso(now, 10 + index), latency=30)
        for index in range(4)
    ]
    pinned = [
        _sample(target=target, probe_type="tls_health", status="up",
                created_at=_iso(now, 10 + index), latency=latency)
        for index in range(4)
        for target, latency in (("eu-west-3", 12), ("eu-west-1", 45))
    ]

    without = status_snapshot(canonical, now=now, settings=_aws_settings())["headline_metrics"]
    with_pinned = status_snapshot(
        canonical + pinned,
        now=now,
        settings=_aws_settings(synthetic_gateway_region_targets=REGION_TARGETS),
    )["headline_metrics"]

    for key in (
        "in_region_gateway_overhead_p50_milliseconds",
        "in_region_gateway_overhead_sample_count",
        "global_gateway_overhead_p50_milliseconds",
        "global_gateway_overhead_sample_count",
        "gateway_overhead_p50_milliseconds",
    ):
        assert with_pinned[key] == without[key], key
    assert with_pinned["in_region_gateway_overhead_p50_milliseconds"] == 30
