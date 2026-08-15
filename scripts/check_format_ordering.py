#!/usr/bin/env python3
"""Refuse to deploy a control plane that writes an envelope format its enclaves cannot read.

    uv run python -m scripts.check_format_ordering
    uv run python -m scripts.check_format_ordering --cloud gcp

THE INVARIANT
-------------
A control plane may deploy only a build whose set of WRITTEN BYOK envelope
formats is a subset of the formats accepted by EVERY enclave serving that cloud.

Violate it and the damage is immediate and customer-visible: the control plane
seals a provider key under an algorithm string the enclave rejects, and that
customer's BYOK key stops working at the next inference request. Not degraded,
not slower — `unsupported envelope algorithm` from byokcache, on the prompt path,
for exactly the customers who took the trouble to bring their own keys.

WHY THIS EXISTS AS A GATE AND NOT AS A PARAGRAPH
------------------------------------------------
docs/design/byok-aad-v2-migration.md §4.0 states the ordering constraint and
then says the dangerous part out loud: the step-2 change reaches AWS and Azure
"as an ordinary version bump rather than as a deliberate migration step. Nobody
has to decide to run it." A rule whose enforcement is "somebody remembers §4.0
before deploying" is enforced by nothing.

It was in fact enforced by nothing. On AWS and Azure the control planes took the
v2-writing build with no check that their enclaves could read v2. That was
harmless — the migration record shows both audits found zero BYOK and zero
Broadcast secret rows, so there were no envelopes to break — but it was harmless
by accident of those databases being empty, not by any property of the deploy.
Each cloud is a standalone TrustedRouter with its own database
(docs/storage-portability/multi-cloud-separation.md), so the next cloud to grow
a BYOK row inherits the same unguarded sequence.

WHAT IT ACTUALLY CHECKS, per cloud and per serving region
---------------------------------------------------------
1. the running measurement, from a live attestation, is in the published
   accepted set;
2. that measurement is the one the record maps to a commit, and that
   `source_commit` is present and is a git object id;
3. the formats the enclave built from that commit ACCEPTS, parsed out of the
   switch in `envelopeAAD` in enclave-go/internal/byokcache/cache.go at that
   commit;
4. the formats this working tree's src/trusted_router/byok_crypto.py WRITES,
   parsed from the `algorithm=` argument of every EncryptedSecretEnvelope it
   constructs — derived, so a future V3 is covered without editing this file;
5. written ⊆ accepted.

Every step fails closed. A parse it does not recognise, a record it cannot map
to a commit, a region it cannot reach: all of those are failures, never
"assume the superset". Assuming the superset is the same as not running.

WHAT THIS DOES NOT ESTABLISH
----------------------------
* It does not verify any signature. The attestation is read for its measurement
  only, exactly as scripts/verify_trust_measurements.py reads it, and it shares
  that script's parsers so the two cannot drift. The cryptographically complete
  verifier is quill-cloud-proxy's tools/verify-attestation.py.
* It does not prove `source_commit` is the commit that built the running
  enclave. Neither PCR0 nor hostdata carries a commit; the field is an assertion
  by whoever ran the release. This check inherits that assertion's strength, and
  its whole value is that a WRONG assertion is now falsifiable (verify-pcr0.sh
  rebuilds PCR0 from a commit) where an ABSENT one was not.
* It does not cover a bind window inside a single region. Two enclave builds
  behind one hostname answer one at a time, and the record names one commit for
  both. The check refuses in that case rather than guessing — see
  `_BIND_WINDOW` below — but "refuses" is not "verified both".
* It says nothing about envelopes already at rest. Reads are dispatched on the
  stored algorithm, so this is about what a deploy starts WRITING.
* Coverage is only as wide as the record it reads. Regions come from the
  record's `regions` array, and the control-plane mirror dropped that array
  until the fix in services/trust_release.py that ships beside this file — so
  against a control plane predating that fix, Azure is checked in one region of
  two and this script cannot tell the difference. It reports what it checked;
  compare the printed region list against the record before trusting a green
  result on a plane you have not confirmed is serving the fix.

Findings recorded while building this, both verified against the LIVE published
records rather than fixtures, and both fixed here:
  * the mirror read source_commit from the SERVING deployment's own settings
    instead of from the record it was mirroring, so trustedrouter.com reported
    `not-configured` for AWS and Azure no matter what quill-cloud-proxy
    published. Publishing the field upstream alone would have changed nothing.
  * the mirror dropped `regions`, making api-azure-sea.trustedrouter.com
    undiscoverable from the surface published as the place to verify us.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.verify_trust_measurements import (
    NOT_CONFIGURED,
    hostdata_from_maa_token,
    image_digest_from_confidential_space_token,
    pcr0_from_nitro_attestation,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BYOK_CRYPTO = REPO_ROOT / "src" / "trusted_router" / "byok_crypto.py"

DEFAULT_CONTROL_PLANE = "https://trustedrouter.com"
TIMEOUT_SECONDS = 25

# The enclave source of truth for what a build accepts. Fetched at a commit
# rather than read from a checkout, because the question is what the RUNNING
# build accepts and that build is whatever commit the record names — not
# whatever quill-cloud-proxy's main happens to be today.
ENCLAVE_REPO = "Lore-Hex/quill-cloud-proxy"
ENCLAVE_SOURCE_PATH = "enclave-go/internal/byokcache/cache.go"

# The constructor whose `algorithm=` argument decides the format written. Every
# envelope this control plane persists is built here; grepping for the constant
# names instead would miss a new one, which is the failure mode that matters.
ENVELOPE_TYPE = "EncryptedSecretEnvelope"

_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")

_BIND_WINDOW = (
    "a bind window publishes two measurements and one source_commit, so the "
    "outgoing build's accepted formats are unknown. Finish the enclave roll and "
    "narrow the accepted set before deploying the control plane -- that ordering "
    "is the whole of byok-aad-v2-migration.md section 4.0."
)


@dataclass(frozen=True)
class PlaneSpec:
    """How one cloud's release record names the thing the enclave measures."""

    cloud: str
    record_path: str
    accepted_key: str
    measurement_key: str
    measurement_label: str
    # AWS serves a certificate the enclave generated itself, so there is no
    # chain to validate. Same reasoning as verify_trust_measurements._fetch:
    # this is an unauthenticated read, adequate to learn what is running and
    # not adequate to authenticate the enclave.
    verify_tls: bool = True


