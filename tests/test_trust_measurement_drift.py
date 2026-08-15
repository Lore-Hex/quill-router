"""The drift check must cover every plane it reports on, and say what it covered.

THE LAW
    scripts/verify_trust_measurements.py may print a success line only for the
    endpoints it actually contacted. Concretely:
      * every plane that publishes a measurement is contacted (GCP included);
      * every region the record enumerates is contacted (both Azure regions,
        not the first one), and each is compared against the hostdata the
        record attributes to THAT region, not merely against the union;
      * a serving region the record cannot give an endpoint for is reported as
        an uncovered plane and exits non-zero, never as a pass;
      * under --strict, a plane that publishes nothing is a failure;
      * the summary states the planes and endpoint count it is based on, so a
        success sentence can never outrun its own coverage again.

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
# The mirror bounds the checker's coverage
# ---------------------------------------------------------------------------


def test_the_control_plane_mirror_carries_the_region_array() -> None:
    """Without this the checker cannot find region two at all.

    trusted_router.trust.azure_release rebuilt the published record out of
    hostdata, accepted_hostdata and attestation_issuers, so the `regions` array
    the plane publishes was dropped on the way through the control plane. The
    checker enumerates endpoints from the record; a record with no endpoints to
    enumerate makes the single-region blind spot structural rather than a bug
    in the checker.
    """
    from trusted_router.config import Settings
    from trusted_router.trust import azure_release

    record = azure_release(Settings(environment="test"), metadata=azure_record())
    urls = [region["attestation_url"] for region in record["regions"]]

    assert urls == [UAEN_URL, SEA_URL]
    assert record["regions"][1]["hostdata"] == SEA_HOSTDATA
    # And the mirrored record is enough on its own to drive a full check.
    assert drift.azure_regions(record)[1] == []


def test_the_mirror_drops_a_region_it_cannot_reconcile_rather_than_republishing_it() -> None:
    """Mirroring is not repeating.

    A region entry whose hostdata is absent from the accepted set is
    self-contradictory: a verifier routed there would be handed a value the
    same record tells them to reject. Dropping it makes the checker report an
    uncovered region, which is true, instead of comparing a live attestation
    against a number the record itself disowns.
    """
    from trusted_router.config import Settings
    from trusted_router.trust import azure_release

    poisoned = azure_record()
    poisoned["regions"][1] = dict(poisoned["regions"][1], hostdata="ab" * 32)
    record = azure_release(Settings(environment="test"), metadata=poisoned)

    assert [region["attestation_url"] for region in record["regions"]] == [UAEN_URL]
    assert drift.azure_regions(record)[1] == [SEA_ISSUER]
