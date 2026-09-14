import hashlib
import hmac
import re
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_PATTERN = re.compile(r"sk-tr-v1-[A-Za-z0-9_-]{43}\Z")


class Credentials:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("A persistent secret of at least 32 bytes is required")
        self._secret = secret

    def fingerprint(self, raw_key: str) -> str:
        if not KEY_PATTERN.fullmatch(raw_key):
            raise ValueError("Use a TrustedRouter API key")
        return hmac.new(self._secret, b"account\0" + raw_key.encode(), hashlib.sha256).hexdigest()

    def invoice_preimage(self, invoice_id: str) -> bytes:
        # Persist the hash before calling LND. Retries recover the SAME invoice,
        # including when LND accepted creation but its HTTP response was lost.
        return hmac.new(self._secret, b"invoice\0" + invoice_id.encode(), hashlib.sha256).digest()

    def _checkout_cipher(self) -> AESGCM:
        key = hmac.new(self._secret, b"checkout-encryption-v1", hashlib.sha256).digest()
        return AESGCM(key)

    def seal_pending_key(self, raw_key: str) -> bytes:
        owner = self.fingerprint(raw_key)
        nonce = secrets.token_bytes(12)
        return nonce + self._checkout_cipher().encrypt(nonce, raw_key.encode(), owner.encode())

    def open_pending_key(self, ciphertext: bytes, owner: str) -> str:
        raw = self._checkout_cipher().decrypt(ciphertext[:12], ciphertext[12:], owner.encode()).decode()
        if not hmac.compare_digest(self.fingerprint(raw), owner):
            raise ValueError("Checkout key binding changed")
        return raw