PLANES: dict[str, PlaneSpec] = {
    "gcp": PlaneSpec(
        cloud="gcp",
        record_path="/trust/gcp-release.json",
        accepted_key="accepted_image_digests",
        measurement_key="image_digest",
        measurement_label="image digest",
    ),
    "aws": PlaneSpec(
        cloud="aws",
        record_path="/trust/aws-release.json",
        accepted_key="accepted_pcr0s",
        measurement_key="pcr0",
        measurement_label="PCR0",
        verify_tls=False,
    ),
    "azure": PlaneSpec(
        cloud="azure",
        record_path="/trust/azure-release.json",
        accepted_key="accepted_hostdata",
        measurement_key="hostdata",
        measurement_label="hostdata",
    ),
}


@dataclass(frozen=True)
class Region:
    """One serving endpoint, as the published record describes it.

    Enumerated from the record's `regions` array and never hardcoded: Azure
    serves from UAE North and Southeast Asia today, each running its own CCE
    policy and therefore its own hostdata, and a fourth region added tomorrow
    must be checked without editing this file.
    """

    host: str
    attestation_url: str
    measurement: str
    issuer: str | None
    source_commit: str | None


@dataclass
class RegionResult:
    cloud: str
    host: str
    measurement: str = ""
    source_commit: str = ""
    accepts: frozenset[str] = frozenset()
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# --------------------------------------------------------------------------
# What the control plane writes
# --------------------------------------------------------------------------


