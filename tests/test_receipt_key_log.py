from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections import OrderedDict
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.receipt_keys import (
    GCP_AUDIENCE,
    GCP_ISSUER,
    b64url_encode,
    receipt_attestation_sha256,
    receipt_key_commitment,
    verify_gcp_attestation_chain,
)
from trusted_router.routes import public as public_routes
from trusted_router.services import receipt_key_collector as collector
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_models import ReceiptKey
from trusted_router.storage_postgres import PostgresStore


def _jwk(seed: bytes = b"receipt-key") -> dict[str, str]:
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(hashlib.sha256(seed).digest()),
    }


def _kid(jwk: dict[str, str]) -> str:
    return collector.receipt_kid(jwk)


def _att(payload: dict[str, object]) -> str:
    return cast(str, payload["att"])


def _jwt(payload: dict[str, object]) -> str:
    header = b64url_encode(json.dumps({"alg": "RS256", "kid": "issuer-key"}).encode())
    body = b64url_encode(json.dumps(payload).encode())
    return f"{header}.{body}.c2ln"


def _signed_gcp_jwt(
    private_key: rsa.RSAPrivateKey,
    payload: dict[str, object],
) -> tuple[str, dict[str, object]]:
    header_segment = b64url_encode(
        json.dumps({"alg": "RS256", "kid": "issuer-key"}).encode()
    )
    payload_segment = b64url_encode(json.dumps(payload).encode())
    signing_input = f"{header_segment}.{payload_segment}".encode()
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    numbers = private_key.public_key().public_numbers()

    def integer_bytes(value: int) -> bytes:
        return value.to_bytes((value.bit_length() + 7) // 8, "big")

    jwks: dict[str, object] = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "issuer-key",
                "n": b64url_encode(integer_bytes(numbers.n)),
                "e": b64url_encode(integer_bytes(numbers.e)),
            }
        ]
    }
    return f"{header_segment}.{payload_segment}.{b64url_encode(signature)}", jwks


def _gcp_payload(
    jwk: dict[str, str],
    *,
    kid: str | None = None,
    include_commitment: bool = True,
    marker: str | None = None,
) -> dict[str, object]:
    nonces = [receipt_key_commitment(jwk).hex()] if include_commitment else ["00" * 32]
    att_payload: dict[str, object] = {"eat_nonce": nonces}
    if marker is not None:
        att_payload["marker"] = marker
    return {
        "kid": kid or _kid(jwk),
        "jwk": jwk,
        "att": _jwt(att_payload),
        "att_kind": "gcp-cs-jwt",
    }


def _aws_payload(jwk: dict[str, str]) -> dict[str, object]:
    user_data = bytes(96) + receipt_key_commitment(jwk)
    cose = cbor2.dumps(
        [
            b"protected",
            {},
            cbor2.dumps({"user_data": user_data}),
            b"signature",
        ]
    )
    return {
        "kid": _kid(jwk),
        "jwk": jwk,
        "att": b64url_encode(cose),
        "att_kind": "aws-nitro-cose",
    }


def _azure_payload(jwk: dict[str, str]) -> dict[str, object]:
    runtime = base64.b64encode(
        json.dumps({"receipt_key_fp": receipt_key_commitment(jwk).hex()}).encode()
    ).decode()
    return {
        "kid": _kid(jwk),
        "jwk": jwk,
        "att": _jwt({"x-ms-runtime": runtime}),
        "att_kind": "azure-maa-jwt",
    }


def _one_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        collector,
        "discover_receipt_key_targets",
        lambda _settings: [collector.ReceiptKeyTarget("api.example", "192.0.2.10")],
    )


def test_discovery_resolves_canonical_and_regional_a_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = {
        "api.example": ["192.0.2.1", "192.0.2.2"],
        "regional-endpoint.example": ["198.51.100.7"],
    }

    def getaddrinfo(host: str, *_args, **_kwargs):
        return [
            (2, 1, 6, "", (address, 443))
            for address in addresses[host]
        ]

    monkeypatch.setattr(collector.socket, "getaddrinfo", getaddrinfo)
    settings = Settings(
        environment="test",
        api_base_url="https://api.example/v1",
        synthetic_gateway_region_targets=(
            "region-a=regional-endpoint.example@api-region-a.example"
        ),
    )

    assert collector.discover_receipt_key_targets(settings) == [
        collector.ReceiptKeyTarget("api-region-a.example", "198.51.100.7"),
        collector.ReceiptKeyTarget("api.example", "192.0.2.1"),
        collector.ReceiptKeyTarget("api.example", "192.0.2.2"),
    ]


