#!/usr/bin/env python3
"""Refuse a change that starts writing a BYOK envelope format an enclave cannot read.

    uv run python -m scripts.check_format_ordering
    uv run python -m scripts.check_format_ordering --cloud gcp

THE LAW
-------
A control plane may run only a build whose set of WRITTEN BYOK envelope formats
is a subset of the formats accepted by EVERY enclave serving that cloud.

Violate it and the damage is immediate and customer-visible: the control plane
seals a provider key under an algorithm string the enclave rejects, and that
customer's BYOK key stops working at the next inference request. Not degraded,
not slower -- `unsupported envelope algorithm` from byokcache, on the prompt
path, for exactly the customers who took the trouble to bring their own keys.

WHERE THIS RUNS: A PULL-REQUEST CHECK, NOT A DEPLOY GATE
--------------------------------------------------------
The `byok-format-ordering` job in .github/workflows/ci.yml runs this on pull
requests and on pushes to main. Nothing else invokes it: .github/workflows/
deploy.yml, scripts/deploy/aws_eu_control_plane.sh and
scripts/deploy/azure_control_plane.sh contain no reference to this file.

That placement is a deliberate reversal. An earlier version of this program was
a deploy gate -- a `needs:` job on deploy.yml plus an inline call in both of
those hand-run cloud scripts. Three insertion points on the most critical path
in the system, running on every deploy forever, to guard an event that happens
about once per envelope-format migration.

The cost of that placement is visible in deploy.yml itself: `deploy` lists
`build-image, migrate-schema, sync-runtime-secrets, confirm-current-main` in its
`needs:`, so anything that breaks in any of them stops control-plane deploys
until somebody fixes it. Adding another blocking dependency there widens that
surface for a check whose subject -- which envelope format the code writes -- is
not a property of the deploy at all. It is a property of a CODE CHANGE, and a
code change is visible in a diff. This catches it at authoring time instead,
where a red check blocks one pull request rather than every deploy on three
clouds.

SILENT UNLESS THE WRITTEN FORMAT SET CHANGES
--------------------------------------------
A check that reddens unrelated pull requests gets deleted, so this one has two
paths and takes the second only when the diff earns it:

  * the written format set equals `BASELINE_WRITTEN_FORMATS` -> print what was
    derived and exit 0. No record is fetched, no attestation endpoint is
    contacted, no file is read from the enclave repository. This path performs
    no network I/O at all.
  * the written format set differs from it -> verify against every cloud in
    PLANES and every region each of their published records describes, and fail
    closed: any region that blocks, and any cloud whose declaration cannot be
    resolved, exits 1.

There is no report-only mode. The reason the earlier version needed one was that
it was wired into deploy, where a refusal stopped releases on three clouds for
whoever happened to be deploying. Here a refusal stops one pull request, in
front of the author of the change that caused it, so the check can simply
enforce.

That distinction is load-bearing TODAY, not in theory. Run against the live
records on 2026-08-16, the loud path refused all three clouds, for two different
reasons and neither of them a format problem:

  * aws-release.json and azure-release.json publish
    `"source_commit": "not-configured"` -- the producer's sentinel for "no commit
    could be named", which is the NOT_CONFIGURED value below. There is no commit
    at which to read an accepted-formats declaration.
  * gcp-release.json publishes `source_commit: f57b791`, and
    enclave-go/internal/byokcache/accepted_formats.json is a 404 at that commit.
    That build predates the generated declaration; the generator is
    quill-cloud-proxy#177 and it is not merged.

Refusing is the right answer to "may we start writing a new format on those
clouds". It is the wrong answer to "may this unrelated pull request merge", and
an enforcing check on that path alone would be red on every pull request. Only
the silent path answers the second question, and it is the one that runs.

WHY A PINNED BASELINE RATHER THAN A MERGE-BASE DIFF
---------------------------------------------------
Two implementations were considered.

(a) Derive the written formats on the pull-request head AND on the merge base,
    and enforce only when they differ. It is the most faithful statement of
    "this change alters the written set". It is workable -- `git worktree add`
    the merge base and run the derivation there in a subprocess, since the
    derivation is behavioural (it imports the package and records envelope
    construction) and cannot be done twice in one interpreter against two
    trees. Its costs are a full-history fetch and a second dependency install on
    every pull request, and one sharper problem: `derive_written_formats`
    refuses for properties of the tree it is given, including an entry point
    with no probe and an `algorithm` attribute assigned after construction. Run
    against the BASE tree, those refusals redden a pull request whose own
    change is fine and whose author cannot fix the base.

(b) Pin the format set known to be readable by every cloud today in
    `BASELINE_WRITTEN_FORMATS`, and enforce only when the derived set differs
    from it. Deterministic, no git plumbing, and changing the constant is itself
    a reviewable diff arriving at exactly the moment a human should look at one.

This file implements (b). Its cost is a hand-maintained constant, which is the
defect class this repository keeps getting bitten by, so the constant is not
trusted to be right: tests/test_check_format_ordering.py::
test_the_baseline_matches_what_this_tree_writes runs the real derivation
against the real tree and asserts the two are equal. A baseline that drifts from
what the tree writes fails CI on the commit that drifts it. Without that test
this design is unsound, and its absence is a defect.

WHAT IT ACTUALLY CHECKS ON THE LOUD PATH, per cloud and per serving region
--------------------------------------------------------------------------
1. the running measurement, from a live attestation, is in the published
   accepted set;
2. every measurement in that accepted set was observed live by this run. An
   accepted measurement nobody served us is a build this run never checked, and
   the change is refused rather than described as covered;
3. the running measurement is the one the record maps to a commit, and that
   `source_commit` is present and is a git object id;
4. the formats the enclave built from that commit ACCEPTS, read from
   enclave-go/internal/byokcache/accepted_formats.json at that commit -- a
   declaration GENERATED by a Go test that seals an envelope per format and
   requires (*Cache).Resolve to return the plaintext;
5. the formats this working tree WRITES -- see the next section, which is the
   part that has been wrong twice;
6. written ⊆ accepted.

Every step fails closed. A declaration it cannot read or cannot bind to the
source at that commit, a record it cannot map to a commit, a region it cannot
reach, an accepted measurement it never saw: all of those are failures, never
"assume the superset". Assuming the superset is the same as not running.

HOW "WRITES" IS DERIVED: two derivations, and their union
----------------------------------------------------------
The first version read the `algorithm=` argument of calls spelled exactly
`EncryptedSecretEnvelope` in one hardcoded file. An adversarial review walked
past it five ways. The second version scanned every module and closed the name
set over aliases and subclasses per module; a second review walked past THAT
with an annotated alias, an alias imported from a sibling module, and a subclass
defined in a sibling module. Both times the answer was still syntax, and syntax
loses to the next spelling nobody thought of.

So the primary derivation is BEHAVIOURAL, the same trick the enclave side uses
for `accepted_formats.json`:

  (a) BEHAVIOURAL -- `probe_write_entry_points()` imports this tree, installs a
      recorder on EncryptedSecretEnvelope's constructor, and CALLS every write
      entry point for real against a local test key wrapper. The formats it
      reports are the `.algorithm` values read off the envelope objects those
      calls actually produced, after the call returned. Spelling is irrelevant
      to it: an alias, a subclass, a cross-module factory, `dataclasses.replace`
      and an assignment to `.algorithm` after construction all end up as a value
      on an object it holds. The recorder is proved able to see a format before
      the probes run, by constructing a control value no entry point produces
      and requiring it to be observed.

      The entry points are enumerated from the source, not listed by hand: every
      function under src/trusted_router whose return annotation names the
      envelope type must appear in `_WRITE_PROBES`, and one that does not is a
      refusal. That is the same closure the Go probe applies to algorithm
      constants -- an unprobed one is not an omission, it is a stop. The
      enumeration has one known hole, spelled out on `_write_entry_points` and
      pinned by a test: a PEP 695 `type` alias in the annotation.

  (b) SYNTACTIC -- `scan_write_surface()` also runs, over every module under
      src/trusted_router, with the constructor-name closure computed over the
      UNION of those modules rather than per module. It covers what (a) cannot:
      a write path no probe exercises, because it persists an envelope instead
      of returning one, or because it is only reachable under a configuration
      the probe does not set up.

The check uses the UNION of the two. Union is the fail-closed combination: a
larger written set can only make the subset check harder to satisfy. When the
two disagree, main() prints which derivation contributed what, because the
disagreement is itself information -- a format only (b) sees is a write path
nothing exercised.

WHAT THIS DOES NOT ESTABLISH
----------------------------
* It does not verify any signature. The attestation is read for its measurement
  only, through scripts/verify_trust_measurements.py's own samplers, so the two
  cannot drift. The cryptographically complete verifier is quill-cloud-proxy's
  tools/verify-attestation.py. `sample_aws_enclave` computes whether the Nitro
  document binds the certificate served on that connection; this program does
  not read that field, and does not claim the AWS read is authenticated.
* It does not prove `source_commit` is the commit that built the running
  enclave. Neither PCR0 nor hostdata carries a commit; the field is an assertion
  by whoever ran the release. This check inherits that assertion's strength, and
  its whole value is that a WRONG assertion is now falsifiable (verify-pcr0.sh
  rebuilds PCR0 from a commit) where an ABSENT one was not.
* It does not prove the accepted-formats declaration was generated rather than
  written by hand. It proves the declaration matches the package source at the
  same commit, byte for byte, for every non-test .go file that commit has in
  that package. Someone who edits that package, recomputes the hashes by hand,
  and never runs CI defeats it; the pin is that quill-cloud-proxy's CI runs
  `go test ./...` on every push and pull request, and that test regenerates the
  declaration from behaviour and diffs it.
* ACCEPTANCE IS MEASURED IN THE GO TEST'S ENVIRONMENT, NOT THE ENCLAVE'S. The
  declaration records what a round trip did under `go test`. A build whose
  refusal is switched on at RUN TIME -- `os.Getenv("TR_BYOK_V2_KILL") == "1"`,
  a config field, a remote flag -- produces a green CI, a declaration naming
  V1 and V2, and a running enclave that rejects V2. That is the surviving form
  of the kill switch this design replaced a source parse to catch, and neither
  the declaration nor this script can see it. NOTHING CLOSES IT. The only thing
  working against it is that the enclave's configuration surface is fixed by
  the CCE policy / PCR0 it ships with, so such a variable is visible to whoever
  reviews a release -- a review property, not a measured one, and not something
  this check would notice failing.
* The same environment-dependence applies to the WRITE side. The behavioural
  probe observes the formats this tree writes when called the way
  `_WRITE_PROBES` calls it. A write path that chooses its format from a runtime
  setting reports whichever branch the probe's environment selects; the
  syntactic scan is what sees the other branch, and it sees it only if the
  format is a module-level string constant.
* The declaration covers the byokcache read path. A refusal further up the
  enclave -- settlement.go declining to pass a v2 envelope to byokcache at all
  -- is not visible to the probe and would read as accepted.
* It does not cover a bind window inside a single region. Two enclave builds
  behind one hostname answer one at a time, and the record names one commit for
  both. The check refuses in that case rather than guessing -- see
  `_BIND_WINDOW` below -- but "refuses" is not "verified both".
* It says nothing about envelopes already at rest. Reads are dispatched on the
  stored algorithm, so this is about what a build starts WRITING.
* Coverage is bounded by the record. Regions come from the record's `regions`
  array and the accepted set is cross-checked against what was observed, so a
  record that publishes more accepted measurements than this run saw is a
  refusal (step 2). What remains invisible is an enclave that serves this cloud
  while appearing in neither the `regions` array nor the accepted set: nothing
  in the record names it and no probe was ever routed to it. The coverage
  refusal can also be cleared by an operator NARROWING the accepted set instead
  of retiring the build, which converts the refusal into exactly that blind
  spot; the refusal text says so where it offers the option.
* NOTHING HERE STOPS A DEPLOY. This runs on pull requests and on pushes to main;
  it stops a format change from being MERGED, and nothing consults it at deploy
  time. A build whose written formats this never checked -- because the job was
  made non-required, or because the change reached main some other way --
  deploys like any other. That is strictly less than the deploy gate this
  replaced would have covered, and it is the trade: see "WHERE THIS RUNS".
* An underivable write side fails this check. `scan_write_surface` refuses on
  any assignment to an `algorithm` attribute anywhere under src/trusted_router
  (deliberately: see `_mutates_an_algorithm_attribute`), and
  `probe_write_entry_points` refuses when a probed entry point cannot be called.
  Both are reachable by an ordinary refactor that has nothing to do with
  envelopes, and such a pull request goes red here. That is the price of "unknown
  is not unchanged": with the written set underivable there is no way to tell a
  format change from an unrelated edit, and the silent path may only be taken
  when the derivation succeeded and matched. The refusal names the file and line,
  and it blocks one pull request rather than every deploy.

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
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.verify_trust_measurements import (
    NOT_CONFIGURED,
    NetworkTransport,
    Transport,
    live_azure_hostdata,
    live_gcp_container,
    sample_aws_enclave,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every module that can persist an envelope, not the one that happens to today.
# The first version of this file read src/trusted_router/byok_crypto.py alone,
# so a write site in any other module was invisible and the check passed.
WRITE_SURFACE = REPO_ROOT / "src" / "trusted_router"

DEFAULT_CONTROL_PLANE = "https://trustedrouter.com"
TIMEOUT_SECONDS = 25

# The switch between this program's two paths: equal to what the tree derives
# -> exit 0 without touching the network; different -> check every cloud.
#
# What it holds is the format set already in production on every cloud.
# docs/design/byok-aad-v2-migration.md's status table records step 1 (the
# enclave reads v1 and v2) complete in every serving region of GCP, AWS and
# Azure, and step 2 (the control plane writes v2) deployed on all three. So V2
# is a format those enclaves have been reading for as long as this build has
# been writing it. That is a record of past releases, not a live measurement;
# the loud path is what measures, and this constant is what decides when
# measuring is warranted.
#
# It is a hand-maintained constant, so it is not trusted. tests/
# test_check_format_ordering.py::test_the_baseline_matches_what_this_tree_writes
# runs the real derivation against the real tree and asserts equality, which is
# what turns "somebody remembered to update this" into "CI fails on the commit
# that did not". Changing this line is the reviewable moment the design is built
# around: see "WHY A PINNED BASELINE RATHER THAN A MERGE-BASE DIFF".
BASELINE_WRITTEN_FORMATS = frozenset({"TR-BYOK-ENVELOPE-AES-256-GCM-V2"})

# The enclave source of truth for what a build accepts. Read at a commit rather
# than from a checkout, because the question is what the RUNNING build accepts
# and that build is whatever commit the record names — not whatever
# quill-cloud-proxy's main happens to be today.
ENCLAVE_REPO = "Lore-Hex/quill-cloud-proxy"
ENCLAVE_PACKAGE = "enclave-go/internal/byokcache"
DECLARATION_PATH = f"{ENCLAVE_PACKAGE}/accepted_formats.json"
DECLARATION_SCHEMA = "trustedrouter/byok-accepted-formats/v1"

# The constructor whose `algorithm=` argument decides the format written. Every
# envelope this control plane persists is built here; grepping for the constant
# names instead would miss a new one, which is the failure mode that matters.
ENVELOPE_TYPE = "EncryptedSecretEnvelope"

# The write-side analogue of the declaration's `rejected_control`, and the same
# string, because it means the same thing on both sides: a value that must never
# be mistaken for a real format. The behavioural probe constructs one envelope
# carrying it and requires the recorder to report it. A recorder that cannot see
# a format it has never been told about is a recorder whose silence proves
# nothing, and "no V3 observed" from such a recorder is not evidence.
PROBE_CONTROL_FORMAT = "TR-BYOK-ENVELOPE-AES-256-GCM-PROBE-NOT-A-FORMAT"

# Calls that receive the envelope type as an ARGUMENT rather than calling it.
# Any OTHER call that receives it as a DIRECT argument — bare
# `EncryptedSecretEnvelope` or dotted `storage_models.EncryptedSecretEnvelope`,
# positionally or by keyword — is building envelopes through an indirection
# this parser cannot follow, so it fails closed instead. `functools.partial(E)`
# and `register(storage_models.E)` are the shapes that reach it (both verified
# to refuse).
#
# What that is NOT is a rule about indirection in general, and an earlier
# version of this comment said "a registry, a factory table" as though it were.
# It is a rule about one syntactic position: the type appearing as a direct
# argument of a call. A registry that never puts it in that position walks
# past — `_REGISTRY = {"v3": EncryptedSecretEnvelope}` then `_REGISTRY["v3"](…)`
# returns cleanly with the V2 answer, and so do a constructor inside a list
# argument, a tuple-unpacking alias, and `getattr(storage_models, "…")`. All
# four are verified, all four are in this scan's KNOWN BLIND SPOTS block, and
# all four are seen by the behavioural probe on any path a probe calls, which
# is why the probe and not this is the primary derivation.
_TYPE_USES = frozenset({"isinstance", "issubclass", "cast", "get_type_hints", "TypeVar"})

# Modules allowed to construct an envelope from a `**mapping`. Such a call
# REHYDRATES a row already in the database — its algorithm was chosen by
# whichever write site persisted it — so it cannot introduce a format, and
# there is no literal for this parser to read. The allowlist is deliberately a
# hardcoded refusal boundary rather than a rule: a `**mapping` construction in
# any other module refuses until a human decides which kind it is.
_REHYDRATION_MODULES = frozenset(
    {
        # ByokProviderConfig / BroadcastDestination.__post_init__, rebuilding a
        # stored dict into the dataclass.
        "src/trusted_router/storage_models.py",
        # The v1 -> v2 backfill, reading an envelope out of a row before
        # decrypting it. What it WRITES it writes through byok_crypto.
        "src/trusted_router/byok_aad_backfill.py",
    }
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")

# (commit, repository-relative path) -> that file's bytes at that commit.
SourceReader = Callable[[str, str], bytes]
# commit -> the file names present in ENCLAVE_PACKAGE at that commit.
PackageLister = Callable[[str], frozenset[str]]

_BIND_WINDOW = (
    "a bind window publishes two measurements and one source_commit, so the "
    "outgoing build's accepted formats are unknown. Finish the enclave roll and "
    "narrow the accepted set before landing a control-plane format change -- that "
    "ordering is the whole of byok-aad-v2-migration.md section 4.0."
)


@dataclass(frozen=True)
class PlaneSpec:
    """How one cloud's release record names the thing the enclave measures."""

    cloud: str
    record_path: str
    accepted_key: str
    measurement_key: str
    measurement_label: str


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
# What the control plane writes: (a) behaviour
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteProbe:
    """What CALLING this tree's write entry points actually produced."""

    formats: frozenset[str]
    # entry point -> the formats that call produced. Printed, so a reader can
    # see which path contributed which format rather than a bare union.
    by_entry_point: tuple[tuple[str, tuple[str, ...]], ...]


