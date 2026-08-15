"""The drift check must cover every plane it reports on, and say what it covered.

THE LAW
    scripts/verify_trust_measurements.py may print a success line only for the
    endpoints it actually contacted. Concretely:
      * every plane that publishes a measurement is contacted (GCP included);
      * every region the record enumerates is contacted (both Azure regions,
        not the first one), and each is compared against the hostdata the
        record attributes to THAT region, not merely against the union;
      * a serving region the record cannot give an endpoint for is reported as
        an uncovered plane and exits non-zero, never as a pass — and the issuer
        it names as unreached is one no contacted endpoint presented, not one
        picked by list position;
      * coverage is counted over DISTINCT ENDPOINTS THAT ANSWERED, so a record
        naming the same URL twice cannot report two regions covered;
      * a record that carries no region census — no attestation_issuers, or no
        regions[] array while naming more than one accepted hostdata value —
        cannot certify its own coverage and is a gap, not a pass;
      * under --strict, a plane that publishes nothing is a failure;
      * the summary states the planes and endpoint count it is based on, so a
        success sentence can never outrun its own coverage again.

    And the law binds the SERVING ROUTE, not just the script. The checker's
    coverage is bounded by what the control plane actually serves, so the
    mirror proofs at the bottom of this file drive trusted_router through its
    real HTTP route rather than calling trust.azure_release directly. That
    distinction is not pedantry: the first version of this change fixed
    azure_release and left validated_azure_metadata stripping `regions` one
    layer earlier, so both mirror proofs passed while the record production
    served was byte-identical to before the change.

WHY A PROOF AND NOT JUST A TEST
    The thing being defended is a claim we make to strangers: "the measurement
    on our trust page is what is running". The only evidence for that claim is
    this script's exit code, and an exit code that is computed from half the
    fleet is indistinguishable from one computed from all of it. There is no
    user-visible symptom when the coverage narrows — the output gets shorter
    and stays green — so nothing but an assertion on WHICH endpoints were
    contacted can hold the line. That is what these fixtures assert: not that a
    comparison function returns False, but that a specific URL was fetched.

THE REAL DEFECT
    Measured 2026-08-15 by running the previous version of the script
    (82be9cdf) against a fixture plane and against production:
      1. Nothing invoked the script. Its only mentions in this repo were two
         comments naming it as the thing that would catch drift, in
         config.py:365 and trust.py:142.
      2. AZURE_ATTESTATION_URL was hardcoded to UAE North. Southeast Asia
         serves api-azure-sea.trustedrouter.com under its own CCE policy and
         therefore its own hostdata, f3a0b4ed…d712d81c, which was published in
         accepted_hostdata and compared against nothing. Head to head on a
         fixture where Southeast Asia served a hostdata published nowhere, the
         old script fetched the UAE North endpoint only, printed
         "[ok] azure: hostdata and issuer match" and exited 0; the current one
         fetches both and exits 1.
      3. It then printed "Every published measurement matches a live
         attestation." Confirmed verbatim in that same run.

    THREE NEGATIVE RESULTS, recorded because each refutes a claim in the brief
    this work started from and acting on any of them would have been wasted or
    wrong:
      * "It does not check GCP AT ALL; main() loops over (aws, azure)." False.
        82be9cdf's main() loops over gcp, aws and azure, and its check_gcp
        compares the running digest against accepted_image_digests — the
        fixture run above printed "[ok] gcp: image digest matches". The real
        GCP gaps were narrower: a hardcoded endpoint and an unchecked
        image_reference.
      * "Its only references are comments in scripts/deploy/rollout.sh." False.
        That file does not mention the script; the comments are in config.py
        and trust.py. The substance — nothing executes it — held.
      * "The published azure-release.json lists a regions array." True of
        trust.trustedrouter.com, FALSE of the control plane the checker reads
        by default. trusted_router.trust.azure_release rebuilt the record from
        four scalar fields and dropped the array, so the endpoints the checker
        enumerates never reached trustedrouter.com at all. Fixing the checker
        alone would have left it structurally unable to find region two, which
        is why the mirror is fixed and proved here too: the checker's coverage
        is bounded by what the mirror carries.
      * "Fixing trust.azure_release makes the SERVED record carry the array."
        FALSE, and this one cost a round. The serving route resolves Azure
        metadata through TrustReleaseResolver with
        services.trust_release.validated_azure_metadata as its validator, and
        that function returned exactly {hostdata, accepted_hostdata,
        attestation_issuers}: `regions` was stripped one layer BEFORE
        azure_release ever saw it. The patched mirror was dead code on the
        production route, and the hourly --strict job would have been
        permanently red on [GAP] azure. Driven through the real route the served
        record still had `regions: []` and both issuers — indistinguishable from
        the state the change claimed to fix. The validator now validates and
        carries the array, and the proofs at the bottom of this file go through
        the route so that composition cannot be skipped again.

SCOPE LIMIT — WHAT THIS DOES NOT ESTABLISH
    * Nothing here verifies a signature. The fixture attestations are
      unsigned and the checker does not check signatures either; both stop at
      "is the published value the value being served". The cryptographic
      verifier is quill-cloud-proxy's tools/verify-attestation.py.
    * AWS is sampled, never enumerated. These tests prove the checker reports
      the distinct enclave count it observed and labels it sampling; they
      cannot prove the fleet was covered, and neither can the checker.
      Measured live: 8 consecutive fetches from one client all reached
      i-02e34e58761097671-enc01a004d7a9c3c307 while the published record's
      observed_module_id names a different enclave — so repeated sampling from
      one vantage point does NOT converge on the fleet. Sampling narrows the
      blind spot from "one of N" to "some of N"; the honest output says so.
    * A green run proves the endpoints named in its own summary matched. It
      proves nothing about an endpoint the record does not mention, which is
      exactly why an unattributable issuer is a failure rather than a note.
    * Every plane here is recorded, not live. These prove the checker's logic
      and the shape of what the route serves; they are not evidence about what
      any deployed control plane publishes today, and no assertion in this file
      should be read as one.
    * A record that names one region, one issuer, and TWO accepted hostdata
      values is accepted as covered. That shape is a one-region plane mid-roll
      and a two-region plane whose second region was never published, and the
      record does not distinguish them — accepted_hostdata rolls, so it cannot
      be used as a region count. Closing this needs the plane to publish the
      region, which is what GAP_REMEDIATION asks for; it is not closeable from
      the reader's side.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import cbor2
import pytest

from scripts import verify_trust_measurements as drift

CONTROL_PLANE = "https://control.example"

GCP_DIGEST = "sha256:" + "fa" * 32
GCP_DIGEST_OUTGOING = "sha256:" + "a7" * 32
GCP_REFERENCE = (
    "us-central1-docker.pkg.dev/quill-cloud-proxy/quill/enclave-multi:gcp-release-f57b791"
)
GCP_ISSUER = "https://confidentialcomputing.googleapis.com"

AWS_PCR0 = "2323b48e8fa4a74b2898459b041da94dd992ee7cc3e9fd0512fb0aa410b2f17e184512c182a39ec35fe7942a07958220"
AWS_MODULE_A = "i-0ada95aad6d11aa56-enc01a004f1c2824652"
AWS_MODULE_B = "i-02e34e58761097671-enc01a004d7a9c3c307"
AWS_CERT_DER = b"-----der-of-the-cert-this-connection-served-----"

UAEN_HOSTDATA = "c55d492aaf98db95e60ac87c0d4a787e07d565b460380a6c16bfcc418d60b89e"
SEA_HOSTDATA = "f3a0b4ed5b27c81ceded35a54ddeedf2795fb733b4ecc026f8597311d712d81c"
UAEN_ISSUER = "https://trquilluaen.uaen.attest.azure.net"
SEA_ISSUER = "https://trquillsea.sasia.attest.azure.net"
UAEN_URL = "https://api-azure.trustedrouter.com/attestation"
SEA_URL = "https://api-azure-sea.trustedrouter.com/attestation"


# ---------------------------------------------------------------------------
# Fixture planes — recorded from production, shapes verbatim
# ---------------------------------------------------------------------------


def _jwt(claims: dict[str, Any]) -> bytes:
    """A three-part token whose signature is a placeholder.

    Deliberately unsigned: the checker does not verify signatures and must not
    be given a fixture that lets it look as if it did.
    """
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return b"eyJhbGciOiJSUzI1NiJ9." + body + b".not-a-signature"


def gcp_token(digest: str = GCP_DIGEST, reference: str = GCP_REFERENCE) -> bytes:
    return _jwt(
        {
            "iss": GCP_ISSUER,
            "aud": "quill-cloud",
            "submods": {"container": {"image_digest": digest, "image_reference": reference}},
        }
    )


def azure_token(hostdata: str, issuer: str) -> bytes:
    return _jwt(
        {
            "iss": issuer,
            "x-ms-attestation-type": "sevsnpvm",
            "x-ms-sevsnpvm-hostdata": hostdata,
            "x-ms-compliance-status": "azure-compliant-uvm",
        }
    )


def aws_document(
    *, pcr0: str = AWS_PCR0, module_id: str = AWS_MODULE_A, cert_der: bytes = AWS_CERT_DER
) -> bytes:
    """The COSE_Sign1 shape the Nitro Security Module emits.

    user_data is 96 bytes with the layout measured on the live enclave:
    [0:32] SHA-256 of the served certificate DER, [32:64] a build-invariant
    constant (same value across nonces and connections; not interpreted here),
    [64:96] the TLS exporter channel binding, which varies per connection.
    """
    user_data = hashlib.sha256(cert_der).digest() + bytes(32) + b"\xe1" * 32
    payload = {
        "module_id": module_id,
        "digest": "SHA384",
        "pcrs": {0: bytes.fromhex(pcr0)},
        "user_data": user_data,
    }
    return cbor2.dumps([b"\xa1\x01\x38\x22", {}, cbor2.dumps(payload), b"sig" * 32])


def gcp_record(**overrides: Any) -> dict[str, Any]:
    record = {
        "platform": "gcp-confidential-space",
        "image_digest": GCP_DIGEST,
        "accepted_image_digests": [GCP_DIGEST],
        "image_reference": GCP_REFERENCE,
        "accepted_image_references": [GCP_REFERENCE],
        "attestation_issuer": GCP_ISSUER,
        "api_base_url": "https://api.trustedrouter.com/v1",
    }
    record.update(overrides)
    return record


def aws_record(**overrides: Any) -> dict[str, Any]:
    record = {
        "platform": "aws-nitro-enclaves",
        "pcr0": AWS_PCR0,
        "accepted_pcr0s": [AWS_PCR0],
        "observed_module_id": AWS_MODULE_A,
        "api_base_url": "https://api-aws.trustedrouter.com/v1",
    }
    record.update(overrides)
    return record


def azure_record(**overrides: Any) -> dict[str, Any]:
    """Both regions, exactly as trust.trustedrouter.com publishes them."""
    record = {
        "platform": "azure-confidential-containers-sev-snp",
        "hostdata": UAEN_HOSTDATA,
        "accepted_hostdata": [UAEN_HOSTDATA, SEA_HOSTDATA],
        "attestation_issuers": [UAEN_ISSUER, SEA_ISSUER],
        "api_base_url": "https://api-azure.trustedrouter.com/v1",
        "regions": [
            {
                "attestation_url": UAEN_URL,
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            },
            {
                "attestation_url": SEA_URL,
                "hostdata": SEA_HOSTDATA,
                "attestation_issuer": SEA_ISSUER,
            },
        ],
    }
    record.update(overrides)
    return record


class FakeTransport(drift.Transport):
    """A recorded plane. Records what was fetched, which is the real assertion.

    An unmapped URL raises rather than 404s: a check that quietly reads
    "unpublished" from a typo'd endpoint is the failure mode these fixtures
    exist to make impossible.
    """

    def __init__(self, responses: dict[str, Any], *, missing: tuple[str, ...] = ()) -> None:
        self.responses = responses
        self.missing = missing
        self.fetched: list[str] = []

    def fetch(
        self, url: str, *, verify_tls: bool = True, want_peer_certificate: bool = False
    ) -> drift.Fetched:
        self.fetched.append(url)
        if url in self.missing:
            raise _http_error(url, 404)
        if url not in self.responses:
            raise AssertionError(f"unexpected fetch of {url}")
        value = self.responses[url]
        if callable(value):
            value = value(len([seen for seen in self.fetched if seen == url]) - 1)
        body = json.dumps(value).encode() if isinstance(value, dict) else value
        peer = AWS_CERT_DER if want_peer_certificate else None
        return drift.Fetched(body, peer)


def _http_error(url: str, code: int) -> Exception:
    import urllib.error

    return urllib.error.HTTPError(url, code, "missing", None, None)  # type: ignore[arg-type]


def whole_fleet(
    *,
    gcp: dict[str, Any] | None = None,
    aws: dict[str, Any] | None = None,
    azure: dict[str, Any] | None = None,
    gcp_live: bytes | None = None,
    uaen_live: bytes | None = None,
    sea_live: bytes | None = None,
    aws_live: Any = None,
) -> FakeTransport:
    """Every plane healthy and matching, unless a caller drifts one."""
    return FakeTransport(
        {
            f"{CONTROL_PLANE}/trust/gcp-release.json": gcp if gcp is not None else gcp_record(),
            f"{CONTROL_PLANE}/trust/aws-release.json": aws if aws is not None else aws_record(),
            f"{CONTROL_PLANE}/trust/azure-release.json": (
                azure if azure is not None else azure_record()
            ),
            "https://api.trustedrouter.com/attestation": gcp_live or gcp_token(),
            "https://api-aws.trustedrouter.com/attestation": (
                aws_live if aws_live is not None else aws_document()
            ),
            UAEN_URL: uaen_live or azure_token(UAEN_HOSTDATA, UAEN_ISSUER),
            SEA_URL: sea_live or azure_token(SEA_HOSTDATA, SEA_ISSUER),
        }
    )


def results_by_plane(transport: FakeTransport) -> dict[str, drift.Result]:
    return {
        result.plane: result for result in drift.run_checks(CONTROL_PLANE, transport, aws_samples=2)
    }


# ---------------------------------------------------------------------------
# GCP — the plane that carries every prompt
# ---------------------------------------------------------------------------


def test_gcp_is_contacted_at_all() -> None:
    """Asserted on the socket, not on the verdict.

    A checker that returned ok("gcp") without fetching anything satisfies every
    assertion about exit codes, and a plane silently dropped from the loop is
    exactly the shape of failure this whole file exists for. The negative
    result stands: GCP had NOT been dropped (see the module docstring), so this
    is a fence around a hole that was not open, not a repair.
    """
    transport = whole_fleet()
    results = results_by_plane(transport)

    assert "https://api.trustedrouter.com/attestation" in transport.fetched
    assert results["gcp"].ok
    assert results["gcp"].endpoints == ("https://api.trustedrouter.com/attestation",)


def test_gcp_endpoint_comes_from_the_record_not_a_constant() -> None:
    """The record tells a verifier where to look; the checker must look there.

    Hardcoding endpoints here is precisely how the second Azure region stayed
    invisible while the record already named it.
    """
    record = gcp_record(api_base_url="https://api.allyrouter.com/v1")
    transport = FakeTransport(
        {
            f"{CONTROL_PLANE}/trust/gcp-release.json": record,
            "https://api.allyrouter.com/attestation": gcp_token(),
        }
    )
    result = drift.check_gcp(CONTROL_PLANE, transport)

    assert result.ok
    assert "https://api.allyrouter.com/attestation" in transport.fetched
    assert drift.GCP_ATTESTATION_URL not in transport.fetched


def test_gcp_digest_drift_fails_and_prints_both_values(capsys: pytest.CaptureFixture[str]) -> None:
    """The headline case: the running image is not what the trust page claims."""
    running = "sha256:" + "de" * 32
    transport = whole_fleet(gcp_live=gcp_token(digest=running))

    code = drift.main(["--control-plane", CONTROL_PLANE], transport=transport)
    out = capsys.readouterr().out

    assert code == 1
    assert "[DRIFT] gcp" in out
    assert running in out and GCP_DIGEST in out


def test_gcp_matches_the_accepted_set_during_a_roll() -> None:
    """A rolling deploy serves the outgoing digest while the record names the
    incoming one. Comparing against the scalar would report drift on every
    deploy and teach the reader to ignore the check."""
    record = gcp_record(accepted_image_digests=[GCP_DIGEST, GCP_DIGEST_OUTGOING])
    transport = whole_fleet(gcp=record, gcp_live=gcp_token(digest=GCP_DIGEST_OUTGOING))
    result = results_by_plane(transport)["gcp"]

    assert result.ok
    assert "rolling" in result.detail


def test_gcp_image_reference_drift_is_reported() -> None:
    """Both fields are published, so both have to be true.

    The digest is the measurement and the reference is only a name — but a
    verifier told to expect one tag and handed another cannot tell which half
    of our record is stale, and the safe reading available to them is that the
    workload was swapped.
    """
    transport = whole_fleet(
        gcp_live=gcp_token(reference="us-central1-docker.pkg.dev/x/quill/enclave-multi:gcp-old")
    )
    result = results_by_plane(transport)["gcp"]

    assert not result.ok
    assert "reference" in result.detail


def test_the_endpoints_gcp_publishes_but_does_not_contact_are_printed_not_hidden() -> None:
    """A scope limit that lives only in a docstring is a scope limit nobody reads.

    The module docstring used to claim "every endpoint the record says exists
    was actually contacted" as a global invariant. It was true of Azure's
    regions[] and false of the GCP record's api_base_urls[] and tls.hostnames[],
    which name alias hostnames for the same workload and are not fetched. The
    claim is now stated per plane, and the endpoints it excludes are printed
    beside the verdict so a reader sees the boundary where the verdict is.
    """
    record = gcp_record(
        api_base_urls=[
            "https://api.trustedrouter.com/v1",
            "https://api.allyrouter.com/v1",
        ]
    )
    result = drift.check_gcp(CONTROL_PLANE, whole_fleet(gcp=record))
    printed = "\n".join(result.extra)

    assert result.ok
    assert result.endpoints == ("https://api.trustedrouter.com/attestation",)
    assert "https://api.allyrouter.com/attestation" in printed
    assert "NOT contacted by this run" in printed
    # ...and the one it did contact is not listed as skipped.
    assert printed.count("https://api.trustedrouter.com/attestation") == 1


# ---------------------------------------------------------------------------
# Azure — every region the record enumerates
# ---------------------------------------------------------------------------


def test_every_enumerated_region_is_contacted() -> None:
    """The single-region blind spot. Asserted on the fetch list, not the verdict.

    Comparing UAE North's hostdata against the union of both regions' accepted
    values passes whether or not Southeast Asia was ever contacted, so a
    verdict-only assertion would have gone green against the broken script.
    """
    transport = whole_fleet()
    result = results_by_plane(transport)["azure"]

    assert UAEN_URL in transport.fetched
    assert SEA_URL in transport.fetched, "the second region was never contacted"
    assert result.ok
    assert result.endpoints == (UAEN_URL, SEA_URL)
    # The success sentence is counted from the fetch list and the live tokens,
    # so it cannot describe more of the plane than answered.
    assert "2 endpoint(s) contacted, 2 distinct MAA issuer(s) presented" in result.detail
    assert "covering all 2 published MAA issuer(s)" in result.detail


def test_second_region_drift_fails_even_though_the_first_is_clean() -> None:
    """The exact shape the old script could not see.

    UAE North still serves its published hostdata, so a single-endpoint check
    reports the plane healthy. Southeast Asia is serving something nobody
    published, and a verifier routed there gets a mismatch we told them means
    tampering.
    """
    drifted = "9c" * 32
    transport = whole_fleet(sea_live=azure_token(drifted, SEA_ISSUER))
    result = results_by_plane(transport)["azure"]

    assert not result.ok
    assert SEA_URL in result.detail
    # ...and the region that is fine is not blamed for it.
    assert UAEN_URL not in result.detail


def test_a_region_serving_its_neighbours_policy_is_drift() -> None:
    """Set membership alone is not enough on a multi-region plane.

    Southeast Asia answering with UAE North's hostdata is inside the accepted
    union and is still a misconfiguration: one region is running the other's
    CCE policy. The record attributes a hostdata to each region, so the check
    is per region.
    """
    transport = whole_fleet(sea_live=azure_token(UAEN_HOSTDATA, SEA_ISSUER))
    result = results_by_plane(transport)["azure"]

    assert not result.ok
    assert "another region" in result.detail


def test_a_serving_region_the_record_cannot_reach_is_a_gap_not_a_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A live region absent from the record's endpoint list.

    The record still publishes Southeast Asia's MAA issuer and its hostdata —
    so we are asking verifiers to trust that region — while giving no endpoint
    for it. One MAA instance is provisioned per serving region and issuers do
    not roll the way hostdata does during a bind window, so an issuer with no
    endpoint is unambiguous: a region we vouch for and never check.

    Reported as [GAP] rather than [DRIFT] on purpose. Nothing compared here
    mismatched; the run simply covered less of the plane than the record
    describes, and calling that "ok" is the old overclaim in a new costume.
    """
    record = azure_record(
        regions=[
            {
                "attestation_url": UAEN_URL,
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            }
        ]
    )
    transport = whole_fleet(azure=record)

    code = drift.main(["--control-plane", CONTROL_PLANE], transport=transport)
    out = capsys.readouterr().out

    assert code == 1
    assert "[GAP] azure" in out
    assert SEA_ISSUER in out
    assert SEA_URL not in transport.fetched, "the fixture must not accidentally cover the gap"


