"""The deploy gate that turns the BYOK envelope ordering constraint into a check.

THE LAW
-------
A control plane may deploy only a build whose set of WRITTEN BYOK envelope
formats is a subset of the formats accepted by EVERY enclave serving that cloud,
in every region, and the gate must refuse whenever it cannot establish that
subset relation — not only when it can disprove it.

WHY THIS IS A PROOF AND NOT A TEST
----------------------------------
The thing under test is a refusal. Its value is entirely in the cases where it
says no, and a refusal that never fires is indistinguishable from no gate at
all while looking exactly like a gate in CI. So the assertions here are almost
all negative: fabricate the outage, fabricate the missing provenance, fabricate
the region nobody wrote down, and require a non-zero verdict for each. The happy
path is one test out of many, and it is the least interesting one.

The subset relation also has to be DERIVED on both sides, and that is asserted
too. A gate that hardcodes "writes V2, needs V2" is correct until the day a V3
write lands, at which point it keeps passing while asserting a fact about a
format the build no longer writes.

BOTH SIDES OF THAT DERIVATION WERE DEFEATED ONCE
------------------------------------------------
An adversarial review broke the first version of both halves, and the tests it
produced are kept here as the ones that matter most:

* ACCEPTS came from parsing case labels out of the switch in `envelopeAAD`.
  Four compiling, gofmt-clean edits kept `case AlgorithmV2:` in cache.go while
  rejecting v2 at run time — an erroring case body, a kill switch ahead of the
  switch, a rejection in the caller, a renamed live dispatch with the old
  function left as dead code — and all four passed with exit 0. The gate now
  reads a declaration the enclave repo GENERATES from a round trip through
  (*Cache).Resolve, and binds it to that commit's package by sha256.
  `test_a_case_label_is_not_acceptance` and
  `test_a_declaration_that_no_longer_matches_the_package_blocks` are that fix.
* WRITES came from calls spelled `EncryptedSecretEnvelope` in one hardcoded
  file. An alias, a subclass, dataclasses.replace, an assignment to
  `.algorithm`, and a write site in any other module were all invisible. The
  five tests in `test_written_formats_sees_a_v3_written_through_*` are that fix.

THE REAL DEFECT THIS COMES FROM
-------------------------------
docs/design/byok-aad-v2-migration.md §4.0 warns, in its own words, that the
step-2 change reaches other clouds "as an ordinary version bump rather than as a
deliberate migration step. Nobody has to decide to run it." Nothing enforced the
order. On AWS and Azure the order was in fact not enforced: their control planes
took the v2-writing build with no check that their enclaves could read v2. It did
no damage only because the migration audits found zero BYOK and zero Broadcast
secret rows in either database — the deploy was unguarded and the databases
happened to be empty. Each cloud is a standalone TrustedRouter with its own
database, so the next cloud with a BYOK row inherits the same unguarded sequence
and the same enclave error, `unsupported envelope algorithm`, on the prompt path.

SCOPE LIMIT, stated plainly
---------------------------
* Nothing here touches the network, so nothing here proves the live attestation
  endpoints answer, that the published records are reachable, or that
  raw.githubusercontent.com serves a file at an arbitrary commit. Those are
  the gate's real failure modes in production and they are covered only by the
  gate failing closed on any exception, which IS asserted, on fabricated
  exceptions.
* No signature is verified here or by the code under test. The attestation
  fixtures below are unsigned, and the gate would accept them in production too;
  it reads attestations for their measurement exactly as
  scripts/verify_trust_measurements.py does.
* `source_commit` is an assertion by whoever ran the enclave release, not a
  measurement. These tests prove the gate refuses when it is absent. They cannot
  prove — and neither can the gate — that a present one is the commit that built
  the running enclave.
* The accepted-formats declaration is proved here to be bound to the package
  source at the same commit. It is NOT proved to have been generated rather than
  hand-written; that is quill-cloud-proxy's CI running the generating test, and
  the property that the generator measures behaviour is proved there, in
  enclave-go/internal/byokcache/accepted_formats_test.go, not here.
* The subset check is over algorithm STRINGS. It does not prove the two
  implementations of a shared format agree byte for byte; that is
  tests/test_byok_aad_namespace_property.py's pinned hex vector, and the two
  proofs are independent.
* Region coverage: the gate now refuses when the record accepts a measurement
  this run never observed, and `test_an_accepted_measurement_no_region_served
  _blocks` fabricates exactly the shape the live Azure mirror had. What no test
  and no gate can see is an enclave serving a cloud while appearing in neither
  the record's `regions` array nor its accepted set.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import cbor2
import pytest

from scripts.check_format_ordering import (
    DECLARATION_PATH,
    DECLARATION_SCHEMA,
    ENCLAVE_PACKAGE,
    PLANES,
    RegionResult,
    accepted_formats,
    check_plane,
    gather,
    read_write_surface,
    regions_of,
    render,
    scan_write_surface,
    written_formats,
)

V1 = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"
V2 = "TR-BYOK-ENVELOPE-AES-256-GCM-V2"
V3 = "TR-BYOK-ENVELOPE-AES-256-GCM-V3"
CONTROL = "TR-BYOK-ENVELOPE-AES-256-GCM-PROBE-NOT-A-FORMAT"

COMMIT_V1_ONLY = "1111111"
COMMIT_V1_AND_V2 = "2222222"

GCP_DIGEST = "sha256:" + "fa" * 32
AWS_PCR0 = "23" * 48
AZURE_UAEN = "c5" * 32
AZURE_SEA = "f3" * 32

UAEN_ISSUER = "https://trquilluaen.uaen.attest.azure.net"
SEA_ISSUER = "https://trquillsea.sasia.attest.azure.net"
UAEN_URL = "https://api-azure.trustedrouter.com/attestation"
SEA_URL = "https://api-azure-sea.trustedrouter.com/attestation"


# --------------------------------------------------------------------------
# Fixtures fabricated to the shape of the real thing
# --------------------------------------------------------------------------


def _jwt(claims: dict[str, Any]) -> bytes:
    """An unsigned JWT. The gate reads claims and never checks signatures."""
    encode = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=")  # noqa: E731
    return b".".join(
        (
            encode(json.dumps({"alg": "none"}).encode()),
            encode(json.dumps(claims).encode()),
            b"",
        )
    )


def gcp_attestation(digest: str = GCP_DIGEST) -> bytes:
    return _jwt(
        {
            "iss": "https://confidentialcomputing.googleapis.com",
            "aud": "quill-cloud",
            "submods": {"container": {"image_digest": digest}},
        }
    )


def azure_attestation(hostdata: str, issuer: str) -> bytes:
    return _jwt(
        {
            "iss": issuer,
            "x-ms-attestation-type": "sevsnpvm",
            "x-ms-sevsnpvm-hostdata": hostdata,
        }
    )


def aws_attestation(pcr0: str = AWS_PCR0) -> bytes:
    """A 4-element COSE_Sign1 envelope carrying a Nitro attestation document.

    Unsigned: the protected header and signature are empty, because the gate
    parses the payload and never verifies the chain.
    """
    document = cbor2.dumps(
        {"digest": "SHA384", "pcrs": {0: bytes.fromhex(pcr0)}, "module_id": "i-0-enc0"}
    )
    return bytes(cbor2.dumps([b"", {}, document, b""]))


def cache_go(*, accepts_v2: bool) -> bytes:
    """cache.go as the enclave really shapes it.

    Present in these fixtures so the `case AlgorithmV2:` label can be varied
    INDEPENDENTLY of the declaration. The label is what the old gate read; the
    tests below require that varying it changes nothing on its own, and that
    changing the file at all invalidates a declaration generated from the
    previous one.
    """
    case_v2 = "\tcase AlgorithmV2:\n\t\treturn aadV2(namespaceProvider, workspaceID, provider)\n"
    return (
        "package byokcache\n\n"
        "const (\n"
        f'\tAlgorithm = "{V1}"\n'
        f'\tAlgorithmV2 = "{V2}"\n'
        '\tnamespaceProvider = "provider"\n'
        ")\n\n"
        "func envelopeAAD(algorithm, workspaceID, provider string) ([]byte, error) {\n"
        "\tswitch algorithm {\n"
        "\tcase Algorithm:\n"
        "\t\treturn aad(workspaceID, provider), nil\n"
        f"{case_v2 if accepts_v2 else ''}"
        "\tdefault:\n"
        '\t\treturn nil, fmt.Errorf("byokcache: unsupported envelope algorithm %q", algorithm)\n'
        "\t}\n"
        "}\n"
    ).encode()


def declaration(
    accepted: list[str], sources: dict[str, bytes], overrides: dict[str, Any] | None = None
) -> bytes:
    """The generated declaration, in the shape the Go test writes it."""
    record: dict[str, Any] = {
        "schema": DECLARATION_SCHEMA,
        "package": ENCLAVE_PACKAGE,
        "accepted": accepted,
        "rejected_control": CONTROL,
        "probe": "seal an envelope, then require (*Cache).Resolve to return the plaintext",
        "generator": "go test ./internal/byokcache -run TestAcceptedFormatsDeclaration",
        "source_sha256": {name: hashlib.sha256(body).hexdigest() for name, body in sources.items()},
    }
    record.update(overrides or {})
    return json.dumps(record, indent=2).encode()


def enclave_tree(accepted: list[str], *, accepts_v2_label: bool | None = None) -> dict[str, bytes]:
    """One commit's worth of the enclave package: the sources and the declaration.

    `accepts_v2_label` controls only the case label in cache.go and defaults to
    agreeing with the declaration. The gate must not read it either way.
    """
    label = accepts_v2_label if accepts_v2_label is not None else (V2 in accepted)
    sources = {"cache.go": cache_go(accepts_v2=label)}
    files = {f"{ENCLAVE_PACKAGE}/{name}": body for name, body in sources.items()}
    files[DECLARATION_PATH] = declaration(accepted, sources)
    return files


ENCLAVE: dict[str, dict[str, bytes]] = {
    COMMIT_V1_ONLY: enclave_tree([V1]),
    COMMIT_V1_AND_V2: enclave_tree([V1, V2]),
}


def source_from(trees: dict[str, dict[str, bytes]] | None = None):  # noqa: ANN201 - test helper
    table = ENCLAVE if trees is None else trees

    def _read(commit: str, path: str) -> bytes:
        if commit not in table or path not in table[commit]:
            # The real reader raises on a 404 exactly like this.
            raise ValueError(f"cannot read {path} at {commit} (HTTP 404)")
        return table[commit][path]

    return _read


def gcp_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "platform": "gcp-confidential-space",
        "api_base_url": "https://api.trustedrouter.com/v1",
        "image_digest": GCP_DIGEST,
        "accepted_image_digests": [GCP_DIGEST],
        "source_commit": COMMIT_V1_AND_V2,
    }
    record.update(overrides)
    return record


def aws_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "platform": "aws-nitro-enclaves",
        "api_base_url": "https://api-aws.trustedrouter.com/v1",
        "pcr0": AWS_PCR0,
        "accepted_pcr0s": [AWS_PCR0],
        "source_commit": COMMIT_V1_AND_V2,
    }
    record.update(overrides)
    return record


def azure_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "platform": "azure-confidential-containers-sev-snp",
        "api_base_url": "https://api-azure.trustedrouter.com/v1",
        "hostdata": AZURE_UAEN,
        "accepted_hostdata": [AZURE_UAEN, AZURE_SEA],
        "attestation_issuers": [UAEN_ISSUER, SEA_ISSUER],
        "source_commit": COMMIT_V1_AND_V2,
        "regions": [
            {
                "attestation_url": UAEN_URL,
                "hostdata": AZURE_UAEN,
                "attestation_issuer": UAEN_ISSUER,
            },
            {
                "attestation_url": SEA_URL,
                "hostdata": AZURE_SEA,
                "attestation_issuer": SEA_ISSUER,
            },
        ],
    }
    record.update(overrides)
    return record


LIVE: dict[str, bytes] = {
    "https://api.trustedrouter.com/attestation": gcp_attestation(),
    "https://api-aws.trustedrouter.com/attestation": aws_attestation(),
    UAEN_URL: azure_attestation(AZURE_UAEN, UAEN_ISSUER),
    SEA_URL: azure_attestation(AZURE_SEA, SEA_ISSUER),
}


def attest_from(live: dict[str, bytes] | None = None):  # noqa: ANN201 - test helper
    table = LIVE if live is None else live

    def _attest(url: str, verify_tls: bool) -> bytes:  # noqa: ARG001 - matches the real signature
        if url not in table:
            raise ValueError(f"nothing serving {url}")
        return table[url]

    return _attest


def run(spec_name: str, record: dict[str, Any], written: frozenset[str], **kwargs: Any):  # noqa: ANN201
    return check_plane(
        PLANES[spec_name],
        record,
        written,
        attest=kwargs.pop("attest", attest_from()),
        source=kwargs.pop("source", source_from()),
        **kwargs,
    )


def problems(results: list[RegionResult]) -> str:
    return " | ".join(problem for result in results for problem in result.problems)


def module(source: str, path: str = "src/trusted_router/byok_crypto.py") -> dict[str, str]:
    return {path: source}


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_every_cloud_clears_when_every_enclave_reads_what_the_plane_writes() -> None:
    results = gather(
        "https://trustedrouter.com",
        ["gcp", "aws", "azure"],
        frozenset({V2}),
        records=lambda path: {
            "/trust/gcp-release.json": gcp_record(),
            "/trust/aws-release.json": aws_record(),
            "/trust/azure-release.json": azure_record(),
        }[path],
        attest=attest_from(),
        source=source_from(),
    )

    assert [result.host for result in results] == [
        "api.trustedrouter.com",
        "api-aws.trustedrouter.com",
        "api-azure.trustedrouter.com",
        "api-azure-sea.trustedrouter.com",
    ], "all four serving regions must be checked, and Azure's two come from the record"
    assert all(result.ok for result in results), problems(results)
    assert all(result.accepts == frozenset({V1, V2}) for result in results)


def test_azure_regions_are_enumerated_from_the_record_not_from_this_file() -> None:
    # A region added to the record must be checked without a code change here.
    # The reverse — a region serving that the record omits — is what
    # test_a_region_serving_outside_the_record_blocks covers, and neither is
    # detectable if the region list is a constant.
    extra_url = "https://api-azure-nz.trustedrouter.com/attestation"
    record = azure_record()
    record["regions"].append(
        {
            "attestation_url": extra_url,
            "hostdata": "ab" * 32,
            "attestation_issuer": "https://trquillnz.nz.attest.azure.net",
        }
    )

    hosts = [region.host for region in regions_of(record, PLANES["azure"])]

    assert hosts == [
        "api-azure.trustedrouter.com",
        "api-azure-sea.trustedrouter.com",
        "api-azure-nz.trustedrouter.com",
    ]


def test_a_record_with_no_regions_array_is_one_region_not_zero() -> None:
    # GCP and AWS publish no `regions` array. Reading that as "no regions to
    # check" would make the gate pass vacuously on the plane that carries every
    # prompt, which is the most expensive way to be green.
    regions = regions_of(gcp_record(), PLANES["gcp"])

    assert len(regions) == 1
    assert regions[0].attestation_url == "https://api.trustedrouter.com/attestation"


# --------------------------------------------------------------------------
# The outage case
# --------------------------------------------------------------------------


def test_enclave_reading_only_v1_blocks_a_plane_that_writes_v2() -> None:
    """The migration's §2.3 outage, as a fixture.

    An enclave at a pre-step-1 commit accepts only V1. The control plane build
    in front of it writes V2. Every BYOK key in that cloud's database stops
    opening at the next inference request. This is the case the gate exists for
    and the one that must never come back green.
    """
    results = run("aws", aws_record(source_commit=COMMIT_V1_ONLY), frozenset({V2}))

    assert not results[0].ok
    assert "WRITES" in problems(results)
    assert V2 in problems(results)
    assert results[0].accepts == frozenset({V1})


def test_a_plane_still_writing_v1_clears_against_a_v1_only_enclave() -> None:
    # The subset is the law, not equality. A pre-step-2 control plane in front
    # of a pre-step-1 enclave is a legal deployment and must not be blocked, or
    # the gate is just "refuse everything" wearing a table.
    results = run("aws", aws_record(source_commit=COMMIT_V1_ONLY), frozenset({V1}))

    assert results[0].ok, problems(results)


def test_an_enclave_ahead_of_the_plane_clears() -> None:
    # Step 1 shipped, step 2 has not. The enclave reads both; the plane writes
    # v1. That is the state §4.0 tells you to reach BEFORE deploying step 2.
    results = run("gcp", gcp_record(), frozenset({V1}))

    assert results[0].ok, problems(results)


# --------------------------------------------------------------------------
# Fail-closed cases
# --------------------------------------------------------------------------


def test_missing_source_commit_blocks() -> None:
    """Absent provenance is a refusal, never a pass.

    aws-release.json and azure-release.json carried no source_commit key at all
    before quill-cloud-proxy started writing one, so this is the state the gate
    meets on a real published record today.
    """
    record = aws_record()
    del record["source_commit"]

    results = run("aws", record, frozenset({V2}))

    assert not results[0].ok
    assert "no source_commit" in problems(results)


@pytest.mark.parametrize("value", ["not-configured", "", "main", "HEAD", "v2.1.0"])
def test_a_source_commit_that_is_not_a_commit_blocks(value: str) -> None:
    # "not-configured" is the sentinel the producer writes when it cannot name
    # a commit; a branch or tag name resolves differently tomorrow than today,
    # which for a provenance field is the same as naming nothing.
    results = run("aws", aws_record(source_commit=value), frozenset({V2}))

    assert not results[0].ok


def test_a_measurement_outside_the_accepted_set_blocks() -> None:
    # Drift. verify_trust_measurements.py already reports this; here it must
    # also STOP a deploy, because a running build the record does not list is a
    # build whose accepted formats the record cannot describe.
    results = run(
        "gcp", gcp_record(accepted_image_digests=["sha256:" + "0b" * 32]), frozenset({V2})
    )

    assert not results[0].ok
    assert "not in the published accepted set" in problems(results)


def test_an_accepted_but_unmapped_measurement_blocks_mid_roll() -> None:
    """A bind window is two builds and one commit, so one of them is unverified.

    This is the sharp edge of the design and it is recorded rather than
    softened: during an enclave roll the gate BLOCKS control-plane deploys.
    That is the ordering §4.0 asks for — roll the enclave out fully, then let
    the control plane take the build — but it does mean an in-progress enclave
    release stops control-plane releases, and whoever finds that surprising
    should read this test rather than widen the check.
    """
    outgoing = "sha256:" + "99" * 32
    record = gcp_record(accepted_image_digests=[outgoing, GCP_DIGEST], image_digest=outgoing)

    results = run("gcp", record, frozenset({V2}))

    assert not results[0].ok
    assert "bind window" in problems(results)


def test_an_accepted_measurement_no_region_served_blocks() -> None:
    """The live Azure mirror's exact shape: two accepted hostdata, no regions array.

    The gate probes api-azure.trustedrouter.com, is never routed to the
    Southeast Asia enclave, and used to print "Every enclave serving azure
    accepts every format this build writes" over a sample of one. The second
    accepted measurement belongs to a build nothing here checked, so the run is
    a refusal, not a green with a footnote.
    """
    record = azure_record()
    del record["regions"]

    results = run("azure", record, frozenset({V2}))

    assert [result.host for result in results] == ["api-azure.trustedrouter.com", "-"]
    assert results[0].ok, "the region that did answer is fine; the coverage is not"
    assert not results[-1].ok
    assert "never checked" in problems(results)
    assert AZURE_SEA in problems(results)


def test_the_bind_window_refusal_does_not_depend_on_probe_routing() -> None:
    """The reviewer's narrowing of the bind-window claim, closed.

    `test_an_accepted_but_unmapped_measurement_blocks_mid_roll` fires only when
    the probe happens to be routed to the un-mapped build. Routed to the mapped
    one — the normal Traffic Manager or NLB primary — the record maps what is
    running and that check says nothing. The coverage refusal is what makes the
    outcome a property of the RECORD rather than of routing: the second accepted
    measurement was still never observed.
    """
    outgoing = "sha256:" + "99" * 32
    record = gcp_record(accepted_image_digests=[GCP_DIGEST, outgoing])

    results = run("gcp", record, frozenset({V2}))

    assert results[0].ok, "the live probe hit the mapped measurement, so this row clears"
    assert not results[-1].ok
    assert outgoing in problems(results)


def test_a_region_serving_outside_the_record_blocks() -> None:
    """A live region the record never described.

    Which region answers a shared Azure hostname is Traffic Manager's decision.
    A region brought up and routed to before its measurement was published
    answers with an MAA issuer the record does not name, and the gate has no
    per-region entry for it — therefore no source_commit, therefore no accepted
    set. The honest answer is to refuse, not to fall back to the record-wide
    values and pretend the unlisted region is the listed one.
    """
    surprise = "https://trquillnz.nz.attest.azure.net"
    live = dict(LIVE)
    live[SEA_URL] = azure_attestation(AZURE_SEA, surprise)

    results = run("azure", azure_record(), frozenset({V2}), attest=attest_from(live))

    assert results[0].ok, "UAE North is untouched and must still clear"
    assert not results[1].ok
    assert "does not describe" in problems(results)


def test_an_unreachable_region_blocks_rather_than_being_skipped() -> None:
    # An attestation endpoint that does not answer is not evidence of anything.
    # verify_trust_measurements.py is allowed to skip; a deploy gate is not.
    results = run("azure", azure_record(), frozenset({V2}), attest=attest_from({}))

    assert not any(result.ok for result in results)
    assert "cannot read a live attestation" in problems(results)


def test_an_unfetchable_declaration_blocks() -> None:
    def exploding(commit: str, path: str) -> bytes:
        raise ValueError(f"HTTP 404 for {path} at {commit}")

    results = run("gcp", gcp_record(), frozenset({V2}), source=exploding)

    assert not results[0].ok
    assert "404" in problems(results)


def test_a_commit_that_predates_the_declaration_blocks() -> None:
    """Every enclave released before this mechanism existed.

    There is no declaration to read at those commits, and the honest answer is
    that what they accept was never measured. The gate refuses; the remedy is
    to roll an enclave built from a commit that carries one.
    """
    tree = {COMMIT_V1_AND_V2: {f"{ENCLAVE_PACKAGE}/cache.go": cache_go(accepts_v2=True)}}

    results = run("gcp", gcp_record(), frozenset({V2}), source=source_from(tree))

    assert not results[0].ok
    assert DECLARATION_PATH in problems(results)


def test_a_record_with_no_accepted_set_blocks() -> None:
    results = run("gcp", gcp_record(accepted_image_digests=[]), frozenset({V2}))

    assert not results[0].ok


def test_a_malformed_regions_array_blocks_the_whole_cloud() -> None:
    # Not "check the regions we could parse and pass". A record we cannot fully
    # read describes a deployment we cannot fully see, and partial coverage
    # reported as a pass is the failure this whole file is about.
    record = azure_record()
    record["regions"] = [{"attestation_url": "https://api-azure.trustedrouter.com/attestation"}]

    results = gather(
        "https://trustedrouter.com",
        ["azure"],
        frozenset({V2}),
        records=lambda path: record,  # noqa: ARG005
        attest=attest_from(),
        source=source_from(),
    )

    assert not results[0].ok
    assert "unreadable" in problems(results)


def test_an_unreadable_record_blocks_the_whole_cloud() -> None:
    results = gather(
        "https://trustedrouter.com",
        ["aws"],
        frozenset({V2}),
        records=lambda path: (_ for _ in ()).throw(ValueError(f"HTTP 404 for {path}")),
        attest=attest_from(),
        source=source_from(),
    )

    assert not results[0].ok
    assert "no usable release record" in problems(results)


# --------------------------------------------------------------------------
# ACCEPTS is behaviour the enclave repo measured, not syntax this file reads
# --------------------------------------------------------------------------


def test_a_case_label_is_not_acceptance() -> None:
    """The reviewer's four evasions, as one assertion at this boundary.

    An erroring case body, a kill switch ahead of the switch, a rejection in
    decryptEnvelope, and a renamed dispatch all produce the same pair: cache.go
    still contains `case AlgorithmV2:`, and a round trip through
    (*Cache).Resolve still fails. This fabricates that pair directly. The old
    parser read the label and returned {V1, V2}; the declaration says what the
    probe observed, and the deploy is blocked.
    """
    tree = {COMMIT_V1_ONLY: enclave_tree([V1], accepts_v2_label=True)}
    assert b"case AlgorithmV2:" in tree[COMMIT_V1_ONLY][f"{ENCLAVE_PACKAGE}/cache.go"]

    assert accepted_formats(COMMIT_V1_ONLY, source_from(tree)) == frozenset({V1})

    results = run(
        "aws",
        aws_record(source_commit=COMMIT_V1_ONLY),
        frozenset({V2}),
        source=source_from(tree),
    )
    assert not results[0].ok
    assert "ACCEPTS only" in problems(results)


def test_a_declaration_that_no_longer_matches_the_package_blocks() -> None:
    """A declaration is evidence about the package it was generated from.

    Edit cache.go without re-running the generator — which is what every one of
    the four evasions does — and the declaration describes a build that is not
    this one. The enclave repo's own test fails on that, and so does this: the
    pinned sha256 no longer matches the file at the same commit.
    """
    tampered = dict(ENCLAVE[COMMIT_V1_AND_V2])
    tampered[f"{ENCLAVE_PACKAGE}/cache.go"] = cache_go(accepts_v2=True).replace(
        b"return aadV2(namespaceProvider, workspaceID, provider)",
        b'return nil, errors.New("byokcache: v2 reads are not enabled in this build")',
    )

    with pytest.raises(ValueError, match="different build"):
        accepted_formats(COMMIT_V1_AND_V2, source_from({COMMIT_V1_AND_V2: tampered}))


def test_a_declaration_is_read_for_every_file_it_pins() -> None:
    # The pin is worth nothing if a pinned file that cannot be fetched is
    # treated as unchanged.
    incomplete = {COMMIT_V1_AND_V2: {DECLARATION_PATH: ENCLAVE[COMMIT_V1_AND_V2][DECLARATION_PATH]}}

    with pytest.raises(ValueError, match="cache.go"):
        accepted_formats(COMMIT_V1_AND_V2, source_from(incomplete))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"schema": "something/else"}, "schema", id="unknown-schema"),
        pytest.param({"package": "enclave-go/internal/other"}, "package", id="wrong-package"),
        pytest.param({"accepted": []}, "no accepted formats", id="empty-accepted"),
        pytest.param({"accepted": [""]}, "non-empty strings", id="blank-format"),
        pytest.param({"accepted": [V1, 7]}, "non-empty strings", id="non-string-format"),
        pytest.param({"rejected_control": ""}, "rejected_control", id="no-control-value"),
        pytest.param({"source_sha256": {}}, "pins no source files", id="no-pins"),
        pytest.param(
            {"source_sha256": {"../../etc/passwd": "0" * 64}}, "not a .go file", id="path-escape"
        ),
        pytest.param({"source_sha256": {"cache.go": "nope"}}, "not a sha256", id="bad-digest"),
    ],
)
def test_a_declaration_this_script_cannot_read_blocks(
    overrides: dict[str, Any], reason: str
) -> None:
    # Never assume a superset. A declaration in a shape this script does not
    # understand is a build whose accepted set is unknown, and unknown blocks.
    sources = {"cache.go": cache_go(accepts_v2=True)}
    files = {f"{ENCLAVE_PACKAGE}/cache.go": sources["cache.go"]}
    files[DECLARATION_PATH] = declaration([V1, V2], sources, overrides)

    with pytest.raises(ValueError, match=reason):
        accepted_formats(COMMIT_V1_AND_V2, source_from({COMMIT_V1_AND_V2: files}))


def test_a_probe_that_accepted_its_own_control_value_blocks() -> None:
    """`accepted` means something only if the probe could have said no.

    A generator that accepts everything would list the control value too. That
    is not a build that accepts everything; it is a measurement of nothing.
    """
    sources = {"cache.go": cache_go(accepts_v2=True)}
    files = {f"{ENCLAVE_PACKAGE}/cache.go": sources["cache.go"]}
    files[DECLARATION_PATH] = declaration([V1, V2, CONTROL], sources)

    with pytest.raises(ValueError, match="cannot distinguish"):
        accepted_formats(COMMIT_V1_AND_V2, source_from({COMMIT_V1_AND_V2: files}))


def test_a_declaration_that_is_not_json_blocks() -> None:
    files = {
        f"{ENCLAVE_PACKAGE}/cache.go": cache_go(accepts_v2=True),
        DECLARATION_PATH: b"404: Not Found",
    }

    with pytest.raises(ValueError, match="not JSON"):
        accepted_formats(COMMIT_V1_AND_V2, source_from({COMMIT_V1_AND_V2: files}))


# --------------------------------------------------------------------------
# WRITES is derived from the whole write surface, or the gate rots
# --------------------------------------------------------------------------


def test_written_formats_follow_the_source_not_a_constant_in_this_file() -> None:
    """A V3 write must be picked up with no edit to the checker.

    Hardcoding "the plane writes V2" is the failure that makes a gate correct
    exactly once. Here a fabricated module writes a format that exists nowhere
    in the codebase, and the parser must report it.
    """
    source = (
        f'ALGORITHM_V3 = "{V3}"\n'
        "def seal():\n"
        "    return EncryptedSecretEnvelope(algorithm=ALGORITHM_V3, key_ref='k')\n"
    )

    assert written_formats(module(source)) == frozenset({V3})


def test_written_formats_reports_every_format_the_module_writes() -> None:
    source = (
        f'A = "{V1}"\n'
        f'B = "{V2}"\n'
        "def old():\n"
        "    return EncryptedSecretEnvelope(algorithm=A)\n"
        "def new():\n"
        "    return EncryptedSecretEnvelope(algorithm=B)\n"
    )

    assert written_formats(module(source)) == frozenset({V1, V2})


@pytest.mark.parametrize(
    ("body", "label"),
    [
        pytest.param(
            "_Envelope = EncryptedSecretEnvelope\n"
            "def seal():\n"
            "    return _Envelope(algorithm=ALGORITHM_V3)\n",
            "a module-level alias",
            id="alias",
        ),
        pytest.param(
            "def seal():\n"
            "    Envelope = EncryptedSecretEnvelope\n"
            "    return Envelope(algorithm=ALGORITHM_V3)\n",
            "an alias bound inside the function",
            id="local-alias",
        ),
        pytest.param(
            "class V3Envelope(EncryptedSecretEnvelope):\n"
            "    pass\n"
            "def seal():\n"
            "    return V3Envelope(algorithm=ALGORITHM_V3)\n",
            "a subclass",
            id="subclass",
        ),
        pytest.param(
            "import dataclasses\n"
            "def seal(envelope):\n"
            "    return dataclasses.replace(envelope, algorithm=ALGORITHM_V3)\n",
            "dataclasses.replace",
            id="replace",
        ),
        pytest.param(
            "from dataclasses import replace\n"
            "def seal(envelope):\n"
            "    return replace(envelope, algorithm=ALGORITHM_V3)\n",
            "an imported replace",
            id="bare-replace",
        ),
    ],
)
def test_written_formats_sees_a_v3_written_through_an_indirection(body: str, label: str) -> None:
    """Each of these returned {V2} while the build wrote V3.

    The reviewer wrote all of them against the first version of this parser,
    which matched a callee spelled exactly EncryptedSecretEnvelope. A gate that
    can be stepped around by renaming a local is a gate about spelling.
    """
    source = f'ALGORITHM_V3 = "{V3}"\n' + body

    assert written_formats(module(source)) == frozenset({V3}), label


def test_written_formats_sees_a_write_site_in_any_module() -> None:
    # The first version opened src/trusted_router/byok_crypto.py and nothing
    # else, so a V3 write in a new module was invisible.
    sources = {
        "src/trusted_router/byok_crypto.py": (
            f'ALGORITHM_V2 = "{V2}"\n'
            "def seal():\n"
            "    return EncryptedSecretEnvelope(algorithm=ALGORITHM_V2)\n"
        ),
        "src/trusted_router/broadcast_crypto.py": (
            f'ALGORITHM_V3 = "{V3}"\n'
            "def seal():\n"
            "    return EncryptedSecretEnvelope(algorithm=ALGORITHM_V3)\n"
        ),
    }

    assert written_formats(sources) == frozenset({V2, V3})


def test_written_formats_refuses_a_format_assigned_after_construction() -> None:
    """`envelope.algorithm = ALGORITHM_V3` writes V3 and no constructor says so.

    EncryptedSecretEnvelope is a plain, non-frozen dataclass, so this runs. The
    format written is not derivable from any call, so the answer is a refusal
    rather than a set that quietly omits it.
    """
    source = (
        f'ALGORITHM_V3 = "{V3}"\n'
        "def seal(envelope):\n"
        "    envelope.algorithm = ALGORITHM_V3\n"
        "    return envelope\n"
    )

    with pytest.raises(ValueError, match="assigns .algorithm"):
        written_formats(module(source))


def test_written_formats_refuses_the_constructor_used_as_a_value() -> None:
    # functools.partial, a factory table, a registry: the format written
    # through one of those is chosen somewhere this parser does not look.
    source = (
        "import functools\n"
        f'ALGORITHM_V3 = "{V3}"\n'
        "make = functools.partial(EncryptedSecretEnvelope, algorithm=ALGORITHM_V3)\n"
    )

    with pytest.raises(ValueError, match="indirection"):
        written_formats(module(source))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "def seal(alg):\n    return EncryptedSecretEnvelope(algorithm=alg)\n",
            id="algorithm-is-a-parameter",
        ),
        pytest.param(
            "def seal(kw):\n    return EncryptedSecretEnvelope(**kw)\n",
            id="kwargs-splat-outside-the-rehydration-modules",
        ),
        pytest.param(
            "def seal(stored, alg):\n"
            "    return EncryptedSecretEnvelope(**{**stored, 'algorithm': alg})\n",
            id="mapping-display-splat",
        ),
        pytest.param(
            "def seal():\n    return EncryptedSecretEnvelope(key_ref='k')\n",
            id="no-algorithm-at-all",
        ),
        pytest.param(
            "def seal(alg):\n    return EncryptedSecretEnvelope(alg, key_ref='k')\n",
            id="positional",
        ),
        pytest.param("X = 1\n", id="constructor-gone"),
    ],
)
def test_written_formats_fails_closed_when_it_cannot_derive_the_format(source: str) -> None:
    # An unreadable write side is not a safe write side. Returning an empty set
    # would make every subset check pass trivially, which is the worst possible
    # way for this to fail.
    with pytest.raises(ValueError):
        written_formats(module(source))


def test_a_rehydration_site_is_allowed_only_where_it_rebuilds_a_stored_row() -> None:
    """`EncryptedSecretEnvelope(**stored)` reads a format, it does not choose one.

    Two modules do this today and both are rebuilding a database row. The
    allowlist is a refusal boundary, not a rule: the same shape anywhere else
    blocks until someone decides which kind it is. This asserts both halves.
    """
    rebuild = "def rebuild(stored):\n    return EncryptedSecretEnvelope(**stored)\n"
    write = f'ALGORITHM_V2 = "{V2}"\ndef seal():\n    return EncryptedSecretEnvelope(algorithm=ALGORITHM_V2)\n'  # noqa: E501

    scan = scan_write_surface(
        {
            "src/trusted_router/storage_models.py": rebuild,
            "src/trusted_router/byok_crypto.py": write,
        }
    )
    assert scan.formats == frozenset({V2})
    assert scan.rehydration_sites == ("src/trusted_router/storage_models.py:2",)

    with pytest.raises(ValueError, match="_REHYDRATION_MODULES"):
        written_formats({"src/trusted_router/storage_gcp.py": rebuild, **module(write)})


def test_the_real_write_surface_writes_exactly_v2_today() -> None:
    # Pins the current answer so a change to the write side is visible in a
    # diff, without the gate itself depending on that answer. Also pins the
    # rehydration sites: a new one appears here before it appears in
    # production, and it is the one shape the scan reads no format from.
    scan = scan_write_surface(read_write_surface())

    assert scan.formats == frozenset({V2})
    assert scan.rehydration_sites == (
        "src/trusted_router/byok_aad_backfill.py:210",
        "src/trusted_router/storage_models.py:217",
        "src/trusted_router/storage_models.py:252",
        "src/trusted_router/storage_models.py:254",
    )


def test_an_unparseable_module_blocks() -> None:
    with pytest.raises(ValueError, match="cannot parse"):
        written_formats(module("def seal(:\n"))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def test_the_table_names_the_blocked_region_and_the_reason() -> None:
    # An operator reads this at the moment a deploy stops. If it does not say
    # which region and why, the next step is to bypass the gate.
    results = run("aws", aws_record(source_commit=COMMIT_V1_ONLY), frozenset({V2}))

    table = render(results, frozenset({V2}))

    assert "BLOCKED" in table
    assert "api-aws.trustedrouter.com" in table
    assert COMMIT_V1_ONLY in table
    assert "V1" in table


def test_a_measurement_that_was_read_never_renders_as_unknown() -> None:
    """ "-" in this table means NOT READ.

    The column elided anything 20 characters or shorter to "-", so a short
    measurement that was successfully read printed identically to one that was
    never obtained — in the operator-facing table of a fail-closed gate, where
    every other "-" means unknown.
    """
    short = RegionResult(cloud="gcp", host="api.example.com", measurement="sha256:abc")
    unread = RegionResult(cloud="gcp", host="api.example.com", problems=["cannot read"])

    table = render([short, unread], frozenset({V2}))

    assert "sha256:abc" in table
    assert table.count("-  ") >= 1, "the unread row still renders its measurement as -"