def _probe_settings() -> Any:
    """A key wrapper the probe can use without a KMS, a network, or a secret.

    `environment="test"` selects LocalAesKeyWrapper over the HKDF-derived
    development key, which is the same path the unit tests use. It cannot reach
    production key material and it cannot reach the network.
    """
    from trusted_router.key_management import KeyWrapperConfig

    return KeyWrapperConfig(environment="test")


# A plaintext, not a credential. It is encrypted and thrown away; only the
# envelope's `algorithm` field is ever read.
_PROBE_PLAINTEXT = "probe-plaintext-not-a-credential"  # noqa: S105 - fixture, not a secret


def _probe_byok_secret(settings: Any) -> object:
    from trusted_router.byok_crypto import encrypt_byok_secret

    return encrypt_byok_secret(
        _PROBE_PLAINTEXT, settings, workspace_id="ws-probe", provider="openai"
    )


def _probe_control_secret(settings: Any) -> object:
    from trusted_router.byok_crypto import encrypt_control_secret

    return encrypt_control_secret(
        _PROBE_PLAINTEXT, settings, workspace_id="ws-probe", purpose="broadcast"
    )


# Every write entry point, keyed exactly as `_write_entry_points()` names them.
# A function under src/trusted_router that returns an envelope and is absent
# from this table is a REFUSAL, not a gap: see `derive_written_formats`.
_WRITE_PROBES: dict[str, Callable[[Any], object]] = {
    "src/trusted_router/byok_crypto.py:encrypt_byok_secret": _probe_byok_secret,
    "src/trusted_router/byok_crypto.py:encrypt_control_secret": _probe_control_secret,
}


