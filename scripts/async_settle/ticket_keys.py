"""Generate purpose-specific pilot signing material; never print private bytes."""
from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.async_settle_ticket import PURPOSE, TicketSigner
from trusted_router.detached_jws import TrustedKey, b64encode


def provision(path: Path, *, kid: str, issuer: str, epoch: int,
              audience: str = "router-settlement") -> dict[str, Any]:
    if type(epoch) is not int or not 0 < epoch < 1 << 63 or audience != "router-settlement" or not path.is_absolute():
        raise ValueError("positive epoch, router-settlement audience and absolute private path required")
    private = Ed25519PrivateKey.generate()
    public = b64encode(private.public_key().public_bytes_raw())
    TicketSigner(private, TrustedKey(kid, PURPOSE, public, issuer, audience))
    # Exclusive creation also rejects existing symlinks; no overwrite or stdout secret.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pins = {"TR_ASYNC_SETTLE_TICKET_KID": kid, "TR_ASYNC_SETTLE_TICKET_ISSUER": issuer,
            "TR_ASYNC_SETTLE_TICKET_AUDIENCE": audience,
            "TR_ASYNC_SETTLE_AUTHORITY_EPOCH": str(epoch),
            "TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE": str(path)}
    return {"enclave_keyring": {kid: issuer + "~" + public},
            "router_pins": [f"{key}={shlex.quote(value)}" for key, value in pins.items()]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key-file", type=Path, required=True)
    parser.add_argument("--kid", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--audience", default="router-settlement")
    args = parser.parse_args()
    print(json.dumps(provision(args.private_key_file, kid=args.kid, issuer=args.issuer,
                              epoch=args.epoch, audience=args.audience), indent=2))


if __name__ == "__main__":
    main()
