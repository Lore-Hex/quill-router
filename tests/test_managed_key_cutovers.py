import base64
import json
import secrets
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from tests.route_inventory import route_paths
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import managed_key_cutovers as cut
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_models import GatewayAuthorization


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


@pytest.fixture
def enrolled():
    key = Ed25519PrivateKey.generate()
    settings = Settings(
        managed_key_receipt_audience="test-finance",
        managed_key_receipt_public_key=b64(key.public_key().public_bytes_raw()),
        managed_key_generations={"fal": "a" * 64, "minimax": "b" * 64},
    )
    return settings, InMemoryStore(), key


def delivery(key, **changes):
    claims = {
        "audience": "test-finance",
        "provider": "minimax",
        "generation": "b" * 64,
        "observed_at": int(time.time()),
        "primary": "subscription_finally_ended",
        "primary_remaining": "0",
        "standby_available_credit": "10.00",
        **changes,
    }
    raw = json.dumps(claims).encode()
    return cut.Delivery(
        payload=b64(raw), signature=b64(key.sign(b"tr.managed-key-exhaustion/1\0" + raw))
    )


def test_scoped_irreversible_switch(enrolled):
    settings, store, key = enrolled
    data = {
        "provider": "minimax",
        "usage_type": "Credits",
        "route_candidates": [
            {"provider": "fal", "usage_type": "BYOK"},
            {"provider": "other", "usage_type": "Credits"},
        ],
    }
    assert cut.decisions(settings, store, data) == {"minimax": "primary:" + "b" * 64}
    event = delivery(key)
    for _ in range(2):
        cut.accept(settings, store, event)
        assert cut.decisions(settings, store, data) == {"minimax": "b" * 64}
    with pytest.raises(ValueError):
        cut.accept(settings, store, delivery(key, primary="refill_disabled"))
    assert cut.decisions(settings, store, data) == {"minimax": "b" * 64}


@pytest.mark.parametrize(
    "changes",
    [
        {"primary": "refill_disabled"},
        {"primary": "rate_limited"},
        {"primary": "HTTP 403"},
        {"primary": "timeout"},
        {"primary": "weekly_quota_exhausted"},
        {"standby_available_credit": "0"},
        {"primary_remaining": "0.000000001"},
        {"standby_available_credit": True},
        {"primary_remaining": "NaN"},
        {"generation": "0" * 64},
        {"audience": "staging"},
        {"observed_at": 0},
        {"observed_at": 4_000_000_000},
        {"provider": "fal"},
    ],
)
def test_partial_or_stale_receipt_never_promotes(enrolled, changes):
    settings, store, key = enrolled
    with pytest.raises(ValueError):
        cut.accept(settings, store, delivery(key, **changes))
    assert not store.managed_key_promoted("minimax", "b" * 64)


def test_wrong_signer_and_wrong_provider(enrolled):
    settings, store, _ = enrolled
    with pytest.raises(ValueError):
        cut.accept(settings, store, delivery(Ed25519PrivateKey.generate()))
    assert not store.managed_key_promoted("minimax", "b" * 64)


def test_minimax_requires_final_entitlement_end(enrolled):
    settings, store, key = enrolled
    cut.accept(
        settings,
        store,
        delivery(
            key, provider="minimax", generation="b" * 64, primary="subscription_finally_ended"
        ),
    )
    assert store.managed_key_promoted("minimax", "b" * 64)
    assert not store.managed_key_promoted("fal", "a" * 64)


def test_store_failure_never_means_primary(enrolled, monkeypatch):
    settings, store, _ = enrolled

    def unavailable(*args):
        raise TimeoutError

    monkeypatch.setattr(InMemoryStore, "managed_key_promoted", unavailable)
    assert cut.decisions(settings, store, {"provider": "fal", "usage_type": "Credits"}) == {
        "fal": "unavailable"
    }


