"""Signed runtime resolver for the GCP Stage D accepted-image policy."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from trusted_router.config import Settings

logger = logging.getLogger(__name__)

_POLICY_PATH = "gcp/stage-d-accepted.json"
_POLICY_KEYS = frozenset(
    {"schema", "plane", "sequence", "kind", "issued_at", "image_digests"}
)
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RFC3339_UTC_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)
_MAX_DOCUMENT_BYTES = 64 * 1024
_MAX_BUNDLE_BYTES = 2 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 5.0
_MAX_FUTURE_SKEW = timedelta(minutes=5)


class BundleVerifier(Protocol):
    """Narrow verification seam; tests never need Sigstore network state."""

    def verify(
        self,
        document: bytes,
        bundle: bytes,
        *,
        certificate_identity: str,
        oidc_issuer: str,
    ) -> None: ...


class PolicyWatermark(Protocol):
    """Durable monotonic sequence gate shared by every serving replica."""

    def advance(self, *, plane: str, sequence: int, updated_at: datetime) -> bool: ...

    def highest_sequence(self, *, plane: str) -> int | None: ...


class SigstoreBundleVerifier:
    """Verify a cosign ``sign-blob --bundle`` bundle with pinned identity."""

    def verify(
        self,
        document: bytes,
        bundle: bytes,
        *,
        certificate_identity: str,
        oidc_issuer: str,
    ) -> None:
        from sigstore.models import Bundle
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity

        parsed_bundle = Bundle.from_json(bundle.decode("utf-8"))
        Verifier.production().verify_artifact(
            input_=document,
            bundle=parsed_bundle,
            policy=Identity(identity=certificate_identity, issuer=oidc_issuer),
        )


class StorePolicyWatermark:
    """Adapter around the optional native-Spanner watermark capability."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def advance(self, *, plane: str, sequence: int, updated_at: datetime) -> bool:
        advance = getattr(self._store, "advance_stage_d_policy_watermark", None)
        if not callable(advance):
            return False
        return bool(advance(plane=plane, sequence=sequence, updated_at=updated_at))

    def highest_sequence(self, *, plane: str) -> int | None:
        read = getattr(self._store, "get_stage_d_policy_watermark", None)
        if not callable(read):
            return None
        value = read(plane=plane)
        return int(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class StageDPolicy:
    schema: str
    plane: str
    sequence: int
    kind: str
    issued_at: datetime
    image_digests: frozenset[str]


def parse_stage_d_policy(document: bytes, *, now: datetime | None = None) -> StageDPolicy:
    """Strictly validate the signed document without normalizing its bytes."""

    def object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Stage D policy contains a duplicate key")
            value[key] = item
        return value

    try:
        payload = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Stage D policy is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Stage D policy must be an object")
    if set(payload) != _POLICY_KEYS:
        raise ValueError("Stage D policy keys do not match the schema")
    if payload["schema"] != "tr.stage-d-accepted/1":
        raise ValueError("unexpected Stage D policy schema")
    if payload["plane"] != "gcp":
        raise ValueError("unexpected Stage D policy plane")
    sequence = payload["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("Stage D policy sequence must be a positive integer")
    kind = payload["kind"]
    if kind not in {"transitional", "final"}:
        raise ValueError("invalid Stage D policy kind")
    issued_at_raw = payload["issued_at"]
    if (
        not isinstance(issued_at_raw, str)
        or _RFC3339_UTC_RE.fullmatch(issued_at_raw) is None
    ):
        raise ValueError("Stage D policy issued_at must be RFC3339 UTC")
    try:
        issued_at = datetime.fromisoformat(issued_at_raw.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("Stage D policy issued_at must be RFC3339 UTC") from exc
    if issued_at.tzinfo is None or issued_at.utcoffset() != timedelta(0):
        raise ValueError("Stage D policy issued_at must be RFC3339 UTC")
    issued_at = issued_at.astimezone(UTC)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if issued_at > current + _MAX_FUTURE_SKEW:
        raise ValueError("Stage D policy issued_at is in the future")
    digests = payload["image_digests"]
    if not isinstance(digests, list) or not digests:
        raise ValueError("Stage D policy image_digests must be a non-empty array")
    if any(not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None for digest in digests):
        raise ValueError("Stage D policy contains an invalid image digest")
    if digests != sorted(digests) or len(digests) != len(set(digests)):
        raise ValueError("Stage D policy image_digests must be sorted and unique")
    return StageDPolicy(
        schema=str(payload["schema"]),
        plane=str(payload["plane"]),
        sequence=sequence,
        kind=str(kind),
        issued_at=issued_at,
        image_digests=frozenset(digests),
    )


class StageDPolicyResolver:
    """Refresh a signed policy in the background and retain only verified state."""

    def __init__(
        self,
        settings: Settings,
        watermark: PolicyWatermark,
        *,
        verifier: BundleVerifier | None = None,
        release_urls: Sequence[str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._settings = settings
        self._watermark = watermark
        self._verifier = verifier or SigstoreBundleVerifier()
        self._release_urls_override = tuple(release_urls) if release_urls is not None else None
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._client_factory = client_factory
        self._state_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._next_refresh_at = 0.0
        self._policy: StageDPolicy | None = None

    def _release_urls(self) -> tuple[str, ...]:
        if self._release_urls_override is not None:
            candidates = self._release_urls_override
        else:
            candidates = (
                self._settings.trust_gcp_release_url,
                *self._settings.trust_gcp_release_fallback_url_list,
            )
        return tuple(dict.fromkeys(url.strip() for url in candidates if url.strip()))

    def accepted_image_digests(self) -> frozenset[str]:
        with self._state_lock:
            return self._policy.image_digests if self._policy is not None else frozenset()

    def current_policy(self) -> StageDPolicy | None:
        with self._state_lock:
            return self._policy

    def kick(self) -> None:
        """Start one due refresh without making the request wait for it."""

        if not self._release_urls() or self._monotonic() < self._next_refresh_at:
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._next_refresh_at = (
                self._monotonic() + self._settings.stage_d_policy_refresh_seconds
            )
            self._worker = threading.Thread(
                target=self.refresh,
                name="stage-d-policy-refresh",
                daemon=True,
            )
            self._worker.start()

    def refresh(self) -> bool:
        """Fetch, verify, validate, and watermark one policy publication."""

        last_error: Exception | None = None
        for release_url in self._release_urls():
            try:
                policy_url = _policy_url(release_url)
                document = self._fetch(policy_url, _MAX_DOCUMENT_BYTES)
                bundle = self._fetch(policy_url + ".bundle", _MAX_BUNDLE_BYTES)
                self._verifier.verify(
                    document,
                    bundle,
                    certificate_identity=self._settings.stage_d_policy_cert_identity,
                    oidc_issuer=self._settings.stage_d_policy_oidc_issuer,
                )
                policy = parse_stage_d_policy(document, now=self._wall_clock())
                advanced = self._watermark.advance(
                    plane=policy.plane,
                    sequence=policy.sequence,
                    updated_at=self._wall_clock(),
                )
                if (
                    not advanced
                    and self._watermark.highest_sequence(plane=policy.plane)
                    != policy.sequence
                ):
                    raise ValueError("Stage D policy sequence is below the watermark")
                with self._state_lock:
                    self._policy = policy
                logger.info(
                    "stage_d.policy_accepted plane=%s sequence=%s kind=%s digests=%s",
                    policy.plane,
                    policy.sequence,
                    policy.kind,
                    len(policy.image_digests),
                )
                return True
            except Exception as exc:  # noqa: BLE001 - verified state survives every failure
                last_error = exc
        if last_error is not None:
            logger.warning(
                "stage_d.policy_refresh_failed error_class=%s",
                type(last_error).__name__,
            )
        return False

    def _fetch(self, url: str, byte_limit: int) -> bytes:
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or not parsed.host:
            raise ValueError("Stage D policy URL must be absolute HTTPS")
        with self._client_factory(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            with client.stream("GET", parsed, headers={"accept": "application/json"}) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length is not None and int(length) > byte_limit:
                    raise ValueError("Stage D policy artifact exceeds its size limit")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > byte_limit:
                        raise ValueError("Stage D policy artifact exceeds its size limit")
                    content.extend(chunk)
        return bytes(content)


def _policy_url(release_url: str) -> str:
    parsed = httpx.URL(release_url)
    if parsed.scheme != "https" or not parsed.host:
        raise ValueError("trust release URL must be absolute HTTPS")
    return urljoin(str(parsed), _POLICY_PATH)
