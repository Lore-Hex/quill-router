"""SIWE (Sign-In With Ethereum, EIP-4361) helpers.

Two responsibilities:

1. Build the canonical message that the wallet will sign. The format is
   strictly defined by EIP-4361; any deviation breaks downstream wallets.
2. Recover the address from a signed message via `eth-account` and verify
   it matches what we expect.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@dataclass(frozen=True)
class SiweMessage:
    domain: str
    address: str  # 0x-prefixed checksum or lowercase address
    statement: str
    uri: str
    version: str
    chain_id: int
    nonce: str
    issued_at: str
    expiration_time: str | None = None


def build_siwe_message(
    *,
    domain: str,
    address: str,
    nonce: str,
    issued_at: dt.datetime,
    statement: str = "Sign in to TrustedRouter.",
    uri: str | None = None,
    chain_id: int = 1,
    expiration_seconds: int = 300,
) -> tuple[str, SiweMessage]:
    """Returns (canonical_message_string, structured_record). The string is
    what the wallet sees and signs; we keep the record for verification."""
    if not ADDRESS_RE.match(address):
        raise ValueError("invalid Ethereum address")
    issued = issued_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    expires = (
        (issued_at + dt.timedelta(seconds=expiration_seconds))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    record = SiweMessage(
        domain=domain,
        address=address,
        statement=statement,
        uri=uri or f"https://{domain}",
        version="1",
        chain_id=chain_id,
        nonce=nonce,
        issued_at=issued,
        expiration_time=expires,
    )
    message = (
        f"{record.domain} wants you to sign in with your Ethereum account:\n"
        f"{record.address}\n"
        "\n"
        f"{record.statement}\n"
        "\n"
        f"URI: {record.uri}\n"
        f"Version: {record.version}\n"
        f"Chain ID: {record.chain_id}\n"
        f"Nonce: {record.nonce}\n"
        f"Issued At: {record.issued_at}\n"
        f"Expiration Time: {record.expiration_time}"
    )
    return message, record


def recover_address(*, message: str, signature: str) -> str:
    """Recover the 0x-lowercase address that signed `message`.

    Uses `eth_account.messages.encode_defunct` (EIP-191 personal_sign,
    which is what MetaMask's `personal_sign` produces). Returns lowercase
    so callers can compare with `address.lower()` directly.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    encoded = encode_defunct(text=message)
    return Account.recover_message(encoded, signature=signature).lower()
