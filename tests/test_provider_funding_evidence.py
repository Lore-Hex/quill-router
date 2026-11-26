import time
from dataclasses import replace
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.provider_funding_evidence import Pair, VerifiedClosure, on_verified_closure, qualify


@pytest.fixture
def pair():
    return Pair("top", "bottom")


def test_fal_cannot_promote_via_billing_event(pair):
    event = VerifiedClosure("fal", pair.generation("fal"), "auto_refill_closed", int(time.time()))
    with pytest.raises(ValueError, match="unrecognized funding closure"):
        qualify(event, pair, "test", Ed25519PrivateKey.generate(), lambda *_: pytest.fail("billing read"))


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
    gen = pair.generation("minimax")
    event = VerifiedClosure("minimax", gen, "subscription_finally_ended", int(time.time()), Decimal("0"))
    tokens = {region: region + "-test" for region in ("gcp", "aws", "azure")}
    calls = []

    def read(endpoint, reader):
        return {"base_resp": {"status_code": 0}, "model_remains": [], "available_amount": 1}

    def post(cloud, token, receipt):
        calls.append(cloud)
        assert token == tokens[cloud] and set(receipt) == {"payload", "signature"}
        if cloud == "aws":
            raise TimeoutError
        return {"accepted": True, "provider": "minimax", "generation": gen if cloud == "gcp" else "old"}

    result = on_verified_closure(
        event, pair, "test", Ed25519PrivateKey.generate(), tokens, read=read, post=post
    )
    assert result == {"gcp": True, "aws": False, "azure": False}
    assert calls == ["gcp", "aws", "azure"]