def test_a_mirror_that_drops_the_region_array_is_a_gap() -> None:
    """Production's actual state on 2026-08-15, as a fixture.

    trustedrouter.com published both hostdata values and both issuers and no
    `regions` array at all, because the control plane rebuilt the record from
    scalars. The checker falls back to the canonical endpoint so the plane is
    still partly checked, and must say out loud that this is one region of two.
    """
    record = azure_record()
    record.pop("regions")
    transport = whole_fleet(azure=record)
    result = results_by_plane(transport)["azure"]

    assert result.gap
    assert result.endpoints == (UAEN_URL,)
    assert result.unreached == (SEA_ISSUER,)


def test_a_single_region_record_without_a_region_array_is_not_a_gap() -> None:
    """The fallback must not manufacture a gap on a plane that has one region.

    A check that cries wolf on every single-region deployment gets muted, and a
    muted check is the state this whole change is undoing.
    """
    record = azure_record(
        accepted_hostdata=[UAEN_HOSTDATA],
        attestation_issuers=[UAEN_ISSUER],
    )
    record.pop("regions")
    transport = whole_fleet(azure=record)
    result = results_by_plane(transport)["azure"]

    assert result.ok
    assert not result.gap
    assert result.endpoints == (UAEN_URL,)


def test_an_unlisted_live_issuer_is_still_reported() -> None:
    """The pre-existing check, kept: a genuine token our record rejects.

    A verifier following the published issuer list would reject a token that is
    in fact authentic, which manufactures an accusation of tampering out of our
    own bookkeeping.
    """
    transport = whole_fleet(
        sea_live=azure_token(SEA_HOSTDATA, "https://trquillnew.westus.attest.azure.net")
    )
    result = results_by_plane(transport)["azure"]

    assert not result.ok
    assert "issuer" in result.detail


