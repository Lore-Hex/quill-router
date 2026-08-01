from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from trusted_router.config import Settings

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")
_EXPECTED_PLATFORM = "gcp-confidential-space"
_EXPECTED_ISSUER = "https://confidentialcomputing.googleapis.com"
_EXPECTED_AUDIENCE = "quill-cloud"
_EXPECTED_SOURCE_REPO = "https://github.com/Lore-Hex/quill-cloud-proxy"
_EXPECTED_IMAGE_PREFIX = "us-central1-docker.pkg.dev/quill-cloud-proxy/quill/"
_MAX_RELEASE_BYTES = 64 * 1024
_FRESH_SECONDS = 60.0
_STALE_IF_ERROR_SECONDS = 5 * 60.0
_RETRY_SECONDS = 15.0
_FETCH_TIMEOUT_SECONDS = 4.0


class TrustReleaseUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedTrustRelease:
    metadata: Mapping[str, str]
    status: str


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    metadata: Mapping[str, str]
    fresh_until: float
    stale_until: float


class TrustReleaseResolver:
    """Resolve a validated gateway release without pinning it to app deploys."""

    def __init__(
        self,
        settings: Settings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._entry: _CacheEntry | None = None
        self._retry_after = 0.0
        self._refresh_lock = asyncio.Lock()

    async def resolve(self) -> ResolvedTrustRelease:
        release_url = self._settings.trust_gcp_release_url.strip()
        if not release_url:
            return ResolvedTrustRelease(
                metadata=_embedded_metadata(self._settings),
                status="embedded",
            )

        now = self._monotonic()
        if self._entry is not None and now < self._entry.fresh_until:
            return ResolvedTrustRelease(metadata=self._entry.metadata, status="live")
        if now < self._retry_after:
            return self._stale_or_raise(now)

        async with self._refresh_lock:
            now = self._monotonic()
            if self._entry is not None and now < self._entry.fresh_until:
                return ResolvedTrustRelease(metadata=self._entry.metadata, status="live")
            if now < self._retry_after:
                return self._stale_or_raise(now)
            try:
                metadata = await self._fetch(release_url)
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                self._retry_after = now + _RETRY_SECONDS
                try:
                    return self._stale_or_raise(now)
                except TrustReleaseUnavailable as unavailable:
                    raise unavailable from exc

            self._retry_after = 0.0
            self._entry = _CacheEntry(
                metadata=metadata,
                fresh_until=now + _FRESH_SECONDS,
                stale_until=now + _FRESH_SECONDS + _STALE_IF_ERROR_SECONDS,
            )
            return ResolvedTrustRelease(metadata=metadata, status="live")

    def _stale_or_raise(self, now: float) -> ResolvedTrustRelease:
        if self._entry is not None and now < self._entry.stale_until:
            return ResolvedTrustRelease(metadata=self._entry.metadata, status="stale")
        raise TrustReleaseUnavailable("live gateway release record unavailable")

    async def _fetch(self, release_url: str) -> Mapping[str, str]:
        url = httpx.URL(release_url)
        if url.scheme != "https" or not url.host:
            raise ValueError("trust release URL must be absolute HTTPS")
        cache_bucket = int(self._wall_clock() // int(_FRESH_SECONDS))
        url = url.copy_merge_params({"tr_cache_bucket": str(cache_bucket)})
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "GET",
                url,
                headers={"accept": "application/json"},
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) > _MAX_RELEASE_BYTES:
                    raise ValueError("trust release record exceeds size limit")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_RELEASE_BYTES:
                        raise ValueError("trust release record exceeds size limit")
                    content.extend(chunk)
        payload = json.loads(content)
        return _validated_metadata(payload)


def unavailable_trust_release() -> ResolvedTrustRelease:
    return ResolvedTrustRelease(
        metadata={
            "source_commit": "live-release-unavailable",
            "image_reference": "live-release-unavailable",
            "image_digest": "live-release-unavailable",
        },
        status="unavailable",
    )


def _embedded_metadata(settings: Settings) -> Mapping[str, str]:
    return {
        "source_commit": settings.trust_gcp_source_commit or "not-configured",
        "image_reference": settings.trust_gcp_image_reference or "not-configured",
        "image_digest": settings.trust_gcp_image_digest or "not-configured",
    }


def _validated_metadata(payload: object) -> Mapping[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("trust release record must be an object")
    if payload.get("platform") != _EXPECTED_PLATFORM:
        raise ValueError("unexpected trust release platform")
    if payload.get("attestation_issuer") != _EXPECTED_ISSUER:
        raise ValueError("unexpected trust release issuer")
    if payload.get("attestation_audience") != _EXPECTED_AUDIENCE:
        raise ValueError("unexpected trust release audience")
    if payload.get("source_repo") != _EXPECTED_SOURCE_REPO:
        raise ValueError("unexpected trust release source repository")

    source_commit = payload.get("source_commit")
    image_reference = payload.get("image_reference")
    image_digest = payload.get("image_digest")
    if not isinstance(source_commit, str) or _COMMIT_RE.fullmatch(source_commit) is None:
        raise ValueError("invalid trust release source commit")
    if not isinstance(image_reference, str) or not image_reference.startswith(
        _EXPECTED_IMAGE_PREFIX
    ):
        raise ValueError("invalid trust release image reference")
    if not image_reference.endswith(f"gcp-release-{source_commit}"):
        raise ValueError("trust release image reference does not match source commit")
    if not isinstance(image_digest, str) or _DIGEST_RE.fullmatch(image_digest) is None:
        raise ValueError("invalid trust release image digest")
    return {
        "source_commit": source_commit,
        "image_reference": image_reference,
        "image_digest": image_digest,
    }