def written_formats(source: str, *, origin: str = str(BYOK_CRYPTO)) -> frozenset[str]:
    """Envelope formats this build WRITES, derived from the source.

    Deliberately not a hardcoded {"…-V2"}. Hardcoding it would make this gate
    correct exactly once: the day a V3 write lands, the gate would keep
    asserting the V2 subset and pass a build that writes something no enclave
    has ever seen. Derived from the `algorithm=` argument of every
    EncryptedSecretEnvelope constructed in the module, it grows on its own.

    Fails closed on an `algorithm=` this cannot resolve to a string literal.
    An expression there is not evidence of safety; it is a build whose written
    format is unknown, and unknown is the case the enclave breaks on.
    """
    tree = ast.parse(source, filename=origin)

    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value

    written: set[str] = set()
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee(node.func) != ENVELOPE_TYPE:
            continue
        if node.args or any(keyword.arg is None for keyword in node.keywords):
            # Positional args or **kwargs hide which algorithm is written.
            unresolved.append(f"{ENVELOPE_TYPE}(...) at line {node.lineno} is not all keywords")
            continue
        algorithms = [keyword for keyword in node.keywords if keyword.arg == "algorithm"]
        if len(algorithms) != 1:
            unresolved.append(f"{ENVELOPE_TYPE}(...) at line {node.lineno} has no algorithm=")
            continue
        value = algorithms[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            written.add(value.value)
        elif isinstance(value, ast.Name) and value.id in constants:
            written.add(constants[value.id])
        else:
            unresolved.append(f"algorithm= at line {value.lineno} is not a string constant")

    if unresolved:
        raise ValueError(f"cannot derive written formats from {origin}: " + "; ".join(unresolved))
    if not written:
        raise ValueError(
            f"found no {ENVELOPE_TYPE}(algorithm=...) in {origin}. Either the constructor moved "
            "or this parser has rotted; both mean the written format is unknown."
        )
    return frozenset(written)


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


# --------------------------------------------------------------------------
# What the enclave accepts
# --------------------------------------------------------------------------

# Grouped `const (\n\tName = "…"\n)` is how cache.go declares these today, but a
# single-line `const Name = "…"` or a typed `const Name string = "…"` is the same
# declaration and must not read as an absent one — an unresolvable case name
# blocks the deploy, so a regex that is too narrow is an outage of its own.
_GO_STRING_CONST = re.compile(
    r'^\s*(?:(?:const|var)\s+)?([A-Za-z_]\w*)(?:\s+[A-Za-z_][\w.]*)?\s*=\s*"((?:[^"\\]|\\.)*)"',
    re.MULTILINE,
)
_GO_FUNC_START = re.compile(r"^func\s+envelopeAAD\s*\(([^)]*)\)", re.MULTILINE)
_GO_FUNC_END = re.compile(r"^\}", re.MULTILINE)
_GO_SWITCH = re.compile(r"\bswitch\s+([A-Za-z_]\w*)\s*\{")
_GO_CASE = re.compile(r"^\s*case\s+(.+?):\s*$", re.MULTILINE)


def accepted_formats(source: str, *, origin: str) -> frozenset[str]:
    """Envelope formats an enclave build ACCEPTS, parsed from cache.go.

    The authority is the switch in `envelopeAAD`, not the const block: cache.go
    can declare AlgorithmV2 while `envelopeAAD` still rejects it, and that
    combination is precisely the pre-step-1 enclave whose deploy this gate
    exists to order. Reading the constants alone would call that build V2-capable
    and wave through the deploy that breaks it.

    Every shape surprise raises. A default branch is not a case and is not
    counted; an unresolvable case expression is a failure, not a wildcard.
    """
    constants = {name: value for name, value in _GO_STRING_CONST.findall(source)}

    start = _GO_FUNC_START.search(source)
    if start is None:
        raise ValueError(f"{origin} has no `func envelopeAAD(` — refusing to guess what it accepts")
    parameters = {
        token.strip()
        for chunk in start.group(1).split(",")
        for token in (chunk.strip().split()[:1] or [""])
        if token.strip()
    }
    end = _GO_FUNC_END.search(source, start.end())
    if end is None:
        raise ValueError(f"{origin}: envelopeAAD has no closing brace at column 0")
    body = source[start.end() : end.start()]

    switches = _GO_SWITCH.findall(body)
    if len(switches) != 1:
        raise ValueError(
            f"{origin}: envelopeAAD dispatches through {len(switches)} plain switch statements, "
            "expected exactly 1. The accepted set is whatever that switch says, so anything else "
            "has to be read by a human rather than assumed."
        )
    if switches[0] not in parameters:
        raise ValueError(
            f"{origin}: envelopeAAD switches on {switches[0]!r}, which is not one of its "
            f"parameters {sorted(parameters)} — that is not the algorithm dispatch."
        )

    accepted: set[str] = set()
    for match in _GO_CASE.finditer(body):
        for token in match.group(1).split(","):
            token = token.strip()
            if token.startswith('"') and token.endswith('"') and len(token) >= 2:
                accepted.add(token[1:-1])
            elif token in constants:
                accepted.add(constants[token])
            else:
                raise ValueError(
                    f"{origin}: cannot resolve `case {token}` in envelopeAAD to a string. "
                    "Refusing to treat an unreadable case as accepted."
                )
    if not accepted:
        raise ValueError(f"{origin}: envelopeAAD's switch has no case clauses")
    return frozenset(accepted)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _fetch(url: str, *, verify_tls: bool = True, headers: dict[str, str] | None = None) -> bytes:
    context: ssl.SSLContext | None = None
    if not verify_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if not url.startswith("https://"):
        # Scheme is fixed by construction; assert it anyway so a record served
        # to us cannot turn this into a file:// read.
        raise ValueError(f"refusing to fetch non-HTTPS URL {url!r}")
    request = urllib.request.Request(url, headers={"accept": "*/*", **(headers or {})})  # noqa: S310
    with urllib.request.urlopen(  # noqa: S310 - scheme checked above
        request, timeout=TIMEOUT_SECONDS, context=context
    ) as response:
        return bytes(response.read())


def fetch_record(control_plane: str, path: str) -> dict[str, Any]:
    """Published release record. Absence is a failure, not a skip.

    scripts/verify_trust_measurements.py treats an unpublished record as SKIP,
    which is right for a drift report: it is answering "does what we publish
    match what runs", and there is nothing to compare. This is a deploy gate
    answering "may this build write v2 here", and no record means no evidence.
    """
    url = f"{control_plane.rstrip('/')}{path}"
    try:
        body = _fetch(url)
    except urllib.error.HTTPError as exc:  # noqa: UP041 - urllib error hierarchy
        raise ValueError(f"{url} returned HTTP {exc.code}; no published record to check") from exc
    record = json.loads(body)
    if not isinstance(record, dict):
        raise ValueError(f"{url} returned a {type(record).__name__}, not a release record")
    return record


def fetch_enclave_source(commit: str) -> str:
    """cache.go as it was at `commit`.

    GH_TOKEN is used when present only to raise the anonymous rate limit; the
    repository is public and nothing here needs authority.
    """
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError(f"{commit!r} is not a git object id")
    url = f"https://raw.githubusercontent.com/{ENCLAVE_REPO}/{commit}/{ENCLAVE_SOURCE_PATH}"
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"authorization": f"Bearer {token}"} if token else None
    try:
        return _fetch(url, headers=headers).decode("utf-8")
    except urllib.error.HTTPError as exc:  # noqa: UP041 - urllib error hierarchy
        raise ValueError(
            f"cannot read {ENCLAVE_SOURCE_PATH} at {commit} (HTTP {exc.code}). Without it the "
            "formats that enclave accepts are unknown, and unknown fails closed."
        ) from exc


