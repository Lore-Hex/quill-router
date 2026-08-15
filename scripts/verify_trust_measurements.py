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
    uv run python scripts/verify_trust_measurements.py --control-plane https://…

Exit status is 0 only if every configured measurement matches what is running.
Unconfigured planes are reported and do NOT fail the run; drift does.

The canonical, cryptographically complete verifier is quill-cloud-proxy's
tools/verify-attestation.py, which checks signatures and certificate chains.
This script deliberately does less: it answers "is what we publish what is
running", which is the question a signature check does not ask.
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import cbor2

AWS_ATTESTATION_URL = "https://api-aws.trustedrouter.com/attestation"
AZURE_ATTESTATION_URL = "https://api-azure.trustedrouter.com/attestation"
DEFAULT_CONTROL_PLANE = "https://trustedrouter.com"
TIMEOUT_SECONDS = 25
NOT_CONFIGURED = "not-configured"


@dataclass
class Result:
    plane: str
    ok: bool
    detail: str
    skipped: bool = False
    extra: list[str] = field(default_factory=list)


def _fetch(url: str, *, verify_tls: bool = True) -> bytes:
    context: ssl.SSLContext | None = None
    if not verify_tls:
        # The AWS enclave serves a certificate it generated itself, so there is
        # no chain to validate — that is the design, not a defect. The binding
        # that replaces chain validation lives in the attestation's user_data.
        # This script does not check that binding, so treat what it returns as
        # an unauthenticated read: good enough to detect our own drift, not
        # good enough to authenticate the enclave. Use verify-attestation.py
        # for the latter.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if not url.startswith(("https://", "http://127.0.0.1:")):
        # Scheme is fixed by construction; assert it anyway so a future
        # caller cannot turn this into a file:// read.
        raise ValueError(f"refusing to fetch non-HTTP(S) URL {url!r}")
    request = urllib.request.Request(url, headers={"accept": "*/*"})  # noqa: S310
    with urllib.request.urlopen(  # noqa: S310 - scheme checked above
        request, timeout=TIMEOUT_SECONDS, context=context
    ) as response:
        return response.read()


def live_aws_pcr0() -> str:
    """PCR0 from the running Nitro enclave.

    Fails closed: anything unexpected in the document shape raises rather than
    returning a value that might silently be the wrong 48 bytes.
    """
    envelope = cbor2.loads(_fetch(AWS_ATTESTATION_URL, verify_tls=False))
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
    return pcr0.hex()


def live_azure_hostdata() -> tuple[str, str]:
    """(hostdata, MAA issuer) from the running confidential container.

    The token's signature is not checked here; see the module docstring.
    """
    token = _fetch(AZURE_ATTESTATION_URL).decode("ascii").strip()
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Azure attestation is not a three-part JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    if claims.get("x-ms-attestation-type") != "sevsnpvm":
        raise ValueError(f"unexpected attestation type {claims.get('x-ms-attestation-type')!r}")
    hostdata = claims.get("x-ms-sevsnpvm-hostdata")
    issuer = claims.get("iss")
    if not isinstance(hostdata, str) or len(hostdata) != 64:
        raise ValueError("Azure attestation has no 32-byte hostdata")
    if not isinstance(issuer, str) or not issuer:
        raise ValueError("Azure attestation has no issuer")
    return hostdata, issuer


def _published(control_plane: str, path: str) -> dict[str, Any] | None:
    """Published release record, or None when the plane reports unconfigured."""
    url = f"{control_plane.rstrip('/')}{path}"
    try:
        return json.loads(_fetch(url))
    except urllib.error.HTTPError as exc:  # noqa: UP041 - urllib error hierarchy
        if exc.code == 503:
            return None
        raise


def check_aws(control_plane: str) -> Result:
    record = _published(control_plane, "/trust/aws-release.json")
    if record is None:
        return Result("aws", ok=True, detail="no measurement published (503)", skipped=True)
    accepted = [value for value in record.get("accepted_pcr0s", []) if value != NOT_CONFIGURED]
    if not accepted:
        return Result("aws", ok=True, detail="no measurement published", skipped=True)
    running = live_aws_pcr0()
    if running not in accepted:
        return Result(
            "aws",
            ok=False,
            detail="running PCR0 is not in the published accepted set",
            extra=[f"running:   {running}", *(f"published: {value}" for value in accepted)],
        )
    note = "" if running == record.get("pcr0") else " (matches accepted set, not the primary)"
    return Result("aws", ok=True, detail=f"PCR0 matches{note}", extra=[f"running: {running}"])


def check_azure(control_plane: str) -> Result:
    record = _published(control_plane, "/trust/azure-release.json")
    if record is None:
        return Result("azure", ok=True, detail="no measurement published (503)", skipped=True)
    accepted = [value for value in record.get("accepted_hostdata", []) if value != NOT_CONFIGURED]
    if not accepted:
        return Result("azure", ok=True, detail="no measurement published", skipped=True)
    running, issuer = live_azure_hostdata()
    problems: list[str] = []
    if running not in accepted:
        problems.append("running hostdata is not in the published accepted set")
    issuers = record.get("attestation_issuers", [])
    if issuers and issuer not in issuers:
        # A region we serve from but never listed. A verifier following our
        # record would reject a token that is in fact genuine.
        problems.append(f"live MAA issuer {issuer} is not in the published issuer list")
    detail_extra = [
        f"running:   {running}",
        *(f"published: {value}" for value in accepted),
        f"issuer:    {issuer}",
    ]
    if problems:
        return Result("azure", ok=False, detail="; ".join(problems), extra=detail_extra)
    return Result("azure", ok=True, detail="hostdata and issuer match", extra=detail_extra)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane", default=DEFAULT_CONTROL_PLANE)
    args = parser.parse_args()

    results: list[Result] = []
    for name, check in (("aws", check_aws), ("azure", check_azure)):
        try:
            results.append(check(args.control_plane))
        except Exception as exc:  # noqa: BLE001 - any failure is a failed check
            results.append(Result(name, ok=False, detail=f"check failed: {exc}"))

    for result in results:
        mark = "SKIP" if result.skipped else ("ok" if result.ok else "DRIFT")
        print(f"[{mark}] {result.plane}: {result.detail}")
        for line in result.extra:
            print(f"       {line}")

    failed = [result for result in results if not result.ok]
    if failed:
        print(
            f"\n{len(failed)} plane(s) publish a measurement that is not what is running.\n"
            "Update the TR_TRUST_* values in scripts/deploy/rollout.sh and redeploy, or\n"
            "widen the accepted set if a bind window is in progress.",
            file=sys.stderr,
        )
        return 1
    print("\nEvery published measurement matches a live attestation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
