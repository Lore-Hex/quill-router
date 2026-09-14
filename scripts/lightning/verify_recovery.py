"""Verify encrypted recovery files locally without displaying their contents."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scripts.lightning.lnd_node import AAD


def decrypt(blob: bytes, private_pem: bytes) -> bytes:
    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("RSA recovery key required")
    payload = json.loads(blob)
    if payload["version"] != 1:
        raise ValueError("unsupported backup format")
    dek = key.decrypt(base64.b64decode(payload["key"], validate=True), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=AAD,
    ))
    return AESGCM(dek).decrypt(base64.b64decode(payload["nonce"], validate=True),
                               base64.b64decode(payload["ciphertext"], validate=True), AAD)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=("seed", "channels"))
    args = parser.parse_args()
    if args.key.stat().st_mode & 0o077:
        raise ValueError("recovery private key must have owner-only permissions")
    plaintext = decrypt(args.backup.read_bytes(), args.key.read_bytes())
    if args.kind == "seed":
        payload = json.loads(plaintext)
        if payload.get("network") != "mainnet" or len(payload.get("seed", [])) != 24 or not payload.get("wallet_password"):
            raise ValueError("invalid seed recovery bundle")
    elif len(plaintext) < 24:
        raise ValueError("invalid static channel backup")
    print(f"Verified {args.kind} recovery decryption; no secret contents displayed")


if __name__ == "__main__":
    main()
