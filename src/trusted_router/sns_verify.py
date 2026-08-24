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
import threading
import time
from collections import OrderedDict
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
_SIGNING_CERT_PATH_RE = re.compile(
    r"^/SimpleNotificationService-[A-Za-z0-9_-]{1,128}\.pem$"
)
_CERT_CACHE_TTL_SECONDS = 60 * 60
_CERT_CACHE_MAX_ENTRIES = 16
_CERT_FETCH_WINDOW_SECONDS = 60.0
_CERT_FETCH_MAX_PER_WINDOW = 8
_CERT_FETCH_MAX_CONCURRENT = 2
_SIGNATURE_VERIFY_MAX_CONCURRENT = 4
_CERT_CACHE_LOCK = threading.Lock()
_CERT_CACHE: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_CERT_FETCH_MISSES: list[float] = []
_CERT_FETCHING: set[str] = set()
_CERT_FETCH_SLOTS = threading.BoundedSemaphore(_CERT_FETCH_MAX_CONCURRENT)
_SIGNATURE_VERIFY_SLOTS = threading.BoundedSemaphore(_SIGNATURE_VERIFY_MAX_CONCURRENT)


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
    try:
        port = parsed.port
    except ValueError as exc:
        raise SnsVerificationError("invalid SigningCertURL port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not _SIGNING_CERT_HOST_RE.fullmatch(parsed.hostname)
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _SIGNING_CERT_PATH_RE.fullmatch(parsed.path)
    ):
        raise SnsVerificationError(f"untrusted SigningCertURL host: {parsed.hostname!r}")

    fields = _SUBSCRIPTION_FIELDS if msg_type != "Notification" else _NOTIFICATION_FIELDS
    signing_string = _canonical_string(message, fields)

    if not _SIGNATURE_VERIFY_SLOTS.acquire(blocking=False):
        raise SnsVerificationError("SNS signature verification capacity exceeded")
    try:
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
            algorithm: hashes.HashAlgorithm = (
                hashes.SHA1() if sig_version == "1" else hashes.SHA256()  # noqa: S303
            )
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
        except Exception as exc:  # noqa: BLE001 - cert/key errors are verification failures.
            raise SnsVerificationError(f"signature verification failed: {exc}") from exc
    finally:
        _SIGNATURE_VERIFY_SLOTS.release()


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
    """Fetch one AWS signing certificate with a small process-wide cache.

    SNS reuses certificates across deliveries. Without caching, every signed
    notification turns into a synchronous outbound GET and a forged stream can
    multiply work even though all signatures ultimately fail. A non-blocking
    semaphore caps outbound work without filling AnyIO's worker pool with
    callers waiting behind a slow certificate host. Concurrent requests for the
    same uncached URL fail fast and let SNS retry after the first fetch fills the
    cache; they never trigger duplicate downloads.
    """

    now = time.monotonic()
    with _CERT_CACHE_LOCK:
        cached = _CERT_CACHE.get(url)
        if cached is not None and cached[0] > now:
            _CERT_CACHE.move_to_end(url)
            return cached[1]
        if cached is not None:
            _CERT_CACHE.pop(url, None)

        if url in _CERT_FETCHING:
            raise RuntimeError("SNS signing-certificate fetch already in progress")

    if not _CERT_FETCH_SLOTS.acquire(blocking=False):
        raise RuntimeError("SNS signing-certificate fetch capacity exceeded")

    try:
        now = time.monotonic()
        with _CERT_CACHE_LOCK:
            # A different fetch may have populated this entry between the
            # first lookup and admission.
            cached = _CERT_CACHE.get(url)
            if cached is not None and cached[0] > now:
                _CERT_CACHE.move_to_end(url)
                return cached[1]
            if cached is not None:
                _CERT_CACHE.pop(url, None)
            if url in _CERT_FETCHING:
                raise RuntimeError("SNS signing-certificate fetch already in progress")

            cutoff = now - _CERT_FETCH_WINDOW_SECONDS
            _CERT_FETCH_MISSES[:] = [seen for seen in _CERT_FETCH_MISSES if seen > cutoff]
            if len(_CERT_FETCH_MISSES) >= _CERT_FETCH_MAX_PER_WINDOW:
                raise RuntimeError("SNS signing-certificate fetch capacity exceeded")
            _CERT_FETCH_MISSES.append(now)
            _CERT_FETCHING.add(url)

        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        content = bytes(response.content)
        with _CERT_CACHE_LOCK:
            _CERT_CACHE[url] = (time.monotonic() + _CERT_CACHE_TTL_SECONDS, content)
            _CERT_CACHE.move_to_end(url)
            while len(_CERT_CACHE) > _CERT_CACHE_MAX_ENTRIES:
                _CERT_CACHE.popitem(last=False)
        return content
    finally:
        with _CERT_CACHE_LOCK:
            _CERT_FETCHING.discard(url)
        _CERT_FETCH_SLOTS.release()


def _reset_sns_cert_cache_for_tests() -> None:
    with _CERT_CACHE_LOCK:
        _CERT_CACHE.clear()
        _CERT_FETCH_MISSES.clear()
        if _CERT_FETCHING:
            raise RuntimeError("cannot reset SNS certificate cache during a fetch")
