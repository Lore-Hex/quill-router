"""Amazon SNS message signature verification.

SNS posts JSON to our HTTPS endpoint for SES bounce/complaint events.
Without signature verification anyone could POST a forged complaint and
get a real user's email address blocked, so we MUST verify each message
before acting on it.

The algorithm is documented at:
https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html

Summary:
1. Confirm `SignatureVersion` is "1" or "2".
2. Confirm `SigningCertURL` points at amazonaws.com (with optional region
   prefix). This is the cert authority — pulling it from any other host
   would let an attacker present their own cert.
3. Build the canonical signing string from the message fields, in the
   order specified per Type.
4. SHA1-RSA (v1) or SHA256-RSA (v2) verify against the public key
   extracted from the X.509 cert.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509 import load_pem_x509_certificate

CertFetcher = Callable[[str], bytes]

# Fields used to build the canonical signing string, by message Type.
_NOTIFICATION_FIELDS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SUBSCRIPTION_FIELDS = (
    "Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type",
)

# AWS publishes signing certs at sns.<region>.amazonaws.com only.
_SIGNING_CERT_HOST_RE = re.compile(r"^sns(\.[a-z0-9-]+)?\.amazonaws\.com$")


class SnsVerificationError(Exception):
    """Raised when we cannot verify a message — caller should drop it."""


def verify_sns_message(
    message: dict[str, Any],
    *,
    cert_fetcher: CertFetcher | None = None,
) -> None:
    """Raise `SnsVerificationError` if the message isn't authentic.

    `cert_fetcher` is injectable so tests can stub the network without
    monkey-patching httpx."""
    msg_type = message.get("Type")
    sig_version = message.get("SignatureVersion")
    signing_cert_url = message.get("SigningCertURL") or message.get("SigningCertUrl")
    signature_b64 = message.get("Signature")
    if msg_type not in {"Notification", "SubscriptionConfirmation", "UnsubscribeConfirmation"}:
        raise SnsVerificationError(f"unsupported SNS Type: {msg_type!r}")
    if sig_version not in {"1", "2"}:
        raise SnsVerificationError(f"unsupported SignatureVersion: {sig_version!r}")
    if not isinstance(signing_cert_url, str) or not isinstance(signature_b64, str):
        raise SnsVerificationError("missing SigningCertURL or Signature")

    parsed = urlparse(signing_cert_url)
    if parsed.scheme != "https" or not parsed.hostname or not _SIGNING_CERT_HOST_RE.match(parsed.hostname):
        raise SnsVerificationError(f"untrusted SigningCertURL host: {parsed.hostname!r}")

    fields = _SUBSCRIPTION_FIELDS if msg_type != "Notification" else _NOTIFICATION_FIELDS
    signing_string = _canonical_string(message, fields)

    fetcher = cert_fetcher or _httpx_cert_fetcher
    try:
        cert_pem = fetcher(signing_cert_url)
    except Exception as exc:
        raise SnsVerificationError(f"failed to fetch signing cert: {exc}") from exc
    try:
        cert = load_pem_x509_certificate(cert_pem)
        public_key = cert.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise SnsVerificationError("signing cert does not carry an RSA public key")
        signature = base64.b64decode(signature_b64)
        algorithm: hashes.HashAlgorithm = hashes.SHA1() if sig_version == "1" else hashes.SHA256()  # noqa: S303
        public_key.verify(
            signature,
            signing_string.encode("utf-8"),
            padding.PKCS1v15(),
            algorithm,
        )
    except InvalidSignature as exc:
        raise SnsVerificationError("signature does not match") from exc
    except SnsVerificationError:
        raise
    except Exception as exc:  # noqa: BLE001 - any cert/key error is a verification failure.
        raise SnsVerificationError(f"signature verification failed: {exc}") from exc


def _canonical_string(message: dict[str, Any], fields: tuple[str, ...]) -> str:
    parts: list[str] = []
    for name in fields:
        value = message.get(name)
        if value is None:
            # Subject is optional on Notifications.
            continue
        parts.append(name)
        parts.append(str(value))
    # SNS spec uses LF as separator AND a trailing LF.
    return "\n".join(parts) + "\n"


def _httpx_cert_fetcher(url: str) -> bytes:
    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    return response.content