# ---------------------------------------------------------------------------
# AWS — sampling, and saying so
# ---------------------------------------------------------------------------


def test_aws_reports_how_many_distinct_enclaves_it_reached_and_calls_it_sampling() -> None:
    """Honesty about coverage is the deliverable here, not coverage itself.

    One fetch of an anycast name reaches one enclave. Sampling narrows the
    blind spot; it never closes it, and the output must not let a reader
    believe otherwise.
    """
    seen = [aws_document(module_id=AWS_MODULE_A), aws_document(module_id=AWS_MODULE_B)]
    transport = whole_fleet(aws_live=lambda index: seen[index % len(seen)])
    result = results_by_plane(transport)["aws"]

    assert result.ok
    printed = "\n".join(result.extra)
    assert "2 fetch(es) reached 2 distinct enclave(s)" in printed
    assert AWS_MODULE_A in printed and AWS_MODULE_B in printed
    assert "SAMPLING, not enumeration" in printed


def test_aws_drift_on_any_sampled_enclave_fails() -> None:
    """A fleet is only as published as its worst member.

    The failure the single-fetch version could not see: one enclave of two
    rebuilt without republishing. Whether it is caught at all depends on
    whether the accelerator routes us there, which is why the sampling caveat
    above is load-bearing rather than decorative.
    """
    stale = "ee" * 48
    seen = [
        aws_document(module_id=AWS_MODULE_A),
        aws_document(module_id=AWS_MODULE_B, pcr0=stale),
    ]
    transport = whole_fleet(aws_live=lambda index: seen[index % len(seen)])
    result = results_by_plane(transport)["aws"]

    assert not result.ok
    assert "accepted set" in result.detail
    assert stale in "\n".join(result.extra)


