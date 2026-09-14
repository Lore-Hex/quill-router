import hashlib
import hmac
import re

KEY_PATTERN = re.compile(r"sk-lr-v1-[A-Za-z0-9_-]{43}\Z")


class Credentials:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("A persistent secret of at least 32 bytes is required")
        self._secret = secret

    def fingerprint(self, raw_key: str) -> str:
        if not KEY_PATTERN.fullmatch(raw_key):
            raise ValueError("Use a LightningRouter API key")
        return hmac.new(self._secret, b"account\0" + raw_key.encode(), hashlib.sha256).hexdigest()

    def invoice_preimage(self, invoice_id: str) -> bytes:
        # Persist the hash before calling LND. Retries recover the SAME invoice,
        # including when LND accepted creation but its HTTP response was lost.
        return hmac.new(self._secret, b"invoice\0" + invoice_id.encode(), hashlib.sha256).digest()
