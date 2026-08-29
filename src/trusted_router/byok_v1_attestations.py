"""Fleet-wide, machine-checkable evidence that no v1 BYOK envelope remains.

THE LAW
    Step 4 of `docs/design/byok-aad-v2-migration.md` — deleting v1 envelope
    support — is permitted only when **every** standalone cloud deployment has
    a recorded, *passing* zero-v1 attestation whose surface fingerprint equals
    the set of encrypted surfaces this repository writes today.

    Nothing here removes v1. This module is the precondition and the record;
    `tests/test_byok_v1_removal_gate.py` is the thing that refuses.

WHY EVERY CLOUD, AND NOT EACH CLOUD ON ITS OWN
    Each cloud is a standalone TrustedRouter with its own database, so each one
    needs its own run. That much was always in the plan. What was written down
    wrongly — §4.0 point 1 of the migration doc said a v2 envelope written by
    one cloud's control plane "never reaches another cloud's enclave" — is that
    the deployments are *isolated*. They are not. Verified 2026-08-15 in
    `quill-cloud-proxy`:

      tools/deploy-aws-nitro.sh:888
        QUILL_TR_CONTROL_PLANE_BASE_URL=https://aws.trustedrouter.com/v1,
                                        https://trustedrouter.com/v1
      tools/deploy-azure-aci.sh:269
        TR_CONTROL_PLANE_BASE_URL=https://azure.trustedrouter.com/v1,
                                  https://trustedrouter.com/v1
      tools/deploy-gcp-mig.sh:208
        TR_CONTROL_PLANE_BASE_URL=https://trustedrouter.com
      enclave-go/internal/trustedrouter/client.go:41-44, 789-826
        "baseURLs is ordered: index 0 is this cloud's OWN control plane, later
         entries are fallbacks used only when an earlier one cannot be dialled."
        `postToFirstDialable` walks that list, and `Authorization` carries
        `byok_encrypted_secret` (client.go:216) — so the envelope an enclave
        decrypts comes from whichever control plane answered.

    So an AWS or Azure enclave whose own control plane cannot be dialled falls
    over to the home (GCP) plane and is handed envelopes out of the GCP
    database. Dropping v1 read support on one cloud while another cloud still
    stores v1 envelopes therefore breaks BYOK on that cloud **during an
    outage** — the worst possible moment and the hardest to attribute.

    `ENCLAVE_CONTROL_PLANE_SOURCES` below records that topology, and the set of
    clouds that must attest is derived from it rather than asserted. Today the
    union is all three, which is why the requirement looks the same as before;
    it now has the right reason underneath it, and adding a fourth deployment
    with its own fallback list changes the requirement automatically.

WHY THIS EXISTS AS A LEDGER AND NOT AS A SENTENCE
    Until now the only artifact standing between an engineer and permanently
    unreadable customer BYOK keys was a table cell in a markdown file. Step 4
    is the one intentionally one-way production step in the plan: once
    `_aad`/`ALGORITHM` are gone from the control plane and enclave, a V1 row
    cannot be opened by the running fleet. Recovery would require an isolated
    rollback to reviewed pre-Step-4 code (or equivalent dedicated recovery
    tooling) with KMS access. A markdown claim is not a precondition. A file
    that a test reads is.

THE NEAR-MISS THIS ENCODES
    As of 2026-08-15 the migration doc renders step 3 as a green check on all
    three clouds. Two of those three checks are a *read-only audit that found
    no rows to rewrite* — AWS and Azure. "No rows existed" and "the backfill
    ran and finished" are different sentences with very different failure
    modes, and they were rendering identically. In particular, a run that
    returns nothing because the cursor was wrong, the table name was wrong, or
    the credentials were wrong renders exactly like a deployment with no BYOK
    customers. So the outcomes here are deliberately not a boolean:

    * `clean`            — envelopes were seen, every one of them was v2, and
                           no row anywhere in the table carries the v1
                           algorithm literal.
    * `empty_witnessed`  — no envelope of any format was seen, the census
                           proved the table was reachable and non-empty, and a
                           search that shares neither the scan's kind filter
                           nor its field-name map found no row anywhere in the
                           table carrying the v1 algorithm literal.
    * `zero_scan`        — no envelopes were seen and nothing corroborates that
                           the scan could have seen any. **Never a pass, never
                           recordable.** This is the AWS/Azure shape.
    * `scan_disagrees_with_census` — the census counted rows of a migrated kind
                           that the scan did not return, or counted rows
                           carrying the v1 literal that the scan did not
                           classify as v1. Bad cursor, bad kind filter, a
                           renamed body field, or a mid-run delete. **Never a
                           pass.**
    * `v1_remains`, `dirty` — the audit found v1 rows, or found rows it could
                           not classify. **Never a pass.**

    The literal census is what makes the last two of those reachable. An
    earlier revision of this module restricted both the scan's WHERE clause and
    the census's WHERE clause to the same migrated kind list, and read
    envelopes only out of the field names in `MIGRATED_SURFACES` — so a renamed
    entity kind or a renamed body field hid the same rows from both halves and
    produced a corroborated-looking `empty_witnessed` over live v1 envelopes.
    The literal search assumes neither.

SCOPE LIMIT — what a full ledger does NOT establish
    * It does not prove the enclave side is ready. `quill-cloud-proxy` has its
      own v1 branch and its own removal decision; this repo cannot see it.
    * **It cannot tell a right database from a wrong-but-populated one.** A
      credential pointed at some other project's `tr_entities` — one that is
      reachable, non-empty, and holds no v1 envelope — produces a genuine
      `empty_witnessed`. Nothing offline can close that. What is done instead
      is to record `census_source` — which database the census was taken from
      — in the attestation, so the mismatch is visible to a reviewer rather
      than invisible. Compare it against the cloud the entry claims.

      Read that field knowing how each adapter obtains it, because they are
      not equally strong. `PostgresEntityStore` asks the server for
      `current_database()` and `current_user`, then obtains host and port from
      the negotiated connection (Aurora DSQL does not implement
      `inet_server_addr()`). `SpannerEntityStore` does not: it
      composes the name client-side out of the `--project`,
      `--spanner-instance` and `--spanner-database` the CLI was given —
      `Database.name` is `Instance.name + "/databases/" + database_id` and
      issues no RPC — so it records the database that was ADDRESSED and would
      read identically against a local emulator. Its value says so in the
      string.
      GCP is the Spanner deployment and the home plane every cloud fails over
      to, so this is the weaker half exactly where it matters most; treat a
      GCP `census_source` as a restatement of the operator's arguments, not as
      evidence, and corroborate it against the run's own environment.
    * **A row that only mentions the v1 literal blocks the cloud.**
      `census_v1_literal_rows` counts rows whose body TEXT contains
      `TR-BYOK-ENVELOPE-AES-256-GCM-V1` — any row, any kind, any field. A
      captured upstream error string or a stored audit line containing that
      name is counted, produces `scan_disagrees_with_census`, and keeps the
      cloud unattestable until someone removes or rewrites the row. This is
      fail-closed and the outcome message says where to look, but it is a real
      way for a deployment holding no v1 envelope to be unable to attest. The
      match is deliberately not narrowed to a JSON-shaped one
      (`"algorithm":"TR-…"`), because the two backends do not render the same
      bytes: Spanner stores `body` as a STRING written by
      `storage_codec.json_body`, `separators=(",", ":")`, so there is no space
      after the colon; Postgres stores `body` as JSONB
      (`storage_postgres_schema.sql:4`) and `::text` re-renders it with one. A
      pattern tuned to either one silently stops matching on the other, and a
      literal search that stops matching fails OPEN — the one direction this
      module must never fail in.
    * The census is issued through the same store object as the scan, so it
      shares the table name. It does NOT prove `tr_entities` is where envelopes
      actually live, and it cannot see a surface that no code path here knows
      about (open question #2 in the migration doc). The fingerprint below is
      the partial answer: adding a surface invalidates every attestation.
    * An attestation is a statement about one moment. It does not prevent a v1
      envelope from being written afterwards — nothing writes v1 any more, but
      that is a property of the code, not of this file.
    * `empty_witnessed` is the weaker of the two passes and is recorded under
      its own name precisely so that a reviewer can see which clouds were
      attested by observation and which by absence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The standalone deployments. Each is its own TrustedRouter with its own
#: database — see docs/storage-portability/multi-cloud-separation.md — so each
#: has its own BYOK envelopes and needs its own attestation. Attesting the one
#: cloud you happened to have credentials for is exactly the mistake this
#: tuple exists to make impossible.
STANDALONE_CLOUDS: tuple[str, ...] = ("aws", "azure", "gcp")

#: For each cloud's ENCLAVE, the clouds whose control plane — and therefore
#: whose database — can serve it a BYOK envelope. Index 0 is the cloud's own
#: plane; later entries are dial-failure fallbacks. `"gcp"` is the home plane at
#: `https://trustedrouter.com`, which is the deployment the GCP enclave is
#: pointed at by `tools/deploy-gcp-mig.sh:208`.
#:
#: Transcribed by hand from `quill-cloud-proxy` on 2026-08-15; the citations are
#: in this module's docstring. **Nothing here re-reads that repository**, so
#: this dict is a claim about a build that was deployed, not a measurement of
#: the one running now. If a deploy script's list changes, this changes with it
#: or the requirement below is wrong.
#:
#: The point of writing it down is that it is the reason step 4 is a fleet-wide
#: decision rather than three independent ones: a cloud that drops v1 reads can
#: still be handed a v1 envelope out of a *different* cloud's database the next
#: time its own control plane is undialable.
ENCLAVE_CONTROL_PLANE_SOURCES: dict[str, tuple[str, ...]] = {
    "aws": ("aws", "gcp"),
    "azure": ("azure", "gcp"),
    "gcp": ("gcp",),
}


def clouds_that_must_attest() -> tuple[str, ...]:
    """Every cloud whose stored envelopes can reach *some* cloud's enclave.

    The union of `STANDALONE_CLOUDS` and every entry in
    `ENCLAVE_CONTROL_PLANE_SOURCES` — both directions, so that neither table
    can weaken the requirement by omission. "All three" is a consequence of the
    failover topology rather than an axiom: v1 support is one codebase deployed
    everywhere, and the enclaves cross-read, so the removal is permitted only
    when every database any enclave can be served from is clear.

    Today this returns exactly `STANDALONE_CLOUDS`. A deployment added to
    either table alone is still required, which is the safe direction; the
    unsafe direction — a fourth cloud that reaches nobody's tables — is why the
    union includes `STANDALONE_CLOUDS` outright.
    """
    required = set(STANDALONE_CLOUDS)
    required.update(ENCLAVE_CONTROL_PLANE_SOURCES)
    required.update(
        cloud for sources in ENCLAVE_CONTROL_PLANE_SOURCES.values() for cloud in sources
    )
    return tuple(cloud for cloud in STANDALONE_CLOUDS if cloud in required) + tuple(
        sorted(cloud for cloud in required if cloud not in STANDALONE_CLOUDS)
    )


#: Every encrypted surface the backfill knows how to walk: (entity kind, body
#: field, AAD namespace family). This is the single source of truth — the
#: backfill's field map is derived from it — so adding a fourth surface here
#: changes `surface_fingerprint()` and thereby invalidates every recorded
#: attestation, which is the intended behaviour: the old runs did not look at
#: the new surface.
MIGRATED_SURFACES: tuple[tuple[str, str, str], ...] = (
    ("broadcast_destination", "encrypted_api_key", "control"),
    ("broadcast_destination", "encrypted_headers", "control"),
    ("byok", "encrypted_secret", "provider"),
    # User-provided model owner secrets. They are v2 from birth — the
    # `user_model` namespace was added after the split and its decryptor
    # refuses a v1 envelope — so the backfill can never have work here. They
    # are listed anyway because this tuple is what makes "the audit walked
    # every encrypted surface" true: a surface left out is a surface an
    # attestation silently did not cover.
    ("user_provided_model", "encrypted_endpoint_api_key", "user_model"),
    ("user_provided_model", "encrypted_signing_secret", "user_model"),
)

MIGRATED_KINDS: tuple[str, ...] = tuple(
    sorted({kind for kind, _field, _family in MIGRATED_SURFACES})
)

#: The v1 `algorithm` value as it is written into stored envelope bodies.
#:
#: Pinned here rather than imported from `byok_crypto.ALGORITHM` on purpose.
#: The census below searches row bodies for this string, and it has to keep
#: working in the tree where step 4 has deleted that constant — a precondition
#: that stops being expressible the moment someone starts the edit it guards is
#: not a precondition. Post-Step-4 tests keep this literal confined to the
#: read-only census and explicit rejection fixtures.
V1_ALGORITHM_LITERAL = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"

#: v3: `census_positive_control_rows` became required so a zero literal count
#: cannot pass until the same whole-body search path demonstrates that it can
#: match a known-present JSON-object marker. Older runs never asked that
#: question and are deliberately not upgradable by changing the schema string.
SCHEMA = "trustedrouter/byok-aad-v1-zero-attestation/v3"

OUTCOME_CLEAN = "clean"
OUTCOME_EMPTY_WITNESSED = "empty_witnessed"
OUTCOME_V1_REMAINS = "v1_remains"
OUTCOME_ZERO_SCAN = "zero_scan"
OUTCOME_SCAN_DISAGREES = "scan_disagrees_with_census"
OUTCOME_DIRTY = "dirty"

#: The only two outcomes that attest "no v1 envelope remains on this cloud".
PASSING_OUTCOMES = frozenset({OUTCOME_CLEAN, OUTCOME_EMPTY_WITNESSED})

ALL_OUTCOMES = frozenset(
    {
        OUTCOME_CLEAN,
        OUTCOME_EMPTY_WITNESSED,
        OUTCOME_V1_REMAINS,
        OUTCOME_ZERO_SCAN,
        OUTCOME_SCAN_DISAGREES,
        OUTCOME_DIRTY,
    }
)

#: Committed next to the migration plan it makes executable. Absent in a
#: deployed wheel, where `load_ledger` returns the empty ledger — this is
#: operator and CI tooling, not request-path code.
DEFAULT_LEDGER_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "design" / "byok-aad-v2-attestations.json"
)


def surface_fingerprint() -> str:
    """Stable digest of the encrypted surfaces an attestation must have covered.

    Recorded inside every attestation and re-checked when the ledger is read,
    so that an attestation taken before a new encrypted surface existed cannot
    silently authorise deleting v1 for a surface it never walked.
    """
    digest = hashlib.sha256()
    for kind, field_name, family in sorted(MIGRATED_SURFACES):
        digest.update(f"{kind}\x00{field_name}\x00{family}\x00".encode())
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class Attestation:
    """One cloud's zero-v1 evidence, as recorded by check_no_v1_envelopes.py.

    Every count the audit produced is kept, not just the verdict. The verdict
    is the cheap part; the counts are what lets a reviewer six months later ask
    "did this run actually look at anything?" without re-running it.
    """

    cloud: str
    outcome: str
    recorded_at: str
    backend: str
    surface_fingerprint: str
    rows_scanned: int
    rows_scanned_by_kind: dict[str, int]
    envelopes_seen: int
    v1_envelopes: int
    v2_envelopes: int
    missing_envelopes: int
    census_migrated_kind_counts: dict[str, int]
    census_sampled_kinds: list[str]
    #: Rows anywhere in the table whose body carries `V1_ALGORITHM_LITERAL`,
    #: found without a kind filter and without assuming a body field name. The
    #: one count in here that is not downstream of `MIGRATED_SURFACES`.
    census_v1_literal_rows: int
    #: Rows matched by the same whole-body literal-search path using a
    #: known-present JSON-object marker. Must be positive for any passing run.
    census_positive_control_rows: int
    #: Which database the census was taken from: the server's own answer on
    #: Postgres, the database the CLI was pointed at on Spanner (the string
    #: says which). Not proof of the right deployment; the thing a reviewer
    #: compares against `cloud`. See SCOPE LIMIT in the module docstring.
    census_source: str
    operator: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cloud": self.cloud,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "backend": self.backend,
            "surface_fingerprint": self.surface_fingerprint,
            "rows_scanned": self.rows_scanned,
            "rows_scanned_by_kind": dict(sorted(self.rows_scanned_by_kind.items())),
            "envelopes_seen": self.envelopes_seen,
            "v1_envelopes": self.v1_envelopes,
            "v2_envelopes": self.v2_envelopes,
            "missing_envelopes": self.missing_envelopes,
            "census_migrated_kind_counts": dict(sorted(self.census_migrated_kind_counts.items())),
            "census_sampled_kinds": sorted(self.census_sampled_kinds),
            "census_v1_literal_rows": self.census_v1_literal_rows,
            "census_positive_control_rows": self.census_positive_control_rows,
            "census_source": self.census_source,
            "operator": self.operator,
            "note": self.note,
        }


_REQUIRED_FIELDS = (
    "cloud",
    "outcome",
    "recorded_at",
    "backend",
    "surface_fingerprint",
    "rows_scanned",
    "rows_scanned_by_kind",
    "envelopes_seen",
    "v1_envelopes",
    "v2_envelopes",
    "missing_envelopes",
    "census_migrated_kind_counts",
    "census_sampled_kinds",
    "census_v1_literal_rows",
    "census_positive_control_rows",
    "census_source",
    "operator",
)

#: Fields a hand-edited entry could weaken by writing the right-looking value
#: at the wrong type: `not "0"` is False, so a string count reads as non-zero
#: and a string zero reads as present. Cheap to check, so checked.
_INT_FIELDS = (
    "rows_scanned",
    "envelopes_seen",
    "v1_envelopes",
    "v2_envelopes",
    "missing_envelopes",
    "census_v1_literal_rows",
    "census_positive_control_rows",
)

_COUNT_MAP_FIELDS = ("rows_scanned_by_kind", "census_migrated_kind_counts")
_TEXT_FIELDS = (
    "cloud",
    "outcome",
    "recorded_at",
    "backend",
    "surface_fingerprint",
    "operator",
)


def empty_ledger() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "about": (
            "Recorded output of scripts/check_no_v1_envelopes.py, one entry per standalone "
            "cloud. Read by tests/test_byok_v1_removal_gate.py, which refuses the removal of "
            "v1 BYOK envelope support until EVERY cloud here attests zero v1 envelopes. It is "
            "one fleet-wide verdict, not three: an AWS or Azure enclave falls over to the home "
            "control plane when its own cannot be dialled, so a v1 envelope in any cloud's "
            "database can be handed to any cloud's enclave. An entry asserts that a run "
            "happened against that cloud's database; writing one by hand asserts a run that "
            "did not. The gate can check the shape, not the truth."
        ),
        "attestations": {},
    }


def load_ledger(path: Path | None = None) -> dict[str, Any]:
    """Read the committed ledger. A missing file is the empty ledger.

    A missing file must read as "nothing is attested", never as "nothing
    blocks": deleting the ledger is the cheapest way to fake a green gate, and
    an empty ledger blocks every cloud.
    """
    target = DEFAULT_LEDGER_PATH if path is None else path
    if not target.exists():
        return empty_ledger()
    loaded = json.loads(target.read_text())
    if not isinstance(loaded, dict):
        return {"schema": None, "attestations": {}, "unparsed": True}
    return loaded


def ledger_defects(ledger: dict[str, Any]) -> list[str]:
    """Structural complaints about the ledger itself, in reviewer-readable form.

    Separate from `zero_v1_blockers` because a malformed ledger is a different
    problem from an incomplete one: the first means someone edited this file by
    hand, the second means the work is not finished.
    """
    defects: list[str] = []
    if ledger.get("schema") != SCHEMA:
        defects.append(f"ledger schema is {ledger.get('schema')!r}, expected {SCHEMA!r}")
    attestations = ledger.get("attestations")
    if not isinstance(attestations, dict):
        defects.append("ledger has no 'attestations' object")
        return defects
    fingerprint = surface_fingerprint()
    for cloud in sorted(attestations):
        entry = attestations[cloud]
        if cloud not in STANDALONE_CLOUDS:
            defects.append(f"{cloud}: not a known standalone cloud {STANDALONE_CLOUDS}")
            continue
        if not isinstance(entry, dict):
            defects.append(f"{cloud}: attestation is not an object")
            continue
        missing = [name for name in _REQUIRED_FIELDS if name not in entry]
        if missing:
            defects.append(f"{cloud}: attestation is missing {', '.join(missing)}")
            continue
        mistyped = [
            name
            for name in _INT_FIELDS
            if not isinstance(entry[name], int) or isinstance(entry[name], bool)
        ]
        if mistyped:
            defects.append(f"{cloud}: these counts are not integers: {', '.join(mistyped)}")
            continue
        negative = [name for name in _INT_FIELDS if entry[name] < 0]
        if negative:
            defects.append(f"{cloud}: these counts are negative: {', '.join(negative)}")
            continue
        mistyped_text = [
            name
            for name in _TEXT_FIELDS
            if not isinstance(entry[name], str) or not entry[name].strip()
        ]
        if mistyped_text:
            defects.append(
                f"{cloud}: these fields are not non-empty strings: {', '.join(mistyped_text)}"
            )
            continue
        invalid_count_maps = [
            name
            for name in _COUNT_MAP_FIELDS
            if not isinstance(entry[name], dict)
            or any(
                not isinstance(kind, str)
                or not kind
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
                for kind, count in (
                    entry[name].items() if isinstance(entry[name], dict) else ()
                )
            )
        ]
        if invalid_count_maps:
            defects.append(
                f"{cloud}: these fields are not string-to-nonnegative-integer maps: "
                f"{', '.join(invalid_count_maps)}"
            )
            continue
        sampled_kinds = entry["census_sampled_kinds"]
        if not isinstance(sampled_kinds, list) or any(
            not isinstance(kind, str) or not kind for kind in sampled_kinds
        ):
            defects.append(f"{cloud}: census_sampled_kinds is not a list of non-empty strings")
            continue
        if entry["cloud"] != cloud:
            defects.append(f"{cloud}: attestation records cloud={entry['cloud']!r}")
        if entry["outcome"] not in ALL_OUTCOMES:
            defects.append(f"{cloud}: unknown outcome {entry['outcome']!r}")
        elif entry["outcome"] not in PASSING_OUTCOMES:
            # Recording a non-passing run is not "extra evidence", it is a
            # green-looking row for a run that established nothing.
            defects.append(
                f"{cloud}: outcome {entry['outcome']!r} is not an attestation and must not be "
                "recorded here"
            )
        if entry["surface_fingerprint"] != fingerprint:
            defects.append(
                f"{cloud}: attestation surface fingerprint "
                f"{entry['surface_fingerprint']} does not match this repository's "
                f"{fingerprint} — re-run the precondition"
            )
        if entry["outcome"] == OUTCOME_CLEAN and not entry["envelopes_seen"]:
            defects.append(f"{cloud}: outcome 'clean' with envelopes_seen=0 is self-contradictory")
        if entry["outcome"] == OUTCOME_EMPTY_WITNESSED:
            if not entry["census_sampled_kinds"]:
                defects.append(
                    f"{cloud}: outcome 'empty_witnessed' with no census witness is a zero scan"
                )
            if entry["envelopes_seen"]:
                defects.append(
                    f"{cloud}: outcome 'empty_witnessed' with "
                    f"envelopes_seen={entry['envelopes_seen']} is self-contradictory"
                )
        if entry["v1_envelopes"]:
            defects.append(f"{cloud}: attestation records v1_envelopes={entry['v1_envelopes']}")
        # The structural half of the run-time check. A passing run that saw no
        # v1 envelope while the literal census could see rows carrying the v1
        # marker is the renamed-kind / renamed-field case: the scan was blind,
        # not the deployment empty. check_no_v1_envelopes refuses to produce
        # this; the reader has to catch what a hand edit could still write.
        if entry["census_v1_literal_rows"]:
            defects.append(
                f"{cloud}: attestation records census_v1_literal_rows="
                f"{entry['census_v1_literal_rows']} — rows carrying the v1 algorithm literal "
                "were counted in the table, so this run did not establish zero v1"
            )
        if entry["census_positive_control_rows"] <= 0:
            defects.append(
                f"{cloud}: attestation records census_positive_control_rows="
                f"{entry['census_positive_control_rows']} — the whole-body literal search "
                "never demonstrated that it could match"
            )
        if not isinstance(entry["census_source"], str) or not entry["census_source"].strip():
            defects.append(
                f"{cloud}: attestation has no census_source, so nobody can check which "
                "database was read"
            )
    return defects


def zero_v1_blockers(ledger: dict[str, Any]) -> list[str]:
    """Why step 4 may not proceed yet. Empty list means every cloud attests zero v1.

    The required set comes from `clouds_that_must_attest()`, i.e. from the
    enclave failover topology, so this is a single fleet-wide verdict and never
    a per-cloud one. There is deliberately no "is cloud X clear?" function to
    call by mistake: an AWS enclave that has dropped v1 reads is broken by a v1
    envelope sitting in the GCP database, so "AWS is clear" is not a claim that
    authorises anything on its own.

    Returns sentences rather than a set of clouds because the caller is a CI
    failure message, and "aws" on its own tells the reader nothing about what
    to run.
    """
    blockers = list(ledger_defects(ledger))
    attestations = ledger.get("attestations")
    if not isinstance(attestations, dict):
        attestations = {}
    for cloud in clouds_that_must_attest():
        entry = attestations.get(cloud)
        if not isinstance(entry, dict) or "outcome" not in entry:
            blockers.append(
                f"{cloud}: no zero-v1 attestation recorded — run "
                f"scripts/check_no_v1_envelopes.py --cloud {cloud} --record against that "
                "deployment's database"
            )
            continue
        if entry["outcome"] not in PASSING_OUTCOMES:
            blockers.append(
                f"{cloud}: recorded outcome {entry['outcome']!r} does not attest zero v1"
            )
    return blockers


def record_attestation(
    attestation: Attestation,
    *,
    path: Path | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one cloud's attestation into the ledger, refusing anything unearned.

    Fails closed on every input the gate would later have to reject: an unknown
    cloud, a non-passing outcome, a stale surface fingerprint. The refusal
    lives here as well as in the gate so that an operator finds out at the
    moment of the run, not in someone else's CI three weeks later.
    """
    if attestation.cloud not in STANDALONE_CLOUDS:
        raise ValueError(
            f"unknown cloud {attestation.cloud!r}; expected one of {STANDALONE_CLOUDS}"
        )
    if attestation.outcome not in PASSING_OUTCOMES:
        raise ValueError(
            f"refusing to record outcome {attestation.outcome!r}: only "
            f"{sorted(PASSING_OUTCOMES)} attest that no v1 envelope remains"
        )
    if attestation.surface_fingerprint != surface_fingerprint():
        raise ValueError("refusing to record an attestation for a different set of surfaces")
    if attestation.census_v1_literal_rows:
        raise ValueError(
            f"refusing to record a pass with census_v1_literal_rows="
            f"{attestation.census_v1_literal_rows}: rows carrying the v1 algorithm literal were "
            "counted in that table"
        )
    if attestation.census_positive_control_rows <= 0:
        raise ValueError(
            "refusing to record an attestation whose whole-body literal-search positive "
            "control matched no rows"
        )
    if not attestation.census_source.strip():
        raise ValueError("refusing to record an attestation that does not name the database read")
    target = DEFAULT_LEDGER_PATH if path is None else path
    current = load_ledger(target) if ledger is None else ledger
    if current.get("schema") != SCHEMA:
        current = empty_ledger()
    attestations = current.get("attestations")
    if not isinstance(attestations, dict):
        attestations = {}
    attestations[attestation.cloud] = attestation.to_dict()
    current["attestations"] = {
        cloud: attestations[cloud] for cloud in STANDALONE_CLOUDS if cloud in attestations
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(current, indent=2, sort_keys=False) + "\n")
    return current


def utc_now(clock: Callable[[], datetime] | None = None) -> str:
    now = datetime.now(UTC) if clock is None else clock()
    return now.astimezone(UTC).isoformat(timespec="seconds")
