import base64
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .money import msats


@dataclass(frozen=True)
class Invoice:
    payment_hash: str
    bolt11: str
    state: str
    amount_msat: int
    settle_index: int
    expires_at: int
    requested_msat: int


class Lnd:
    """Invoice-only capability. Never load an admin or wallet-unlock macaroon."""

    def __init__(self, client: httpx.Client, network: str = "mainnet") -> None:
        if network not in {"mainnet", "regtest"}:
            raise ValueError("Unsupported Lightning network")
        self.client = client
        self.prefix = "lnbc" if network == "mainnet" else "lnbcrt"
        self.network = network

    def _parse(self, data: dict[str, Any], payment_hash: str) -> Invoice:
        actual = base64.b64decode(data["r_hash"], validate=True).hex()
        bolt = data["payment_request"]
        if actual != payment_hash or not isinstance(bolt, str) or len(bolt) > 8192:
            raise ValueError("Invalid LND invoice identity")
        if not re.fullmatch(self.prefix + r"[0-9]+[munp]?1[023456789acdefghjklmnpqrstuvwxyz]+", bolt):
            raise ValueError("Wrong invoice network or malformed payment request")
        state = data["state"]
        if state not in {"OPEN", "ACCEPTED", "SETTLED", "CANCELED"}:
            raise ValueError("Unknown LND state")
        return Invoice(
            payment_hash=actual, bolt11=bolt, state=state,
            amount_msat=msats(data.get("amt_paid_msat", "0")),
            settle_index=msats(data.get("settle_index", "0")),
            expires_at=msats(data["creation_date"]) + msats(data["expiry"]),
            requested_msat=msats(data["value_msat"]),
        )

    def lookup(self, payment_hash: str) -> Invoice | None:
        if not re.fullmatch(r"[0-9a-f]{64}", payment_hash):
            raise ValueError("Invalid payment hash")
        response = self.client.get(f"/v1/invoice/{payment_hash}")
        # LND's gRPC NOT_FOUND (5) is translated to HTTP 404.
        if response.status_code == 404:
            if response.json().get("code") != 5:
                response.raise_for_status()
            return None
        response.raise_for_status()
        return self._parse(response.json(), payment_hash)

    def receiving_capacity(self) -> int:
        info = self.client.get("/v1/getinfo")
        info.raise_for_status()
        data = info.json()
        if data.get("synced_to_chain") is not True or data.get("synced_to_graph") is not True:
            return 0
        if {("bitcoin", self.network)} != {(chain.get("chain"), chain.get("network")) for chain in data.get("chains", [])}:
            raise ValueError("LND is on the wrong network")
        response = self.client.get("/v1/channels")
        response.raise_for_status()
        capacities = []
        for channel in response.json()["channels"]:
            if channel.get("active") is not True:
                continue
            pending = sum(msats(htlc["amount"]) * 1000 for htlc in channel.get("pending_htlcs", []))
            balance = max(0, (msats(channel["remote_balance"]) - msats(channel["remote_constraints"]["chan_reserve_sat"])) * 1000 - pending)
            inflight = max(0, msats(channel["remote_constraints"]["max_pending_amt_msat"]) - pending)
            capacities.append(min(balance, inflight))
        # Do not assume a payer can use MPP across all our channels.
        return max(capacities, default=0)

    def ensure(self, preimage: bytes, amount_msat: int, *, expires_at: int) -> Invoice | None:
        payment_hash = hashlib.sha256(preimage).hexdigest()
        existing = self.lookup(payment_hash)
        if existing:
            if existing.requested_msat != amount_msat:
                raise ValueError("Existing invoice amount differs")
            return existing
        # Never create a fresh invoice using an old FX quote during retries.
        # Allow for the configured eight-second RPC deadline when computing
        # LND's relative expiry; the funding service also checks actual expiry.
        remaining = min(900, expires_at - int(time.time()) - 8)
        if remaining <= 0:
            return None
        try:
            response = self.client.post("/v1/invoices", json={
                "r_preimage": base64.b64encode(preimage).decode(),
                "value_msat": str(msats(amount_msat)), "expiry": str(remaining),
                "memo": "LightningRouter API credit", "private": True,
            })
            response.raise_for_status()
        except httpx.HTTPError:
            # Creation may have committed remotely. Only retry lookup, never
            # invent another payment hash or credit an ambiguous result.
            recovered = self.lookup(payment_hash)
            if recovered and recovered.requested_msat == amount_msat:
                return recovered
            raise
        created = self.lookup(payment_hash)
        if created is None or created.requested_msat != amount_msat:
            raise ValueError("LND creation not verified")
        return created

    def cancel(self, payment_hash: str) -> Invoice:
        response = self.client.post("/v2/invoices/cancel", json={
            "payment_hash": base64.b64encode(bytes.fromhex(payment_hash)).decode(),
        })
        # Cancellation can lose a race to payment. Always verify actual state.
        invoice = self.lookup(payment_hash)
        if invoice and invoice.state in {"CANCELED", "SETTLED"}:
            return invoice
        response.raise_for_status()
        raise ValueError("Invoice cancellation has not completed")
