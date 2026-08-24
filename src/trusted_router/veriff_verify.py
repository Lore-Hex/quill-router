from __future__ import annotations

import hashlib
import hmac


class VeriffVerificationError(ValueError):
    """The Veriff webhook signature is absent or invalid."""


def verify_veriff_signature(
    raw_body: bytes,
    signature: str | None,
    *,
    shared_secret: str,
) -> None:
    if not signature:
        raise VeriffVerificationError("missing Veriff signature")
    expected = hmac.new(
        shared_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise VeriffVerificationError("invalid Veriff signature")