def test_aws_document_must_bind_the_certificate_this_connection_was_served() -> None:
    """The AWS fetch runs with TLS chain validation off, by design.

    What replaces the chain is the attestation binding the certificate the
    connection actually got. Without checking it, "we read a document over an
    unverified connection from an unauthenticated hostname" is all the AWS line
    means, and a relay in front of the enclave would satisfy it.
    """
    transport = whole_fleet(aws_live=aws_document(cert_der=b"a-certificate-we-were-not-served"))
    result = results_by_plane(transport)["aws"]

    assert not result.ok
    assert "user_data[0:32]" in result.detail


def test_aws_document_binding_nothing_fails_closed() -> None:
    """Absent binding must not read as satisfied binding.

    Same rule probes.py enforces for the live probes: an attested endpoint
    whose document binds nothing is indistinguishable from a relay, and
    "unverifiable" must never print as "verified".
    """
    payload = {"module_id": AWS_MODULE_A, "digest": "SHA384", "pcrs": {0: bytes.fromhex(AWS_PCR0)}}
    document = cbor2.dumps([b"\xa1\x01\x38\x22", {}, cbor2.dumps(payload), b"sig" * 32])
    transport = whole_fleet(aws_live=document)
    result = results_by_plane(transport)["aws"]

    assert not result.ok
    assert "binds no certificate" in result.detail


# ---------------------------------------------------------------------------
# --strict, and the summary that cannot outrun its coverage
# ---------------------------------------------------------------------------


def _nothing_published() -> FakeTransport:
    paths = tuple(
        f"{CONTROL_PLANE}/trust/{plane}-release.json" for plane in ("gcp", "aws", "azure")
    )
    return FakeTransport({}, missing=paths)


def test_a_skip_is_lenient_locally_and_fatal_under_strict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ "We stopped publishing a measurement" must not be a green tick on a cron.

    Locally the same state is ordinary: a staging mirror, a plane mid
    bring-up, a branch where nothing is published yet. Making it fatal there
    teaches people to pass a flag to silence the check, and a check people
    silence by habit is the check that goes unrun for as long as this one did.
    """
    lenient = drift.main(["--control-plane", CONTROL_PLANE], transport=_nothing_published())
    lenient_out = capsys.readouterr().out
    strict = drift.main(
        ["--control-plane", CONTROL_PLANE, "--strict"], transport=_nothing_published()
    )
    strict_out = capsys.readouterr().out

    assert lenient == 0
    assert strict == 1
    for out in (lenient_out, strict_out):
        assert out.count("[SKIP]") == 3
    assert "Skipped (nothing published): gcp, aws, azure" in strict_out


def test_a_run_that_checked_nothing_does_not_say_everything_matched(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Found by running the checker against https://aws.trustedrouter.com.

    That control plane publishes "not-configured" for all three planes, so
    every check skips — and the lenient run printed "Every published
    measurement ... matches a live attestation." directly under a summary
    reading "Checked 0 plane(s) at 0 endpoint(s): none". Vacuously true, and
    the exact reading this file exists to stop: a sentence claiming everything
    matched, above nothing.

    Exit stays 0, because locally that state is ordinary; --strict is what
    makes it fatal. Only the sentence changes.
    """
    code = drift.main(["--control-plane", CONTROL_PLANE], transport=_nothing_published())
    out = capsys.readouterr().out

    assert code == 0
    assert "Checked 0 plane(s) at 0 endpoint(s): none" in out
    assert "Every published measurement" not in out
    assert "Nothing was checked" in out