def _attestation_url(record: dict[str, Any]) -> str:
    """Attestation endpoint for a record with no per-region array.

    Derived from api_base_url rather than listed here, so a plane that moves
    hostnames does not need this file edited to keep being checked.
    """
    base = record.get("api_base_url")
    if not isinstance(base, str) or not base.startswith("https://"):
        raise ValueError("record has no https api_base_url to derive an attestation URL from")
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}/attestation"


def regions_of(record: dict[str, Any], spec: PlaneSpec) -> list[Region]:
    """Every serving region the record describes.

    From the record's `regions` array when it has one. Azure does, because its
    regions genuinely differ — each runs its own CCE policy, so hostdata is a
    real set rather than a bind-window artifact. GCP and AWS publish a single
    plane and no array, which is one region, not zero.
    """
    entries = record.get("regions")
    record_commit = record.get("source_commit")
    if isinstance(entries, list) and entries:
        regions: list[Region] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"regions[{index}] is not an object")
            url = entry.get("attestation_url")
            measurement = entry.get(spec.measurement_key)
            if not isinstance(url, str) or not isinstance(measurement, str):
                raise ValueError(
                    f"regions[{index}] is missing attestation_url or {spec.measurement_key}"
                )
            issuer = entry.get("attestation_issuer")
            regions.append(
                Region(
                    host=urlsplit(url).netloc or url,
                    attestation_url=url,
                    measurement=measurement,
                    issuer=issuer if isinstance(issuer, str) else None,
                    # A per-region source_commit when the producer can attribute
                    # one; the record-wide value otherwise. capture-plane-
                    # measurements.py writes only the latter today.
                    source_commit=entry.get("source_commit") or record_commit,
                )
            )
        return regions

    measurement = record.get(spec.measurement_key)
    if not isinstance(measurement, str):
        raise ValueError(f"record has no {spec.measurement_key}")
    url = _attestation_url(record)
    return [
        Region(
            host=urlsplit(url).netloc,
            attestation_url=url,
            measurement=measurement,
            issuer=record.get("attestation_issuer"),
            source_commit=record_commit,
        )
    ]


