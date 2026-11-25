"""The enclave's idempotency_key_source on authorize (E4 R1).

The field is accepted and classified but never part of a request's identity:
an authorize fingerprint is byte-identical with or without it, so a retry
that carries it replays an authorization made before the enclave sent it
(and the reverse), and no value of it fails an authorize.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.fakes.spanner import make_fake_store
from tests.test_video_derived_routing import URL, payload
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount, GatewayBoot, Workspace

LOOKUP = "/v1/internal/gateway/video/replay-lookup"
GATEWAY_LOGGER = "trusted_router.routes.internal.gateway"
MINTED = "tr-gateway-" + "0123456789abcdef" * 2
IMAGE_DIGEST = "sha256:" + "11" * 32

# Several authorize shapes whose fingerprints reach different branches:
# ordinary chat, tags and attribution, a receipt and hosted-tool cost, media
# (video drops execution fields), and a native Batch key (drops estimates).
BODIES: list[dict[str, Any]] = [
    {"model": "anthropic/claude-opus-4.7", "estimated_input_tokens": 20, "max_output_tokens": 4},
    {
        "model": "anthropic/claude-haiku-4.5", "estimated_input_tokens": 100, "max_tokens": 50,
        "stream": True, "route_type": "chat.completions", "tags": {"team": "a"},
        "user": "u-1", "session_id": "s-1", "app": "demo", "requested_parameters": ["temperature"],
        "provider": {"order": ["anthropic"]}, "region": "us-central1",
    },
    {
        "model": "openai/gpt-5.5", "estimated_input_tokens": 1, "max_output_tokens": 1,
        "route_type": "responses", "inference_receipt": True,
        "additional_cost_reservation_microdollars": 10_000, "invocation_nonce": "n" * 32,
    },
    {
        "model": "minimax/hailuo-3", "route_type": "videos", "estimated_input_tokens": 0,
        "max_output_tokens": 1, "request_fingerprint": "a" * 64, "region": "europe-west4",
        "video_resolution": "720p", "additional_cost_reservation_microdollars": 850_500,
    },
    {
        "model": "anthropic/claude-haiku-4.5", "estimated_input_tokens": 9, "max_output_tokens": 9,
        "route_type": "batch", "idempotency_key": "tr-native-batch:abc",
    },
]


def _fingerprint(fields: dict[str, Any], idempotency_key: str = "retry-1") -> str:
    body = GatewayAuthorizeRequest(api_key_hash="k" * 64, **fields)
    body_dict, _ = gateway._gateway_authorize_body(body)
    return gateway._gateway_authorize_fingerprint(
        workspace_id="ws", key_hash="hash", body=body_dict,
        idempotency_key=fields.get("idempotency_key", idempotency_key),
    )


@pytest.mark.parametrize("fields", BODIES)
@pytest.mark.parametrize("source", ["gateway", "client", "future-value", "x" * 40, ""])
def test_fingerprint_is_byte_identical_with_and_without_the_field(
    fields: dict[str, Any], source: str,
) -> None:
    assert _fingerprint({**fields, "idempotency_key_source": source}) == _fingerprint(fields)


@pytest.mark.parametrize("source", ["gateway", "client", "other"])
def test_field_never_reaches_the_dumped_body(source: str) -> None:
    body = GatewayAuthorizeRequest(api_key_hash="k" * 64, model="m/x", idempotency_key_source=source)
    assert body.idempotency_key_source == source
    assert "idempotency_key_source" not in body.model_dump()
    assert "idempotency_key_source" not in body.model_dump(exclude_none=True)
    assert "idempotency_key_source" not in body.model_dump_json()
    assert "idempotency_key_source" not in gateway._gateway_authorize_body(body)[0]
    # A declared field is not an extra: the async-settle eligibility checks
    # that refuse any model_extra see the same body with or without it.
    assert not body.model_extra


def test_fingerprint_backstop_ignores_the_field_in_a_raw_body() -> None:
    body = {"model": "m/x", "route_type": "chat.completions"}
    fingerprint = gateway._gateway_authorize_fingerprint
    assert fingerprint(workspace_id="ws", key_hash="h", body={**body, "idempotency_key_source": "gateway"}) == (
        fingerprint(workspace_id="ws", key_hash="h", body=body)
    )


def test_positive_control_an_undeclared_field_does_change_the_fingerprint() -> None:
    """The comparison above can fail: an undeclared extra (the field before
    this change, under extra="allow") reaches the dump and forks the hash."""
    fields = BODIES[0]
    assert _fingerprint({**fields, "idempotency_key_sourcf": "gateway"}) != _fingerprint(fields)


@pytest.mark.parametrize(("raw", "stored"), [
    ("future-value", "future-value"),
    ("x" * 17, "x" * 16),
    ("gateway" + "!" * 100, "gateway" + "!" * 9),
    (7, None),
    ({"nested": True}, None),
    (None, None),
])
def test_no_value_is_refused_and_long_values_are_bounded(raw: Any, stored: str | None) -> None:
    body = GatewayAuthorizeRequest(api_key_hash="k" * 64, model="m/x", idempotency_key_source=raw)
    assert body.idempotency_key_source == stored
    assert gateway.idempotency_key_source(
        body, MINTED, boot_verified=True, capability_declared=True,
    ) == "legacy"


def _classify(
    source: Any, key: str | None, *, verified: bool = True, declared: bool = True,
) -> str:
    body = GatewayAuthorizeRequest(api_key_hash="k" * 64, model="m/x", idempotency_key_source=source)
    return gateway.idempotency_key_source(
        body, key, boot_verified=verified, capability_declared=declared,
    )


def test_gateway_needs_the_minted_shape_and_a_verified_boot() -> None:
    # Positive control: a well-formed minted key under a verified boot.
    assert _classify("gateway", MINTED) == "gateway"
    for key in (
        None, "", "client-order-42", MINTED.upper(), MINTED + "0", MINTED[:-1],
        "tr-gateway-" + "g" * 32, " " + MINTED, MINTED + "\n", "x" + MINTED,
    ):
        assert _classify("gateway", key) == "legacy", key
    assert _classify("gateway", MINTED, verified=False) == "legacy"


def test_gateway_needs_the_boots_capability_declaration() -> None:
    # E4 R2: a boot that never declared capabilities.idempotency_key_source
    # at registration cannot have a key treated as gateway-minted on the
    # strength of the body field alone, however well-formed the key.
    assert _classify("gateway", MINTED, declared=True) == "gateway"
    assert _classify("gateway", MINTED, declared=False) == "legacy"
    assert _classify("gateway", MINTED, verified=False, declared=True) == "legacy"
    assert _classify("gateway", MINTED, verified=False, declared=False) == "legacy"
    # The declaration does not make a client-sent or absent mark a gateway one.
    assert _classify("client", "client-order-42", declared=True) == "client"
    assert _classify("client", "client-order-42", declared=False) == "client"
    assert _classify(None, MINTED, declared=True) == "legacy"
    assert _classify("future-value", MINTED, declared=True) == "legacy"


def test_client_unknown_and_absent() -> None:
    assert _classify("client", "client-order-42") == "client"
    assert _classify("client", MINTED, verified=False) == "client"
    for source in (None, "", "Gateway", "GATEWAY", "unkeyed", "future-value"):
        assert _classify(source, MINTED) == "legacy", source


# --- chat authorize over HTTP -------------------------------------------------


def _chat_key(client: TestClient, user_headers: dict[str, str]) -> str:
    created = client.post("/v1/keys", headers=user_headers, json={"name": "gateway"}).json()
    return str(created["data"]["hash"])


def _chat_body(key_hash: str, **extra: Any) -> dict[str, Any]:
    return {
        "api_key_hash": key_hash, "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 20, "max_output_tokens": 4, **extra,
    }


@pytest.mark.parametrize(("first", "retry"), [
    (None, "gateway"), ("gateway", None), (None, "client"), ("client", None),
    ("gateway", "client"), (None, "future-value"), (None, "y" * 64),
])
def test_chat_same_key_replays_across_the_field(
    client: TestClient, user_headers: dict[str, str], first: str | None, retry: str | None,
) -> None:
    key_hash = _chat_key(client, user_headers)
    headers = {"idempotency-key": MINTED}

    def body(source: str | None) -> dict[str, Any]:
        return _chat_body(key_hash, **({"idempotency_key_source": source} if source is not None else {}))

    original = client.post("/v1/internal/gateway/authorize", headers=headers, json=body(first))
    assert original.status_code == 200, original.text
    again = client.post("/v1/internal/gateway/authorize", headers=headers, json=body(retry))
    assert again.status_code == 200, again.text
    assert again.json()["data"]["idempotent_replay"] is True
    assert again.json()["data"]["authorization_id"] == original.json()["data"]["authorization_id"]
    # Positive control: a material change under the same key is still a 409.
    changed = client.post(
        "/v1/internal/gateway/authorize", headers=headers,
        json={**body(retry), "max_output_tokens": 8},
    )
    assert changed.status_code == 409, changed.text


# --- video authorize and read-only lookup over HTTP ---------------------------


@pytest.mark.parametrize(("first", "retry"), [(None, "gateway"), ("gateway", None), (None, "x" * 99)])
def test_video_same_key_replays_across_the_field(
    client: TestClient, inference_key: str, first: str | None, retry: str | None,
) -> None:
    def body(source: str | None) -> dict[str, Any]:
        return payload(inference_key, **({"idempotency_key_source": source} if source is not None else {}))

    original = client.post(URL, json=body(first))
    assert original.status_code == 200, original.text
    authorization_id = original.json()["data"]["authorization_id"]
    again = client.post(URL, json=body(retry))
    assert again.status_code == 200, again.text
    assert again.json()["data"]["idempotent_replay"] is True
    assert again.json()["data"]["authorization_id"] == authorization_id
    lookup = client.post(LOOKUP, json=body(retry))
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["data"]["authorization"]["authorization_id"] == authorization_id
    # Positive control: a different logical request under the key conflicts.
    for endpoint in (URL, LOOKUP):
        conflict = client.post(endpoint, json={**body(retry), "request_fingerprint": "b" * 64})
        assert conflict.status_code == 409, conflict.text


def test_authorize_with_an_unknown_or_overlong_value_is_not_refused(
    client: TestClient, user_headers: dict[str, str],
) -> None:
    key_hash = _chat_key(client, user_headers)
    for index, source in enumerate(["future-value", "z" * 10_000, 12, {"a": 1}, [], True]):
        response = client.post(
            "/v1/internal/gateway/authorize",
            headers={"idempotency-key": f"odd-source-{index}"},
            json=_chat_body(key_hash, idempotency_key_source=source),
        )
        assert response.status_code == 200, (source, response.text)


# --- the source split log -----------------------------------------------------


def _seed_with_boot(
    *, declares_idempotency_key_source: bool = True, declares_settle_outbox: bool = False,
) -> Any:
    store, database = make_fake_store(request_record_write_mode="typed")
    workspace = Workspace(id="ws-key-source", name="Key source", owner_user_id="user-1")
    store._write_entity("workspace", workspace.id, workspace)
    store._write_entity("credit", workspace.id, CreditAccount(workspace_id=workspace.id))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[(workspace.id, 0)] = {
        "workspace_id": workspace.id, "shard": 0, "total_credits": 1_000_000,
        "total_usage": 0, "reserved": 0, "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.api_keys.create(
        workspace_id=workspace.id, name="key-source", creator_user_id=workspace.owner_user_id,
    )
    store.observe_gateway_boot(GatewayBoot(
        kid="boot-key-source", jwk={"kty": "OKP", "crv": "Ed25519", "x": "AA"},
        approved=True, verified=True, image_digest=IMAGE_DIGEST,
        attestation_kind="gcp-cs-jwt", registered_at="2026-10-23T00:00:00Z",
        declares_idempotency_key_source=declares_idempotency_key_source,
        declares_settle_outbox=declares_settle_outbox,
    ))
    configure_store(store)
    return key


def _signed_authorize(key: Any, idempotency_key: str, source: str | None) -> dict[str, Any]:
    request = Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"x-tr-boot-auth", b"kid=boot-key-source,sig=x")],
    })
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, idempotency_key=idempotency_key,
        idempotency_key_source=source, model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100, max_output_tokens=100, route_type="chat.completions",
    )
    return dict(gateway._authorize_gateway_sync(request, body, Settings(environment="test"))["data"])


def _source_lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


@pytest.mark.parametrize(("verified", "declared", "key", "source", "line", "mismatch"), [
    (True, True, MINTED, "gateway", f"source=gateway declared=gateway image_digest={IMAGE_DIGEST}", None),
    (False, True, MINTED, "gateway", "source=legacy declared=gateway image_digest=-", "reason=boot_unverified"),
    (True, True, "client-picked-key", "gateway", f"source=legacy declared=gateway image_digest={IMAGE_DIGEST}",
     "reason=key_shape"),
    # E4 R2: a verified boot that did not declare the capability.
    (True, False, MINTED, "gateway", f"source=legacy declared=gateway image_digest={IMAGE_DIGEST}",
     "reason=capability_undeclared"),
    # The key shape is checked before the declaration, and the boot before
    # the declaration, so each reason names the first failing check.
    (True, False, "client-picked-key", "gateway", f"source=legacy declared=gateway image_digest={IMAGE_DIGEST}",
     "reason=key_shape"),
    (False, False, MINTED, "gateway", "source=legacy declared=gateway image_digest=-", "reason=boot_unverified"),
    (True, False, "client-picked-key", "client", f"source=client declared=client image_digest={IMAGE_DIGEST}", None),
    (True, True, "client-picked-key", "client", f"source=client declared=client image_digest={IMAGE_DIGEST}", None),
    (True, True, MINTED, None, f"source=legacy declared=absent image_digest={IMAGE_DIGEST}", None),
    (True, True, MINTED, "future-value", f"source=legacy declared=other image_digest={IMAGE_DIGEST}", None),
])
def test_source_split_is_logged_per_boot_image_without_the_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    verified: bool, declared: bool, key: str, source: str | None, line: str, mismatch: str | None,
) -> None:
    api_key = _seed_with_boot(declares_idempotency_key_source=declared)
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: verified)
    with caplog.at_level(logging.INFO, logger=GATEWAY_LOGGER):
        _signed_authorize(api_key, key, source)
    assert _source_lines(caplog, "gateway.idempotency_key_source ") == [
        f"gateway.idempotency_key_source {line}",
    ]
    mismatches = _source_lines(caplog, "gateway.idempotency_key_source_mismatch")
    if mismatch is None:
        assert mismatches == []
    else:
        assert len(mismatches) == 1 and mismatch in mismatches[0]
    for record in caplog.records:
        assert key not in record.getMessage()
        assert "future-value" not in record.getMessage()


def test_the_observation_never_fails_an_authorize(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    api_key = _seed_with_boot()
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)

    def broken(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(gateway, "idempotency_key_source", broken)
    data = _signed_authorize(api_key, MINTED, "gateway")
    assert data["authorization_id"]



# --- the boot context carries both R2 declarations ---------------------------


@pytest.mark.parametrize(("key_source", "outbox"), [
    (False, False), (True, False), (False, True), (True, True),
])
def test_authorize_boot_context_carries_the_boots_r2_declarations(
    monkeypatch: pytest.MonkeyPatch, key_source: bool, outbox: bool,
) -> None:
    """What the boot declared at registration reaches the authorize boot
    context, next to heartbeat_declared, so a gate can read it there."""
    api_key = _seed_with_boot(declares_idempotency_key_source=key_source, declares_settle_outbox=outbox)
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    seen: list[dict[str, Any]] = []
    observe = gateway._observe_idempotency_key_source

    def capture(request: Any, body: Any, key: Any, boot_context: dict[str, Any]) -> None:
        seen.append(dict(boot_context))
        observe(request, body, key, boot_context)

    monkeypatch.setattr(gateway, "_observe_idempotency_key_source", capture)
    _signed_authorize(api_key, MINTED, "gateway")
    assert len(seen) == 1
    assert seen[0]["boot_verified"] is True
    assert seen[0]["idempotency_key_source_declared"] is key_source
    assert seen[0]["settle_outbox_declared"] is outbox


def test_authorize_without_a_boot_header_declares_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    api_key = _seed_with_boot(declares_idempotency_key_source=True, declares_settle_outbox=True)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        gateway, "_observe_idempotency_key_source",
        lambda _request, _body, _key, boot_context: seen.append(dict(boot_context)),
    )
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    body = GatewayAuthorizeRequest(
        api_key_hash=api_key.hash, idempotency_key=MINTED, idempotency_key_source="gateway",
        model="anthropic/claude-haiku-4.5", estimated_input_tokens=100, max_output_tokens=100,
        route_type="chat.completions",
    )
    gateway._authorize_gateway_sync(request, body, Settings(environment="test"))
    assert len(seen) == 1
    assert seen[0]["boot_verified"] is False
    assert seen[0]["heartbeat_declared"] is False
    assert seen[0]["idempotency_key_source_declared"] is False
    assert seen[0]["settle_outbox_declared"] is False
