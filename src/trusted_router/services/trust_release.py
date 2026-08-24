from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from trusted_router.config import Settings
from trusted_router.endpoint_identity import Identity, parse_endpoint

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
    metadata: Mapping[str, Any]
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
        urls: Sequence[str] | None = None,
        validator: Callable[[object], Mapping[str, Any]] | None = None,
        embedded: Callable[[Settings], Mapping[str, Any]] | None = None,
    ) -> None:
        self._settings = settings
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._entry: _CacheEntry | None = None
        self._retry_after = 0.0
        self._refresh_lock = asyncio.Lock()
        # Which record, how to validate it, and what to fall back to are inputs
        # rather than hardcoded GCP. The control plane's job is to MIRROR each
        # plane's authoritative record — it is not the author of any of them,
        # and hardcoding one plane's shape is what made it look like one.
        self._urls = tuple(urls) if urls is not None else None
        self._validator = validator or _validated_metadata
        self._embedded = embedded or _embedded_metadata

    def _release_urls(self) -> tuple[str, ...]:
        if self._urls is not None:
            return tuple(url.strip() for url in self._urls if url.strip())
        return tuple(
            dict.fromkeys(
                [
                    self._settings.trust_gcp_release_url.strip(),
                    *self._settings.trust_gcp_release_fallback_url_list,
                ]
            )
        )

    async def resolve(self) -> ResolvedTrustRelease:
        release_urls = tuple(url for url in self._release_urls() if url)
        if not release_urls:
            return ResolvedTrustRelease(
                metadata=self._embedded(self._settings),
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
            last_error: Exception | None = None
            metadata: Mapping[str, Any] | None = None
            for release_url in release_urls:
                try:
                    metadata = await self._fetch(release_url)
                    break
                except Exception as exc:  # noqa: BLE001 - see below
                    # ANY failure to obtain a validated record is the same
                    # outcome for a mirror: there is nothing trustworthy to
                    # serve, so fall back, back off, and let the route answer
                    # 503. This was an enumeration of exception classes
                    # (httpx.HTTPError, TypeError, ValueError) and the
                    # enumeration was wrong: `_validator` is a constructor
                    # PARAMETER, so this layer cannot know what its vocabulary
                    # is, and a validator built on httpx.URL raised
                    # httpx.InvalidURL — outside httpx.HTTPError — which escaped
                    # onto a public trust route as a 500 with no fallback, no
                    # retry backoff and no stale-if-error serving, for a
                    # malformed field in a record fetched from a remote mirror.
                    #
                    # The cost, stated plainly: a genuine bug in the fetch or
                    # validate path now looks like an upstream outage instead of
                    # a 500. It is chained into the TrustReleaseUnavailable
                    # raised below (`from last_error`), so the cause travels
                    # with it. BaseException is deliberately not caught, so
                    # asyncio.CancelledError still propagates.
                    last_error = exc
            if metadata is None:
                self._retry_after = now + _RETRY_SECONDS
                try:
                    return self._stale_or_raise(now)
                except TrustReleaseUnavailable as unavailable:
                    raise unavailable from last_error

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

    async def _fetch(self, release_url: str) -> Mapping[str, Any]:
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
        return self._validator(payload)


def unavailable_trust_release() -> ResolvedTrustRelease:
    return ResolvedTrustRelease(
        metadata={
            "source_commit": "live-release-unavailable",
            "image_reference": "live-release-unavailable",
            "image_digest": "live-release-unavailable",
            "accepted_image_digests": ["live-release-unavailable"],
            "accepted_image_references": ["live-release-unavailable"],
            "release_state": "unavailable",
        },
        status="unavailable",
    )


def _embedded_metadata(settings: Settings) -> Mapping[str, Any]:
    digest = settings.trust_gcp_image_digest or "not-configured"
    reference = settings.trust_gcp_image_reference or "not-configured"
    return {
        "source_commit": settings.trust_gcp_source_commit or "not-configured",
        "image_reference": reference,
        "image_digest": digest,
        "accepted_image_digests": [digest],
        "accepted_image_references": [reference],
        "release_state": "current",
    }


def _validated_metadata(payload: object) -> Mapping[str, Any]:
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
    # The accepted SETS must survive the trip. Dropping them is not a cosmetic
    # loss: during a rolling deploy the record names the incoming digest while
    # the fleet still serves the outgoing one, so a record carrying only the
    # scalar tells a verifier that the enclave answering them does not match its
    # published measurement. Observed live on 2026-08-15 — trust.trustedrouter.com
    # correctly published both digests and trustedrouter.com republished one.
    #
    # A mirror serves a record. Narrowing a pin in transit makes it an author,
    # and a careless one.
    accepted_digests = _validated_set(payload, "accepted_image_digests", image_digest, _DIGEST_RE)
    accepted_references = _validated_set(
        payload, "accepted_image_references", image_reference, None
    )
    return {
        "source_commit": source_commit,
        "image_reference": image_reference,
        "image_digest": image_digest,
        "accepted_image_digests": accepted_digests,
        "accepted_image_references": accepted_references,
        "release_state": (
            "current"
            if accepted_digests == [image_digest] and accepted_references == [image_reference]
            else "rolling"
        ),
    }


def _validated_set(
    payload: dict[str, object],
    key: str,
    primary: str,
    pattern: re.Pattern[str] | None,
) -> list[str]:
    """The accepted set from an upstream record, primary always included.

    An upstream that omits the set, or publishes one missing the value it also
    calls current, must not produce a published set that rejects the running
    enclave — so the primary is unconditionally a member.
    """
    raw = payload.get(key)
    values = [primary]
    if isinstance(raw, list):
        for value in raw:
            if not isinstance(value, str) or not value:
                raise ValueError(f"invalid entry in {key}")
            if pattern is not None and pattern.fullmatch(value) is None:
                raise ValueError(f"invalid entry in {key}")
            if value not in values:
                values.append(value)
    elif raw is not None:
        raise ValueError(f"{key} must be a list when present")
    return values


# --- Mirroring the AWS and Azure records ------------------------------------
#
# These planes publish their own records, produced from live attestations by
# quill-cloud-proxy's tools/capture-plane-measurements.py and signed under a
# per-plane identity. The control plane fetches and republishes them; it does
# not compute them. Embedding a measurement here would make one deployment the
# authority for what three independent planes are running — a single place to
# falsify and a single place to fail, which is precisely what the three-plane
# arrangement exists to avoid.

_PCR0_RE = re.compile(r"[0-9a-f]{96}")
_HOSTDATA_RE = re.compile(r"[0-9a-f]{64}")


def _validated_set_of(payload: dict[str, object], key: str, pattern: re.Pattern[str]) -> list[str]:
    raw = payload.get(key)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{key} must be a non-empty list")
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValueError(f"invalid entry in {key}")
        if value not in values:
            values.append(value)
    return values


def validated_aws_metadata(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("AWS trust record must be an object")
    if payload.get("platform") != "aws-nitro-enclaves":
        raise ValueError("unexpected AWS trust record platform")
    accepted = _validated_set_of(payload, "accepted_pcr0s", _PCR0_RE)
    pcr0 = payload.get("pcr0")
    if not isinstance(pcr0, str) or _PCR0_RE.fullmatch(pcr0) is None:
        raise ValueError("invalid AWS PCR0")
    if pcr0 not in accepted:
        # A record whose accepted set excludes its own current measurement would
        # have a verifier reject the enclave that is answering them.
        raise ValueError("AWS pcr0 is absent from its own accepted set")
    return {"pcr0": pcr0, "accepted_pcr0s": accepted}


#: Optional descriptions a region entry may carry, copied verbatim and not
#: interpreted here. attestation_url, hostdata and attestation_issuer are
#: required of every entry and are validated individually below; everything
#: else an upstream record sends is dropped, because mirroring whatever arrives
#: would let the plane's record inject arbitrary fields into ours.
_AZURE_REGION_OPTIONAL = ("launch_measurement", "compliance_status")
#: An upper bound on how many serving regions a record may claim. The payload is
#: already capped at _MAX_RELEASE_BYTES, which still leaves room for hundreds of
#: entries — and every entry is an endpoint scripts/verify_trust_measurements.py
#: will fetch, so an unbounded array is a fetch-amplification lever for anyone
#: who can write the upstream record. Two regions are in service; sixteen is
#: room to grow and still a refusal long before it becomes a load generator.
_MAX_AZURE_REGIONS = 16


def _endpoint_identity(url: object) -> Identity | None:
    """What actually gets contacted, or None if this is not a plain https URL.

    A region entry's attestation_url is a coverage claim, so what makes two of
    them the same has to be what a client would end up talking to — scheme,
    host, port and path — and not the string. That comparison lives in
    trusted_router.endpoint_identity because the drift checker has to make it
    the same way: it counts distinct endpoints contacted, so a pair this layer
    accepts as two places and that one folds into one (or the reverse) is a
    false coverage count with a mirror's signature on it.

    Refused here, on top of "does not name a place at all": a scheme other than
    https, and a URL carrying a query or a fragment. Neither is part of the
    place, so two entries differing only there would inflate the region count
    while contacting one endpoint.

    Returns a value for every input, including malformed ones. The version this
    replaces called httpx.URL(), which RAISES httpx.InvalidURL on a bad
    authority — an exception outside httpx.HTTPError's hierarchy, so it escaped
    TrustReleaseResolver.resolve() and made the public /trust/azure-release.json
    route answer 500 for an upstream record naming a region at `https://[::1`.
    """
    endpoint = parse_endpoint(url)
    if endpoint is None or endpoint.scheme != "https" or not endpoint.bare:
        return None
    return endpoint.identity


def _validated_azure_regions(
    payload: dict[str, object], accepted: Sequence[str], issuers: Sequence[str]
) -> list[dict[str, str]]:
    """The per-region endpoint array, validated rather than dropped.

    WHY THIS EXISTS AT ALL. accepted_hostdata is a union and cannot say which
    endpoint serves which value, so a reader holding only the union can verify
    whichever region anycast happens to give them and has no way to reach the
    others. This array is the only thing in the record that names WHERE each
    region answers, and it is what scripts/verify_trust_measurements.py
    enumerates. Until this function existed the mirror whitelisted three scalar
    keys and dropped it, so the checker read a record with no endpoints to
    enumerate and could reach exactly one of the two Azure regions.

    WHY A CONTRADICTORY ENTRY RAISES INSTEAD OF BEING DROPPED. A region whose
    hostdata or issuer the rest of the record disowns is a self-inconsistent
    record, the same class as an AWS record whose accepted set excludes its own
    pcr0 — and that one is refused here rather than repaired. Dropping the entry
    instead would erase a live serving region from the published record with no
    trace: the region simply stops existing, the drift check contacts what is
    left and goes green, and the plane we quietly stopped covering is the one
    nobody is looking at. It is also the only choice that keeps this layer and
    trust._azure_regions agreeing — that one drops an entry missing a required
    field, because azure_release has no error channel, so accepting an
    incomplete entry here would just move the silent erasure one function later.

    THE BLAST RADIUS, STATED PLAINLY. Every refusal here is a ValueError, which
    TrustReleaseResolver.resolve() turns into TrustReleaseUnavailable, so the
    mirror falls back to the embedded Azure measurement — and rollout.sh does
    not set TR_TRUST_AZURE_HOSTDATA, so in the current production configuration
    that fallback is unconfigured and /trust/azure-release.json answers 503.
    Loud, on purpose, and the same blast radius the existing rules above already
    carry (an AWS pcr0 outside its own accepted set, an Azure record naming no
    MAA issuer). Serving no Azure record is a state a verifier can act on;
    serving one that has quietly lost a serving region is not.

    That paragraph was false for one input class when it was first written, and
    the shape of the mistake is worth keeping: _endpoint_identity was built on
    httpx.URL, which RAISES rather than returning None for a malformed
    authority, and httpx.InvalidURL is not an httpx.HTTPError — so a region
    named `https://[::1` escaped the resolver entirely and the route answered
    500 with no fallback, no backoff and no stale serving. A refusal that is
    documented as one status code and delivered as another is worse than no
    documentation. It is closed twice over: the identity parser returns a value
    for every input, and resolve() no longer enumerates exception classes.
    """
    if "regions" not in payload:
        return []
    raw = payload["regions"]
    if not isinstance(raw, list):
        # An explicit JSON null lands here rather than being read as "absent".
        # "The key is missing" and "the key is present and says nothing" are
        # different upstream states and must not collapse into each other.
        raise ValueError("Azure regions must be a list when present")
    if len(raw) > _MAX_AZURE_REGIONS:
        raise ValueError("Azure record claims more serving regions than we would publish")
    regions: list[dict[str, str]] = []
    seen_urls: set[Identity] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Azure region entry must be an object")
        url = entry.get("attestation_url")
        if not isinstance(url, str) or _endpoint_identity(url) is None:
            raise ValueError("Azure region entry must name a plain https attestation_url")
        # Compared on IDENTITY, not on the raw string. Two URLs differing only
        # by fragment or query — .../attestation#a and .../attestation#b — are
        # distinct strings that fetch the same endpoint, so a raw-string
        # comparison here would let a record claim two regions on the strength
        # of one, which is the coverage count this whole change exists to stop.
        identity = _endpoint_identity(url)
        assert identity is not None  # noqa: S101 - refused above; narrows for the type checker
        if identity in seen_urls:
            raise ValueError("Azure regions name the same attestation endpoint twice")
        seen_urls.add(identity)
        hostdata = entry.get("hostdata")
        if not isinstance(hostdata, str) or _HOSTDATA_RE.fullmatch(hostdata) is None:
            raise ValueError("invalid hostdata in Azure region entry")
        if hostdata not in accepted:
            raise ValueError("Azure region hostdata is absent from the record's accepted set")
        issuer = entry.get("attestation_issuer")
        if not isinstance(issuer, str) or not issuer.startswith("https://"):
            raise ValueError("invalid MAA issuer in Azure region entry")
        if issuer not in issuers:
            raise ValueError("Azure region issuer is absent from the record's issuer list")
        region = {"attestation_url": url, "hostdata": hostdata, "attestation_issuer": issuer}
        for field in _AZURE_REGION_OPTIONAL:
            value = entry.get(field)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"invalid {field} in Azure region entry")
            if isinstance(value, str):
                region[field] = value
        regions.append(region)
    return regions


def validated_azure_metadata(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Azure trust record must be an object")
    if payload.get("platform") != "azure-confidential-containers-sev-snp":
        raise ValueError("unexpected Azure trust record platform")
    accepted = _validated_set_of(payload, "accepted_hostdata", _HOSTDATA_RE)
    hostdata = payload.get("hostdata")
    if not isinstance(hostdata, str) or _HOSTDATA_RE.fullmatch(hostdata) is None:
        raise ValueError("invalid Azure hostdata")
    if hostdata not in accepted:
        raise ValueError("Azure hostdata is absent from its own accepted set")
    issuers = payload.get("attestation_issuers")
    if not isinstance(issuers, list) or not issuers:
        raise ValueError("Azure record must name at least one MAA issuer")
    for issuer in issuers:
        if not isinstance(issuer, str) or not issuer.startswith("https://"):
            raise ValueError("invalid MAA issuer")
    return {
        "hostdata": hostdata,
        "accepted_hostdata": accepted,
        "attestation_issuers": list(issuers),
        "regions": _validated_azure_regions(payload, accepted, issuers),
    }


def embedded_aws_metadata(settings: Settings) -> Mapping[str, Any]:
    accepted = list(settings.trust_aws_accepted_pcr0_list)
    return {
        "pcr0": settings.trust_aws_pcr0 or "not-configured",
        "accepted_pcr0s": accepted or ["not-configured"],
    }


def embedded_azure_metadata(settings: Settings) -> Mapping[str, Any]:
    accepted = list(settings.trust_azure_accepted_hostdata_list)
    return {
        "hostdata": settings.trust_azure_hostdata or "not-configured",
        "accepted_hostdata": accepted or ["not-configured"],
        "attestation_issuers": list(settings.trust_azure_attestation_issuer_list),
    }