def test_the_summary_states_what_was_checked_and_what_was_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The overclaiming success line, directly.

    The line this replaces read "Every published measurement matches a live
    attestation." after contacting one of the two Azure regions the record
    itself enumerated. Any success sentence that does not enumerate its own
    coverage will say that again the next time coverage narrows.
    """
    code = drift.main(["--control-plane", CONTROL_PLANE], transport=whole_fleet())
    out = capsys.readouterr().out

    assert code == 0
    assert "Checked 3 plane(s) at 4 endpoint(s): gcp (1), aws (1), azure (2)" in out
    assert "Skipped (nothing published): none" in out
    # The success sentence must be bounded by the list above it, not global.
    assert "Every published measurement listed above matches a live attestation." in out


def test_one_unpublished_plane_does_not_stop_the_others_being_checked() -> None:
    """A plane going quiet must not silently shrink the run to nothing."""
    transport = whole_fleet()
    transport.missing = (f"{CONTROL_PLANE}/trust/aws-release.json",)
    results = results_by_plane(transport)

    assert results["aws"].skipped
    assert results["gcp"].ok and results["azure"].ok
    assert UAEN_URL in transport.fetched and SEA_URL in transport.fetched


# ---------------------------------------------------------------------------
# The original defect: something has to actually run it
# ---------------------------------------------------------------------------


def test_a_scheduled_workflow_runs_the_checker_in_strict_mode() -> None:
    """The other twenty assertions are worth nothing if nobody runs the script.

    Asserted against the workflow file rather than trusted to review, because
    the defect being closed IS the absence of an invocation: every correctness
    property this file proves held just as well while the checker sat unrun,
    and no test could tell the difference.

    Deliberately NOT claimed: that the workflow passes, that GitHub's scheduler
    fires it on time (cron on Actions is best-effort and drops runs under load),
    or that a failure reaches a human. This asserts wiring, which is the part
    that was missing.
    """
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/trust-drift.yml"
    ).read_text(encoding="utf-8")

    assert "scripts/verify_trust_measurements.py" in workflow
    assert "--strict" in workflow, "a scheduled run where a skip is green is the defect itself"
    assert "schedule:" in workflow and "cron:" in workflow
    # Prod state must never redden a PR — the offline proofs above are what
    # gate PRs. Same rule prod-smoke.yml states for itself.
    assert "pull_request" not in workflow


# ---------------------------------------------------------------------------
# Coverage must not hang on one optional field, one list order, or one count
# ---------------------------------------------------------------------------


def test_an_unreached_issuer_is_named_by_evidence_not_by_list_position() -> None:
    """The [GAP] line is what an operator acts on, so it has to name the right one.

    Attribution used to be positional — `issuers[len(regions):]` — so with the
    issuer list in the other order the gap named as unreached the very issuer
    whose region HAD been contacted. A true verdict with a false reason is the
    same class of output this file exists to remove. Coverage is now attributed
    from the issuers the contacted endpoints presented live.
    """
    record = azure_record(attestation_issuers=[SEA_ISSUER, UAEN_ISSUER])
    record.pop("regions")
    transport = whole_fleet(azure=record)
    result = drift.check_azure(CONTROL_PLANE, transport)

    assert result.gap
    assert result.endpoints == (UAEN_URL,), "UAE North is the endpoint that answered"
    assert result.unreached == (SEA_ISSUER,), "the issuer that answered must not be blamed"
    assert UAEN_ISSUER not in "".join(result.unreached)
    # And the sentence carrying that verdict counts what ANSWERED, not what the
    # record listed: two issuers are published here and one was presented. A
    # census read off the record would print "2 distinct MAA issuer(s)
    # presented" beside a gap saying one of them was presented by nobody.
    assert "over 1 endpoint(s) contacted, 1 distinct MAA issuer(s) presented" in result.detail


def test_coverage_does_not_hang_on_the_optional_issuer_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Drop one optional field and the whole multi-region guarantee used to switch off.

    With `attestation_issuers` absent there was nothing left to compute a gap
    from AND nothing left to check the live issuer against, so a two-region
    plane passed --strict having contacted one region and printed the success
    sentence. `accepted_hostdata` still listed both regions' values the whole
    time. A record that carries no region census at all cannot certify its own
    coverage, and that is now a [GAP] rather than a pass.
    """
    record = azure_record()
    record.pop("regions")
    record.pop("attestation_issuers")
    transport = whole_fleet(azure=record)

    code = drift.main(["--control-plane", CONTROL_PLANE, "--strict"], transport=transport)
    out = capsys.readouterr().out

    assert code == 1
    assert "[GAP] azure" in out
    assert "no attestation_issuers" in out
    assert SEA_URL not in transport.fetched, "the fixture must not accidentally cover the gap"
    assert "Every published measurement" not in out


def test_a_single_region_record_missing_both_censuses_is_still_not_a_gap() -> None:
    """The rule above must not cry wolf on a plane that genuinely has one region.

    One accepted hostdata value and one issuer is a complete description of a
    one-region plane, and a check that reddens on those gets muted.
    """
    record = azure_record(accepted_hostdata=[UAEN_HOSTDATA], attestation_issuers=[UAEN_ISSUER])
    record.pop("regions")
    result = drift.check_azure(CONTROL_PLANE, whole_fleet(azure=record))

    assert result.ok and not result.gap
    assert result.endpoints == (UAEN_URL,)


def test_two_region_entries_at_one_endpoint_are_not_two_regions_covered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The t4 defect in a new shape: a count of entries printed as coverage.

    Coverage was `len(regions)` — the number of entries in the record — so two
    entries naming the SAME url read as two regions covered, over two fetches of
    one endpoint, and the summary counted `azure (2)`. Counting is now over the
    distinct endpoints actually fetched, and the duplicate is itself named.
    """
    record = azure_record(regions=[{"attestation_url": UAEN_URL}, {"attestation_url": UAEN_URL}])
    transport = whole_fleet(azure=record)

    code = drift.main(["--control-plane", CONTROL_PLANE, "--strict"], transport=transport)
    out = capsys.readouterr().out

    assert code == 1
    assert "[GAP] azure" in out
    assert "more than once" in out
    assert "azure (1)" in out, "one endpoint answered, so the summary must count one"
    assert SEA_URL not in transport.fetched


def test_a_record_with_one_issuer_and_two_policies_and_no_endpoints_is_a_gap() -> None:
    """Rule 3 on its own, with nothing else able to fire.

    One published issuer, so the live-issuer census is satisfied by the one
    endpoint that answered. No regions[] array, so the only endpoint available
    is the canonical fallback. And two accepted hostdata values, which is either
    a second CCE policy at a second region or a bind window on one — the record
    does not say, and a run that cannot tell those apart has not established its
    coverage. Written separately because the two-census fixture above satisfies
    this rule and rule 2 at once, so neither was pinned on its own.
    """
    record = azure_record(attestation_issuers=[UAEN_ISSUER])
    record.pop("regions")
    result = drift.check_azure(CONTROL_PLANE, whole_fleet(azure=record))

    assert result.gap
    assert result.unreached == (), "no published issuer went unpresented; this is the other rule"
    assert "no regions[] array" in result.detail
    assert "2 accepted hostdata values" in result.detail


def test_two_endpoints_differing_only_by_fragment_are_one_endpoint() -> None:
    """The duplicate-URL defence must compare endpoints, not strings.

    Found by an adversarial pass over the first fix: .../attestation#a and
    .../attestation#b are distinct strings that pass a raw-string duplicate
    check, and the fragment is never transmitted — so both entries fetch the
    same place while the count says two regions covered. That is this file's own
    defect rebuilt out of punctuation, so the comparison is on endpoint identity
    (scheme, host, port, path) at both layers.
    """
    record = azure_record(
        regions=[
            {
                "attestation_url": UAEN_URL + "#uae-north",
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            },
            {
                "attestation_url": UAEN_URL + "?region=sea",
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            },
        ]
    )
    transport = FakeTransport(
        {
            f"{CONTROL_PLANE}/trust/azure-release.json": record,
            UAEN_URL + "#uae-north": azure_token(UAEN_HOSTDATA, UAEN_ISSUER),
        }
    )
    result = drift.check_azure(CONTROL_PLANE, transport)

    assert result.gap
    assert len(result.endpoints) == 1, "one place was contacted, so the count must say one"
    assert "more than once" in result.detail


def test_a_gap_alongside_drift_is_still_reported_under_the_drift_mark(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Drift takes the mark; the gap must not vanish with it.

    check_azure returns on the drift branch before the gap branch, so a run with
    both prints [DRIFT] and not [GAP]. That is deliberate — drift is the more
    serious verdict — but it means the docstring's "a [GAP] is raised when any
    of these hold" was false, and it would be a real defect if the coverage
    finding disappeared along with the mark. It does not: the gap lines and the
    unreached issuer are reported under the drift verdict.
    """
    record = azure_record(
        regions=[
            {
                "attestation_url": UAEN_URL,
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            }
        ]
    )
    transport = whole_fleet(azure=record, uaen_live=azure_token("9c" * 32, UAEN_ISSUER))

    code = drift.main(["--control-plane", CONTROL_PLANE, "--strict"], transport=transport)
    out = capsys.readouterr().out

    assert code == 1
    assert "[DRIFT] azure" in out
    assert SEA_ISSUER in out, "the coverage gap must survive the more serious verdict"
    assert "Not reached: azure " + SEA_ISSUER in out


