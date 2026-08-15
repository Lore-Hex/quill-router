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
format the build no longer writes. `test_written_formats_follow_the_source_not_a
_constant_in_this_file` and `test_accepted_set_comes_from_the_switch_not_the
_const_block` are the two that keep it from rotting into that.

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
  raw.githubusercontent.com serves cache.go at an arbitrary commit. Those are
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
* The subset check is over algorithm STRINGS. It does not prove the two
  implementations of a shared format agree byte for byte; that is
  tests/test_byok_aad_namespace_property.py's pinned hex vector, and the two
  proofs are independent.
* Region coverage is only as wide as the record the gate reads. These fixtures
  hand it a record with two Azure regions and prove both get checked; they do
  not prove the published record has two. The control-plane mirror dropped the
  `regions` array until the fix in services/trust_release.py — see
  test_trust_release.py's provenance-and-regions block — so against a plane
  predating that fix the gate checks one Azure region of two and reports a
  green result it is entitled to. That is a coverage limit, not a false pass,
  and it is the reason the checker prints the region list it used.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import cbor2
import pytest

from scripts.check_format_ordering import (
    PLANES,
    RegionResult,
    accepted_formats,
    check_plane,
    gather,
    regions_of,
    render,
    written_formats,
)

V1 = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"
V2 = "TR-BYOK-ENVELOPE-AES-256-GCM-V2"

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


def cache_go(*, accepts_v2: bool, declares_v2: bool = True) -> str:
    """cache.go as the enclave really shapes it.

    `declares_v2` and `accepts_v2` are separate on purpose: the pre-step-1
    enclave is exactly the build that can declare AlgorithmV2 in its const block
    while envelopeAAD still rejects it.
    """
    const_v2 = f'\tAlgorithmV2 = "{V2}"\n' if declares_v2 else ""
    case_v2 = "\tcase AlgorithmV2:\n\t\treturn aadV2(namespaceProvider, workspaceID, provider)\n"
    return (
        "package byokcache\n\n"
        "const (\n"
        f'\tAlgorithm = "{V1}"\n'
        f"{const_v2}"
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
    )


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


def enclave_source(commit: str) -> str:
    """Injected stand-in for the raw.githubusercontent.com fetch."""
    if commit == COMMIT_V1_ONLY:
        return cache_go(accepts_v2=False)
    if commit == COMMIT_V1_AND_V2:
        return cache_go(accepts_v2=True)
    raise ValueError(f"no fixture for commit {commit}")


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
        source=kwargs.pop("source", enclave_source),
        **kwargs,
    )


def problems(results: list[RegionResult]) -> str:
    return " | ".join(problem for result in results for problem in result.problems)


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
        source=enclave_source,
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
    results = run("gcp", gcp_record(accepted_image_digests=["sha256:" + "0b" * 32]), frozenset({V2}))

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


def test_an_unfetchable_enclave_source_blocks() -> None:
    def exploding(commit: str) -> str:
        raise ValueError(f"HTTP 404 for {commit}")

    results = run("gcp", gcp_record(), frozenset({V2}), source=exploding)

    assert not results[0].ok
    assert "404" in problems(results)


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
        source=enclave_source,
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
        source=enclave_source,
    )

    assert not results[0].ok
    assert "no usable release record" in problems(results)


# --------------------------------------------------------------------------
# Both sides of the subset must be DERIVED, or the gate rots
# --------------------------------------------------------------------------


def test_written_formats_follow_the_source_not_a_constant_in_this_file() -> None:
    """A V3 write must be picked up with no edit to the checker.

    Hardcoding "the plane writes V2" is the failure that makes a gate correct
    exactly once. Here a fabricated module writes a format that exists nowhere
    in the codebase, and the parser must report it.
    """
    source = (
        'ALGORITHM_V3 = "TR-BYOK-ENVELOPE-AES-256-GCM-V3"\n'
        "def seal():\n"
        "    return EncryptedSecretEnvelope(algorithm=ALGORITHM_V3, key_ref='k')\n"
    )

    assert written_formats(source, origin="fabricated") == frozenset(
        {"TR-BYOK-ENVELOPE-AES-256-GCM-V3"}
    )