def probe_write_entry_points(
    probes: Mapping[str, Callable[[Any], object]] | None = None,
    *,
    envelope_type: type[Any] | None = None,
) -> WriteProbe:
    """Formats this tree WRITES, by calling it and reading the envelopes back.

    This is the write-side twin of the enclave's accepted-formats declaration,
    and it exists for the same reason: two rounds of adversarial review defeated
    a syntactic answer, each time with a spelling the previous fix had not
    imagined (a module alias, a subclass, `dataclasses.replace`, an assignment
    to `.algorithm`, a write from another module; then an ANNOTATED alias, an
    alias imported from a sibling module, a subclass defined in a sibling
    module). A value read off the object a real call produced has no spelling.

    The mechanism: install a recorder on `EncryptedSecretEnvelope.__init__`,
    call each entry point in `_WRITE_PROBES`, and read `.algorithm` off every
    envelope that call constructed — AFTER it returns, so a format assigned to
    the object later in the same call is the value observed. Every indirection
    listed above converges on that object.

    Before any of that means anything, the recorder has to be able to see a
    format at all. So it is first shown one: an envelope carrying
    PROBE_CONTROL_FORMAT is constructed and must be observed. If it is not, this
    raises rather than reporting an empty or partial set — a blind recorder
    reports "no V3" for a build that writes V3.

    What the control proves is exactly that the recorder fires for constructions
    of the class this function patched. It does NOT prove an entry point builds
    THAT class rather than a look-alike; what covers that is reading the value
    each entry point returns as well, and refusing an entry point that produced
    nothing this probe could read a format from.

    Fails closed on: an entry point that raises, an entry point that produced no
    envelope, or a recorder that missed the control.

    LIMITS, plainly:
      * It sees only what `_WRITE_PROBES` calls. A path that persists an
        envelope without returning one is not enumerated here at all; that is
        the syntactic scan's job, and the check uses the union.
      * A subclass that overrides `__init__` without calling `super().__init__`
        is missed by the recorder. It is still seen when it is the value an
        entry point RETURNS, which is also read, but a subclass like that
        constructed and persisted internally would not be.
      * It reports the branch this process's environment selects. A format
        chosen from a runtime setting shows up as whichever value
        `_probe_settings()` and the ambient environment produce.

    `envelope_type` exists so a test can fabricate a recorder that cannot be
    installed, which is the one failure the control is there to catch and which
    cannot otherwise be produced without editing this function.
    """
    if envelope_type is None:
        from trusted_router.storage_models import EncryptedSecretEnvelope

        envelope_type = EncryptedSecretEnvelope

    table = _WRITE_PROBES if probes is None else probes
    settings = _probe_settings()
    constructed: list[Any] = []
    original_init = envelope_type.__init__

    def recording_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        constructed.append(self)

    envelope_type.__init__ = recording_init  # type: ignore[method-assign]
    try:
        envelope_type(
            algorithm=PROBE_CONTROL_FORMAT,
            key_ref="probe",
            encrypted_dek="",
            dek_nonce="",
            ciphertext="",
            nonce="",
        )
        if PROBE_CONTROL_FORMAT not in {getattr(e, "algorithm", None) for e in constructed}:
            raise ValueError(
                "the write probe's recorder did not observe its own control envelope, so it "
                "cannot see what this build constructs. Anything it reports about written "
                "formats — including reporting none — is meaningless."
            )

        by_entry_point: list[tuple[str, tuple[str, ...]]] = []
        formats: set[str] = set()
        for name in sorted(table):
            constructed.clear()
            try:
                returned = table[name](settings)
            except Exception as exc:  # noqa: BLE001 - a probe that cannot run proves nothing
                raise ValueError(
                    f"write entry point {name} could not be called by the probe ({exc!r}), so "
                    "the format it writes was not measured. Fix the probe in _WRITE_PROBES; do "
                    "not delete it, because a deleted probe is an unmeasured write path."
                ) from exc
            observed = {
                value
                for value in (getattr(envelope, "algorithm", None) for envelope in constructed)
                if isinstance(value, str)
            }
            returned_algorithm = getattr(returned, "algorithm", None)
            if isinstance(returned_algorithm, str):
                observed.add(returned_algorithm)
            if not observed:
                raise ValueError(
                    f"write entry point {name} produced no envelope the probe could read a "
                    "format from, so what it writes is unknown."
                )
            if PROBE_CONTROL_FORMAT in observed:
                raise ValueError(
                    f"write entry point {name} produced the probe's own control value, so the "
                    "probe cannot tell a real write from its own fixture."
                )
            by_entry_point.append((name, tuple(sorted(observed))))
            formats |= observed
    finally:
        envelope_type.__init__ = original_init  # type: ignore[method-assign]

    return WriteProbe(formats=frozenset(formats), by_entry_point=tuple(by_entry_point))


