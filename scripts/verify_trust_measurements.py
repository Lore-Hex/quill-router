#!/usr/bin/env python3
"""Check every measurement we publish against a live attestation from that plane.

This exists because the failure it catches already happened. The Quill trust
bucket published a PCR0 from the initial commit onward, and it matched no
running enclave — the file was correct once, the enclave was rebuilt, and
nothing anywhere compared the two. A published measurement that has drifted is
worse than no measurement: it invites a verifier to conclude the gateway has
been tampered with, or worse, teaches them to ignore a mismatch.

Fetching happens against the live serving planes. Nothing here is billed and no
prompt is sent; every endpoint used is an unauthenticated attestation route.

    uv run python scripts/verify_trust_measurements.py
    uv run python scripts/verify_trust_measurements.py --strict
    uv run python scripts/verify_trust_measurements.py --control-plane https://…

WHAT THIS ANSWERS
    "Is what we publish what is running, on every plane we serve from?"
Exit status is 0 only if every published measurement matched a live attestation
AND every SERVING ENDPOINT the record enumerates was actually contacted. Named
exactly, because the sweeping version of this sentence was false: the endpoints
enumerated are gcp `api_base_url`, aws `api_base_url`, and azure `api_base_url`
plus every `regions[].attestation_url` that names a reachable place — one that
does not is reported as a coverage gap rather than fetched. That last item is a
PLUS and not an OR, which it was not when this sentence was first written:
regions[] was an else branch for api_base_url, so a record enumerating one
region left the gateway it advertises to verifiers uncontacted, and the run
still printed the full success sentence. Two published lists are NOT contacted —
gcp `api_base_urls[]` and `tls.hostnames[]`. The attestation routes derived from
`api_base_urls[]` are printed under the gcp result, so the limit is visible
where the verdict is and not only here.

WHAT THIS VERSION CLOSES, AND ONE CLAIM IT REFUTES
Measured 2026-08-15 by running the previous version of this file (82be9cdf)
against a fixture plane and against production:

  1. Nothing ran it. Every other mention of this file in quill-router was a
     COMMENT pointing at it as the thing that would catch drift — config.py
     line 365 and trust.py line 142 — and no workflow, script or test invoked
     it. (The brief that started this work placed those comments in
     scripts/deploy/rollout.sh; that file does not mention the script at all.
     The substance held, the location did not.) A drift check nobody is
     required to run has the reliability of a note in a drawer, which is why it
     is now wired to .github/workflows/trust-drift.yml on a schedule.
  2. REFUTED, and recorded because acting on it would have been wasted work:
     the brief said GCP was not checked at all and that main() looped over
     (aws, azure). It did not. 82be9cdf's main() loops over
     (("gcp", check_gcp), ("aws", check_aws), ("azure", check_azure)) and its
     check_gcp compares the running digest against accepted_image_digests. Run
     against a fixture, the old file printed "[ok] gcp: image digest matches".
     What was actually wrong on GCP was smaller: the endpoint was a constant
     here rather than the record's own api_base_url, and image_reference was
     published but compared against nothing. Both are fixed below.
  3. It contacted ONE Azure region. AZURE_ATTESTATION_URL was hardcoded to UAE
     North while the plane also serves Southeast Asia from
     api-azure-sea.trustedrouter.com with its own CCE policy and therefore its
     own hostdata (f3a0b4ed…d712d81c). That region was never contacted, and its
     measurement was never compared to anything. Demonstrated: with Southeast
     Asia serving a hostdata published nowhere, the old file fetched only the
     UAE North endpoint, printed "[ok] azure: hostdata and issuer match", and
     exited 0.
  4. Having checked one Azure region of two it printed "Every published
     measurement matches a live attestation." Run and confirmed verbatim. The
     endpoints reached are now stated in the summary, so the last line can
     never claim more coverage than the run had.

ENDPOINTS COME FROM THE RECORD, NOT FROM CONSTANTS HERE
    A plane that grows a region grows it in its own published record. Hardcoding
    the endpoint list here is how hole 3 happened: the record already named the
    second Azure region and this script could not see it. Azure endpoints are
    read from record["regions"]; the per-plane constants below survive only as
    the fallback for a record that names no api_base_url.

    A corollary that has teeth: if the record publishes an MAA issuer that NONE
    of the endpoints this run contacted presented, there is a serving region we
    publish measurements for and cannot reach. That is reported as [GAP] and
    fails the run, because an "ok" printed under it would be the same overclaim
    as hole 4. The attribution is from the issuers observed live, not from the
    issuer the record attributes to an entry and never by list position — an
    earlier revision sliced the issuer list by count and could name as unreached
    the very issuer whose region HAD answered.

    That corollary rests on an OPTIONAL field, which is its weak point and is
    handled rather than assumed. A record that publishes no `attestation_issuers`
    has no region census in it at all, and a record that publishes no `regions[]`
    array while naming more than one accepted hostdata value cannot distinguish a
    second serving region from a bind window. Both are [GAP], not [ok]: coverage
    that cannot be established is not coverage. Before that, deleting one
    optional field from the record turned off both halves of the multi-region
    guarantee and a two-region plane passed --strict having contacted one region.

THE MIRROR BOUNDS THIS SCRIPT, AND THE VALIDATOR BOUNDS THE MIRROR
    Endpoints can only come from the record if the record still has them when it
    is served. trusted_router.trust.azure_release mirrors the plane's `regions[]`
    array through, and trusted_router.services.trust_release.validated_azure_metadata
    — the validator the serving route interposes ahead of it — validates and
    carries that array rather than whitelisting three scalar keys and dropping
    it. Fixing only the former is dead code on the production route; that is
    exactly what the first version of this change did, and the record served was
    byte-identical to before it. A region entry the rest of the record
    contradicts is refused whole there, not dropped: dropping erases a live
    serving region from the published record with nothing left downstream to
    notice, which is this script's failure mode wearing the mirror's clothes.

WHY A SKIP IS LENIENT BY DEFAULT AND FATAL UNDER --strict
    Locally this script is a diagnostic: it is run against staging mirrors, a
    control plane mid-bring-up, and branches where a plane genuinely publishes
    nothing yet. Making an unpublished plane fatal there trains the reader to
    pass a flag to silence it, and a check people routinely silence is a check
    that stops being read.
    In CI the opposite is true, and it is the whole reason the scheduled job
    exists: "we stopped publishing a measurement" and "everything matches" must
    not be the same green tick. On a schedule, silence is the failure mode being
    hunted, so the workflow passes --strict and a skip is a failure there.

WHAT THIS DOES NOT ESTABLISH
    * No signature is verified. Not the Nitro COSE_Sign1 signature, not the
      Nitro certificate chain, not the MAA JWT signature, not the Confidential
      Space JWT signature. Every live value here is read out of an unauthenticated
      response. The canonical, cryptographically complete verifier is
      quill-cloud-proxy's tools/verify-attestation.py. This script deliberately
      does less: it answers "is what we publish what is running", which is the
      question a signature check does not ask.
    * AWS is SAMPLED, not enumerated. api-aws.trustedrouter.com is fronted by a
      Global Accelerator across more than one enclave, so N fetches reach some
      subset of the fleet. Measured 2026-08-15: 8 consecutive fetches from one
      client all reached i-02e34e58761097671-enc01a004d7a9c3c307, while the
      published record's observed_module_id names a DIFFERENT enclave
      (i-0ada95aad6d11aa56-enc01a004f1c2824652) — so repeated sampling from one
      vantage point does not converge on the fleet, and the distinct-module-id
      count this prints is a lower bound, never a census. A green AWS line means
      "every enclave we happened to reach matched", nothing more. Enumerating
      the fleet needs per-NLB pinning (see trusted_router.synthetic.probes and
      TR_SYNTHETIC_GATEWAY_REGION_TARGETS), which is a different tool's job.
    * The AWS certificate binding checked here is user_data[0:32] only. user_data
      is 96 bytes: [0:32] is SHA-256 of the served certificate DER (verified on
      6/6 samples when this was written, 8/8 on re-measurement), [32:64] is a
      constant this script does not interpret, and [64:96] is the TLS exporter
      channel binding, which cannot be checked from here because CPython's ssl
      module exposes no keying-material export. The payload's public_key/SPKI
      binding is likewise not checked here; probes.py::_aws_cert_binding_ok
      checks both and is the reference for that.
    * Matching the accepted SET is not proof the primary is serving. Both are
      reported; only set membership is enforced, because a rolling deploy
      legitimately serves the outgoing measurement while the record already
      names the incoming one.
    * The GCP record's `api_base_urls[]` and `tls.hostnames[]` are NOT contacted.
      They are alias hostnames (api.allyrouter.com, api.uptimerouter.com) for
      the one Confidential Space workload the scalar `api_base_url` names, and
      the record attributes no separate measurement to them — so fetching them
      would re-verify the same image digest. Whether each alias still RESOLVES
      to that workload is a different question, endpoint hijack rather than
      measurement drift, and .github/workflows/deploy.yml's alias smoke is what
      asks it. This run prints those endpoints under the gcp result rather than
      leaving the gap to be inferred from silence. Azure's `regions[]` is not
      the same case and is enumerated: those entries name DIFFERENT CCE policies
      and therefore different measurements at different endpoints.
    * "The same endpoint" is decided on scheme, host, port and path, by
      trusted_router.endpoint_identity — the same code the mirror's validator
      uses, because two implementations of this question disagreeing is itself a
      false-coverage lever. DNS is NOT consulted: two hostnames that resolve to
      one workload count as two endpoints here, so a record can still inflate
      its region count by publishing two names for one gateway. Nothing in the
      record says they are the same, and finding out is endpoint-identity work
      rather than measurement drift. Percent-encoding and path case are compared
      as written, for the same reason an origin is entitled to distinguish them.
    * An Azure record naming ONE region, ONE issuer and TWO accepted hostdata
      values is accepted as covered. That shape is a one-region plane mid-roll
      and a two-region plane whose second region was never published, and the
      record does not distinguish them — accepted_hostdata rolls when a CCE
      policy changes, so it cannot be read as a region count. Closing this needs
      the plane to publish the region; it is not closeable from this side, and
      the gap rules above deliberately do not guess.
    * The three planes are a constant here, not read from the record. There is
      no index endpoint listing them; gcp, aws and azure are the only release
      records that exist. A fourth plane would have to be added to this file by
      hand, and nothing in this file would notice if it were not.
    * NetworkTransport accepts a literal `http://127.0.0.1:` prefix so a local
      fixture plane can be pointed at. `http://127.0.0.1:80@evil.com/x` passes
      that prefix check through the URL userinfo trick — reachable only by
      passing a hostile --control-plane, and the guard's stated purpose
      (refusing file://) is unaffected.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import cbor2

# The one thing this script shares with the control plane it reads from: what
# makes two published URLs the same endpoint. See _endpoint_identity below for
# why a private copy here was a defect rather than a convenience.
from trusted_router.endpoint_identity import Identity, parse_endpoint

# Fallbacks only: used when a record names no api_base_url to derive from.
GCP_ATTESTATION_URL = "https://api.trustedrouter.com/attestation"
AWS_ATTESTATION_URL = "https://api-aws.trustedrouter.com/attestation"
AZURE_ATTESTATION_URL = "https://api-azure.trustedrouter.com/attestation"
GCP_ATTESTATION_ISSUER = "https://confidentialcomputing.googleapis.com"
DEFAULT_CONTROL_PLANE = "https://trustedrouter.com"
TIMEOUT_SECONDS = 25
NOT_CONFIGURED = "not-configured"
# Enough fetches that a fleet the accelerator actually spreads us across shows
# up as more than one module id, few enough that the whole run stays under a
# CI step's patience. It buys a lower bound, not coverage — see the scope limit.
DEFAULT_AWS_SAMPLES = 5
# The enclave's user_data layout, in bytes. Named so a future reader does not
# have to rediscover that the certificate hash is 32 bytes and not 64, which is
# what trust.py's published `certificate_binding` string used to say.
AWS_USER_DATA_LENGTH = 96
AWS_USER_DATA_CERT_SHA256 = slice(0, 32)


@dataclass
class Result:
    """One plane's verdict, plus what the verdict is actually based on.

    `endpoints` and `unreached` exist so the summary can be honest: the old
    success line could not distinguish "checked three planes" from "checked one
    and skipped two" because nothing carried the coverage.
    """

    plane: str
    ok: bool
    detail: str
    skipped: bool = False
    gap: bool = False
    endpoints: tuple[str, ...] = ()
    unreached: tuple[str, ...] = ()
    extra: list[str] = field(default_factory=list)

    @property
    def mark(self) -> str:
        if self.skipped:
            return "SKIP"
        if self.gap:
            return "GAP"
        return "ok" if self.ok else "DRIFT"


@dataclass(frozen=True)
class Fetched:
    body: bytes
    peer_certificate_der: bytes | None = None


class Transport:
    """Every network read this script performs, behind one seam.

    The proofs in tests/test_trust_measurement_drift.py substitute recorded
    plane responses for live ones through this. Without the seam the only way
    to test the checker is to point it at production, which means the test
    suite passes or fails on whether prod is up — and a drift check whose own
    tests are flaky gets muted, which returns us to hole 1.
    """

    def fetch(
        self, url: str, *, verify_tls: bool = True, want_peer_certificate: bool = False
    ) -> Fetched:  # pragma: no cover - interface
        raise NotImplementedError


class NetworkTransport(Transport):
    def fetch(
        self, url: str, *, verify_tls: bool = True, want_peer_certificate: bool = False
    ) -> Fetched:
        if not url.startswith(("https://", "http://127.0.0.1:")):
            # Scheme is fixed by construction; assert it anyway so a future
            # caller cannot turn this into a file:// read.
            raise ValueError(f"refusing to fetch non-HTTP(S) URL {url!r}")
        if want_peer_certificate:
            return self._fetch_with_peer_certificate(url, verify_tls=verify_tls)
        context: ssl.SSLContext | None = None
        if not verify_tls:
            context = _unverified_context()
        request = urllib.request.Request(url, headers={"accept": "*/*"})  # noqa: S310
        with urllib.request.urlopen(  # noqa: S310 - scheme checked above
            request, timeout=TIMEOUT_SECONDS, context=context
        ) as response:
            return Fetched(response.read())

    def _fetch_with_peer_certificate(self, url: str, *, verify_tls: bool) -> Fetched:
        """http.client rather than urllib, because urllib hands back no socket.

        The certificate that has to be hashed is the one served on THIS
        connection; reading it from a second connection would compare the
        attestation against a certificate it never saw.
        """
        parts = urllib.parse.urlsplit(url)
        context = ssl.create_default_context() if verify_tls else _unverified_context()
        connection = http.client.HTTPSConnection(
            parts.hostname or "",
            parts.port or 443,
            context=context,
            timeout=TIMEOUT_SECONDS,
        )
        target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        try:
            connection.request("GET", target, headers={"accept": "*/*"})
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                raise urllib.error.HTTPError(
                    url, response.status, response.reason, response.headers, None
                )
            sock = connection.sock
            peer = sock.getpeercert(binary_form=True) if isinstance(sock, ssl.SSLSocket) else None
        finally:
            connection.close()
        return Fetched(body, peer)


def _unverified_context() -> ssl.SSLContext:
    # The AWS enclave serves a certificate it generated itself, so there is no
    # chain to validate — that is the design, not a defect. The binding that
    # replaces chain validation lives in the attestation's user_data, and this
    # script now checks the certificate half of it (see check_aws). The other
    # half, the TLS exporter value, still is not checked here, so treat an AWS
    # read as authenticated only as far as "the document names this cert".
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# 503 is what the control plane returns for a record it has no measurement for.
# Static origins say the same thing differently: S3 returns 403 for a missing
# object on a bucket that denies listing, GCS and GitHub Pages return 404.
# Treating only 503 as "unpublished" made this script CRASH against exactly the
# mirrors the records are moving to — a stack trace where the honest answer is
# "that plane publishes nothing here".
NOT_PUBLISHED_STATUSES = frozenset({403, 404, 503})


def _published(control_plane: str, path: str, transport: Transport) -> dict[str, Any] | None:
    """Published release record, or None when nothing is published there."""
    url = f"{control_plane.rstrip('/')}{path}"
    try:
        return json.loads(transport.fetch(url).body)
    except urllib.error.HTTPError as exc:  # noqa: UP041 - urllib error hierarchy
        if exc.code in NOT_PUBLISHED_STATUSES:
            return None
        raise
    except ValueError as exc:
        # A 200 carrying something that is not a record is a different failure
        # from an absent record, and must not be silently read as "unpublished"
        # — that is how a broken mirror looks identical to an honest one.
        raise ValueError(f"{url} returned a non-JSON body: {exc}") from exc


def _attestation_url(record: dict[str, Any], fallback: str) -> str:
    """The plane's attestation route, derived from the record it published.

    api_base_url is where a verifier is told to go, so the drift check has to
    go THERE and not to a constant compiled in here. A record whose api_base_url
    disagrees with the endpoint this script polls would let the two describe
    different gateways while the run stayed green.
    """
    endpoint = parse_endpoint(record.get("api_base_url"))
    if endpoint is None:
        # Includes an api_base_url urllib cannot parse at all. Returning the
        # fallback rather than raising keeps a malformed field in ONE plane's
        # record from being indistinguishable from the whole check crashing.
        return fallback
    netloc = endpoint.host if endpoint.port is None else f"{endpoint.host}:{endpoint.port}"
    return f"{endpoint.scheme}://{netloc}/attestation"


def _alias_attestation_urls(record: dict[str, Any], contacted: str) -> list[str]:
    """Endpoints the record also publishes in api_base_urls[] and does not get contacted.

    Returned so the GCP result can PRINT them rather than leave the reader to
    infer coverage from silence. They are deliberately not fetched — see the
    scope limit in the module docstring for why alias hostnames are a different
    question from measurement drift — and this is the mechanism that keeps the
    limit visible in every run instead of only in a docstring.
    """
    raw = record.get("api_base_urls")
    if not isinstance(raw, list):
        return []
    urls: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value:
            continue
        url = _attestation_url({"api_base_url": value}, "")
        if url and url != contacted and url not in urls:
            urls.append(url)
    return urls


def _endpoint_identity(url: object) -> Identity | None:
    """What a client would actually talk to, or None when the URL names no place.

    Coverage is a count of PLACES CONTACTED, so what makes two published URLs
    the same has to be what the request lands on — scheme, host, port, path —
    and never the raw string. `.../attestation#a` and `.../attestation#b` are
    different strings; the fragment is not transmitted, so they are one
    endpoint, and counting them as two is this file's own defect rebuilt out of
    punctuation. Query strings are folded in for the same reason: an attestation
    route is not made into a second region by a parameter.

    The comparison itself is trusted_router.endpoint_identity, imported rather
    than reimplemented, and that import is the point. This file had its own copy
    and the mirror's validator had another; the two disagreed — this one kept an
    explicit `:443` and the validator's normalized it away — and neither folded
    a trailing slash, a trailing dot on the host, or a doubled path slash. Two
    normalizers that disagree let a record be accepted at the mirror on one
    reading and counted as N covered regions on the other. Read that module for
    the exact fold rules and for what they deliberately do not cover (no DNS, no
    percent-decoding).
    """
    return endpoint.identity if (endpoint := parse_endpoint(url)) is not None else None


def _jwt_claims(token_bytes: bytes, plane: str) -> dict[str, Any]:
    token = token_bytes.decode("ascii").strip()
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"{plane} attestation is not a three-part JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    if not isinstance(claims, dict):
        raise ValueError(f"{plane} attestation payload is not a JSON object")
    return claims


def _accepted(record: dict[str, Any], plural_key: str, scalar_key: str) -> list[str]:
    """The accepted SET, never the scalar.

    A rolling deploy legitimately serves the outgoing measurement while the
    record already names the incoming one — observed exactly that mid-roll on
    2026-08-15 — so a scalar comparison would report drift on every deploy and
    train the reader to ignore it.
    """
    values = record.get(plural_key) or [record.get(scalar_key)]
    return [value for value in values if isinstance(value, str) and value != NOT_CONFIGURED]


# ---------------------------------------------------------------------------
# GCP — Confidential Space
# ---------------------------------------------------------------------------


def live_gcp_container(url: str, transport: Transport) -> tuple[str, str, str]:
    """(image digest, image reference, issuer) from the running workload."""
    claims = _jwt_claims(transport.fetch(url).body, "GCP")
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer:
        raise ValueError("GCP attestation has no issuer")
    container = claims.get("submods", {}).get("container", {})
    digest = container.get("image_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError("GCP attestation has no container image digest")
    reference = container.get("image_reference")
    return digest, reference if isinstance(reference, str) else "", issuer


def check_gcp(control_plane: str, transport: Transport) -> Result:
    """Compare the running GCP workload against the published accepted set.

    This plane WAS already checked before this rewrite — see the refutation in
    the module docstring. What changed is that the endpoint now comes from the
    record instead of a constant, the issuer is compared against the record's
    own attestation_issuer, and image_reference is compared at all.
    """
    record = _published(control_plane, "/trust/gcp-release.json", transport)
    if record is None:
        return Result("gcp", ok=True, detail="no measurement published", skipped=True)
    accepted = _accepted(record, "accepted_image_digests", "image_digest")
    if not accepted:
        return Result("gcp", ok=True, detail="no measurement published", skipped=True)
    url = _attestation_url(record, GCP_ATTESTATION_URL)
    running, running_reference, issuer = live_gcp_container(url, transport)
    accepted_references = _accepted(record, "accepted_image_references", "image_reference")
    expected_issuer = record.get("attestation_issuer") or GCP_ATTESTATION_ISSUER

    problems: list[str] = []
    if running not in accepted:
        problems.append("running image digest is not in the published accepted set")
    if issuer != expected_issuer:
        problems.append(f"live attestation issuer {issuer} is not the published {expected_issuer}")
    if accepted_references and running_reference and running_reference not in accepted_references:
        # The digest is the measurement and the reference is only a name, but a
        # verifier told to expect one tag and handed another has no way to know
        # which of the two is stale. Both are published, so both must be true.
        problems.append("running image reference is not in the published accepted set")

    aliases = _alias_attestation_urls(record, url)
    extra = [
        f"endpoint:  {url}",
        f"running:   {running}",
        *(f"published: {value}" for value in accepted),
        f"reference: {running_reference or '(absent)'}",
        *(
            [
                "scope:     api_base_urls[] also publishes "
                + ", ".join(aliases)
                + " — NOT contacted by this run; these are alias hostnames for the same "
                "workload, and whether each resolves to it is deploy.yml's alias smoke, "
                "not measurement drift"
            ]
            if aliases
            else []
        ),
    ]
    if problems:
        return Result("gcp", ok=False, detail="; ".join(problems), endpoints=(url,), extra=extra)
    note = "" if running == record.get("image_digest") else " (rolling; matches accepted set)"
    return Result(
        "gcp",
        ok=True,
        detail=f"image digest matches{note}",
        endpoints=(url,),
        extra=extra,
    )


# ---------------------------------------------------------------------------
# AWS — Nitro Enclaves
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AwsSample:
    pcr0: str
    module_id: str
    binding: str  # "" when the certificate binding held; otherwise why it did not


def sample_aws_enclave(url: str, transport: Transport) -> AwsSample:
    """One attestation from whichever enclave the accelerator picked.

    Fails closed: anything unexpected in the document shape raises rather than
    returning a value that might silently be the wrong 48 bytes.
    """
    fetched = transport.fetch(url, verify_tls=False, want_peer_certificate=True)
    envelope = cbor2.loads(fetched.body)
    if not isinstance(envelope, list) or len(envelope) != 4:
        raise ValueError("AWS attestation is not a 4-element COSE_Sign1 envelope")
    document = cbor2.loads(envelope[2])
    if not isinstance(document, dict):
        raise ValueError("AWS attestation payload is not a map")
    if document.get("digest") != "SHA384":
        raise ValueError(f"unexpected PCR digest algorithm {document.get('digest')!r}")
    pcrs = document.get("pcrs")
    if not isinstance(pcrs, dict) or 0 not in pcrs:
        raise ValueError("AWS attestation has no PCR0")
    pcr0 = pcrs[0]
    if not isinstance(pcr0, bytes) or len(pcr0) != 48:
        raise ValueError(f"PCR0 is {len(pcr0) if isinstance(pcr0, bytes) else '?'} bytes, want 48")
    module_id = document.get("module_id")
    return AwsSample(
        pcr0.hex(),
        module_id if isinstance(module_id, str) else "(unnamed)",
        _aws_binding_problem(document, fetched.peer_certificate_der),
    )


def _aws_binding_problem(document: dict[Any, Any], peer_certificate_der: bytes | None) -> str:
    """Does the document bind the certificate this connection was served?

    Fails closed on absence. An attested endpoint whose document binds nothing
    is indistinguishable from a relay, and "unverifiable" must never be printed
    as "verified" — the same rule probes.py enforces for the live probes.
    """
    if peer_certificate_der is None:
        return "no peer certificate was captured, so the binding is unverifiable"
    user_data = document.get("user_data")
    if not isinstance(user_data, bytes):
        return "attestation carries no user_data, so it binds no certificate"
    if len(user_data) != AWS_USER_DATA_LENGTH:
        return f"user_data is {len(user_data)} bytes, want {AWS_USER_DATA_LENGTH}"
    expected = hashlib.sha256(peer_certificate_der).digest()
    if user_data[AWS_USER_DATA_CERT_SHA256] != expected:
        return "user_data[0:32] is not SHA-256 of the certificate served on this connection"
    return ""


def check_aws(control_plane: str, transport: Transport, samples: int) -> Result:
    """Sample the AWS fleet and compare every sample to the accepted set.

    Deliberately called sampling in the output. One fetch of an anycast name
    reaches one enclave, so the previous single-fetch version reported "ok" for
    a fleet it had observed one member of. Sampling narrows that; it does not
    close it (see the module docstring's measurement).
    """
    record = _published(control_plane, "/trust/aws-release.json", transport)
    if record is None:
        return Result("aws", ok=True, detail="no measurement published", skipped=True)
    accepted = _accepted(record, "accepted_pcr0s", "pcr0")
    if not accepted:
        return Result("aws", ok=True, detail="no measurement published", skipped=True)
    url = _attestation_url(record, AWS_ATTESTATION_URL)

    observed = [sample_aws_enclave(url, transport) for _ in range(max(1, samples))]
    module_ids = sorted({sample.module_id for sample in observed})
    drifted = sorted({sample.pcr0 for sample in observed if sample.pcr0 not in accepted})
    unbound = sorted({sample.binding for sample in observed if sample.binding})

    extra = [
        f"endpoint:  {url}",
        f"sampled:   {len(observed)} fetch(es) reached {len(module_ids)} distinct enclave(s): "
        + ", ".join(module_ids),
        "note:      this is SAMPLING, not enumeration — an enclave the accelerator "
        "never routed us to was not checked",
        *(f"running:   {sample_pcr0}" for sample_pcr0 in sorted({s.pcr0 for s in observed})),
        *(f"published: {value}" for value in accepted),
    ]
    published_module_id = record.get("observed_module_id")
    if isinstance(published_module_id, str) and published_module_id not in module_ids:
        # Not drift: the record names the enclave that was live when the
        # measurement was captured, and the fleet has more than one member.
        # Worth printing, because it is the cheapest evidence that the sample
        # above is a subset.
        extra.append(
            f"note:      record's observed_module_id {published_module_id} was not among "
            "the sampled enclaves, which is expected for a fleet and is further "
            "evidence this run saw a subset"
        )

    problems: list[str] = []
    if drifted:
        problems.append(f"{len(drifted)} sampled PCR0(s) are not in the published accepted set")
    if unbound:
        problems.append("; ".join(unbound))
    if problems:
        return Result("aws", ok=False, detail="; ".join(problems), endpoints=(url,), extra=extra)
    return Result(
        "aws",
        ok=True,
        detail=f"every sampled PCR0 matches, certificate binding holds on {len(observed)} sample(s)",
        endpoints=(url,),
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Azure — Confidential Containers on SEV-SNP
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureRegion:
    url: str
    hostdata: str  # "" when the record's entry named none
    issuer: str  # "" when the record's entry named none


@dataclass(frozen=True)
class AzurePlan:
    """What the record SAYS this plane is, before anything has been contacted.

    Kept separate from what was observed so the two can be compared rather than
    conflated. The defect this whole file exists to remove was a count of
    RECORD ENTRIES being printed as if it were a count of endpoints reached.
    """

    regions: tuple[AzureRegion, ...]
    issuers: tuple[str, ...]
    enumerated: bool  # True when regions[] supplied the endpoints, not the fallback
    defects: tuple[str, ...]  # ways the record's own account cannot ground coverage


def azure_plan(record: dict[str, Any]) -> AzurePlan:
    """Read the record's own account of where this plane serves from.

    The record is the authority on how many places this plane serves from, and
    it carries exactly TWO census signals:

      * regions[] — the endpoints themselves. Authoritative when present.
      * attestation_issuers — one MAA instance is provisioned per serving
        region, and issuers do not roll the way hostdata does.

    accepted_hostdata is deliberately NOT a third. It rolls when a CCE policy
    changes, so a two-value accepted set is a two-region plane or a one-region
    plane mid-roll and the record does not say which. check_azure uses it for
    the one thing it can support: a record that gives no endpoint array at all
    while naming more than one accepted value has left this run unable to tell
    those two cases apart, which is a hole in coverage rather than a pass.

    api_base_url is not a census either — it names one endpoint, not a count —
    but it IS the address the record hands a verifier, so it is contacted
    whether or not regions[] mentions it. It used to be read only when regions[]
    was empty, which meant publishing a region array could leave the plane's own
    advertised gateway unchecked while the run printed the full success
    sentence. A record whose regions[] omits it is contradicting itself about
    where it serves, so that is a defect as well as an extra fetch.

    Every endpoint returned here is a distinct place: entries are folded on
    endpoint identity, an entry that names no reachable place is dropped with a
    defect rather than fetched, and the advertised endpoint is added only when
    no entry already named it. check_azure counts coverage over what answered,
    and it can only do that honestly if this list holds no twins.
    """
    issuers = tuple(
        value for value in record.get("attestation_issuers", []) if isinstance(value, str) and value
    )
    raw = record.get("regions")
    entries = [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []
    defects: list[str] = []
    regions: list[AzureRegion] = []
    seen: set[Identity] = set()
    for entry in entries:
        url = entry.get("attestation_url")
        if not isinstance(url, str) or not url:
            defects.append("a regions[] entry names no attestation_url, so it cannot be contacted")
            continue
        identity = _endpoint_identity(url)
        if identity is None:
            # Not fetched, and not counted. A string that names no reachable
            # place — a malformed authority, a non-http scheme, or the
            # `https://a@b/` userinfo trick, which reads as host a and contacts
            # host b — is a coverage claim this run cannot honour, so it is a
            # defect rather than an endpoint.
            defects.append(
                f"a regions[] entry names {url!r}, which is not a plain http(s) endpoint, "
                "so it cannot be contacted"
            )
            continue
        if identity in seen:
            # Counting entries rather than distinct endpoints is how a success
            # line claims two regions on the strength of one fetch. Compared on
            # endpoint identity, so two entries differing only by fragment or
            # query cannot buy a second region either.
            defects.append(
                f"regions[] names the endpoint behind {url} more than once, so it counts more "
                "regions than it has places to contact"
            )
            continue
        seen.add(identity)
        hostdata = entry.get("hostdata")
        issuer = entry.get("attestation_issuer")
        if not isinstance(hostdata, str) or not hostdata:
            defects.append(
                f"regions[] entry for {url} names no hostdata, so what it serves could only be "
                "compared against the union of every region's accepted values"
            )
            hostdata = ""
        if not isinstance(issuer, str) or not issuer:
            defects.append(
                f"regions[] entry for {url} names no attestation_issuer, so the issuer it "
                "presents cannot be attributed to it"
            )
            issuer = ""
        regions.append(AzureRegion(url, hostdata, issuer))
    enumerated = bool(regions)
    if not enumerated:
        # A single-region record — or a mirror that dropped the array — still
        # gets checked at its canonical endpoint. The coverage rules in
        # check_azure are what say out loud when that is less than the plane.
        regions = [AzureRegion(_attestation_url(record, AZURE_ATTESTATION_URL), "", "")]
    else:
        # The record's OWN api_base_url is where it tells a verifier to go, and
        # regions[] is where it says it serves from. When they disagree, the
        # endpoint an actual reader would contact is the one nothing checks:
        # regions[] used to be an ELSE for api_base_url, so a record enumerating
        # only Southeast Asia left the UAE North gateway it advertises
        # uncontacted while the run printed the full success sentence. It is
        # contacted now, and the disagreement is itself a defect — a record that
        # advertises an endpoint its region census omits cannot ground a count.
        advertised = _attestation_url(record, "")
        advertised_identity = _endpoint_identity(advertised)
        if advertised_identity is not None and advertised_identity not in seen:
            defects.append(
                f"the record's api_base_url advertises {advertised} to verifiers but regions[] "
                "does not name it, so the record disagrees with itself about where it serves"
            )
            regions.append(AzureRegion(advertised, "", ""))
    return AzurePlan(tuple(regions), issuers, enumerated, tuple(defects))


def live_azure_hostdata(url: str, transport: Transport) -> tuple[str, str]:
    """(hostdata, MAA issuer) from one running confidential container.

    The token's signature is not checked here; see the module docstring.
    """
    claims = _jwt_claims(transport.fetch(url).body, "Azure")
    if claims.get("x-ms-attestation-type") != "sevsnpvm":
        raise ValueError(f"unexpected attestation type {claims.get('x-ms-attestation-type')!r}")
    hostdata = claims.get("x-ms-sevsnpvm-hostdata")
    issuer = claims.get("iss")
    if not isinstance(hostdata, str) or len(hostdata) != 64:
        raise ValueError("Azure attestation has no 32-byte hostdata")
    if not isinstance(issuer, str) or not issuer:
        raise ValueError("Azure attestation has no issuer")
    return hostdata, issuer


def check_azure(control_plane: str, transport: Transport) -> Result:
    """Contact every region the record enumerates, and count what was reached.

    Southeast Asia runs a different CCE policy from UAE North, so it has a
    permanently different hostdata. The version before this file was rewritten
    compared UAE North's hostdata against the union of both and printed success
    — a check that would have stayed green through any drift in the region it
    never contacted.

    COVERAGE IS COMPUTED FROM WHAT ANSWERED, NOT FROM WHAT THE RECORD LISTS.
    Every count printed below is over the DISTINCT endpoints actually fetched,
    and the issuer census is matched against the issuers those endpoints
    presented live — not against the issuer the record attributes to an entry,
    and never by list position. An earlier revision of this function did both:
    it printed len(regions[]) as an endpoint count (two entries naming one URL
    read as two regions covered) and, when no entry named an issuer, attributed
    non-coverage by slicing the issuer list, which could name as unreached the
    very issuer whose region HAD been contacted.

    Each of the following is a coverage gap. None of them can produce an "ok" or
    a zero exit; each is printed as a `gap:` line beside the verdict. The plane
    is marked [GAP] when they are the only thing wrong, and [DRIFT] when a
    measurement also mismatched — drift is the more serious verdict and takes
    the mark, while the gap lines and the unreached issuers are still reported
    under it. Stated this way because the shorter version of this sentence
    ("a [GAP] is raised when any of these hold") was false for exactly that case.
      1. a published MAA issuer was presented by none of the endpoints reached;
      2. the record publishes no attestation_issuers at all, so nothing in it
         says how many regions this plane serves from;
      3. the record publishes no usable regions[] array while naming more than
         one accepted hostdata value, so this run cannot tell a second region
         from a bind window;
      4. the regions[] array itself cannot ground a count — duplicate URLs, an
         entry with no URL or one that names no reachable place, or an entry
         naming no hostdata or issuer to attribute what it serves;
      5. the record's own api_base_url resolves to an attestation endpoint that
         regions[] does not name, so the record disagrees with itself about
         where this plane serves from. That endpoint IS contacted anyway — see
         azure_plan — because it is where a verifier reading the record is told
         to go; the gap is that the region census does not account for it.
    Rules 2 and 3 exist because coverage used to hang entirely on the optional
    attestation_issuers field: dropping it from the record turned off both the
    gap detection and the live-issuer check, and a two-region plane passed
    --strict having contacted one region.
    """
    record = _published(control_plane, "/trust/azure-release.json", transport)
    if record is None:
        return Result("azure", ok=True, detail="no measurement published", skipped=True)
    accepted = _accepted(record, "accepted_hostdata", "hostdata")
    if not accepted:
        return Result("azure", ok=True, detail="no measurement published", skipped=True)
    plan = azure_plan(record)

    problems: list[str] = []
    extra: list[str] = []
    contacted: list[str] = []
    live_issuers: list[str] = []
    for region in plan.regions:
        running, issuer = live_azure_hostdata(region.url, transport)
        # Appended after the fetch returned, so this is a list of endpoints that
        # ANSWERED. azure_plan guarantees the URLs are distinct — a record
        # naming one twice loses the duplicate there and gains a defect — so
        # len(contacted) is an endpoint count and never an entry count.
        contacted.append(region.url)
        if issuer not in live_issuers:
            live_issuers.append(issuer)
        extra.append(f"endpoint:  {region.url}")
        extra.append(f"  running:   {running}")
        extra.append(f"  issuer:    {issuer}")
        if running not in accepted:
            problems.append(f"{region.url} serves hostdata that is not in the published set")
            extra.append("  published: " + ", ".join(accepted))
        elif region.hostdata and running != region.hostdata:
            # Set membership alone would pass a region serving its NEIGHBOUR's
            # policy, which is a real misconfiguration and not a bind window.
            problems.append(f"{region.url} serves hostdata the record attributes to another region")
            extra.append(f"  expected:  {region.hostdata}")
        if plan.issuers and issuer not in plan.issuers:
            # A region we serve from but never listed. A verifier following our
            # record would reject a token that is in fact genuine.
            problems.append(f"live MAA issuer {issuer} is not in the published issuer list")
        elif region.issuer and issuer != region.issuer:
            problems.append(f"{region.url} is signed by {issuer}, not the record's {region.issuer}")

    # Attribution by evidence: an issuer counts as reached only if an endpoint
    # this run actually contacted presented it.
    unreached = tuple(issuer for issuer in plan.issuers if issuer not in live_issuers)
    gaps = [
        f"published MAA issuer {issuer} was presented by none of the "
        f"{len(contacted)} endpoint(s) this run contacted"
        for issuer in unreached
    ]
    if not plan.issuers:
        gaps.append(
            "the record publishes no attestation_issuers, so nothing in it says how many "
            "regions this plane serves from and this run cannot know what it did not reach"
        )
    if not plan.enumerated and len(accepted) > 1:
        gaps.append(
            f"the record publishes no regions[] array, so its {len(accepted)} accepted hostdata "
            f"values share the one endpoint this run could derive from it — a second serving "
            "region and a bind window are indistinguishable from here"
        )
    gaps.extend(plan.defects)

    endpoints = tuple(contacted)
    reached = (
        f"{len(contacted)} endpoint(s) contacted, "
        f"{len(live_issuers)} distinct MAA issuer(s) presented"
    )
    if problems:
        return Result(
            "azure",
            ok=False,
            detail="; ".join(problems),
            endpoints=endpoints,
            unreached=unreached,
            extra=[*extra, *(f"gap:       {gap}" for gap in gaps)],
        )
    if gaps:
        return Result(
            "azure",
            ok=False,
            gap=True,
            detail=f"{len(gaps)} coverage gap(s) over {reached}: " + "; ".join(gaps),
            endpoints=endpoints,
            unreached=unreached,
            extra=[*extra, *(f"gap:       {gap}" for gap in gaps)],
        )
    return Result(
        "azure",
        ok=True,
        # Counted from the fetch list and the live tokens, so this sentence
        # cannot describe more of the plane than answered.
        detail=(
            f"hostdata and issuer match at every one of {reached}, "
            f"covering all {len(plan.issuers)} published MAA issuer(s)"
        ),
        endpoints=endpoints,
        extra=extra,
    )


# ---------------------------------------------------------------------------


REMEDIATION = (
    "Republish from the plane that drifted — in quill-cloud-proxy run\n"
    "`python3 tools/capture-plane-measurements.py --write` and commit\n"
    "trust-page/, adding --keep-accepted if a roll is still in progress. The\n"
    "control plane mirrors those records, so there is nothing to change here."
)
GAP_REMEDIATION = (
    "A [GAP] is not drift: every measurement that was compared matched. It means\n"
    "this run could not establish that it covered the whole plane — a published\n"
    "MAA issuer was presented by none of the endpoints reached, the record\n"
    "carries no census to check coverage against, or the record disagrees with\n"
    "itself about where it serves. The gap lines printed above say which.\n"
    "Fix it by publishing every serving region in the record's `regions` array,\n"
    "each with its own attestation_url, hostdata and attestation_issuer, and by\n"
    "keeping api_base_url among them: quill-cloud-proxy's\n"
    "tools/capture-plane-measurements.py writes it, quill-router's\n"
    "trusted_router.services.trust_release.validated_azure_metadata validates and\n"
    "carries it, and trusted_router.trust.azure_release publishes it."
)


def run_checks(
    control_plane: str, transport: Transport, *, aws_samples: int = DEFAULT_AWS_SAMPLES
) -> list[Result]:
    checks = (
        ("gcp", lambda: check_gcp(control_plane, transport)),
        ("aws", lambda: check_aws(control_plane, transport, aws_samples)),
        ("azure", lambda: check_azure(control_plane, transport)),
    )
    results: list[Result] = []
    for name, check in checks:
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001 - any failure is a failed check
            results.append(Result(name, ok=False, detail=f"check failed: {exc}"))
    return results


def summary_lines(results: Sequence[Result], *, strict: bool) -> list[str]:
    """What was checked and what was not, in the same breath as the verdict.

    The line this replaces read "Every published measurement matches a live
    attestation." after contacting one of the two Azure regions the record
    itself enumerated. Any success sentence that does not enumerate its own
    coverage can say that again the next time coverage narrows.
    """
    checked = [result for result in results if not result.skipped]
    skipped = [result for result in results if result.skipped]
    endpoints = sum(len(result.endpoints) for result in results)
    lines = [
        f"Checked {len(checked)} plane(s) at {endpoints} endpoint(s): "
        + (", ".join(f"{result.plane} ({len(result.endpoints)})" for result in checked) or "none"),
        "Skipped (nothing published): " + (", ".join(result.plane for result in skipped) or "none"),
    ]
    unreached = [(result.plane, name) for result in results for name in result.unreached]
    if unreached:
        lines.append("Not reached: " + ", ".join(f"{plane} {name}" for plane, name in unreached))
    if skipped and strict:
        lines.append(
            "--strict: a plane that publishes no measurement is a failure here, because "
            "on a schedule 'we stopped publishing' and 'everything matches' must not be "
            "the same green tick."
        )
    return lines


def main(argv: Sequence[str] | None = None, transport: Transport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane", default=DEFAULT_CONTROL_PLANE)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat a plane that publishes no measurement as a failure (used by CI)",
    )
    parser.add_argument(
        "--aws-samples",
        type=int,
        default=DEFAULT_AWS_SAMPLES,
        help="attestations to fetch from the AWS accelerator (sampling, not enumeration)",
    )
    args = parser.parse_args(argv)

    results = run_checks(
        args.control_plane, transport or NetworkTransport(), aws_samples=args.aws_samples
    )

    for result in results:
        print(f"[{result.mark}] {result.plane}: {result.detail}")
        for line in result.extra:
            print(f"       {line}")

    print()
    for line in summary_lines(results, strict=args.strict):
        print(line)

    drifted = [result for result in results if not result.ok and not result.gap]
    gaps = [result for result in results if result.gap]
    skipped = [result for result in results if result.skipped]
    if drifted:
        print(
            f"\n{len(drifted)} plane(s) publish a measurement that is not what is running.\n"
            + REMEDIATION,
            file=sys.stderr,
        )
    if gaps:
        print(
            f"\n{len(gaps)} plane(s) were only partly covered.\n" + GAP_REMEDIATION,
            file=sys.stderr,
        )
    if skipped and args.strict:
        print(
            f"\n{len(skipped)} plane(s) publish no measurement at all, and --strict is on.\n"
            "A measurement that silently stopped being published looks exactly like a\n"
            "plane that never had one; on a schedule that is the failure being hunted.",
            file=sys.stderr,
        )
    if drifted or gaps or (args.strict and skipped):
        return 1
    if len(skipped) == len(results):
        # Found by running this against https://aws.trustedrouter.com, which
        # publishes no measurement for any plane: the success sentence printed
        # under a summary reading "Checked 0 plane(s)". Vacuously true and
        # exactly the reading this rewrite exists to stop — a line saying
        # everything matched, above nothing.
        print("\nNothing was checked: no plane at this control plane publishes a measurement.")
        return 0
    print("\nEvery published measurement listed above matches a live attestation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
