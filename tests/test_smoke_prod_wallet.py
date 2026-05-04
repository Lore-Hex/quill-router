"""Opt-in production canary for wallet-only SIWE login.

Run separately from the normal prod smoke suite:

    TR_PROD_WALLET_CANARY=1 uv run pytest tests/test_smoke_prod_wallet.py -q

It creates a throwaway wallet-only user/workspace in production and verifies
that MetaMask-style auth reaches the console without an email gate and with
zero credits.
"""

from __future__ import annotations

import os

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

PROD_BASE_URL = os.environ.get("TR_PROD_BASE_URL", "https://trustedrouter.com")
ENABLED = os.environ.get("TR_PROD_WALLET_CANARY") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="TR_PROD_WALLET_CANARY=1 to create a prod throwaway wallet session",
)


def test_prod_wallet_siwe_reaches_console_without_email_gate() -> None:
    account = Account.create()
    with httpx.Client(base_url=PROD_BASE_URL, timeout=20.0, follow_redirects=False) as client:
        challenge = client.post("/v1/auth/wallet/challenge", json={"address": account.address})
        assert challenge.status_code == 200, challenge.text
        challenge_data = challenge.json()["data"]
        signature = Account.sign_message(
            encode_defunct(text=challenge_data["message"]),
            private_key=account.key,
        ).signature.hex()

        verify = client.post(
            "/v1/auth/wallet/verify",
            json={
                "address": account.address,
                "signature": "0x" + signature,
                "nonce": challenge_data["nonce"],
            },
        )
        assert verify.status_code == 200, verify.text
        verify_data = verify.json()["data"]
        assert verify_data["redirect"] == "/console/api-keys"
        assert verify_data["state"] == "active"
        assert verify_data["email_required"] is False
        assert client.cookies.get("tr_session")

        console = client.get("/console/api-keys")
        assert console.status_code == 200, console.text
        assert "API Keys" in console.text
        credits = client.get("/console/credits")
        assert credits.status_code == 200, credits.text
        assert "$0.00" in credits.text