# --------------------------------------------------------------------------
# What the control plane writes: (b) syntax
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteScan:
    """What the write-side scan found, including what it deliberately did not read."""

    formats: frozenset[str]
    # Constructions from a `**mapping`: an already-stored envelope being rebuilt.
    # Named rather than hidden, because they are the one construction shape this
    # scan reads no format out of.
    rehydration_sites: tuple[str, ...]
    # Functions whose return annotation names the envelope type. Each one must
    # have a behavioural probe; see derive_written_formats.
    entry_points: tuple[str, ...] = ()


@dataclass(frozen=True)
class WriteDerivation:
    """The union of the two derivations, with each side's answer kept separate."""

    formats: frozenset[str]
    behavioural: frozenset[str]
    syntactic: frozenset[str]
    rehydration_sites: tuple[str, ...]
    by_entry_point: tuple[tuple[str, tuple[str, ...]], ...]


def read_write_surface(root: Path = WRITE_SURFACE) -> dict[str, str]:
    """Every Python module that could persist an envelope."""
    modules = {}
    for path in sorted(root.rglob("*.py")):
        modules[path.relative_to(REPO_ROOT).as_posix()] = path.read_text(encoding="utf-8")
    if not modules:
        raise ValueError(f"no Python modules under {root}; the write side is unreadable")
    return modules


def scan_write_surface(sources: Mapping[str, str]) -> WriteScan:
    """Envelope formats this build WRITES, as far as reading the source can tell.

    This is the SECOND of the two write-side derivations. The first is
    `probe_write_entry_points()`, which calls the code; this one reads it. The
    union is what gets checked, because each covers what the other cannot: the
    probe cannot see a path it does not call, and this cannot see a spelling it
    does not recognise.

    Deliberately not a hardcoded {"…-V2"}: hardcoding makes a derivation correct
    exactly once, and the day a V3 write lands it would keep asserting the V2
    subset. (BASELINE_WRITTEN_FORMATS is a hardcoded set, and it is compared
    against this one rather than substituted for it — that is the whole point of
    the test that pins it.)

    What it does. Every module under src/trusted_router is parsed. The set of
    names that reach the constructor is closed over the UNION of those modules —
    `E = EncryptedSecretEnvelope`, `E: type[…] = EncryptedSecretEnvelope`,
    `from … import EncryptedSecretEnvelope as E`, and
    `class E(EncryptedSecretEnvelope)` all add E, wherever in the surface they
    appear, and a name added in one module is treated as the constructor in
    every module. That is deliberately over-broad: over-broad here produces a
    refusal, and a refusal is the safe direction. A `dataclasses.replace` that
    sets `algorithm=` is a write.

    Fails closed on an `algorithm=` it cannot resolve to a module-level string
    constant. An expression there is not evidence of safety; it is a build whose
    written format is unknown, and unknown is the case the enclave breaks on.
    It also refuses outright, rather than returning a set, when it sees the
    constructor appear as a DIRECT ARGUMENT of another call (bare or dotted),
    or any `.algorithm` assigned after construction — including through
    `setattr`, `object.__setattr__` and `obj.__dict__["algorithm"]`. "Direct
    argument" is the exact scope of the first of those: a constructor handed
    onward some other way is a blind spot, listed below.

    KNOWN BLIND SPOTS OF THIS SCAN, named rather than implied. Several are
    covered by the behavioural probe and are marked; the ones marked NOT COVERED
    are covered by nothing and are the real write-side limit.
      * a `**mapping` construction in a module on _REHYDRATION_MODULES. Those
        four calls rebuild an envelope already in the database, so they read a
        format rather than choosing one, and there is no literal here to read.
        The same shape in any other module refuses, and main() prints the sites
        it did not read. Covered by the probe when the call sits on a probed
        entry point's path.
      * an envelope constructed outside src/trusted_router entirely. NOT COVERED
        unless a probed entry point reaches it.
      * a format chosen at run time from configuration. This scan refuses to
        resolve it; the probe reports whichever branch its environment selects.
        Neither sees the other branch. NOT COVERED.
      * the constructor reaching a call site through an indirection that never
        puts the type in a call's argument list. The refusal above catches
        `functools.partial(E)` and `register(module.E)` because the type is a
        direct argument there; it catches nothing when the type is a dict
        value, a list element, one side of a tuple assignment, or the result of
        `getattr`. All four were demonstrated by a third review and each
        returns the V2 answer over a V3 write:
        `_REGISTRY = {"v3": E}` / `_REGISTRY["v3"](algorithm=V3, …)`;
        `cls = register([E])`; `_A, _B = E, None`;
        `cls = getattr(storage_models, "EncryptedSecretEnvelope")`.
        Covered by the probe on any path a probe calls, and by nothing on a
        path no probe calls.
      * a spelling nobody has thought of. Three reviews found twelve between
        them, and the honest expectation is that a fourth would find more —
        which is why the probe, not this, is the primary derivation.
    An `algorithm=` computed at run time is NOT a silent omission here: it
    raises. Being unable to read a format is a refusal.
    """
    trees: dict[str, ast.Module] = {}
    for origin, source in sources.items():
        try:
            trees[origin] = ast.parse(source, filename=origin)
        except SyntaxError as exc:
            raise ValueError(f"cannot parse {origin}: {exc}") from exc

    constants = _module_level_string_constants(trees)
    constructors = _constructor_names(trees)

    written: set[str] = set()
    rehydration: list[str] = []
    unresolved: list[str] = []
    for origin, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                _scan_call(node, origin, constructors, constants, written, rehydration, unresolved)
            else:
                mutation = _mutates_an_algorithm_attribute(node)
                if mutation is not None:
                    unresolved.append(f"{origin}:{getattr(node, 'lineno', 0)} {mutation}")

    if unresolved:
        raise ValueError("the syntactic scan cannot read " + "; ".join(sorted(unresolved)))
    if not written:
        raise ValueError(
            f"found no {ENVELOPE_TYPE}(algorithm=...) under {WRITE_SURFACE}. Either the "
            "constructor moved or this parser has rotted; both mean the written format is unknown."
        )
    return WriteScan(
        formats=frozenset(written),
        rehydration_sites=tuple(sorted(rehydration)),
        entry_points=_write_entry_points(trees, constructors),
    )


def written_formats(sources: Mapping[str, str]) -> frozenset[str]:
    """The SYNTACTIC derivation's answer alone, for callers that need only the set.

    Not the set this program checks. main() takes the union of this and the
    behavioural probe; see derive_written_formats.
    """
    return scan_write_surface(sources).formats


