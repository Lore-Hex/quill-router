import base64
import time
from dataclasses import replace
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.provider_funding_evidence import Pair, VerifiedClosure, on_verified_closure, qualify
from trusted_router.config import Settings
from trusted_router.services.managed_key_cutovers import Delivery, accept, decisions
from trusted_router.storage import InMemoryStore


@pytest.fixture
def pair():
    return Pair("top", "bottom", "team-1", "team-2", "read1", "read2")


def test_fal_remaining_credit_first_then_one_way_switch(pair):
    signer = Ed25519PrivateKey.generate()
    gen = pair.generation("fal")
    event = VerifiedClosure("fal", gen, "auto_refill_closed", int(time.time()))
    settings = Settings(
        managed_key_generations={"fal": gen},
        managed_key_receipt_audience="prod-test",
        managed_key_receipt_public_key=base64.b64encode(
            signer.public_key().public_bytes_raw()
        ).decode(),
    )
    store = InMemoryStore()
    primary = Decimal("0.000001")
    calls = []

    def billing(endpoint, key):
        calls.append((endpoint, key))
        return {
            "username": "team-1" if key == "read1" else "team-2",
            "credits": {
                "currency": "USD",
                "current_balance": primary if key == "read1" else Decimal("10"),
            },
        }

    assert qualify(event, pair, "prod-test", signer, billing) is None
    assert calls == [("fal", "read1")]
    primary = Decimal("0")
    receipt = qualify(event, pair, "prod-test", signer, billing)
    assert receipt is not None
    assert calls[-2:] == [("fal", "read1"), ("fal", "read2")]
    accept(settings, store, Delivery(**receipt))
    assert decisions(settings, store, {"provider": "fal", "usage_type": "Credits"}) == {"fal": gen}
    assert decisions(settings, store, {"provider": "fal", "usage_type": "BYOK"}) == {}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"username": "other", "credits": {"currency": "USD", "current_balance": 0}},
        {"username": "team-1", "credits": {"currency": "USD"}},
        {"username": "team-1", "credits": {"currency": "USD", "current_balance": False}},
        {"username": "team-1", "credits": {"currency": "EUR", "current_balance": 0}},
    ],
)
def test_fal_unavailable_or_unbound_balance_not_zero(pair, body):
    event = VerifiedClosure("fal", pair.generation("fal"), "auto_refill_closed", int(time.time()))
    with pytest.raises(ValueError):
        qualify(event, pair, "test", Ed25519PrivateKey.generate(), lambda *_: body)


def test_minimax_requires_all_included_and_pack_credit_to_be_gone(pair):
    event = VerifiedClosure(
        "minimax", pair.generation("minimax"), "subscription_finally_ended", int(time.time())
    )
    signer = Ed25519PrivateKey.generate()
    for pack in (None, Decimal("0.01")):
        assert (
            qualify(
                replace(event, remaining_pack_credit=pack),
                pair,
                "test",
                signer,
                lambda *_: pytest.fail("unknown or positive pack credit"),
            )
            is None
        )
    ended = replace(event, remaining_pack_credit=Decimal("0"))
    for rows in (None, [{"current_interval_status": 2, "current_weekly_status": 2}]):
        assert (
            qualify(
                ended,
                pair,
                "test",
                signer,
                lambda *_, r=rows: {"base_resp": {"status_code": 0}, "model_remains": r},
            )
            is None
        )

    def billing(endpoint, key):
        assert (endpoint, key) in {("plan", "top"), ("wallet", "bottom")}
        return {
            "base_resp": {"status_code": 0},
            "model_remains": [],
            "available_amount": Decimal("1"),
        }

    assert qualify(ended, pair, "test", signer, billing) is not None


def test_real_event_handler_never_claims_partial_delivery(pair):
    gen = pair.generation("fal")
    event = VerifiedClosure("fal", gen, "auto_refill_closed", int(time.time()))
    tokens = {region: region + "-test" for region in ("gcp", "aws", "azure")}
    calls = []

    def read(endpoint, reader):
        primary = reader == "read1"
        return {
            "username": "team-1" if primary else "team-2",
            "credits": {"currency": "USD", "current_balance": 0 if primary else 1},
        }

    def post(cloud, token, receipt):
        calls.append(cloud)
        assert token == tokens[cloud] and set(receipt) == {"payload", "signature"}
        if cloud == "aws":
            raise TimeoutError
        return {"accepted": True, "provider": "fal", "generation": gen if cloud == "gcp" else "old"}

    result = on_verified_closure(
        event, pair, "test", Ed25519PrivateKey.generate(), tokens, read=read, post=post
    )
    assert result == {"gcp": True, "aws": False, "azure": False}
    assert calls == ["gcp", "aws", "azure"]
