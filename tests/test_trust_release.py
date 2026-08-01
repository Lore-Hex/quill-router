from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services.trust_release import (
    TrustReleaseResolver,
    TrustReleaseUnavailable,
)

SOURCE_COMMIT = "5e7c096"
IMAGE_DIGEST = "sha256:" + "7f" * 32
IMAGE_REFERENCE = (
    "us-central1-docker.pkg.dev/quill-cloud-proxy/quill/"
    f"enclave-multi:gcp-release-{SOURCE_COMMIT}"
)


def release_payload() -> dict[str, object]:
    return {
        "platform": "gcp-confidential-space",
        "attestation_issuer": "https://confidentialcomputing.googleapis.com",
        "attestation_audience": "quill-cloud",
        "source_repo": "https://github.com/Lore-Hex/quill-cloud-proxy",
        "source_commit": SOURCE_COMMIT,
        "image_reference": IMAGE_REFERENCE,
        "image_digest": IMAGE_DIGEST,
    }


@pytest.mark.asyncio
async def test_live_release_is_validated_and_cached(httpx_mock: HTTPXMock) -> None:
    monotonic = [100.0]
    wall_clock = [600.0]
    httpx_mock.add_response(
        method="GET",
        url="https://trust.example/release.json?tr_cache_bucket=10",
        json=release_payload(),
    )
    resolver = TrustReleaseResolver(
        Settings(trust_gcp_release_url="https://trust.example/release.json"),
        monotonic=lambda: monotonic[0],
        wall_clock=lambda: wall_clock[0],
    )

    first = await resolver.resolve()
    second = await resolver.resolve()

    assert first.status == "live"
    assert first.metadata["image_digest"] == IMAGE_DIGEST
    assert second == first
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_live_release_uses_bounded_stale_cache_on_refresh_error(
    httpx_mock: HTTPXMock,
) -> None:
    monotonic = [0.0]
    wall_clock = [600.0]
    httpx_mock.add_response(
        url="https://trust.example/release.json?tr_cache_bucket=10",
        json=release_payload(),
    )
    httpx_mock.add_response(
        url="https://trust.example/release.json?tr_cache_bucket=11",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://trust.example/release.json?tr_cache_bucket=16",
        status_code=503,
    )
    resolver = TrustReleaseResolver(
        Settings(trust_gcp_release_url="https://trust.example/release.json"),
        monotonic=lambda: monotonic[0],
        wall_clock=lambda: wall_clock[0],
    )
    assert (await resolver.resolve()).status == "live"

    monotonic[0] = 61.0
    wall_clock[0] = 660.0
    stale = await resolver.resolve()
    throttled_stale = await resolver.resolve()

    assert stale.status == "stale"
    assert stale.metadata["image_digest"] == IMAGE_DIGEST
    assert throttled_stale == stale
    assert len(httpx_mock.get_requests()) == 2

    monotonic[0] = 361.0
    wall_clock[0] = 960.0
    with pytest.raises(TrustReleaseUnavailable):
        await resolver.resolve()
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_live_release_rejects_invalid_or_expired_metadata(
    httpx_mock: HTTPXMock,
) -> None:
    monotonic = [0.0]
    wall_clock = [600.0]
    invalid = release_payload()
    invalid["image_digest"] = "sha256:not-a-digest"
    httpx_mock.add_response(
        url="https://trust.example/invalid.json?tr_cache_bucket=10",
        json=invalid,
    )
    resolver = TrustReleaseResolver(
        Settings(trust_gcp_release_url="https://trust.example/invalid.json"),
        monotonic=lambda: monotonic[0],
        wall_clock=lambda: wall_clock[0],
    )

    with pytest.raises(TrustReleaseUnavailable):
        await resolver.resolve()
    with pytest.raises(TrustReleaseUnavailable):
        await resolver.resolve()
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_live_release_rejects_oversized_response(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://trust.example/large.json?tr_cache_bucket=10",
        content=b"x" * (64 * 1024 + 1),
    )
    resolver = TrustReleaseResolver(
        Settings(trust_gcp_release_url="https://trust.example/large.json"),
        monotonic=lambda: 0.0,
        wall_clock=lambda: 600.0,
    )

    with pytest.raises(TrustReleaseUnavailable):
        await resolver.resolve()


@pytest.mark.asyncio
async def test_empty_release_url_uses_embedded_local_metadata(
    httpx_mock: HTTPXMock,
) -> None:
    resolver = TrustReleaseResolver(
        Settings(
            trust_gcp_source_commit=SOURCE_COMMIT,
            trust_gcp_image_reference=IMAGE_REFERENCE,
            trust_gcp_image_digest=IMAGE_DIGEST,
        )
    )

    release = await resolver.resolve()

    assert release.status == "embedded"
    assert release.metadata["image_digest"] == IMAGE_DIGEST
    assert httpx_mock.get_requests() == []


def test_alias_trust_routes_render_live_release(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url=re.compile(
            r"https://trust\.trustedrouter\.com/trust/gcp-release\.json"
            r"\?tr_cache_bucket=\d+"
        ),
        json=release_payload(),
    )
    settings = Settings(
        environment="test",
        trust_gcp_release_url=(
            "https://trust.trustedrouter.com/trust/gcp-release.json"
        ),
        trust_gcp_source_commit="stale000",
        trust_gcp_image_reference=(
            "us-central1-docker.pkg.dev/quill-cloud-proxy/quill/"
            "enclave-multi:gcp-release-stale000"
        ),
        trust_gcp_image_digest="sha256:" + "00" * 32,
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        page = client.get("/", headers={"host": "trust.allyrouter.com"})
        digest = client.get(
            "/trust/image-digest-gcp.txt",
            headers={"host": "trust.allyrouter.com"},
        )
        release = client.get(
            "/trust/gcp-release.json",
            headers={"host": "trust.allyrouter.com"},
        )

    assert page.status_code == 200
    assert page.headers["x-trustedrouter-release-status"] == "live"
    assert IMAGE_DIGEST in page.text
    assert "https://api.allyrouter.com/attestation" in page.text
    assert digest.status_code == 200
    assert digest.text.strip() == IMAGE_DIGEST
    assert release.json()["release_metadata_status"] == "live"
    assert release.json()["api_base_urls"] == [
        "https://api.trustedrouter.com/v1",
        "https://api.allyrouter.com/v1",
    ]
    assert len(httpx_mock.get_requests()) == 1


def test_alias_trust_route_fails_closed_without_live_or_cached_release(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/release\.json\?tr_cache_bucket=\d+"),
        status_code=503,
    )
    settings = Settings(
        environment="test",
        trust_gcp_release_url="https://trust.example/release.json",
        trust_gcp_image_digest="sha256:" + "00" * 32,
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        page = client.get("/", headers={"host": "trust.allyrouter.com"})
        digest = client.get(
            "/trust/image-digest-gcp.txt",
            headers={"host": "trust.allyrouter.com"},
        )

    assert page.status_code == 503
    assert page.headers["x-trustedrouter-release-status"] == "unavailable"
    assert "Live release record unavailable" in page.text
    assert "sha256:" + "00" * 32 not in page.text
    assert digest.status_code == 503
    assert digest.text.strip() == "live-release-unavailable"
    assert digest.headers["cache-control"] == "no-store"
    assert len(httpx_mock.get_requests()) == 1
