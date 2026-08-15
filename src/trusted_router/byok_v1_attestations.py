"""Per-cloud, machine-checkable evidence that no v1 BYOK envelope remains.

THE LAW
    Step 4 of `docs/design/byok-aad-v2-migration.md` — deleting v1 envelope
    support — is permitted only when **every** standalone cloud deployment has
    a recorded, *passing* zero-v1 attestation whose surface fingerprint equals
    the set of encrypted surfaces this repository writes today.

    Nothing here removes v1. This module is the precondition and the record;
    `tests/test_byok_v1_removal_gate.py` is the thing that refuses.

WHY THIS EXISTS AS A LEDGER AND NOT AS A SENTENCE
    Until now the only artifact standing between an engineer and permanently
    unreadable customer BYOK keys was a table cell in a markdown file. Step 4
    is the one irreversible step in the plan: once `_aad`/`ALGORITHM` are gone
    from the control plane and the enclave, a surviving v1 row is not a bug
    that can be rolled back — the plaintext provider key is gone, because the
    AAD that seals both the ciphertext and the wrapped DEK cannot be
    reconstructed from a format nobody implements any more. A markdown claim is
    not a precondition. A file that a test reads is.

THE NEAR-MISS THIS ENCODES
    As of 2026-08-15 the migration doc renders step 3 as a green check on all
    three clouds. Two of those three checks are a *read-only audit that found
    no rows to rewrite* — AWS and Azure. "No rows existed" and "the backfill
    ran and finished" are different sentences with very different failure
    modes, and they were rendering identically. In particular, a run that
    returns nothing because the cursor was wrong, the table name was wrong, or
    the credentials were wrong renders exactly like a deployment with no BYOK
    customers. So the outcomes here are deliberately not a boolean:

    * `clean`            — envelopes were seen and every one of them was v2.
    * `empty_witnessed`  — no envelopes were seen, AND an independently shaped
                           census of the same table proved it was reachable,
                           non-empty, and held zero rows of the migrated kinds.
    * `zero_scan`        — no envelopes were seen and nothing corroborates that
                           the scan could have seen any. **Never a pass, never
                           recordable.** This is the AWS/Azure shape.
    * `scan_disagrees_with_census` — the census counted rows of a migrated kind
                           that the scan did not return. Bad cursor, bad
                           filter, or a mid-run delete. **Never a pass.**
    * `v1_remains`, `dirty` — the audit found v1 rows, or found rows it could
                           not classify. **Never a pass.**

SCOPE LIMIT — what a full ledger does NOT establish
    * It does not prove the enclave side is ready. `quill-cloud-proxy` has its
      own v1 branch and its own removal decision; this repo cannot see it.
    * The census is issued through the same store object as the scan, so it
      shares the table name. It corroborates reachability, credentials, and the
      scan's cursor/filter — it does NOT prove `tr_entities` is where envelopes
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
)

MIGRATED_KINDS: tuple[str, ...] = tuple(
    sorted({kind for kind, _field, _family in MIGRATED_SURFACES})
)

SCHEMA = "trustedrouter/byok-aad-v1-zero-attestation/v1"

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
    census_migrated_kind_counts: dict[str, int]
    census_sampled_kinds: list[str]
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
            "census_migrated_kind_counts": dict(sorted(self.census_migrated_kind_counts.items())),
            "census_sampled_kinds": sorted(self.census_sampled_kinds),
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
    "census_migrated_kind_counts",
    "census_sampled_kinds",
    "operator",
)


def empty_ledger() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "about": (
            "Recorded output of scripts/check_no_v1_envelopes.py, one entry per standalone "
            "cloud. Read by tests/test_byok_v1_removal_gate.py, which refuses the removal of "
            "v1 BYOK envelope support until every cloud here attests zero v1 envelopes. An "
            "entry asserts that a run happened against that cloud's database; writing one by "
            "hand asserts a run that did not. The gate can check the shape, not the truth."
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
                f"{cloud}: attestation covers surfaces {entry['surface_fingerprint']} but this "
                f"repository now writes {fingerprint} — re-run the precondition"
            )
        if entry["outcome"] == OUTCOME_CLEAN and not entry["envelopes_seen"]:
            defects.append(f"{cloud}: outcome 'clean' with envelopes_seen=0 is self-contradictory")
        if entry["outcome"] == OUTCOME_EMPTY_WITNESSED and not entry["census_sampled_kinds"]:
            defects.append(
                f"{cloud}: outcome 'empty_witnessed' with no census witness is a zero scan"
            )
        if entry["v1_envelopes"]:
            defects.append(f"{cloud}: attestation records v1_envelopes={entry['v1_envelopes']}")
    return defects


def zero_v1_blockers(ledger: dict[str, Any]) -> list[str]:
    """Why step 4 may not proceed yet. Empty list means every cloud attests zero v1.

    Returns sentences rather than a set of clouds because the caller is a CI
    failure message, and "aws" on its own tells the reader nothing about what
    to run.
    """
    blockers = list(ledger_defects(ledger))
    attestations = ledger.get("attestations")
    if not isinstance(attestations, dict):
        attestations = {}
    for cloud in STANDALONE_CLOUDS:
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