def test_instance_fetch_connects_by_ip_with_gateway_sni_and_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, *, chunk_size: int):
            assert chunk_size == 64 * 1024
            yield b'{"kid":"sample"}'

        def close(self) -> None:
            captured["closed"] = True

    class Client:
        def __init__(self, **kwargs) -> None:
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def build_request(self, method: str, url: str, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return object()

        def send(self, _request: object, *, stream: bool) -> Response:
            assert stream is True
            return Response()

    monkeypatch.setattr(collector.httpx, "Client", Client)

    assert collector._fetch_receipt_key(  # noqa: SLF001 - transport contract
        collector.ReceiptKeyTarget("api.example", "192.0.2.10"),
        verify_tls=True,
    ) == {"kid": "sample"}
    assert captured["url"] == "https://192.0.2.10/receipt-key"
    assert captured["headers"] == {"Host": "api.example", "Accept": "application/json"}
    assert captured["extensions"] == {"sni_hostname": "api.example"}
    assert captured["closed"] is True


def test_instance_fetch_enforces_size_limit_while_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks_read = 0

    class Response:
        @property
        def content(self) -> bytes:
            raise AssertionError("response must not be buffered")

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, *, chunk_size: int):
            nonlocal chunks_read
            assert chunk_size == 64 * 1024
            for _ in range(100):
                chunks_read += 1
                yield b"x" * chunk_size

        def close(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def build_request(self, *_args, **_kwargs) -> object:
            return object()

        def send(self, _request: object, *, stream: bool) -> Response:
            assert stream is True
            return Response()

    monkeypatch.setattr(collector.httpx, "Client", Client)

    with pytest.raises(ValueError, match="exceeds size limit"):
        collector._fetch_receipt_key(  # noqa: SLF001 - transport contract
            collector.ReceiptKeyTarget("api.example", "192.0.2.10"),
            verify_tls=True,
        )
    assert chunks_read == 33


@pytest.mark.parametrize(
    "payload",
    [
        _gcp_payload(_jwk(), kid="wrong-kid"),
        _gcp_payload(_jwk(), include_commitment=False),
    ],
    ids=["bad-kid", "commitment-absent"],
)
def test_collector_rejects_bad_key_material(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    _one_target(monkeypatch)
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(
        collector, "verify_gcp_attestation_chain", lambda _att, **_kwargs: None
    )
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["errors"] == 1
    assert store.list_receipt_keys() == []


@pytest.mark.parametrize("payload", [_aws_payload(_jwk()), _azure_payload(_jwk())])
def test_unverifiable_chain_kind_is_logged_as_unverified(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    _one_target(monkeypatch)
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["appended"] == 1
    assert store.list_receipt_keys()[0].verified is False


def test_failed_gcp_chain_is_not_appended(monkeypatch: pytest.MonkeyPatch) -> None:
    _one_target(monkeypatch)
    payload = _gcp_payload(_jwk())
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)

    def reject(_att: str) -> None:
        raise ValueError("bad issuer signature")

    monkeypatch.setattr(collector, "verify_gcp_attestation_chain", reject)
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["errors"] == 1
    assert store.list_receipt_keys() == []


def test_gcp_chain_verifier_checks_signature_validity_and_debug_state() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = 1_777_000_000
    claims: dict[str, object] = {
        "iss": GCP_ISSUER,
        "aud": GCP_AUDIENCE,
        "iat": now - 10,
        "nbf": now - 10,
        "exp": now + 60,
        "dbgstat": "disabled-since-boot",
    }
    token, jwks = _signed_gcp_jwt(private_key, claims)
    verify_gcp_attestation_chain(token, now=now, jwks=jwks)

    debug_token, _ = _signed_gcp_jwt(private_key, {**claims, "dbgstat": "enabled"})
    with pytest.raises(ValueError, match="debug status is enabled"):
        verify_gcp_attestation_chain(debug_token, now=now, jwks=jwks)

    expired_token, _ = _signed_gcp_jwt(private_key, {**claims, "exp": now - 301})
    with pytest.raises(ValueError, match="expired"):
        verify_gcp_attestation_chain(expired_token, now=now, jwks=jwks)


def test_good_key_appends_once_and_reobservation_only_advances_last_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_target(monkeypatch)
    payload = _gcp_payload(_jwk())
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(
        collector, "verify_gcp_attestation_chain", lambda _att, **_kwargs: None
    )
    seen = iter(["2026-08-26T00:00:00Z", "2026-08-26T00:05:00Z"])
    monkeypatch.setattr(collector, "iso_now", lambda: next(seen))
    store = InMemoryStore()
    settings = Settings(environment="test", api_base_url="https://api.example/v1")

    first = collector.collect_receipt_keys(settings, store=store)
    second = collector.collect_receipt_keys(settings, store=store)

    assert first["appended"] == 1
    assert second["appended"] == 0
    assert second["refreshed"] == 1
    assert len(store.list_receipt_keys()) == 1
    row = store.list_receipt_keys()[0]
    assert row.first_seen == "2026-08-26T00:00:00Z"
    assert row.last_seen == "2026-08-26T00:05:00Z"
    assert row.verified is True


def test_attestation_hash_uses_raw_decoded_document_bytes() -> None:
    raw_document = b"known raw attestation bytes\x00\xff"
    encoded_document = b64url_encode(raw_document)

    assert receipt_attestation_sha256(encoded_document, "aws-nitro-cose") == b64url_encode(
        hashlib.sha256(raw_document).digest()
    )
    assert receipt_attestation_sha256(encoded_document, "aws-nitro-cose") != b64url_encode(
        hashlib.sha256(encoded_document.encode()).digest()
    )


def test_collector_observes_current_and_every_history_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_target(monkeypatch)
    jwk = _jwk()
    current = _gcp_payload(jwk, marker="current")
    history = [_gcp_payload(jwk, marker=f"history-{index}") for index in range(3)]
    current["att_history"] = [
        {
            "att": _att(item),
            "att_kind": item["att_kind"],
            "att_sha256": receipt_attestation_sha256(
                _att(item), str(item["att_kind"])
            ),
        }
        for item in history
    ]
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(
        collector, "verify_gcp_attestation_chain", lambda _att, **_kwargs: None
    )
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["appended"] == 4
    assert result["errors"] == 0
    assert len(store.list_receipt_keys(kid=_kid(jwk))) == 4


def test_collector_processes_at_most_64_history_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_target(monkeypatch)
    jwk = _jwk()
    current = _gcp_payload(jwk, marker="current")
    history = [_gcp_payload(jwk, marker=f"history-{index}") for index in range(65)]
    current["att_history"] = [
        {
            "att": _att(item),
            "att_kind": item["att_kind"],
            "att_sha256": receipt_attestation_sha256(
                _att(item), str(item["att_kind"])
            ),
        }
        for item in history
    ]
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(
        collector, "verify_gcp_attestation_chain", lambda _att, **_kwargs: None
    )
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["appended"] == 65
    assert result["skipped"] == 1
    assert len(store.list_receipt_keys(kid=_kid(jwk))) == 65


def test_history_never_refreshes_over_the_current_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_target(monkeypatch)
    jwk = _jwk()
    current = _gcp_payload(jwk, marker="current")
    history = _gcp_payload(jwk, marker="history")
    current["att_history"] = [
        {
            "att": _att(history),
            "att_kind": history["att_kind"],
            "att_sha256": receipt_attestation_sha256(
                _att(history), str(history["att_kind"])
            ),
        }
    ]
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(
        collector, "verify_gcp_attestation_chain", lambda _att, **_kwargs: None
    )
    times = iter(["2026-08-26T00:00:10Z", "2026-08-26T00:05:00Z"])
    monkeypatch.setattr(collector, "iso_now", lambda: next(times))
    store = InMemoryStore()
    settings = Settings(environment="test", api_base_url="https://api.example/v1")

    collector.collect_receipt_keys(settings, store=store)
    collector.collect_receipt_keys(settings, store=store)

    rows = store.list_receipt_keys(kid=_kid(jwk))
    current_row = next(row for row in rows if row.att == _att(current))
    history_row = next(row for row in rows if row.att == _att(history))
    assert current_row.last_seen == "2026-08-26T00:05:00Z"
    assert history_row.last_seen == "2026-08-26T00:00:09Z"
    assert rows[0] == current_row


def test_collector_accepts_an_expired_but_authentic_history_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_target(monkeypatch)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = _jwk()
    now = 1_777_000_000
    common_claims: dict[str, object] = {
        "iss": GCP_ISSUER,
        "aud": GCP_AUDIENCE,
        "iat": now - 600,
        "nbf": now - 600,
        "dbgstat": "disabled-since-boot",
        "eat_nonce": [receipt_key_commitment(jwk).hex()],
    }
    current_att, jwks = _signed_gcp_jwt(
        private_key, {**common_claims, "exp": now + 60}
    )
    historical_att, _ = _signed_gcp_jwt(
        private_key, {**common_claims, "exp": now - 301}
    )
    payload: dict[str, object] = {
        "kid": _kid(jwk),
        "jwk": jwk,
        "att": current_att,
        "att_kind": "gcp-cs-jwt",
        "att_history": [
            {
                "att": historical_att,
                "att_kind": "gcp-cs-jwt",
                "att_sha256": receipt_attestation_sha256(
                    historical_att, "gcp-cs-jwt"
                ),
            }
        ],
    }

    def verify(att: str, *, allow_expired: bool = False) -> None:
        verify_gcp_attestation_chain(
            att,
            now=now,
            jwks=jwks,
            allow_expired=allow_expired,
        )

    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(collector, "verify_gcp_attestation_chain", verify)
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["appended"] == 2
    assert result["errors"] == 0
    rows = store.list_receipt_keys(kid=_kid(jwk))
    assert len(rows) == 2
    assert all(row.verified for row in rows)


def test_collector_skips_one_bad_history_document_without_losing_current(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _one_target(monkeypatch)
    jwk = _jwk()
    current = _gcp_payload(jwk, marker="current")
    bad = _gcp_payload(_jwk(b"different-key"), marker="bad-history")
    current["att_history"] = [
        {
            "att": _att(bad),
            "att_kind": bad["att_kind"],
            "att_sha256": receipt_attestation_sha256(_att(bad), str(bad["att_kind"])),
        }
    ]
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(
        collector, "verify_gcp_attestation_chain", lambda _att, **_kwargs: None
    )
    store = InMemoryStore()

    with caplog.at_level(logging.WARNING, logger=collector.__name__):
        result = collector.collect_receipt_keys(
            Settings(environment="test", api_base_url="https://api.example/v1"),
            store=store,
        )

    assert result["appended"] == 1
    assert result["skipped"] == 1
    assert result["errors"] == 0
    assert len(store.list_receipt_keys(kid=_kid(jwk))) == 1
    assert "receipt_key_history_skipped" in caplog.text


def test_history_store_failure_is_counted_as_a_target_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_target(monkeypatch)
    jwk = _jwk()
    current = _gcp_payload(jwk, marker="current")
    history = _gcp_payload(jwk, marker="history")
    current["att_history"] = [
        {
            "att": _att(history),
            "att_kind": history["att_kind"],
            "att_sha256": receipt_attestation_sha256(
                _att(history), str(history["att_kind"])
            ),
        }
    ]
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(
        collector, "verify_gcp_attestation_chain", lambda _att, **_kwargs: None
    )

    class StoreFailsOnHistory(InMemoryStore):
        observations = 0

        def observe_receipt_key(
            self,
            record: ReceiptKey,
            *,
            refresh_last_seen: bool = True,
        ):
            self.observations += 1
            if self.observations > 1:
                raise RuntimeError("database unavailable")
            return super().observe_receipt_key(
                record, refresh_last_seen=refresh_last_seen
            )

    store = StoreFailsOnHistory()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["appended"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == 1
    assert len(store.list_receipt_keys()) == 1


def test_collector_without_att_history_remains_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_target(monkeypatch)
    payload = _gcp_payload(_jwk(), marker="legacy-enclave")
    monkeypatch.setattr(collector, "_fetch_receipt_key", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(collector, "verify_gcp_attestation_chain", lambda _att: None)
    store = InMemoryStore()

    result = collector.collect_receipt_keys(
        Settings(environment="test", api_base_url="https://api.example/v1"),
        store=store,
    )

    assert result["appended"] == 1
    assert result["errors"] == 0
    assert len(store.list_receipt_keys()) == 1


def test_legacy_row_is_served_with_lazy_attestation_hash() -> None:
    jwk = _jwk()
    att = _att(_gcp_payload(jwk, marker="legacy"))
    legacy = ReceiptKey(
        kid=_kid(jwk),
        jwk=jwk,
        att=att,
        att_kind="gcp-cs-jwt",
        plane="api.example",
        first_seen="2026-08-26T00:00:00Z",
        last_seen="2026-08-26T00:05:00Z",
    )
    store = InMemoryStore()
    store.receipt_keys[legacy.kid] = legacy

    row = store.list_receipt_keys()[0]

    assert row.att_sha256 == receipt_attestation_sha256(att, legacy.att_kind)


def test_durable_receipt_key_reads_filter_and_limit_in_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args, **_kwargs) -> None:
        pytest.fail("unbounded entity scan")

    pg_calls: list[tuple[str, tuple[object, ...]]] = []

    class PgCursor:
        def fetchall(self) -> list[tuple[object]]:
            return []

    class PgConn:
        def execute(self, query: str, params: tuple[object, ...]) -> PgCursor:
            pg_calls.append((query, params))
            return PgCursor()

    pg = PostgresStore.__new__(PostgresStore)
    pg._run_transaction = lambda operation: operation(PgConn())  # type: ignore[method-assign]
    monkeypatch.setattr(pg, "_list_entities", forbidden)

    assert pg.list_receipt_keys(kid="missing-kid", limit=7) == []
    assert "kid = %s" in pg_calls[0][0]
    assert "LIMIT %s" in pg_calls[0][0]
    assert pg_calls[0][1] == ("receipt_key", "missing-kid", "missing-kid", 7)

    spanner_calls: list[tuple[str, dict[str, object]]] = []

    class Snapshot:
        def execute_sql(self, query: str, *, params, param_types) -> list[tuple[object]]:
            del param_types
            spanner_calls.append((query, params))
            return []

    class Database:
        @contextmanager
        def snapshot(self):
            yield Snapshot()

    spanner = SpannerBigtableStore.__new__(SpannerBigtableStore)
    spanner._database = Database()
    spanner._param_types = SimpleNamespace(STRING="STRING", INT64="INT64")
    monkeypatch.setattr(spanner, "_list_entities", forbidden)

    assert spanner.list_receipt_keys(kid="missing-kid", limit=7) == []
    assert "kid=@kid" in spanner_calls[0][0]
    assert "LIMIT @limit" in spanner_calls[0][0]
    assert spanner_calls[0][1] == {
        "kind": "receipt_key",
        "kid": "missing-kid",
        "limit": 7,
    }


def test_public_routes_list_versions_with_hash_and_filter_by_kid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwk = _jwk()
    other_jwk = _jwk(b"other-key")
    records = [
        ReceiptKey(
            kid=_kid(jwk),
            jwk={**jwk, "d": "private-material-must-not-escape"},
            att=_att(_gcp_payload(jwk, marker="newest")),
            att_kind="gcp-cs-jwt",
            plane="api.example",
            first_seen="2026-08-26T00:05:00Z",
            last_seen="2026-08-26T00:10:00Z",
            verified=True,
        ),
        ReceiptKey(
            kid=_kid(jwk),
            jwk=jwk,
            att=_att(_gcp_payload(jwk, marker="older")),
            att_kind="gcp-cs-jwt",
            plane="api.example",
            first_seen="2026-08-26T00:00:00Z",
            last_seen="2026-08-26T00:05:00Z",
            verified=True,
        ),
        ReceiptKey(
            kid=_kid(other_jwk),
            jwk=other_jwk,
            att=_att(_gcp_payload(other_jwk, marker="other")),
            att_kind="gcp-cs-jwt",
            plane="api.example",
            first_seen="2026-08-26T00:00:00Z",
            last_seen="2026-08-26T00:01:00Z",
            verified=True,
        ),
    ]
    store = InMemoryStore()
    configure_store(store)

    def list_records(_self, *, limit: int, kid: str | None = None):
        selected = [record for record in records if kid is None or record.kid == kid]
        return selected[:limit]

    monkeypatch.setattr(InMemoryStore, "list_receipt_keys", list_records)
    client = TestClient(
        create_app(
            Settings(environment="test"),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    well_known = client.get("/.well-known/inference-receipt-keys")
    mirror = client.get("/trust/receipt-keys.json")

    assert well_known.status_code == 200
    assert mirror.status_code == 200
    payload = well_known.json()
    assert payload["spec"] == "inference-receipt/1"
    assert payload["degraded"] is False
    assert payload["keys"] == mirror.json()["keys"]
    assert len(payload["keys"]) == 2
    assert set(payload["keys"][0]) == {
        "kid",
        "jwk",
        "att",
        "att_kind",
        "att_sha256",
        "plane",
        "first_seen",
        "last_seen",
        "revoked",
        "verified",
    }
    assert payload["keys"][0]["jwk"] == jwk
    assert payload["keys"][0]["att_sha256"] == receipt_attestation_sha256(
        str(records[0].att), records[0].att_kind
    )
    assert len({item["kid"] for item in payload["keys"]}) == len(payload["keys"])
    assert "s-maxage=300" in well_known.headers["cache-control"]
    assert "s-maxage=3600" not in well_known.headers["cache-control"]
    assert well_known.headers["etag"].startswith('W/"')

    revalidated = client.get(
        "/.well-known/inference-receipt-keys",
        headers={"if-none-match": well_known.headers["etag"]},
    )
    assert revalidated.status_code == 304
    assert revalidated.headers["etag"] == well_known.headers["etag"]

    filtered = client.get("/trust/receipt-keys.json", params={"kid": _kid(jwk)})
    assert filtered.status_code == 200
    assert len(filtered.json()["keys"]) == 2
    assert {item["kid"] for item in filtered.json()["keys"]} == {_kid(jwk)}
    assert [item["att_sha256"] for item in filtered.json()["keys"]] == [
        receipt_attestation_sha256(str(record.att), record.att_kind)
        for record in records[:2]
    ]

    def unavailable(_self, *, limit: int, kid: str | None = None):
        raise RuntimeError(f"storage unavailable at limit {limit}")

    monkeypatch.setattr(InMemoryStore, "list_receipt_keys", unavailable)
    degraded = client.get("/.well-known/inference-receipt-keys")
    assert degraded.status_code == 200
    assert degraded.json()["degraded"] is True
    assert degraded.json()["keys"] == payload["keys"]


def test_public_route_rejects_noncanonical_kids_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    calls = 0

    def list_records(_self, *, limit: int, kid: str | None = None):
        nonlocal calls
        del limit, kid
        calls += 1
        return []

    monkeypatch.setattr(InMemoryStore, "list_receipt_keys", list_records)
    client = TestClient(
        create_app(
            Settings(environment="test"),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    for malformed in ("short", f"{_kid(_jwk())}=", "!" * 43):
        response = client.get("/trust/receipt-keys.json", params={"kid": malformed})
        assert response.status_code == 400
    assert calls == 0


def test_per_kid_receipt_cache_ignores_unknowns_and_is_bounded() -> None:
    cache: OrderedDict[str, list[ReceiptKey]] = OrderedDict()
    public_routes._remember_receipt_key_records(cache, _kid(_jwk(b"unknown")), [])
    assert cache == {}

    first_kid = ""
    for index in range(public_routes._RECEIPT_KEY_CACHE_MAX_KIDS + 1):
        jwk = _jwk(f"cache-{index}".encode())
        kid = _kid(jwk)
        if index == 0:
            first_kid = kid
        public_routes._remember_receipt_key_records(
            cache,
            kid,
            [
                ReceiptKey(
                    kid=kid,
                    jwk=jwk,
                    att=_att(_gcp_payload(jwk)),
                    att_kind="gcp-cs-jwt",
                    plane="api.example",
                    first_seen="2026-08-26T00:00:00Z",
                    last_seen="2026-08-26T00:00:00Z",
                )
            ],
        )

    assert len(cache) == public_routes._RECEIPT_KEY_CACHE_MAX_KIDS
    assert first_kid not in cache


def test_scheduler_route_rejects_anonymous() -> None:
    client = TestClient(
        create_app(
            Settings(
                environment="test",
                internal_gateway_token="receipt-collector-test-token",  # noqa: S106
            ),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    response = client.post("/internal/gateway/receipt-keys/collect")

    assert response.status_code == 401


def test_scheduler_route_collects_and_records_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.routes.internal import gateway

    heartbeats: list[str] = []
    monkeypatch.setattr(
        gateway,
        "collect_receipt_keys",
        lambda _settings: {
            "discovered": 1,
            "fetched": 1,
            "appended": 1,
            "refreshed": 0,
            "skipped": 0,
            "errors": 0,
        },
    )
    monkeypatch.setattr(
        gateway,
        "record_heartbeat",
        lambda name, *, settings: heartbeats.append(name),
    )
    token = "receipt-collector-test-token"  # noqa: S105
    client = TestClient(
        create_app(
            Settings(environment="test", internal_gateway_token=token),
            configure_store_arg=False,
            init_observability=False,
        )
    )

    response = client.post(
        "/v1/internal/gateway/receipt-keys/collect",
        headers={"x-trustedrouter-internal-token": token},
    )

    assert response.status_code == 200
    assert response.json()["appended"] == 1
    assert heartbeats == ["job:receipt-key-collector"]
