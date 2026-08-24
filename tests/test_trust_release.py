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
async def test_a_validator_raising_an_unforeseen_class_is_still_unavailable_not_a_crash(
    httpx_mock: HTTPXMock,
) -> None:
    """The resolver cannot know its validator's exception vocabulary.

    `validator` is a constructor parameter, so the set of exceptions it can
    raise is not knowable here — and this used to be an enumeration,
    `except (httpx.HTTPError, TypeError, ValueError)`. The enumeration was
    wrong: validated_azure_metadata was widened to parse region URLs with
    httpx.URL, which raises httpx.InvalidURL, a plain Exception outside
    httpx.HTTPError. It escaped resolve(), sailed past the route's
    `except TrustReleaseUnavailable`, and /trust/azure-release.json answered 500
    with no fallback and no backoff.

    Asserted with a validator raising a class nothing in this repo raises,
    because the point is not that InvalidURL is handled now — it is that a
    failure to produce a validated record is ONE outcome for a mirror however it
    arrives. The backoff is asserted too: an escape skips the whole failure
    path, so every subsequent request went back to the upstream.
    """

    class UnforeseenValidatorError(Exception):
        pass

    def hostile_validator(payload: object) -> dict[str, object]:
        raise UnforeseenValidatorError("a class this layer was never told about")

    httpx_mock.add_response(
        url="https://trust.example/release.json?tr_cache_bucket=10",
        json=release_payload(),
    )
    resolver = TrustReleaseResolver(
        Settings(trust_gcp_release_url="https://trust.example/release.json"),
        monotonic=lambda: 0.0,
        wall_clock=lambda: 600.0,
        validator=hostile_validator,
    )

    with pytest.raises(TrustReleaseUnavailable) as first:
        await resolver.resolve()
    with pytest.raises(TrustReleaseUnavailable):
        await resolver.resolve()

    assert isinstance(first.value.__cause__, UnforeseenValidatorError), (
        "the cause has to travel with it, or a real bug becomes an untraceable 503"
    )
    assert len(httpx_mock.get_requests()) == 1, "the second resolve must be inside the backoff"


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


# --- A mirror serves a record; it does not rewrite it ------------------------


def test_gcp_record_describes_the_gcp_plane_from_every_deployment() -> None:
    # Shipped live for some time: the AWS- and Azure-hosted control planes each
    # served a gcp-confidential-space record, Google issuer and audience intact,
    # whose api_base_url pointed at their OWN gateway — because api_base_url came
    # from per-deployment settings. Verified on production 2026-08-15:
    # aws.trustedrouter.com advertised https://api-aws.trustedrouter.com/v1.
    #
    # A verifier following that record fetches COSE_Sign1 CBOR over a self-signed
    # certificate while expecting a Confidential Space JWT, and correctly
    # concludes the running code does not match the published measurement. The
    # accusation of tampering is manufactured entirely by us, which makes this
    # worse than serving nothing.
    from trusted_router.trust import gcp_release

    canonical = Settings(environment="test", trust_gcp_image_digest="sha256:" + "11" * 32)
    aws_hosted = Settings(
        environment="test",
        trust_gcp_image_digest="sha256:" + "11" * 32,
        api_base_url="https://api-aws.trustedrouter.com/v1",
    )
    azure_hosted = Settings(
        environment="test",
        trust_gcp_image_digest="sha256:" + "11" * 32,
        api_base_url="https://api-azure.trustedrouter.com/v1",
    )

    for settings in (canonical, aws_hosted, azure_hosted):
        record = gcp_release(settings)
        assert record["platform"] == "gcp-confidential-space"
        assert record["api_base_url"] == "https://api.trustedrouter.com/v1", (
            "a GCP record must name the GCP plane no matter which deployment mirrors it"
        )
        assert record["tls"]["hostname"] == "api.trustedrouter.com"
        # The two fields must never disagree about which host terminates the
        # prompt path; that disagreement is what makes the record unverifiable.
        assert record["api_base_url"] == f"https://{record['tls']['hostname']}/v1"
        # The PLURAL had the same leak and kept it after the scalar was fixed:
        # api_base_urls was built with api_base_url_for_domain(), which returns
        # settings.api_base_url for the canonical domain — per-deployment — so
        # entry 0 of a gcp-confidential-space record served from the AWS-hosted
        # control plane named api-aws.trustedrouter.com. Every entry is a
        # property of the GCP plane, and must pair with tls.hostnames entry for
        # entry.
        assert record["api_base_urls"] == [
            f"https://{hostname}/v1" for hostname in record["tls"]["hostnames"]
        ]
        assert record["api_base_urls"][0] == record["api_base_url"]
        assert not any("api-aws" in url or "api-azure" in url for url in record["api_base_urls"])


