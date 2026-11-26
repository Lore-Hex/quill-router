import base64
import json
import secrets
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.config import Settings
from trusted_router.services import managed_key_cutovers as cut
from trusted_router.storage import InMemoryStore


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
        "provider": "fal",
        "generation": "a" * 64,
        "observed_at": int(time.time()),
        "primary": "auto_refill_closed",
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
        "provider": "fal",
        "usage_type": "Credits",
        "route_candidates": [
            {"provider": "minimax", "usage_type": "BYOK"},
            {"provider": "other", "usage_type": "Credits"},
        ],
    }
    assert cut.decisions(settings, store, data) == {"fal": "primary:" + "a" * 64}
    event = delivery(key)
    for _ in range(2):
        cut.accept(settings, store, event)
        assert cut.decisions(settings, store, data) == {"fal": "a" * 64}
    with pytest.raises(ValueError):
        cut.accept(settings, store, delivery(key, primary="refill_disabled"))
    assert cut.decisions(settings, store, data) == {"fal": "a" * 64}


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
        {"provider": "minimax"},
    ],
)
def test_partial_or_stale_receipt_never_promotes(enrolled, changes):
    settings, store, key = enrolled
    with pytest.raises(ValueError):
        cut.accept(settings, store, delivery(key, **changes))
    assert not store.managed_key_promoted("fal", "a" * 64)


def test_wrong_signer_and_wrong_provider(enrolled):
    settings, store, _ = enrolled
    with pytest.raises(ValueError):
        cut.accept(settings, store, delivery(Ed25519PrivateKey.generate()))
    assert not store.managed_key_promoted("fal", "a" * 64)


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


def test_internal_event_requires_dedicated_token_and_signature(client, test_settings, enrolled):
    cfg, _, key = enrolled
    test_settings.managed_key_generations = cfg.managed_key_generations
    test_settings.managed_key_receipt_public_key = cfg.managed_key_receipt_public_key
    test_settings.managed_key_receipt_audience = cfg.managed_key_receipt_audience
    test_settings.internal_gateway_token = secrets.token_hex(16)
    test_settings.managed_key_funding_token = secrets.token_hex(16)
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
        "provider": "fal",
        "generation": "a" * 64,
    }