def test_internal_event_requires_dedicated_token_and_signature(test_settings, enrolled):
    cfg, _, key = enrolled
    test_settings.managed_key_generations = cfg.managed_key_generations
    test_settings.managed_key_receipt_public_key = cfg.managed_key_receipt_public_key
    test_settings.managed_key_receipt_audience = cfg.managed_key_receipt_audience
    test_settings.internal_gateway_token = secrets.token_hex(16)
    test_settings.managed_key_funding_token = secrets.token_hex(16)
    client = TestClient(create_app(test_settings, init_observability=False))
    url = "/v1/internal/gateway/managed-key-funding-event"
    receipt = delivery(key).model_dump()
    assert client.post(url, json=receipt).status_code == 401
    assert (
        client.post(
            url, json=receipt, headers={"Authorization": f"Bearer {test_settings.internal_gateway_token}"}
        ).status_code
        == 401
    )
    headers = {"Authorization": f"Bearer {test_settings.managed_key_funding_token}"}
    forged = delivery(Ed25519PrivateKey.generate()).model_dump()
    assert client.post(url, json=forged, headers=headers).status_code == 403
    assert client.post(url, json=receipt, headers=headers).json() == {
        "accepted": True,
        "provider": "minimax",
        "generation": "b" * 64,
    }


@pytest.mark.parametrize("surface", ["combined", "internal", "public", "control", "observer"])
def test_funding_route_only_on_enrolled_authorities(surface, enrolled):
    cfg, _, _ = enrolled
    cfg.service_surface = surface
    path = "/v1/internal/gateway/managed-key-funding-event"
    for configured in (False, True):
        cfg.managed_key_funding_token = secrets.token_hex(16) if configured else None
        app = create_app(cfg, configure_store_arg=False, init_observability=False)
        assert (path in route_paths(app)) == (
            configured and surface in {"combined", "internal"}
        )


def test_funding_token_must_not_share_gateway_authority():
    token = secrets.token_hex(16)
    with pytest.raises(ValueError, match="TR_INTERNAL_GATEWAY_TOKEN"):
        Settings(internal_gateway_token=token, managed_key_funding_token=token)


@pytest.mark.parametrize("as_candidate", [False, True])
def test_only_authenticated_fal_primary_submit_402_switches(test_settings, monkeypatch, as_candidate):
    endpoint = "fal/flux-1-schnell@fal/prepaid"
    gen = "a" * 64
    test_settings.managed_key_generations = {"fal": gen}
    test_settings.internal_gateway_token = secrets.token_hex(16)
    client = TestClient(create_app(test_settings, init_observability=False))
    auth = GatewayAuthorization("auth-test", "w", "k", "m", "fal", "Credits", 1, endpoint_id=endpoint)
    if as_candidate:
        auth.endpoint_id = "other"
        auth.candidate_endpoint_ids = [endpoint]
    monkeypatch.setattr(InMemoryStore, "get_gateway_authorization", lambda self, id: auth if id == auth.id else None)
    url = "/v1/internal/gateway/fal-primary-402"
    headers = {"x-trustedrouter-internal-token": test_settings.internal_gateway_token}
    body = {"authorization_id": auth.id, "endpoint_id": endpoint, "primary_generation": gen, "upstream_status": 402}
    assert client.post(url, json=body).status_code == 401
    for invalid in ({"primary_generation": "b" * 64}, {"authorization_id": "unissued"}, {"endpoint_id": "another"}):
        assert client.post(url, json=body | invalid, headers=headers).status_code == 403
    for status in (401, 403, 429, 503, "402"):
        assert client.post(url, json=body | {"upstream_status": status}, headers=headers).status_code // 100 == 4
    auth.settled = True
    assert client.post(url, json=body, headers=headers).status_code == 403
    auth.settled = False
    for other in ("fal/flux-1-schnell@fal/byok", "minimax/h3-max@minimax/prepaid"):
        auth.candidate_endpoint_ids.append(other)
        assert client.post(url, json=body | {"endpoint_id": other}, headers=headers).status_code == 403
    data = {"provider": "fal", "usage_type": "Credits"}
    assert cut.decisions(test_settings, STORE, data) == {"fal": "primary:" + gen}
    for _ in range(2):
        assert client.post(url, json=body, headers=headers).json() == {"accepted": True, "generation": gen}
        assert cut.decisions(test_settings, STORE, data) == {"fal": gen}
    assert cut.decisions(test_settings, STORE, data | {"usage_type": "BYOK"}) == {}
    assert not STORE.managed_key_promoted("minimax", gen)