def test_an_explicit_null_regions_key_is_not_read_as_an_absent_one(httpx_mock: Any) -> None:
    """ "The key is missing" and "the key says nothing" are different upstream states.

    Both used to reach the same `return []`, so an upstream could publish
    `"regions": null` and have it mirror exactly like a record that predates the
    field. Refused instead, because a record that names the field and empties it
    is making a claim, and the mirror should not translate that into silence.
    """
    served = _served_azure_record(azure_record(regions=None), httpx_mock)

    assert served["status_code"] == 503


def test_a_region_entry_naming_no_hostdata_cannot_be_attributed_and_is_a_gap() -> None:
    """An entry that names only a URL degrades to union membership, silently.

    Both per-region comparisons are guarded on the field being present, so an
    entry carrying no hostdata or issuer was checked against the union of every
    region's accepted values and reported exactly as if it had been attributed.
    That is less coverage than the record's own shape implies, so it is a gap.
    """
    record = azure_record(
        regions=[
            {"attestation_url": UAEN_URL, "attestation_issuer": UAEN_ISSUER},
            {
                "attestation_url": SEA_URL,
                "hostdata": SEA_HOSTDATA,
                "attestation_issuer": SEA_ISSUER,
            },
        ]
    )
    result = drift.check_azure(CONTROL_PLANE, whole_fleet(azure=record))

    assert result.gap
    assert "names no hostdata" in result.detail
    assert result.endpoints == (UAEN_URL, SEA_URL), "both were still contacted"


def test_a_regions_entry_with_no_attestation_url_is_a_gap_on_its_own() -> None:
    """The fourth coverage rule, isolated so that deleting it cannot stay green.

    Found by an adversarial pass that deleted this rule and watched the whole
    suite stay green: every fixture that carried a URL-less entry also tripped
    the issuer census, so two rules were covering each other and neither was
    pinned. Here nothing else can fire — one accepted hostdata value, one
    issuer, and that issuer is presented by the endpoint that answers — so the
    single gap reported is this rule or the rule is gone.
    """
    record = azure_record(
        accepted_hostdata=[UAEN_HOSTDATA],
        attestation_issuers=[UAEN_ISSUER],
        regions=[
            {
                "attestation_url": UAEN_URL,
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            },
            {"hostdata": UAEN_HOSTDATA, "attestation_issuer": UAEN_ISSUER},
        ],
    )
    result = drift.check_azure(CONTROL_PLANE, whole_fleet(azure=record))

    assert result.gap
    assert "1 coverage gap(s)" in result.detail, "no other rule may be propping this one up"
    assert "names no attestation_url" in result.detail
    assert result.endpoints == (UAEN_URL,)


def test_a_regions_entry_that_names_no_reachable_place_is_a_gap_and_is_not_fetched() -> None:
    """A URL the checker cannot honour is a coverage claim, not an endpoint.

    The checker reads records the mirror never validated, so it meets strings
    the mirror would have refused: a malformed authority, a non-http scheme, or
    `https://a@b/`, which reads as host a and contacts host b. Counting one as
    an endpoint would be a coverage claim on a place this run cannot reach, and
    FETCHING one would let whoever writes the record aim the hourly job. It is
    reported and skipped. Isolated like the rule above so nothing props it up.
    """
    hostile = "https://someone@api-azure-sea.trustedrouter.com/attestation"
    record = azure_record(
        accepted_hostdata=[UAEN_HOSTDATA],
        attestation_issuers=[UAEN_ISSUER],
        regions=[
            {
                "attestation_url": UAEN_URL,
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            },
            {
                "attestation_url": hostile,
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            },
        ],
    )
    transport = whole_fleet(azure=record)
    result = drift.check_azure(CONTROL_PLANE, transport)

    assert result.gap
    assert "1 coverage gap(s)" in result.detail, "no other rule may be propping this one up"
    assert "not a plain http(s) endpoint" in result.detail
    assert result.endpoints == (UAEN_URL,)
    assert hostile not in transport.fetched, "a record must not be able to aim this run"


def test_the_endpoint_the_record_advertises_is_contacted_even_when_regions_omits_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """regions[] was an ELSE for api_base_url, so publishing one hid the other.

    api_base_url is the address the record hands a verifier. Reading regions[]
    INSTEAD of it — which is what azure_plan did — means a record enumerating
    only Southeast Asia leaves the UAE North gateway it advertises uncontacted,
    and the run prints the full success sentence over a plane whose front door
    was never knocked on. Here that door is serving a hostdata published
    nowhere: before, exit 0 and "Every published measurement ..."; now the
    endpoint is contacted and the drift is caught.
    """
    record = azure_record(
        accepted_hostdata=[SEA_HOSTDATA],
        attestation_issuers=[SEA_ISSUER],
        regions=[
            {
                "attestation_url": SEA_URL,
                "hostdata": SEA_HOSTDATA,
                "attestation_issuer": SEA_ISSUER,
            }
        ],
    )
    transport = whole_fleet(azure=record)

    code = drift.main(["--control-plane", CONTROL_PLANE, "--strict"], transport=transport)
    out = capsys.readouterr().out

    assert code == 1
    assert UAEN_URL in transport.fetched, "the record's own api_base_url was never contacted"
    assert "[DRIFT] azure" in out
    assert "Every published measurement" not in out