def test_written_formats_reports_every_format_the_module_writes() -> None:
    source = (
        f'A = "{V1}"\n'
        f'B = "{V2}"\n'
        "def old():\n"
        "    return EncryptedSecretEnvelope(algorithm=A)\n"
        "def new():\n"
        "    return EncryptedSecretEnvelope(algorithm=B)\n"
    )

    assert written_formats(source, origin="fabricated") == frozenset({V1, V2})


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "def seal(alg):\n    return EncryptedSecretEnvelope(algorithm=alg)\n",
            id="algorithm-is-a-parameter",
        ),
        pytest.param(
            "def seal(kw):\n    return EncryptedSecretEnvelope(**kw)\n",
            id="kwargs-splat",
        ),
        pytest.param(
            "def seal():\n    return EncryptedSecretEnvelope(key_ref='k')\n",
            id="no-algorithm-at-all",
        ),
        pytest.param("X = 1\n", id="constructor-gone"),
    ],
)
def test_written_formats_fails_closed_when_it_cannot_derive_the_format(source: str) -> None:
    # An unreadable write side is not a safe write side. Returning an empty set
    # would make every subset check pass trivially, which is the worst possible
    # way for this to fail.
    with pytest.raises(ValueError):
        written_formats(source, origin="fabricated")


def test_the_real_byok_crypto_module_writes_exactly_v2_today() -> None:
    # Pins the current answer so a change to the write side is visible in a
    # diff, without the gate itself depending on that answer.
    from scripts.check_format_ordering import BYOK_CRYPTO

    assert written_formats(BYOK_CRYPTO.read_text(encoding="utf-8")) == frozenset({V2})


def test_accepted_set_comes_from_the_switch_not_the_const_block() -> None:
    """Declaring AlgorithmV2 is not the same as accepting it.

    This is the exact shape of the pre-step-1 enclave: the constant exists in
    cache.go, the dispatch does not handle it. A parser that read the const
    block would call this build v2-capable and wave through the deploy that
    breaks every BYOK key in that cloud.
    """
    source = cache_go(accepts_v2=False, declares_v2=True)

    assert V2 in source, "the fixture must declare the constant, or it proves nothing"
    assert accepted_formats(source, origin="fabricated") == frozenset({V1})


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("package byokcache\n", id="no-envelopeAAD"),
        pytest.param(
            "package byokcache\n"
            "func envelopeAAD(algorithm string) ([]byte, error) {\n"
            "\tswitch algorithm {\n"
            "\tcase somethingUndeclared:\n"
            "\t\treturn nil, nil\n"
            "\t}\n"
            "}\n",
            id="unresolvable-case",
        ),
        pytest.param(
            "package byokcache\n"
            "func envelopeAAD(algorithm string) ([]byte, error) {\n"
            "\tif algorithm == Algorithm {\n"
            "\t\treturn nil, nil\n"
            "\t}\n"
            "\treturn nil, nil\n"
            "}\n",
            id="not-a-switch",
        ),
        pytest.param(
            "package byokcache\n"
            "func envelopeAAD(algorithm string) ([]byte, error) {\n"
            "\tswitch other {\n"
            "\tcase Algorithm:\n"
            "\t\treturn nil, nil\n"
            "\t}\n"
            "}\n",
            id="switch-on-something-else",
        ),
        pytest.param(
            "package byokcache\n"
            "func envelopeAAD(algorithm string) ([]byte, error) {\n"
            "\tswitch algorithm {\n"
            "\tdefault:\n"
            "\t\treturn nil, nil\n"
            "\t}\n"
            "}\n",
            id="default-only",
        ),
    ],
)
def test_accepted_formats_fails_closed_on_anything_it_cannot_read(source: str) -> None:
    # Never assume a superset. A cache.go this parser cannot read is a build
    # whose accepted set is unknown, and unknown must block.
    with pytest.raises(ValueError):
        accepted_formats(source, origin="fabricated")


def test_accepted_formats_handles_a_combined_case_clause() -> None:
    # gofmt permits `case A, B:` and a future cache.go may collapse the two
    # branches. Reading only the first token would silently narrow the accepted
    # set and block a legal deploy.
    source = (
        "package byokcache\n"
        f'const Algorithm = "{V1}"\n'
        f'const AlgorithmV2 = "{V2}"\n'
        "func envelopeAAD(algorithm, workspaceID string) ([]byte, error) {\n"
        "\tswitch algorithm {\n"
        "\tcase Algorithm, AlgorithmV2:\n"
        "\t\treturn nil, nil\n"
        "\t}\n"
        "}\n"
    )

    assert accepted_formats(source, origin="fabricated") == frozenset({V1, V2})


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