def derive_written_formats(
    sources: Mapping[str, str],
    *,
    probe: Callable[[], WriteProbe] = probe_write_entry_points,
) -> WriteDerivation:
    """The formats this build writes: behaviour ∪ syntax, or a refusal.

    Refuses when the source declares a write entry point — a function under
    src/trusted_router whose return annotation names the envelope type — that
    `_WRITE_PROBES` does not call. An unprobed entry point is a write path whose
    format nobody measured, and reporting the syntactic answer for it alone
    would demote the primary derivation, without saying so, to the one that has
    now been walked past twice.

    The union is the fail-closed combination: adding a format can only make
    `written ⊆ accepted` harder to satisfy, never easier.
    """
    scan = scan_write_surface(sources)
    ambiguous = sorted({name for name in scan.entry_points if scan.entry_points.count(name) > 1})
    if ambiguous:
        raise ValueError(
            "these write entry points share a name inside one module, so one probe key would "
            f"stand for two functions and only one of them would be measured: {', '.join(ambiguous)}"
        )
    unprobed = sorted(name for name in scan.entry_points if name not in _WRITE_PROBES)
    if unprobed:
        raise ValueError(
            "these functions return an envelope and no behavioural probe calls them, so what "
            f"they write was never measured: {', '.join(unprobed)}. Add each to _WRITE_PROBES "
            "in scripts/check_format_ordering.py with a call that exercises it."
        )
    observed = probe()
    return WriteDerivation(
        formats=observed.formats | scan.formats,
        behavioural=observed.formats,
        syntactic=scan.formats,
        rehydration_sites=scan.rehydration_sites,
        by_entry_point=observed.by_entry_point,
    )


def _scan_call(
    node: ast.Call,
    origin: str,
    constructors: frozenset[str],
    constants: Mapping[str, set[str]],
    written: set[str],
    rehydration: list[str],
    unresolved: list[str],
) -> None:
    callee = _callee(node.func)
    where = f"{origin}:{node.lineno}"

    # setattr(envelope, "algorithm", …) and object.__setattr__(…) reach the same
    # place as `envelope.algorithm = …` and are refused for the same reason.
    if callee in {"setattr", "__setattr__"} and _names_the_algorithm_attribute(node.args):
        unresolved.append(
            f"{where} sets an `algorithm` attribute through {callee}(), so the format written "
            "there cannot be read from a constructor call"
        )
        return

    # The constructor handed to something else as a value: a factory, a
    # partial, a registry. Whatever that something does with it is out of
    # reach, so it is a refusal rather than a silent zero. Bare `E` and dotted
    # `module.E` are the same handover and are treated the same.
    if callee not in constructors and callee not in _TYPE_USES:
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if isinstance(argument, ast.Name | ast.Attribute) and _callee(argument) in constructors:
                unresolved.append(
                    f"{where} passes {_callee(argument)} to {callee or 'an expression'}(), so "
                    "envelopes may be built through an indirection this scan cannot follow"
                )
                return

    is_construction = callee in constructors
    is_replace = _is_dataclasses_replace(node)
    if not (is_construction or is_replace):
        return

    keywords = {keyword.arg for keyword in node.keywords}
    if is_construction and node.keywords and all(keyword.arg is None for keyword in node.keywords):
        # `EncryptedSecretEnvelope(**stored)`: rebuilding a row, not choosing a
        # format. Only where that is the established meaning; anywhere else the
        # same shape is a write whose format is unknown.
        # A plain reference only. `**{**stored, "algorithm": V3}` and
        # `**dict(stored, algorithm=V3)` are how a NEW format would arrive
        # through this shape, and both stay refusals.
        splat_only = len(node.keywords) == 1 and isinstance(
            node.keywords[0].value, ast.Name | ast.Attribute
        )
        if splat_only and not node.args and origin in _REHYDRATION_MODULES:
            rehydration.append(where)
            return
        unresolved.append(
            f"{where} builds an envelope from a mapping, so the format it writes is not in "
            "the source. If it rebuilds a stored envelope, add the module to "
            "_REHYDRATION_MODULES with the reason."
        )
        return
    if is_construction and (node.args or None in keywords):
        unresolved.append(f"{where} is not an all-keyword {ENVELOPE_TYPE}(...) call")
        return
    if "algorithm" not in keywords:
        # A replace() that does not touch algorithm keeps the stored one; a
        # construction with no algorithm= at all cannot be a valid envelope.
        if is_construction:
            unresolved.append(f"{where} constructs {ENVELOPE_TYPE} with no algorithm=")
        return

    values = [keyword.value for keyword in node.keywords if keyword.arg == "algorithm"]
    if len(values) != 1:
        unresolved.append(f"{where} passes algorithm= {len(values)} times")
        return
    resolved = _resolve_string(values[0], constants)
    if resolved is None:
        unresolved.append(f"{where}: algorithm= is not a module-level string constant")
        return
    written.add(resolved)


def _module_level_string_constants(trees: Mapping[str, ast.Module]) -> dict[str, set[str]]:
    """Module-level `NAME = "…"` across the whole surface, keyed by name.

    Keyed by name rather than by module so `algorithm=ALGORITHM_V2` resolves in
    a module that imported it. A name defined twice with different values maps
    to two values and therefore resolves to neither: ambiguous is unknown, and
    unknown refuses.
    """
    constants: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            if not isinstance(node.value.value, str):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants.setdefault(target.id, set()).add(node.value.value)
    return constants


def _constructor_names(trees: Mapping[str, ast.Module]) -> frozenset[str]:
    """Every name ANYWHERE in the surface that reaches the envelope constructor.

    `E = EncryptedSecretEnvelope` and `E: type[…] = EncryptedSecretEnvelope` at
    any scope, `from … import X as E`, and `class E(EncryptedSecretEnvelope)`
    all produce a callable that writes an envelope.

    Computed over the UNION of the modules, not one at a time. Per module, a
    reviewer got past this three ways in a single sitting: an alias defined in
    a sibling module and imported, a subclass defined in a sibling module and
    imported, and an annotated alias the ast.Assign branch never looked at.
    Names are global here, so a name bound to the constructor in any module is
    treated as the constructor in every module. That over-approximates — an
    unrelated class that happens to share the name of an alias would be scanned
    as a constructor — and over-approximating produces a refusal, which is the
    direction this is allowed to be wrong in.

    Closed over repeatedly so an alias of an alias is included.
    """
    names = {ENVELOPE_TYPE}
    for _ in range(8):
        before = set(names)
        for tree in trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in names and alias.asname:
                            names.add(alias.asname)
                elif isinstance(node, ast.Assign) and _callee(node.value) in names:
                    names.update(
                        target.id for target in node.targets if isinstance(target, ast.Name)
                    )
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    if _callee(node.value) in names and isinstance(node.target, ast.Name):
                        names.add(node.target.id)
                elif isinstance(node, ast.ClassDef):
                    if any(_callee(base) in names for base in node.bases):
                        names.add(node.name)
        if names == before:
            break
    return frozenset(names)


def _write_entry_points(
    trees: Mapping[str, ast.Module], constructors: frozenset[str]
) -> tuple[str, ...]:
    """`origin:function` for every function whose return annotation is an envelope.

    This is what makes the behavioural probe's coverage checkable instead of
    aspirational: `derive_written_formats` refuses on any name here that
    `_WRITE_PROBES` does not call. It mirrors the enclave probe, which fails
    when cache.go declares an algorithm constant `writerAAD` does not know.

    A name defined twice in one module is reported twice, and
    `derive_written_formats` refuses on the duplicate: one probe key cannot
    stand for two functions without one of them going unmeasured.

    Annotations are read from the AST, so `-> EncryptedSecretEnvelope`,
    `-> EncryptedSecretEnvelope | None` and `-> "EncryptedSecretEnvelope"` are
    all read. A function that returns an envelope WITHOUT saying so is not
    enumerated; what makes that a narrow gap rather than a wide one is
    pyproject.toml's `disallow_untyped_defs = true` over src/trusted_router,
    which makes an unannotated return a mypy failure in CI.

    One shape is known not to be enumerated and is checked rather than assumed:
    a PEP 695 `type Env = EncryptedSecretEnvelope` alias. That statement parses
    to `ast.TypeAlias`, which the constructor closure does not read, so
    `-> Env` is invisible here. A plain `Env = EncryptedSecretEnvelope` alias IS
    read. Neither is a fail-open on its own — the write itself still has to get
    past both derivations — but an unenumerated entry point is one the probe
    never calls, so it falls back to the syntactic scan alone.
    """
    found: list[str] = []
    for origin, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.returns is not None and _annotation_names_envelope(node.returns, constructors):
                found.append(f"{origin}:{node.name}")
    return tuple(sorted(found))


def _annotation_names_envelope(annotation: ast.expr, constructors: frozenset[str]) -> bool:
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name | ast.Attribute) and _callee(node) in constructors:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                inner = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                continue
            if any(
                isinstance(child, ast.Name | ast.Attribute) and _callee(child) in constructors
                for child in ast.walk(inner)
            ):
                return True
    return False