def test_gcp_endpoint_fields_are_derived_from_the_domain_not_hardcoded() -> None:
    # Without this case the assertions above pass against a hardcoded
    # "api.trustedrouter.com", because the default domain makes the literal and
    # the derived value identical. Exercising a different canonical domain is
    # what proves the value is computed rather than coincidentally right — the
    # difference matters the day the record is served under another domain.
    from trusted_router.trust import gcp_release

    settings = Settings(
        environment="test",
        trusted_domain="allyrouter.com",
        trust_gcp_image_digest="sha256:" + "22" * 32,
    )
    record = gcp_release(settings)
    assert record["tls"]["hostname"] == "api.allyrouter.com"
    assert record["api_base_url"] == "https://api.allyrouter.com/v1"


def test_every_plane_record_is_self_consistent_about_its_own_endpoint() -> None:
    from trusted_router.trust import aws_release, azure_release, gcp_release

    settings = configured_multicloud_settings(
        trust_gcp_image_digest="sha256:" + "11" * 32,
        api_base_url="https://api-aws.trustedrouter.com/v1",
    )
    for record in (gcp_release(settings), aws_release(settings), azure_release(settings)):
        assert record["api_base_url"] == f"https://{record['tls']['hostname']}/v1", (
            f"{record['platform']} points a verifier at {record['api_base_url']} while "
            f"claiming TLS terminates at {record['tls']['hostname']}"
        )