def live_measurement(spec: PlaneSpec, payload: bytes) -> tuple[str, str | None]:
    """(measurement, issuer) from one region's live attestation.

    The parsers are imported from scripts/verify_trust_measurements.py rather
    than rewritten. Two fail-closed COSE parsers is one fail-closed COSE parser
    and one that quietly stopped being fail-closed.
    """
    if spec.cloud == "aws":
        return pcr0_from_nitro_attestation(payload), None
    if spec.cloud == "azure":
        hostdata, issuer = hostdata_from_maa_token(payload)
        return hostdata, issuer
    return image_digest_from_confidential_space_token(payload), None


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------

Attestor = Callable[[str, bool], bytes]
SourceReader = Callable[[str], str]


def check_plane(
    spec: PlaneSpec,
    record: dict[str, Any],
    written: frozenset[str],
    *,
    attest: Attestor,
    source: SourceReader,
    source_cache: dict[str, frozenset[str]] | None = None,
) -> list[RegionResult]:
    """One cloud, every region it publishes."""
    cache = source_cache if source_cache is not None else {}
    accepted_set = [
        value
        for value in record.get(spec.accepted_key) or []
        if isinstance(value, str) and value != NOT_CONFIGURED
    ]
    issuers = [value for value in record.get("attestation_issuers") or [] if isinstance(value, str)]

    results: list[RegionResult] = []
    for region in regions_of(record, spec):
        result = RegionResult(cloud=spec.cloud, host=region.host)
        results.append(result)

        if not accepted_set:
            result.problems.append(f"record publishes no {spec.accepted_key}")
            continue

        try:
            running, issuer = live_measurement(spec, attest(region.attestation_url, spec.verify_tls))
        except Exception as exc:  # noqa: BLE001 - any failure to read is a failure to clear
            result.problems.append(f"cannot read a live attestation: {exc}")
            continue
        result.measurement = running

        if running not in accepted_set:
            result.problems.append(
                f"running {spec.measurement_label} is not in the published accepted set "
                f"({', '.join(accepted_set)})"
            )
            continue

        # An accepted measurement that is not the one this record maps to a
        # commit. Which build is running is then unknown, so what it accepts is
        # unknown too.
        if running != region.measurement:
            result.problems.append(
                f"running {spec.measurement_label} {running} is accepted but the record maps "
                f"{region.measurement} to source_commit; {_BIND_WINDOW}"
            )
            continue

        # A region we are being served by that the record does not describe.
        # Which region answers a shared name is Traffic Manager's decision, not
        # ours, so this is how a live region absent from the record shows up.
        if issuer is not None:
            if region.issuer is not None and issuer != region.issuer:
                result.problems.append(
                    f"attestation came from {issuer}, but the record says this endpoint is "
                    f"{region.issuer} — a region is serving that this record does not describe"
                )
                continue
            if issuers and issuer not in issuers:
                result.problems.append(
                    f"live attestation issuer {issuer} is in no published issuer list "
                    "— a region is serving that this record does not describe"
                )
                continue

        commit = region.source_commit
        if not isinstance(commit, str) or not commit or commit == NOT_CONFIGURED:
            result.problems.append(
                "record carries no source_commit, so the running build cannot be mapped to "
                "source and its accepted formats are unknowable. In quill-cloud-proxy run "
                "`python3 tools/capture-plane-measurements.py --write` and commit trust-page/."
            )
            continue
        if not _COMMIT_RE.fullmatch(commit):
            result.problems.append(f"source_commit {commit!r} is not a git object id")
            continue
        result.source_commit = commit

        if commit not in cache:
            try:
                cache[commit] = accepted_formats(
                    source(commit), origin=f"{ENCLAVE_SOURCE_PATH}@{commit}"
                )
            except Exception as exc:  # noqa: BLE001 - unparsed is unknown is failed
                result.problems.append(str(exc))
                continue
        result.accepts = cache[commit]

        missing = sorted(written - result.accepts)
        if missing:
            result.problems.append(
                f"this build WRITES {', '.join(sorted(written))} but the enclave at {commit} "
                f"ACCEPTS only {', '.join(sorted(result.accepts))}. Deploying it breaks every "
                f"BYOK key in the {spec.cloud} database at the next inference request. "
                f"Unreadable: {', '.join(missing)}."
            )
    return results