def test_an_advertised_endpoint_missing_from_the_region_census_is_a_gap_even_when_healthy() -> None:
    """Contacting it is half the fix; the record still disagrees with itself.

    A record that tells verifiers to go to one endpoint and lists a different
    set of serving regions cannot ground a region count, whichever of the two is
    stale. Separated from the test above because there the drift verdict would
    have masked a deleted gap rule: here every measurement matches and every
    published issuer is presented, so the one gap reported is this rule alone.
    """
    record = azure_record(
        regions=[
            {
                "attestation_url": SEA_URL,
                "hostdata": SEA_HOSTDATA,
                "attestation_issuer": SEA_ISSUER,
            }
        ],
    )
    result = drift.check_azure(CONTROL_PLANE, whole_fleet(azure=record))

    assert result.gap
    assert "1 coverage gap(s)" in result.detail
    assert "api_base_url advertises" in result.detail
    assert set(result.endpoints) == {SEA_URL, UAEN_URL}
    assert result.unreached == (), "both published issuers answered; this is the other rule"


# ---------------------------------------------------------------------------
# The PRODUCTION route bounds the checker's coverage
# ---------------------------------------------------------------------------
#
# These drive trusted_router through its real HTTP route rather than calling
# trust.azure_release directly. Calling it directly is what let the first
# version of this proof pass while production behaviour was unchanged: the
# route interposes TrustReleaseResolver's validator, and that validator
# whitelisted three scalar keys and dropped `regions` before azure_release was
# ever reached. The mirror fix was dead code on the only path that matters.


def _served_azure_record(upstream: dict[str, Any], httpx_mock: Any) -> dict[str, Any]:
    """The azure-release.json a real control plane would serve for this upstream.

    raise_server_exceptions=False on purpose: an exception that escapes the
    route must show up here as the 500 a client would receive, not as a test
    error. Every refusal below is documented as a 503 carrying the embedded
    fallback, and the only way to hold that documentation to account is to let
    the wrong status code be asserted against.
    """
    import re

    from fastapi.testclient import TestClient

    from trusted_router.config import Settings
    from trusted_router.main import create_app

    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/azure\.json\?tr_cache_bucket=\d+"),
        json=upstream,
    )
    settings = Settings(
        environment="test", trust_azure_release_url="https://trust.example/azure.json"
    )
    with TestClient(
        create_app(settings, init_observability=False), raise_server_exceptions=False
    ) as client:
        response = client.get("/trust/azure-release.json")
    try:
        payload = response.json()
    except ValueError:
        # A 500 from an escaped exception carries no JSON body. Reporting the
        # status alone keeps the failure readable as "the route answered 500"
        # rather than as a decode error two frames away from the cause.
        payload = {}
    return {"status_code": response.status_code, **payload}