def test_rolling_accepted_set_survives_the_mirror(httpx_mock: HTTPXMock) -> None:
    # Live on 2026-08-15: trust.trustedrouter.com correctly published BOTH
    # digests mid-roll, and trustedrouter.com republished only the incoming one
    # because the resolver kept just three scalar fields. The fleet was still
    # serving the outgoing digest, so anyone verifying against the control
    # plane's copy would have concluded the enclave did not match its published
    # measurement. Narrowing a pin in transit turns a mirror into an author.
    outgoing = "sha256:" + "a7" * 32
    incoming = "sha256:" + "7a" * 32
    payload = release_payload()
    payload["image_digest"] = incoming
    payload["accepted_image_digests"] = [outgoing, incoming]
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/release\.json\?tr_cache_bucket=\d+"),
        json=payload,
    )
    settings = Settings(
        environment="test", trust_gcp_release_url="https://trust.example/release.json"
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        record = client.get("/trust/gcp-release.json").json()

    assert record["image_digest"] == incoming
    assert outgoing in record["accepted_image_digests"], (
        "the still-serving digest was dropped; a verifier hitting an enclave that "
        "has not rolled yet would read this record as a measurement mismatch"
    )
    assert record["release_state"] == "rolling"


def test_primary_digest_is_always_in_its_own_accepted_set(httpx_mock: HTTPXMock) -> None:
    # An upstream record whose accepted set omits its own current digest must
    # not produce a published set that rejects the running enclave.
    payload = release_payload()
    payload["accepted_image_digests"] = ["sha256:" + "cc" * 32]
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/release\.json\?tr_cache_bucket=\d+"),
        json=payload,
    )
    settings = Settings(
        environment="test", trust_gcp_release_url="https://trust.example/release.json"
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        record = client.get("/trust/gcp-release.json").json()
    assert record["image_digest"] in record["accepted_image_digests"]


# --- The control plane mirrors; it does not author ---------------------------


def _aws_upstream(pcr0: str, accepted: list[str]) -> dict[str, object]:
    return {"platform": "aws-nitro-enclaves", "pcr0": pcr0, "accepted_pcr0s": accepted}


def test_aws_record_is_mirrored_from_the_plane_not_control_plane_config(
    httpx_mock: HTTPXMock,
) -> None:
    # The point of the change: what the AWS enclave runs is published by AWS's
    # own pipeline. If the control plane computed it from its own settings, one
    # deployment would be the authority for three independent planes — a single
    # place to falsify and a single place to fail.
    upstream = "ab" * 48
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/aws\.json\?tr_cache_bucket=\d+"),
        json=_aws_upstream(upstream, [upstream]),
    )
    settings = Settings(
        environment="test",
        trust_aws_release_url="https://trust.example/aws.json",
        trust_aws_pcr0="cd" * 48,  # stale local config, deliberately different
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        record = client.get("/trust/aws-release.json").json()

    assert record["pcr0"] == upstream, "the mirrored record must win over local config"
    assert record["accepted_pcr0s"] == [upstream]


def test_unreachable_upstream_falls_back_without_claiming_it_is_live(
    httpx_mock: HTTPXMock,
) -> None:
    # An upstream outage should degrade to a stale-but-verified measurement
    # rather than to none — but the response must not imply it was just
    # confirmed from the source.
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/aws\.json\?tr_cache_bucket=\d+"), status_code=503
    )
    configured = "ef" * 48
    settings = Settings(
        environment="test",
        trust_aws_release_url="https://trust.example/aws.json",
        trust_aws_pcr0=configured,
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        response = client.get("/trust/aws-release.json")

    assert response.status_code == 200
    assert response.json()["pcr0"] == configured
    assert response.headers["x-trustedrouter-release-status"] == "embedded"


def test_a_poisoned_upstream_record_is_rejected_not_republished(
    httpx_mock: HTTPXMock,
) -> None:
    # Mirroring must not mean repeating whatever arrives. A record whose
    # accepted set excludes its own current measurement would have a verifier
    # reject the very enclave answering them, so it is refused and the verified
    # local fallback is served instead.
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/aws\.json\?tr_cache_bucket=\d+"),
        json=_aws_upstream("ab" * 48, ["cd" * 48]),
    )
    configured = "ef" * 48
    settings = Settings(
        environment="test",
        trust_aws_release_url="https://trust.example/aws.json",
        trust_aws_pcr0=configured,
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        record = client.get("/trust/aws-release.json").json()

    assert record["pcr0"] == configured, "a self-inconsistent upstream record was republished"


def test_azure_mirror_requires_an_issuer_and_carries_every_region(
    httpx_mock: HTTPXMock,
) -> None:
    uaen, syd = "44" * 32, "26" * 32
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/azure\.json\?tr_cache_bucket=\d+"),
        json={
            "platform": "azure-confidential-containers-sev-snp",
            "hostdata": uaen,
            "accepted_hostdata": [uaen, syd],
            "attestation_issuers": [
                "https://trquilluaen.uaen.attest.azure.net",
                "https://trquillsyd.eau.attest.azure.net",
            ],
            "regions": [
                {
                    "attestation_url": "https://api-azure.trustedrouter.com/attestation",
                    "hostdata": uaen,
                    "attestation_issuer": "https://trquilluaen.uaen.attest.azure.net",
                },
                {
                    "attestation_url": "https://api-azure-syd.trustedrouter.com/attestation",
                    "hostdata": syd,
                    "attestation_issuer": "https://trquillsyd.eau.attest.azure.net",
                },
            ],
        },
    )
    settings = Settings(
        environment="test", trust_azure_release_url="https://trust.example/azure.json"
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        record = client.get("/trust/azure-release.json").json()

    # Both regions run different CCE policies, so both hostdata values are
    # permanently correct. Dropping either makes a verifier routed to that
    # region conclude tampering.
    assert record["accepted_hostdata"] == [uaen, syd]
    assert len(record["attestation_issuers"]) == 2
    # ...and WHERE each of them answers has to survive the trip too. This
    # assertion used to be absent while the test's name promised it: the
    # validator whitelisted three scalar keys, so the array never reached
    # trust.azure_release and the drift check had one endpoint to enumerate.
    assert [region["attestation_url"] for region in record["regions"]] == [
        "https://api-azure.trustedrouter.com/attestation",
        "https://api-azure-syd.trustedrouter.com/attestation",
    ]
    assert record["regions"][1]["hostdata"] == syd


def test_azure_record_without_an_issuer_is_rejected(httpx_mock: HTTPXMock) -> None:
    # An MAA record naming no issuer is unverifiable: the reader has no way to
    # know which attestation service's signature to trust, so "hostdata is X"
    # is an unsupported assertion. Republishing it would look like a measurement
    # while carrying none of the evidence that makes one meaningful.
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/azure\.json\?tr_cache_bucket=\d+"),
        json={
            "platform": "azure-confidential-containers-sev-snp",
            "hostdata": "44" * 32,
            "accepted_hostdata": ["44" * 32],
            "attestation_issuers": [],
        },
    )
    configured = "99" * 32
    settings = Settings(
        environment="test",
        trust_azure_release_url="https://trust.example/azure.json",
        trust_azure_hostdata=configured,
        trust_azure_attestation_issuers="https://trquilluaen.uaen.attest.azure.net",
    )
    with TestClient(create_app(settings, init_observability=False)) as client:
        record = client.get("/trust/azure-release.json").json()

    assert record["hostdata"] == configured, "an issuer-less upstream record was republished"
    assert record["attestation_issuers"] == ["https://trquilluaen.uaen.attest.azure.net"]
