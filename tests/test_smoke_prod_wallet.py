"""Opt-in production canary: wallet sign-up is closed.

Run separately from the normal prod smoke suite:

    TR_PROD_WALLET_CANARY=1 uv run pytest tests/test_smoke_prod_wallet.py -q

Production runs with ``TR_WALLET_SIGNUP_ENABLED`` unset, so a wallet that has
no account is refused when it asks for a challenge and nothing is written for
it. The canary creates no user and no workspace.
"""

from __future__ import annotations

import os

import httpx
import pytest
from eth_account import Account

PROD_BASE_URL = os.environ.get("TR_PROD_BASE_URL", "https://trustedrouter.com")
ENABLED = os.environ.get("TR_PROD_WALLET_CANARY") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLED,
    reason="TR_PROD_WALLET_CANARY=1 to probe the prod wallet sign-up gate",
)


def test_prod_refuses_a_first_time_wallet_before_issuing_a_challenge() -> None:
    account = Account.create()
    with httpx.Client(base_url=PROD_BASE_URL, timeout=20.0, follow_redirects=False) as client:
        challenge = client.post("/v1/auth/wallet/challenge", json={"address": account.address})
        assert challenge.status_code == 403, challenge.text
        assert "nonce" not in challenge.text
        assert not client.cookies.get("tr_session")