def _is_dataclasses_replace(node: ast.Call) -> bool:
    """`dataclasses.replace(envelope, algorithm=…)` writes a format too."""
    if isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
        return isinstance(node.func.value, ast.Name) and node.func.value.id == "dataclasses"
    return isinstance(node.func, ast.Name) and node.func.id == "replace"


def _names_the_algorithm_attribute(args: Sequence[ast.expr]) -> bool:
    return any(
        isinstance(argument, ast.Constant) and argument.value == "algorithm" for argument in args
    )


def _mutates_an_algorithm_attribute(node: ast.AST) -> str | None:
    """Why this node assigns some object's `algorithm` after construction.

    Deliberately over-broad: it matches `.algorithm` on ANY object, and it
    cannot tell an envelope from a JWT header or a KMS request. That is why the
    message it produces says so. A narrower rule would have to decide which
    objects are envelopes, which is the inference this whole scan exists because
    it cannot make reliably — and being wrong in the narrow direction is a
    silent green over a V3 write, while being wrong in this direction is a
    refusal a human clears in one reading.

    The cost of the breadth is real and lands on whoever writes the unrelated
    assignment: a `self.algorithm = "RS256"` added anywhere under
    src/trusted_router makes the written set underivable, and this program
    refuses rather than taking its silent path. See the module docstring's last
    limit.
    """
    targets: Iterable[ast.expr]
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        targets = [node.target]
    else:
        return None
    for target in targets:
        if isinstance(target, ast.Attribute) and target.attr == "algorithm":
            return (
                "assigns an `algorithm` attribute after construction. This scan cannot tell "
                "whether that object is an envelope, and if it is, the format written there is "
                "not readable from any constructor call -- so it refuses either way"
            )
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "__dict__"
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "algorithm"
        ):
            return (
                "writes an `algorithm` entry into an object's __dict__, which sets the "
                "attribute without going through any constructor call this scan can read"
            )
    return None


def _resolve_string(value: ast.expr, constants: Mapping[str, set[str]]) -> str | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    name = value.id if isinstance(value, ast.Name) else None
    if isinstance(value, ast.Attribute):
        name = value.attr
    if name is not None and len(constants.get(name, set())) == 1:
        return next(iter(constants[name]))
    return None


def _callee(func: ast.expr | None) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


# --------------------------------------------------------------------------
# What the enclave accepts
# --------------------------------------------------------------------------

_GO_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+\.go$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def accepted_formats(
    commit: str, read: SourceReader, list_package: PackageLister
) -> frozenset[str]:
    """Envelope formats the enclave built from `commit` ACCEPTS.

    Read from the generated declaration at
    enclave-go/internal/byokcache/accepted_formats.json, NOT parsed out of
    cache.go. The declaration is written by a Go test that, for every algorithm
    constant the package declares, seals an envelope with the control plane's
    own associated data and requires (*Cache).Resolve — the entry point on the
    prompt path — to return the plaintext. A format is in `accepted` because a
    round trip succeeded, not because a case label exists.

    That distinction is the whole reason this function changed shape. Reading
    the switch, four compiling and gofmt-clean edits kept `case AlgorithmV2:`
    while rejecting v2: an erroring case body, a kill switch ahead of the
    switch, a rejection in the caller, and a renamed live dispatch with the old
    function left behind as dead code. All four passed. All four fail a round
    trip.

    The declaration is bound to the source by `source_sha256`. Every non-test
    .go file the package HAS at that commit — enumerated with `list_package`,
    not taken from the declaration's own key set — must be pinned, and each pin
    must match the file fetched at the same commit. Checking only the entries
    the declaration happens to list would let a declaration pinning one
    unrelated file describe a changed cache.go.

    Fails closed on: no declaration at that commit (a build older than this
    mechanism cannot be checked this way and must not be assumed to accept
    anything), an unreadable or wrong-schema declaration, a package listing it
    cannot obtain, a pinned file whose hash does not match, a non-test .go file
    the declaration does not pin at all, or a probe that declared its own
    control value accepted.

    It does NOT establish that the round trip the declaration records would
    still succeed in the enclave's own environment; see the module docstring's
    note on run-time kill switches.
    """
    origin = f"{DECLARATION_PATH}@{commit}"
    try:
        declaration = json.loads(read(commit, DECLARATION_PATH))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{origin} is not JSON: {exc}") from exc
    if not isinstance(declaration, dict):
        raise ValueError(f"{origin} is a {type(declaration).__name__}, not a declaration")
    if declaration.get("schema") != DECLARATION_SCHEMA:
        raise ValueError(
            f"{origin} declares schema {declaration.get('schema')!r}, expected "
            f"{DECLARATION_SCHEMA!r}. A schema this script does not know is a declaration it "
            "cannot read, not one it may skip."
        )
    if declaration.get("package") != ENCLAVE_PACKAGE:
        raise ValueError(
            f"{origin} describes package {declaration.get('package')!r}, not {ENCLAVE_PACKAGE!r}"
        )

    accepted = declaration.get("accepted")
    if not isinstance(accepted, list) or not accepted:
        raise ValueError(f"{origin} declares no accepted formats")
    if any(not isinstance(value, str) or not value for value in accepted):
        raise ValueError(f"{origin}: `accepted` must be non-empty strings")

    control = declaration.get("rejected_control")
    if not isinstance(control, str) or not control:
        raise ValueError(
            f"{origin} names no rejected_control. Without a format the probe confirmed it "
            "REJECTS, `accepted` could have been produced by a probe that accepts everything."
        )
    if control in accepted:
        raise ValueError(
            f"{origin} lists its own control value {control!r} as accepted, so the probe that "
            "produced it cannot distinguish acceptance from rejection."
        )

    _verify_declaration_binds_to_source(commit, read, list_package, declaration, origin)
    return frozenset(accepted)


