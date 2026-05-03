from __future__ import annotations

import hashlib
import hmac
import secrets


def new_api_key(prefix: str = "sk-tr-v1") -> str:
    return f"{prefix}-{secrets.token_urlsafe(32)}"


def new_key_id(prefix: str = "key") -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def new_hash_salt() -> str:
    return secrets.token_hex(32)


def hash_api_key(key: str, salt: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt) + key.encode("utf-8")).hexdigest()


def lookup_hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(key: str, salt: str, digest: str) -> bool:
    return constant_time_equal(hash_api_key(key, salt), digest)


def key_label(key: str) -> str:
    if len(key) <= 12:
        return key
    return f"{key[:10]}...{key[-4:]}"


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
