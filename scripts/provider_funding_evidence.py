"""Billing-verifier side of ordered key cutover. No schedule or inference calls.

Invoke only from an authenticated provider lifecycle adapter. The closure is
NOT a public webhook schema: never construct it from request errors, timers,
an HTTP body sent by a customer, or a card freeze. The signing key and provider
credentials stay in the verifier; the router only receives a signed statement.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@dataclass(frozen=True)
class VerifiedClosure:
    provider: Literal["fal", "minimax"]
    generation: str
    state: Literal["auto_refill_closed", "subscription_finally_ended"]
    observed_at: int
    # MiniMax packs may remain usable after an included quota window.
    # Absence of a model_remains row proves nothing about those packs.
    remaining_pack_credit: Decimal | None = None


@dataclass(frozen=True)
class Pair:
    primary: str = field(repr=False)
    standby: str = field(repr=False)
    # Independently enrolled fal billing accounts and least-privilege readers.
    fal_primary_team: str = ""
    fal_standby_team: str = ""
    fal_primary_reader: str = field(default="", repr=False)
    fal_standby_reader: str = field(default="", repr=False)

    def generation(self, provider: str) -> str:
        keys = (self.primary, self.standby)
        if keys[0] == keys[1] or any(
            not k or k.strip() != k or any(c in k for c in "\0\r\n") for k in keys
        ):
            raise ValueError("invalid pairing")
        return hashlib.sha256(
            "\0".join(("tr.ordered-key.v1", provider, *keys)).encode()
        ).hexdigest()


def _read(endpoint: str, key: str) -> dict[str, Any]:
    # Read-only, fixed origins, no redirects, no retries and no response logs.
    host, path, auth = {
        "fal": ("api.fal.ai", "/v1/account/billing?expand=credits", "Key "),
        "plan": ("api.minimax.io", "/v1/token_plan/remains", "Bearer "),
        "wallet": ("api.minimax.io", "/account/query_balance", "Bearer "),
    }[endpoint]
    conn = http.client.HTTPSConnection(host, timeout=5)
    try:
        conn.request("GET", path, headers={"Authorization": auth + key})
        response = conn.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError
        result = json.loads(raw, parse_float=Decimal, parse_int=int)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError, http.client.HTTPException):
        raise ValueError("billing observation unavailable") from None
    finally:
        conn.close()


def _amount(raw: object) -> Decimal:
    if type(raw) is not int and not isinstance(raw, Decimal):
        raise ValueError("missing numeric balance")
    amount = Decimal(str(raw))
    if (
        not amount.is_finite()
        or amount < 0
        or abs(amount.adjusted()) > 24
        or len(amount.as_tuple().digits) > 24
    ):
        raise ValueError("invalid balance")
    return amount


def _fal_balance(data: dict[str, Any], team: str) -> Decimal:
    credits = data.get("credits")
    if (
        not team
        or data.get("username") != team
        or not isinstance(credits, dict)
        or credits.get("currency") != "USD"
    ):
        raise ValueError("billing identity or currency mismatch")
    return _amount(credits.get("current_balance"))


def _minimax_success(data: dict[str, Any]) -> bool:
    meta = data.get("base_resp")
    return (
        isinstance(meta, dict) and type(meta.get("status_code")) is int and meta["status_code"] == 0
    )


def qualify(
    closure: VerifiedClosure,
    pair: Pair,
    audience: str,
    signer: Ed25519PrivateKey,
    read: Callable[[str, str], dict[str, Any]] = _read,
) -> dict[str, str] | None:
    """Return no event for positive remaining credit, resetting quotas or an unfunded standby."""
    if not audience or not 0 <= int(time.time()) - closure.observed_at <= 120:
        raise ValueError("stale or unbound closure")
    if closure.generation != pair.generation(closure.provider):
        raise ValueError("closure concerns a different credential pair")
    if closure.provider == "fal" and closure.state == "auto_refill_closed":
        if (
            pair.fal_primary_team == pair.fal_standby_team
            or not pair.fal_primary_reader
            or not pair.fal_standby_reader
            or pair.fal_primary_reader == pair.fal_standby_reader
        ):
            raise ValueError("distinct enrolled billing readers required")
        if _fal_balance(read("fal", pair.fal_primary_reader), pair.fal_primary_team) != 0:
            return None
        standby = _fal_balance(read("fal", pair.fal_standby_reader), pair.fal_standby_team)
    elif closure.provider == "minimax" and closure.state == "subscription_finally_ended":
        if closure.remaining_pack_credit is None or _amount(closure.remaining_pack_credit) != 0:
            return None
        plan = read("plan", pair.primary)
        if not _minimax_success(plan) or plan.get("model_remains") != []:
            return None
        wallet = read("wallet", pair.standby)
        if not _minimax_success(wallet):
            return None
        standby = _amount(wallet.get("available_amount"))
    else:
        raise ValueError("unrecognized funding closure")
    if standby <= 0:
        return None
    raw = json.dumps(
        {
            "audience": audience,
            "provider": closure.provider,
            "generation": closure.generation,
            "primary": closure.state,
            "primary_remaining": "0",
            "standby_available_credit": format(standby, "f"),
            "observed_at": closure.observed_at,
        },
        separators=(",", ":"),
    ).encode()
    return {
        "payload": base64.b64encode(raw).decode(),
        "signature": base64.b64encode(signer.sign(b"tr.managed-key-exhaustion/1\0" + raw)).decode(),
    }


_AUTHORITIES = {
    "gcp": "trustedrouter.com",
    "aws": "aws.trustedrouter.com",
    "azure": "azure.trustedrouter.com",
}


def _deliver(cloud: str, token: str, receipt: dict[str, str]) -> dict[str, Any]:
    conn = http.client.HTTPSConnection(_AUTHORITIES[cloud], timeout=5)
    try:
        conn.request(
            "POST",
            "/v1/internal/gateway/managed-key-funding-event",
            json.dumps(receipt),
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096:
            raise ValueError
        ack = json.loads(raw)
        if not isinstance(ack, dict):
            raise ValueError
        return ack
    except (OSError, ValueError, http.client.HTTPException):
        raise ValueError("authority did not acknowledge the event") from None
    finally:
        conn.close()


def on_verified_closure(
    closure: VerifiedClosure,
    pair: Pair,
    audience: str,
    signer: Ed25519PrivateKey,
    tokens: dict[str, str],
    *,
    read: Callable[[str, str], dict[str, Any]] = _read,
    post: Callable[[str, str, dict[str, str]], dict[str, Any]] = _deliver,
) -> dict[str, bool] | None:
    """One delivery per authority; return partial acknowledgements explicitly."""
    if tokens.keys() != _AUTHORITIES.keys() or not all(tokens.values()):
        raise ValueError("missing authority enrollment")
    receipt = qualify(closure, pair, audience, signer, read)
    if receipt is None:
        return None
    results: dict[str, bool] = {}
    for cloud, token in tokens.items():
        try:
            ack = post(cloud, token, receipt)
            results[cloud] = (
                ack.get("accepted") is True
                and ack.get("provider") == closure.provider
                and ack.get("generation") == closure.generation
            )
        except (OSError, ValueError, http.client.HTTPException):
            results[cloud] = False
    return results