def _verify_declaration_binds_to_source(
    commit: str,
    read: SourceReader,
    list_package: PackageLister,
    declaration: dict[str, Any],
    origin: str,
) -> None:
    """The declaration must pin every non-test .go file, and every pin must match.

    Two separate properties, and the first one was missing. Verifying only the
    entries the declaration lists proves those files are unchanged and says
    nothing about the ones it omits, so a declaration pinning a single unrelated
    file passed over an edited cache.go. The package's file list therefore comes
    from `list_package` — the repository at that commit — and the declaration is
    checked against it.

    This is what stops a stale declaration from describing a package that has
    since changed. It does NOT prove the declaration was generated rather than
    hand-written; that is quill-cloud-proxy CI running the generating test on
    every push, and it is stated as a limit in this module's docstring.
    """
    hashes = declaration.get("source_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError(f"{origin} pins no source files, so it is bound to no particular build")

    # Shape first, and before any I/O: a pin naming `../../etc/passwd` is not a
    # pin this script will turn into a fetch, whatever else is wrong.
    for name, digest in sorted(hashes.items()):
        if not isinstance(name, str) or not _GO_FILENAME.fullmatch(name):
            raise ValueError(f"{origin} pins {name!r}, which is not a .go file in that package")
        if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
            raise ValueError(f"{origin} pins {name} to {digest!r}, which is not a sha256")

    present = list_package(commit)
    required = {name for name in present if name.endswith(".go") and not name.endswith("_test.go")}
    if not required:
        raise ValueError(
            f"{ENCLAVE_PACKAGE} at {commit} lists no non-test .go files, so there is nothing the "
            "declaration could be bound to. Refusing rather than treating an empty package as a "
            "package that matches."
        )
    unpinned = sorted(required - set(hashes))
    if unpinned:
        raise ValueError(
            f"{origin} does not pin {', '.join(unpinned)}, which {ENCLAVE_PACKAGE} contains at "
            f"{commit}. A declaration that pins a subset of the package proves nothing about the "
            "files it left out, and one of those files is where acceptance is decided. Re-run "
            "the generator in quill-cloud-proxy."
        )

    for name, digest in sorted(hashes.items()):
        body = read(commit, f"{ENCLAVE_PACKAGE}/{name}")
        actual = hashlib.sha256(body).hexdigest()
        if actual != digest:
            raise ValueError(
                f"{origin} pins {name} to {digest[:12]}… but {name} at {commit} hashes to "
                f"{actual[:12]}…. The declaration describes a different build than this commit's "
                "source, so what that enclave accepts was never measured. Re-run the generator in "
                "quill-cloud-proxy and publish a commit whose declaration matches its package."
            )


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _fetch(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    if not url.startswith("https://"):
        # Scheme is fixed by construction; assert it anyway so a record served
        # to us cannot turn this into a file:// read.
        raise ValueError(f"refusing to fetch non-HTTPS URL {url!r}")
    request = urllib.request.Request(url, headers={"accept": "*/*", **(headers or {})})  # noqa: S310
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        return bytes(response.read())


def fetch_record(control_plane: str, path: str) -> dict[str, Any]:
    """Published release record. Absence is a failure, not a skip.

    scripts/verify_trust_measurements.py treats an unpublished record as SKIP,
    which is right for a drift report: it is answering "does what we publish
    match what runs", and there is nothing to compare. This is answering "may
    this build start writing a new format here", and no record means no
    evidence.
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


def _github_headers(accept: str = "*/*") -> dict[str, str]:
    """GH_TOKEN is used when present only to raise the anonymous rate limit.

    The repository is public and nothing here needs authority.
    """
    headers = {"accept": accept}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def fetch_enclave_file(commit: str, path: str) -> bytes:
    """One file from the enclave repository as it was at `commit`.

    Bytes, not text: what comes back is hashed against the declaration's pin,
    and a decode-then-encode round trip is not the identity on every input.
    """
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError(f"{commit!r} is not a git object id")
    url = f"https://raw.githubusercontent.com/{ENCLAVE_REPO}/{commit}/{path}"
    try:
        return _fetch(url, headers=_github_headers())
    except urllib.error.HTTPError as exc:  # noqa: UP041 - urllib error hierarchy
        hint = ""
        if path == DECLARATION_PATH and exc.code == 404:
            hint = (
                " That build predates the generated declaration, so what it accepts was never "
                "measured. Roll an enclave built from a commit that carries "
                f"{DECLARATION_PATH} before landing a control-plane format change."
            )
        raise ValueError(
            f"cannot read {path} at {commit} (HTTP {exc.code}). Without it the formats that "
            f"enclave accepts are unknown, and unknown fails closed.{hint}"
        ) from exc


def fetch_enclave_package_files(commit: str) -> frozenset[str]:
    """Every file name in ENCLAVE_PACKAGE at `commit`.

    Needed to answer "does the declaration pin the WHOLE package", which its own
    key set cannot answer. raw.githubusercontent.com serves files and does not
    list directories, so this is the contents API. A listing that cannot be
    obtained is a completeness claim that cannot be checked, and that is a
    refusal like every other unknown here.
    """
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError(f"{commit!r} is not a git object id")
    url = f"https://api.github.com/repos/{ENCLAVE_REPO}/contents/{ENCLAVE_PACKAGE}?ref={commit}"
    try:
        body = _fetch(url, headers=_github_headers("application/vnd.github+json"))
    except urllib.error.HTTPError as exc:  # noqa: UP041 - urllib error hierarchy
        raise ValueError(
            f"cannot list {ENCLAVE_PACKAGE} at {commit} (HTTP {exc.code}), so whether the "
            "declaration pins every file in that package is unknown."
        ) from exc
    entries = json.loads(body)
    if not isinstance(entries, list):
        raise ValueError(f"{url} did not return a directory listing")
    names = {
        entry["name"]
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("type") == "file"
        and isinstance(entry.get("name"), str)
    }
    if not names:
        raise ValueError(f"{ENCLAVE_PACKAGE} at {commit} listed no files")
    return frozenset(names)


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


def live_measurement(spec: PlaneSpec, url: str, transport: Transport) -> tuple[str, str | None]:
    """(measurement, issuer) from one region's live attestation.

    The samplers are imported from scripts/verify_trust_measurements.py rather
    than rewritten. Two fail-closed COSE parsers is one fail-closed COSE parser
    and one that stopped being fail-closed without anyone noticing. They also
    own the transport details that go with each plane, including the AWS read's
    unverified TLS context — the enclave serves a certificate it generated
    itself, so there is no chain to validate.

    The issuer is returned for Azure only. That is where it is load-bearing: one
    hostname is routed by Traffic Manager to per-region MAA issuers, so the
    issuer is how a region absent from the record shows up. `sample_aws_enclave`
    reports no issuer, and `live_gcp_container` reports one that this function
    discards — GCP publishes a single plane, and comparing its issuer is a check
    scripts/verify_trust_measurements.py already makes.
    """
    if spec.cloud == "aws":
        return sample_aws_enclave(url, transport).pcr0, None
    if spec.cloud == "azure":
        hostdata, issuer = live_azure_hostdata(url, transport)
        return hostdata, issuer
    digest, _reference, _issuer = live_gcp_container(url, transport)
    return digest, None


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------


def check_plane(
    spec: PlaneSpec,
    record: dict[str, Any],
    written: frozenset[str],
    *,
    transport: Transport,
    source: SourceReader,
    list_package: PackageLister,
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
    observed: set[str] = set()
    for region in regions_of(record, spec):
        result = RegionResult(cloud=spec.cloud, host=region.host)
        results.append(result)

        if not accepted_set:
            result.problems.append(f"record publishes no {spec.accepted_key}")
            continue

        try:
            running, issuer = live_measurement(spec, region.attestation_url, transport)
        except Exception as exc:  # noqa: BLE001 - any failure to read is a failure to clear
            result.problems.append(f"cannot read a live attestation: {exc}")
            continue
        result.measurement = running
        observed.add(running)

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
                "`python3 tools/capture-plane-measurements.py --write --source-commit <sha "
                "that built the RUNNING enclave>` and commit trust-page/. That flag has no "
                "default: HEAD is the released build only by coincidence."
            )
            continue
        if not _COMMIT_RE.fullmatch(commit):
            result.problems.append(f"source_commit {commit!r} is not a git object id")
            continue
        result.source_commit = commit

        if commit not in cache:
            try:
                cache[commit] = accepted_formats(commit, source, list_package)
            except Exception as exc:  # noqa: BLE001 - unread is unknown is failed
                result.problems.append(str(exc))
                continue
        result.accepts = cache[commit]

        missing = sorted(written - result.accepts)
        if missing:
            result.problems.append(
                f"this build WRITES {', '.join(sorted(written))} but the enclave at {commit} "
                f"ACCEPTS only {', '.join(sorted(result.accepts))}. Shipping it to {spec.cloud} "
                f"breaks every BYOK key in that database at the next inference request. "
                f"Unreadable: {', '.join(missing)}."
            )

    # The record holds both numbers and the first version of this file never
    # compared them: it probed one Azure enclave, and printed a sentence about
    # every enclave serving Azure. An accepted measurement that no region
    # served us belongs to a build this run did not check, and a claim about
    # every serving enclave cannot be made over a subset of them.
    unchecked = sorted(value for value in accepted_set if value not in observed)
    if unchecked:
        results.append(
            RegionResult(
                cloud=spec.cloud,
                host="-",
                problems=[
                    f"the record accepts {len(accepted_set)} {spec.measurement_label} value(s) and "
                    f"this run observed {len(observed)} across the {len(results)} region(s) the "
                    f"record describes. Never served to us, therefore never checked: "
                    f"{', '.join(unchecked)}. Either a region is missing from the record's "
                    "`regions` array -- publish it, and this run will check it -- or the accepted "
                    "set names a build that has genuinely stopped serving, in which case narrow "
                    "the set. Narrowing is the option that clears this refusal WITHOUT anything "
                    "here confirming the build is gone: if it is still serving, it becomes an "
                    "enclave named in neither the regions array nor the accepted set, which is "
                    "the one thing this check cannot see at all."
                ],
            )
        )
    return results


def gather(
    control_plane: str,
    clouds: Sequence[str],
    written: frozenset[str],
    *,
    records: Callable[[str], dict[str, Any]],
    transport: Transport,
    source: SourceReader,
    list_package: PackageLister,
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
                check_plane(
                    spec,
                    record,
                    written,
                    transport=transport,
                    source=source,
                    list_package=list_package,
                    source_cache=cache,
                )
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
_BANNER = "=" * 78


def _short(algorithm: str) -> str:
    match = _SHORT.match(algorithm)
    return match.group(1) if match else algorithm


def _shorts(algorithms: frozenset[str] | Iterable[str]) -> str:
    return ",".join(_short(value) for value in sorted(algorithms)) or "-"


def _elide(measurement: str) -> str:
    if not measurement:
        return "-"
    return measurement[:19] + "…" if len(measurement) > 20 else measurement


def derivation_report(derivation: WriteDerivation) -> str:
    """What the two derivations found, before either path is chosen.

    Printed on both paths, so the silent run still shows its work: a reader can
    see which entry point produced which format rather than a bare set.
    """
    lines = [
        f"Called {name}: wrote {_shorts(formats)}" for name, formats in derivation.by_entry_point
    ]
    if derivation.syntactic != derivation.behavioural:
        lines.append(
            f"Derivations differ: calling this tree wrote {_shorts(derivation.behavioural)}; "
            f"reading it found {_shorts(derivation.syntactic)}. The union "
            f"({_shorts(derivation.formats)}) is what this check uses."
        )
    if derivation.rehydration_sites:
        lines.append(
            "Not read as writes (an envelope rebuilt from a stored mapping): "
            + ", ".join(derivation.rehydration_sites)
        )
    return "\n".join(lines)


def unchanged_report(written: frozenset[str]) -> str:
    """The silent path's whole output.

    It states what was compared and what was NOT done, because "this run
    contacted no enclave" is the fact a reader needs in order to know what this
    green tick is worth.
    """
    return "\n".join(
        (
            f"This tree writes {_shorts(written)}, which is BASELINE_WRITTEN_FORMATS in",
            "scripts/check_format_ordering.py -- the format set recorded as already readable",
            "by every enclave serving every cloud (docs/design/byok-aad-v2-migration.md",
            "section 4.1). This change does not alter what gets written, so no release record",
            "was fetched, no attestation endpoint was contacted, and no enclave source was",
            "read. Nothing about the CURRENT state of any cloud was checked here, and nothing",
            "about it is claimed.",
        )
    )


def changed_banner(written: frozenset[str]) -> str:
    return "\n".join(
        (
            "",
            _BANNER,
            "THIS CHANGE ALTERS THE WRITTEN ENVELOPE FORMAT SET: "
            f"{_shorts(BASELINE_WRITTEN_FORMATS)} -> {_shorts(written)}.",
            "Checking it against every enclave serving every cloud. This is the one path that",
            "reads the network, and it fails closed: any region that blocks, and any cloud",
            "whose declaration cannot be resolved, fails this check.",
            _BANNER,
        )
    )


def render(results: Sequence[RegionResult], written: frozenset[str]) -> str:
    rows = [("CLOUD", "REGION", "MEASUREMENT", "COMMIT", "ACCEPTS", "WRITES", "")]
    for result in results:
        rows.append(
            (
                result.cloud,
                result.host,
                # "-" means NOT READ. A short measurement is still a measurement:
                # printing it as "-" would make a row that was checked look
                # identical to one that was not, in the table an author reads
                # at the moment a check goes red.
                _elide(result.measurement),
                result.source_commit or "-",
                _shorts(result.accepts),
                _shorts(written),
                "ok" if result.ok else "BLOCKED",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    ]
    for result in results:
        for problem in result.problems:
            lines.append(f"\nBLOCKED {result.cloud}/{result.host}: {problem}")
    return "\n".join(lines)


def verdict(results: Sequence[RegionResult], written: frozenset[str], clouds: Sequence[str]) -> str:
    """The line the author reads. Never a pass when anything blocked."""
    blocked = [result for result in results if not result.ok]
    if blocked:
        return (
            f"\n{_BANNER}\n"
            f"BLOCKED: {len(blocked)} serving region(s) refuse this format change. A control\n"
            "plane may only write envelope formats every enclave serving its cloud already\n"
            "reads; see docs/design/byok-aad-v2-migration.md section 4.1.\n"
            f"{_BANNER}"
        )
    # Says what was checked, not what is true of the world. Every measurement
    # each record accepts was observed live (check_plane refuses otherwise) and
    # every one mapped to a commit whose generated declaration covers what this
    # build writes. An enclave serving this cloud that appears in neither the
    # record's regions nor its accepted set is outside what any of this saw.
    return "\n".join(
        (
            "",
            f"{len(results)} serving region(s) checked for {', '.join(clouds)}; every "
            "measurement those records accept was observed",
            "live, and each of those enclaves declares it accepts every format this build "
            f"writes ({_shorts(written)}).",
            f"Now set BASELINE_WRITTEN_FORMATS to {{{', '.join(repr(v) for v in sorted(written))}}}"
            " in scripts/check_format_ordering.py,",
            "in this same pull request. Until you do, tests/test_check_format_ordering.py::",
            "test_the_baseline_matches_what_this_tree_writes fails, and every later pull "
            "request takes",
            "this same network path.",
        )
    )


def did_not_run_report(reason: str) -> str:
    """What the author sees when the write side could not be derived.

    Printed as loudly as a block. No cloud was checked, and — the part that
    matters here — neither was the question this program routes on, so it could
    not take its silent path either.
    """
    return (
        f"\n{_BANNER}\n"
        f"FAILED, and not by a cloud: {reason}\n"
        "No release record was read and no enclave was contacted. This says nothing about\n"
        "whether the ordering holds; it says the formats this tree writes could not be\n"
        "derived, so this program cannot tell a format change from an unrelated edit.\n"
        "That is a defect in quill-router and it is fixed in quill-router: the message\n"
        "above names the file and line.\n"
        f"{_BANNER}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 when the written format set is unchanged or fully verified; 1 otherwise.

    Three outcomes, and the routing between them is the whole design:

    * the write side cannot be derived -> `did_not_run_report`, exit 1. Unknown
      is not unchanged, so the silent path is not available here.
    * the derived set equals BASELINE_WRITTEN_FORMATS -> `unchanged_report`,
      exit 0, having performed no network I/O.
    * the derived set differs -> fetch every published record, check every
      region of every requested cloud, exit 1 unless all of them cleared.

    Only `OSError` and `ValueError` out of the derivation are turned into
    `did_not_run_report`; those are what `read_write_surface` and
    `derive_written_formats` raise on the conditions they refuse. Anything else
    -- an `ImportError` from a tree that will not import, a bug in this file --
    propagates as a traceback, which also exits non-zero. Both are refusals;
    only one of them is a readable one, and that is stated rather than implied.

    There is no report-only mode and no `--mode` flag. Both existed because the
    earlier version of this program was a non-skippable dependency of the deploy
    job and an inline call in two hand-run cloud scripts, where a refusal stopped
    releases on three clouds. Here a refusal fails one pull request, in front of
    the author of the change that caused it.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane", default=DEFAULT_CONTROL_PLANE)
    parser.add_argument(
        "--cloud",
        action="append",
        choices=sorted(PLANES),
        help=(
            "cloud to check on the loud path; repeatable. Default: all of them, which is what "
            "CI runs. A format change is a change to what EVERY cloud's control plane will "
            "write once this merges, so narrowing this is for investigating one cloud by hand, "
            "not for landing a change."
        ),
    )
    args = parser.parse_args(argv)
    clouds = args.cloud or sorted(PLANES)

    try:
        derivation = derive_written_formats(read_write_surface())
    except (OSError, ValueError) as exc:
        print(
            did_not_run_report(f"cannot determine what this build writes: {exc}"), file=sys.stderr
        )
        return 1

    written = derivation.formats
    print(derivation_report(derivation))
    if written == BASELINE_WRITTEN_FORMATS:
        print(unchanged_report(written))
        return 0

    print(changed_banner(written))
    results = gather(
        args.control_plane,
        clouds,
        written,
        records=lambda path: fetch_record(args.control_plane, path),
        transport=NetworkTransport(),
        source=fetch_enclave_file,
        list_package=fetch_enclave_package_files,
    )
    print(render(results, written))
    print(verdict(results, written, clouds))
    return 1 if any(not result.ok for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
