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
    f"us-central1-docker.pkg.dev/quill-cloud-proxy/quill/enclave-multi:gcp-release-{SOURCE_COMMIT}"
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
async def test_release_uses_independent_validated_fallback(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://trust.trustedrouter.com/release.json?tr_cache_bucket=10",
        status_code=503,
    )
    httpx_mock.add_response(
        url="https://trust.backup.example/release.json?tr_cache_bucket=10",
        json=release_payload(),
    )
    resolver = TrustReleaseResolver(
        Settings(
            trust_gcp_release_url="https://trust.trustedrouter.com/release.json",
            trust_gcp_release_fallback_urls=("https://trust.backup.example/release.json"),
        ),
        monotonic=lambda: 100.0,
        wall_clock=lambda: 600.0,
    )

    release = await resolver.resolve()

    assert release.status == "live"
    assert release.metadata["image_digest"] == IMAGE_DIGEST
    assert [str(request.url) for request in httpx_mock.get_requests()] == [
        "https://trust.trustedrouter.com/release.json?tr_cache_bucket=10",
        "https://trust.backup.example/release.json?tr_cache_bucket=10",
    ]


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
        trust_gcp_release_url=("https://trust.trustedrouter.com/trust/gcp-release.json"),
        trust_gcp_source_commit="stale000",
        trust_gcp_image_reference=(
            "us-central1-docker.pkg.dev/quill-cloud-proxy/quill/enclave-multi:gcp-release-stale000"
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
        "https://api.uptimerouter.com/v1",
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


# --- AWS and Azure serving planes -------------------------------------------
#
# These are deploy-time configured rather than resolved live, so the risk they
# carry is the opposite of the GCP record's: not a fetch that fails, but a
# value that quietly stops being true. The tests below pin the two behaviours
# that keep that survivable — an unconfigured plane must not render as a
# measurement, and the accepted set must always contain what should be serving.

AWS_PCR0 = "ae" + "f4" * 23 + "8a"
AWS_PCR0_PREVIOUS = "bb" + "c1" * 23 + "9d"
AZURE_HOSTDATA = "44" + "e4" * 31
AZURE_HOSTDATA_PREVIOUS = "77" + "a2" * 31


def configured_multicloud_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "test",
        "trust_aws_pcr0": AWS_PCR0,
        "trust_azure_hostdata": AZURE_HOSTDATA,
        "trust_azure_attestation_issuers": "https://trquilluaen.uaen.attest.azure.net",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_aws_and_azure_release_records_publish_configured_measurements() -> None:
    settings = configured_multicloud_settings()
    with TestClient(create_app(settings, init_observability=False)) as client:
        aws = client.get("/trust/aws-release.json")
        azure = client.get("/trust/azure-release.json")

    assert aws.status_code == 200
    assert aws.json()["platform"] == "aws-nitro-enclaves"
    assert aws.json()["pcr0"] == AWS_PCR0
    assert aws.json()["accepted_pcr0s"] == [AWS_PCR0]
    assert aws.json()["measurement_type"] == "nitro-pcr0-sha384"
    # The self-signed certificate is the design; the page must say what stands
    # in for chain validation rather than leaving it looking like a weakness.
    assert aws.json()["tls"]["mode"] == "attested-self-signed-inside-enclave"
    assert "exporter" in aws.json()["tls"]["certificate_binding"]

    assert azure.status_code == 200
    assert azure.json()["platform"] == "azure-confidential-containers-sev-snp"
    assert azure.json()["hostdata"] == AZURE_HOSTDATA
    assert azure.json()["accepted_hostdata"] == [AZURE_HOSTDATA]
    assert azure.json()["attestation_issuers"] == ["https://trquilluaen.uaen.attest.azure.net"]
    # Same repo set as GCP, so a reader comparing planes sees one source of code.
    assert azure.json()["source_repositories"] == aws.json()["source_repositories"]


def test_unconfigured_plane_fails_closed_rather_than_publishing_a_placeholder() -> None:
    # The bug this prevents shipped once already on the Quill trust bucket: a
    # measurement file that outlived the enclave it described. Serving 200 with
    # "not-configured" would be the same mistake in a new costume — a verifier
    # scripting against this must get "no answer", not a string to compare.
    settings = Settings(environment="test")
    with TestClient(create_app(settings, init_observability=False)) as client:
        aws = client.get("/trust/aws-release.json")
        azure = client.get("/trust/azure-release.json")
        pcr0 = client.get("/trust/pcr0-aws.txt")
        hostdata = client.get("/trust/hostdata-azure.txt")

    assert aws.status_code == 503
    assert azure.status_code == 503
    assert aws.json()["release_metadata_status"] == "not-configured"
    assert azure.json()["release_metadata_status"] == "not-configured"
    assert aws.json()["accepted_pcr0s"] == []
    assert azure.json()["accepted_hostdata"] == []
    assert aws.headers["cache-control"] == "no-store"
    assert azure.headers["cache-control"] == "no-store"
    assert pcr0.status_code == 503
    assert hostdata.status_code == 503
    assert pcr0.text == ""
    assert hostdata.text == ""


def test_bind_window_publishes_both_measurements_with_the_incoming_one_primary() -> None:
    # During a rollout the released key is bound to both measurements at once,
    # so a verifier that accepts only one fails exactly while a deploy is in
    # flight. Both values must be published, and the primary must survive being
    # omitted from the accepted list — a set that rejects the enclave currently
    # answering is worse than no set at all.
    settings = configured_multicloud_settings(
        trust_aws_accepted_pcr0s=AWS_PCR0_PREVIOUS,
        trust_azure_accepted_hostdata=f"{AZURE_HOSTDATA_PREVIOUS},{AZURE_HOSTDATA}",
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        aws = client.get("/trust/aws-release.json").json()
        azure = client.get("/trust/azure-release.json").json()
        pcr0 = client.get("/trust/pcr0-aws.txt")

    assert aws["pcr0"] == AWS_PCR0
    assert aws["accepted_pcr0s"] == [AWS_PCR0, AWS_PCR0_PREVIOUS]
    assert azure["hostdata"] == AZURE_HOSTDATA
    # Configured twice (primary plus the accepted list); published once.
    assert azure["accepted_hostdata"] == [AZURE_HOSTDATA, AZURE_HOSTDATA_PREVIOUS]
    assert pcr0.status_code == 200
    assert pcr0.text.split() == [AWS_PCR0, AWS_PCR0_PREVIOUS]


def test_trust_page_shows_every_plane_and_flags_the_ones_without_a_measurement() -> None:
    configured = configured_multicloud_settings(trust_gcp_image_digest="sha256:" + "00" * 32)
    with TestClient(create_app(configured, init_observability=False)) as client:
        page = client.get("/trust").text
    assert AWS_PCR0 in page
    assert AZURE_HOSTDATA in page
    assert "aws-release.json" in page
    assert "azure-release.json" in page
    assert "No measurement published for this plane yet" not in page

    bare = Settings(environment="test", trust_gcp_image_digest="sha256:" + "00" * 32)
    with TestClient(create_app(bare, init_observability=False)) as client:
        bare_page = client.get("/trust").text
    # Absence has to be legible on the page too, not just in the JSON.
    assert bare_page.count("No measurement published for this plane yet") == 2