def gather(
    control_plane: str,
    clouds: Sequence[str],
    written: frozenset[str],
    *,
    records: Callable[[str], dict[str, Any]],
    attest: Attestor,
    source: SourceReader,
) -> list[RegionResult]:
    cache: dict[str, frozenset[str]] = {}
    results: list[RegionResult] = []
    for cloud in clouds:
        spec = PLANES[cloud]
        try:
            record = records(spec.record_path)
        except Exception as exc:  # noqa: BLE001
            results.append(
                RegionResult(cloud=cloud, host="-", problems=[f"no usable release record: {exc}"])
            )
            continue
        try:
            results.extend(
                check_plane(spec, record, written, attest=attest, source=source, source_cache=cache)
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                RegionResult(cloud=cloud, host="-", problems=[f"record is unreadable: {exc}"])
            )
    return results


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

_SHORT = re.compile(r"^TR-BYOK-ENVELOPE-AES-256-GCM-(V\d+)$")


def _short(algorithm: str) -> str:
    match = _SHORT.match(algorithm)
    return match.group(1) if match else algorithm


def _shorts(algorithms: frozenset[str]) -> str:
    return ",".join(_short(value) for value in sorted(algorithms)) or "-"


def render(results: Sequence[RegionResult], written: frozenset[str]) -> str:
    rows = [("CLOUD", "REGION", "MEASUREMENT", "COMMIT", "ACCEPTS", "WRITES", "")]
    for result in results:
        rows.append(
            (
                result.cloud,
                result.host,
                (result.measurement[:19] + "…") if len(result.measurement) > 20 else "-",
                result.source_commit or "-",
                _shorts(result.accepts),
                _shorts(written),
                "ok" if result.ok else "BLOCKED",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    for result in results:
        for problem in result.problems:
            lines.append(f"\nBLOCKED {result.cloud}/{result.host}: {problem}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane", default=DEFAULT_CONTROL_PLANE)
    parser.add_argument(
        "--cloud",
        action="append",
        choices=sorted(PLANES),
        help=(
            "cloud to gate on; repeatable. Default: all of them. A deploy should pass its OWN "
            "cloud -- the databases are separate, so Azure's enclave has no say over a GCP "
            "rollout and blocking one on the other only teaches people to bypass the gate."
        ),
    )
    args = parser.parse_args(argv)
    clouds = args.cloud or sorted(PLANES)

    try:
        written = written_formats(BYOK_CRYPTO.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot determine what this build writes: {exc}", file=sys.stderr)
        return 1

    results = gather(
        args.control_plane,
        clouds,
        written,
        records=lambda path: fetch_record(args.control_plane, path),
        attest=lambda url, verify_tls: _fetch(url, verify_tls=verify_tls),
        source=fetch_enclave_source,
    )

    print(render(results, written))
    blocked = [result for result in results if not result.ok]
    if blocked:
        print(
            f"\n{len(blocked)} serving region(s) block this deploy. The control plane may only "
            "write envelope formats every enclave serving its cloud already reads; see "
            "docs/design/byok-aad-v2-migration.md section 4.0.",
            file=sys.stderr,
        )
        return 1
    print(f"\nEvery enclave serving {', '.join(clouds)} accepts every format this build writes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