def test_the_serving_route_carries_the_region_array_and_the_check_covers_both_regions(
    httpx_mock: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Driven through the route, because the route is what production runs.

    trust.azure_release rebuilt the published record out of four scalars and
    dropped the plane's `regions` array; the checker enumerates endpoints from
    the record, so a record with no endpoints to enumerate made the
    single-region blind spot structural. Fixing azure_release alone did not fix
    it — validated_azure_metadata stripped `regions` one layer earlier, so the
    record served here still carried `regions: []` and the hourly --strict job
    would have been permanently red on [GAP] azure.

    The assertion is on the fetch list of a checker fed the SERVED record.
    """
    served = _served_azure_record(azure_record(), httpx_mock)

    assert served["status_code"] == 200
    assert [region["attestation_url"] for region in served["regions"]] == [UAEN_URL, SEA_URL]
    assert served["regions"][1]["hostdata"] == SEA_HOSTDATA

    transport = whole_fleet(azure=served)
    code = drift.main(["--control-plane", CONTROL_PLANE, "--strict"], transport=transport)
    out = capsys.readouterr().out

    assert code == 0
    assert SEA_URL in transport.fetched, "the served record did not reach the second region"
    assert "Checked 3 plane(s) at 4 endpoint(s): gcp (1), aws (1), azure (2)" in out


def test_the_route_refuses_a_region_whose_hostdata_the_record_disowns(
    httpx_mock: Any,
) -> None:
    """Mirroring is not repeating — and it is not quiet editing either.

    A region entry whose hostdata is absent from the accepted set is
    self-contradictory: a verifier routed there would be handed a value the same
    record tells them to reject. The mirror used to DROP such an entry, which
    erased a live serving region from the published record leaving the check
    nothing to notice. It is now refused whole, exactly as a self-inconsistent
    AWS record is, and the plane falls back to the embedded measurement — which
    is unconfigured here, so the route answers 503 and the checker reports the
    plane as publishing nothing, which --strict makes fatal.
    """
    poisoned = azure_record()
    poisoned["regions"][1] = dict(poisoned["regions"][1], hostdata="ab" * 32)
    served = _served_azure_record(poisoned, httpx_mock)

    assert served["status_code"] == 503, "a self-contradictory record was republished"
    assert served["release_metadata_status"] == "not-configured"


def test_the_route_refuses_a_region_whose_issuer_the_record_never_listed(
    httpx_mock: Any,
) -> None:
    """The drop condition that could never surface downstream.

    The mirror dropped a region entry whose issuer was absent from
    attestation_issuers — precisely the condition under which the checker's
    issuer census has nothing left to notice, because the issuer is not in the
    list either. A region brought up before its issuer was published, or an
    issuer string that changed, was silently erased and the run went green
    having contacted one of two live regions. Refusing the record is what makes
    that state loud.
    """
    upstream = azure_record(attestation_issuers=[UAEN_ISSUER])
    served = _served_azure_record(upstream, httpx_mock)

    assert served["status_code"] == 503
    assert served["regions"] == []
    assert served["accepted_hostdata"] == []


@pytest.mark.parametrize(
    "attestation_url",
    [
        UAEN_URL + "#uae-north",
        UAEN_URL + "?region=uaen",
        "https://someone@api-azure.trustedrouter.com/attestation",
        "http://api-azure.trustedrouter.com/attestation",
    ],
)
def test_the_route_refuses_a_region_url_that_is_not_plainly_the_endpoint(
    attestation_url: str, httpx_mock: Any
) -> None:
    """A region URL is a coverage claim, so it has to be exactly the place contacted.

    A fragment or query makes two entries distinct strings that reach one place,
    which buys a region count the plane cannot support. Userinfo makes
    `https://a@b/` read as host a while contacting host b. Neither is refused by
    a startswith("https://") check, which is all this validated at first.
    """
    upstream = azure_record()
    upstream["regions"][0] = dict(upstream["regions"][0], attestation_url=attestation_url)
    served = _served_azure_record(upstream, httpx_mock)

    assert served["status_code"] == 503


def test_the_route_refuses_a_region_array_long_enough_to_be_a_load_generator(
    httpx_mock: Any,
) -> None:
    """Every entry is an endpoint the hourly job will fetch.

    The payload cap is 64KB, which still leaves room for hundreds of region
    entries, so an unbounded array turns whoever can write the upstream record
    into someone who can point the drift check at arbitrary hosts as often as
    they like. Two regions are in service; the cap refuses long before the array
    becomes a load generator.
    """
    upstream = azure_record(
        regions=[
            {
                "attestation_url": f"https://api-azure-{index}.trustedrouter.com/attestation",
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            }
            for index in range(17)
        ]
    )
    served = _served_azure_record(upstream, httpx_mock)

    assert served["status_code"] == 503


def test_the_route_refuses_two_region_entries_at_one_endpoint(httpx_mock: Any) -> None:
    """A duplicate URL is a region count the record cannot support.

    Caught in the checker as well (see above), but a mirror that republishes it
    has already put a false count on the trust page for anyone reading the
    record directly.
    """
    upstream = azure_record()
    upstream["regions"][1] = dict(upstream["regions"][1], attestation_url=UAEN_URL)
    served = _served_azure_record(upstream, httpx_mock)

    assert served["status_code"] == 503


# ---------------------------------------------------------------------------
# A malformed field in a remote record must not take the public route down
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attestation_url",
    [
        "https://[::1",  # unterminated IPv6 literal
        "https://api-azure.trustedrouter.com:notaport/attestation",
        "https://api-azure.trustedrouter.com:99999/attestation",
    ],
)
def test_a_malformed_region_url_answers_503_and_not_500(
    attestation_url: str, httpx_mock: Any
) -> None:
    """The widening that carried regions[] through also widened the input surface.

    /trust/azure-release.json is public and the record behind it is FETCHED FROM
    A REMOTE MIRROR, so a malformed field in it is an ordinary input and not a
    hypothetical. The first version of the validator parsed these with
    httpx.URL, which raises httpx.InvalidURL — a plain Exception, not an
    httpx.HTTPError — so it escaped TrustReleaseResolver.resolve(), sailed past
    the route's `except TrustReleaseUnavailable`, and the endpoint answered 500:
    no embedded fallback, no retry backoff, no stale-if-error serving, and a
    stack trace on the one route whose whole job is to be a dependable answer
    about what is running. The documented behaviour is a 503 carrying the
    embedded measurement, and that is what is asserted here.
    """
    upstream = azure_record()
    upstream["regions"][0] = dict(upstream["regions"][0], attestation_url=attestation_url)
    served = _served_azure_record(upstream, httpx_mock)

    assert served["status_code"] == 503, "a malformed region URL must not be a 500"
    assert served["release_metadata_status"] == "not-configured"


def test_a_malformed_region_url_backs_off_rather_than_refetching_every_request(
    httpx_mock: Any,
) -> None:
    """Failing closed is not enough if it fails closed once per request.

    An exception that escapes resolve() skips the whole failure path, not just
    the fallback: `_retry_after` is never set, so every subsequent request goes
    back to the upstream mirror, and a previously cached good record is never
    served stale. A public route that re-fetches an upstream on every request
    during an incident is a load amplifier pointed at the plane we are trying to
    describe. One upstream fetch for two requests is what the backoff means.
    """
    import re

    from fastapi.testclient import TestClient

    from trusted_router.config import Settings
    from trusted_router.main import create_app

    upstream = azure_record()
    upstream["regions"][0] = dict(upstream["regions"][0], attestation_url="https://[::1")
    httpx_mock.add_response(
        url=re.compile(r"https://trust\.example/azure\.json\?tr_cache_bucket=\d+"),
        json=upstream,
    )
    settings = Settings(
        environment="test", trust_azure_release_url="https://trust.example/azure.json"
    )
    with TestClient(
        create_app(settings, init_observability=False), raise_server_exceptions=False
    ) as client:
        first = client.get("/trust/azure-release.json")
        second = client.get("/trust/azure-release.json")

    assert first.status_code == 503
    assert second.status_code == 503
    assert len(httpx_mock.get_requests()) == 1, "the second request must be inside the backoff"


def test_the_identity_parser_answers_with_a_value_for_every_hostile_string() -> None:
    """The invariant the 500 came from, asserted directly.

    Every caller of parse_endpoint treats None as "this names no place" and has
    no handler for an exception, because its contract is that there is never one
    to handle. Written as its own test because the two route tests above only
    cover the inputs someone already thought of, and the defect was not that a
    particular string was mishandled — it was that the parser had an exit nobody
    upstream knew about.
    """
    from trusted_router.endpoint_identity import parse_endpoint

    hostile = [
        "https://[::1",
        "https://h:notaport/x",
        "https://h:99999/x",
        "https://",
        "http://",
        "",
        "not a url at all",
        "file:///etc/passwd",
        "//api-azure.trustedrouter.com/attestation",
        "https://a@b/attestation",
        "\x00",
        "https://h:-1/x",
        None,
        42,
        {"attestation_url": UAEN_URL},
    ]

    assert [parse_endpoint(value) for value in hostile] == [None] * len(hostile)


# ---------------------------------------------------------------------------
# One implementation of "the same endpoint", used by the mirror and the checker
# ---------------------------------------------------------------------------

#: Pairs that name ONE place. Every one of these was accepted as two regions by
#: at least one of the two normalizers that used to exist: the checker's kept an
#: explicit ':443' and the validator's dropped it, and neither folded a trailing
#: slash, a trailing dot on the host, or a doubled path slash.
ENDPOINT_TWINS = [
    (UAEN_URL, "https://api-azure.trustedrouter.com:443/attestation"),
    (UAEN_URL, UAEN_URL + "/"),
    (UAEN_URL, "https://api-azure.trustedrouter.com./attestation"),
    (UAEN_URL, "https://api-azure.trustedrouter.com//attestation"),
    (UAEN_URL, "https://api-azure.trustedrouter.com/./attestation"),
    (UAEN_URL, "https://API-AZURE.TrustedRouter.com/attestation"),
]


@pytest.mark.parametrize(("first", "second"), ENDPOINT_TWINS)
def test_the_mirror_refuses_two_spellings_of_one_endpoint(
    first: str, second: str, httpx_mock: Any
) -> None:
    """Two spellings of one place are a region count the record cannot support.

    Same rule as the duplicate-URL test above; these are the spellings that slip
    past a comparison that is nearly-but-not-quite endpoint identity.
    """
    upstream = azure_record()
    upstream["regions"][0] = dict(upstream["regions"][0], attestation_url=first)
    upstream["regions"][1] = dict(
        upstream["regions"][1], attestation_url=second, hostdata=UAEN_HOSTDATA
    )
    served = _served_azure_record(upstream, httpx_mock)

    assert served["status_code"] == 503


@pytest.mark.parametrize(("first", "second"), ENDPOINT_TWINS)
def test_the_checker_counts_two_spellings_of_one_endpoint_as_one(first: str, second: str) -> None:
    """And the checker has to agree, or the record is safe in one place only.

    The checker reads records the mirror never validated — --control-plane takes
    any URL, and the workflow exposes it as a dispatch input — so a fold the
    mirror refuses and the checker accepts still buys a false coverage count in
    the run output. This is the same function on both sides now; these two
    parametrized tests are what says so out loud.
    """
    record = azure_record(
        regions=[
            {
                "attestation_url": first,
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            },
            {
                "attestation_url": second,
                "hostdata": UAEN_HOSTDATA,
                "attestation_issuer": UAEN_ISSUER,
            },
        ]
    )
    transport = FakeTransport(
        {
            f"{CONTROL_PLANE}/trust/azure-release.json": record,
            first: azure_token(UAEN_HOSTDATA, UAEN_ISSUER),
        }
    )
    result = drift.check_azure(CONTROL_PLANE, transport)

    assert result.gap
    assert len(result.endpoints) == 1, "one place was contacted, so the count must say one"
    assert "more than once" in result.detail
