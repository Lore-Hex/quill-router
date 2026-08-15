"""The step-4 precondition: "no v1 envelopes remain" must be earned, per cloud.

THE LAW
    For each standalone cloud deployment, `check_no_v1_envelopes` reports that
    the deployment holds no v1 BYOK envelope only when the audit (a) found zero
    v1 envelopes, (b) is corroborated by a census of the same table showing the
    scan could have seen rows if any existed, and (c) is corroborated by a
    search for the v1 algorithm literal that shares neither the scan's kind
    filter nor its field-name map. Every other shape — v1 rows found, rows it
    could not classify, a scan that disagrees with either census question, a
    scan with nothing behind it — is its own named outcome and none of them may
    be written into the attestation ledger.

    And the ledger is read as one fleet-wide verdict, never per cloud: an AWS
    or Azure enclave falls over to the home control plane when its own cannot
    be dialled, so a v1 envelope in any database can reach any enclave. See
    `trusted_router.byok_v1_attestations`, "WHY EVERY CLOUD".

WHY THIS IS A PROOF AND NOT A TEST
    Step 4 of docs/design/byok-aad-v2-migration.md deletes `_aad` and
    `ALGORITHM`. After that, a surviving v1 row is not recoverable: the AAD
    that seals both the ciphertext and the KMS-wrapped DEK cannot be rebuilt
    from a format nobody implements. The precondition for an irreversible step
    has to be executable, because the cost of it being wrong is a customer's
    provider key, and the thing it replaces — a green check in a markdown
    table — cannot be re-run, cannot be dated, and cannot say what it looked at.

THE REAL NEAR-MISS
    The migration doc, as of 2026-08-15, shows step 3 complete on GCP, AWS and
    Azure. GCP migrated 7 envelopes. AWS and Azure were **read-only audits that
    found no rows to rewrite**, and both render in that table as the same green
    check as GCP's. "No rows existed" and "the backfill ran and finished" are
    different sentences. Worse, "no rows existed" is also what a wrong cursor,
    a wrong table, a renamed entity kind, or a credential scoped to the wrong
    project produces — silently, with exit code 0. The audit cannot tell you
    whether it looked at anything, because from inside the audit those cases
    are identical.

    Of those four, this file makes three impossible to commit as a pass: a
    wrong cursor and a wrong ordering (the per-kind census), a renamed kind or
    a renamed body field (the v1 literal census). The fourth — a credential on
    a wrong but populated database — is NOT caught, is not catchable from here,
    and is named as a scope limit below rather than papered over. An earlier
    revision of this docstring claimed all four.

SCOPE LIMIT — what these tests do NOT establish
    * They exercise the classification and the ledger, against in-memory
      stores. They do not exercise Spanner or Postgres SQL. `census()` on
      either real adapter is unproven here — including the two new queries, the
      `STRPOS`/`LIKE` literal count and the `source` lookup. A census that
      always returned an empty result would fail closed (zero_scan), not open.
    * **A wrong-but-populated database passes.** Point a read-only credential
      at some other project's `tr_entities`: it is reachable, non-empty, and
      holds no v1 envelope, which is exactly what success looks like. Nothing
      offline distinguishes them. What the run does instead is record
      `census_source` — which database the census was taken from — so a
      reviewer can compare it against the cloud the entry claims. On Postgres
      that is the server's own answer to "which database am I?"; on Spanner it
      is only the database the CLI was pointed at, composed client-side with no
      RPC, and GCP is the Spanner deployment. The value says which it is. These
      tests use a Postgres-shaped `SOURCE` string and therefore prove nothing
      about either adapter's value; see `byok_v1_attestations`, SCOPE LIMIT.
    * **A row that only mentions the v1 literal makes a cloud unattestable.**
      The literal census is a text search over whole bodies, so a stored
      upstream error string containing `TR-BYOK-ENVELOPE-AES-256-GCM-V1` is
      counted like an envelope and blocks the cloud until it is dealt with.
      Fail-closed, deliberate, and covered by
      `test_a_row_that_only_mentions_the_v1_literal_blocks_the_cloud` below,
      which exists so that the cost is visible rather than discovered at 2am.
    * A passing precondition is about one moment on one cloud's database. It
      says nothing about the enclave side (`quill-cloud-proxy` has its own v1
      branch), and nothing about a surface no code path in this repo knows
      about — though the literal census does see a v1 envelope stored under a
      field name this repo has never heard of, which is most of that risk.
    * `empty_witnessed` rests on the census reaching the same table as the
      scan through the same store object. It does NOT prove `tr_entities` is
      where envelopes live; nothing offline can.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import check_no_v1_envelopes as check_script
from trusted_router.byok_aad_backfill import (
    EntityCensus,
    EntityRow,
    attestation_for,
    check_no_v1_envelopes,
)
from trusted_router.byok_v1_attestations import (
    DEFAULT_LEDGER_PATH,
    ENCLAVE_CONTROL_PLANE_SOURCES,
    MIGRATED_KINDS,
    OUTCOME_CLEAN,
    OUTCOME_DIRTY,
    OUTCOME_EMPTY_WITNESSED,
    OUTCOME_SCAN_DISAGREES,
    OUTCOME_V1_REMAINS,
    OUTCOME_ZERO_SCAN,
    STANDALONE_CLOUDS,
    V1_ALGORITHM_LITERAL,
    Attestation,
    clouds_that_must_attest,
    empty_ledger,
    ledger_defects,
    load_ledger,
    record_attestation,
    surface_fingerprint,
    zero_v1_blockers,
)

V1 = "TR-BYOK-ENVELOPE-AES-256-GCM-V1"
V2 = "TR-BYOK-ENVELOPE-AES-256-GCM-V2"


def _envelope(algorithm: str) -> dict[str, str]:
    """An envelope's shape, not its contents. The audit classifies on
    `algorithm` alone and never decrypts, so real ciphertext would add nothing
    but a KMS dependency to a read-only check."""
    return {
        "algorithm": algorithm,
        "key_ref": "projects/example/cryptoKeys/byok",
        "encrypted_dek": "ZGVr",
        "dek_nonce": "bm9uY2U",
        "ciphertext": "Y2lwaGVy",
        "nonce": "bm9uY2U",
    }


SOURCE = "postgres:trustedrouter@10.0.0.7:5432 as auditor"


class FakeCloudDatabase:
    """One cloud's entity table, modelled the way the real adapters behave.

    The important fidelity here is what the two halves SHARE, because that is
    where an earlier version of these tests lied. Both `scan` and the per-kind
    census restrict to the same kind list, as both real adapters do — the
    per-kind census from `MIGRATED_KINDS` on each, the scan from
    `MIGRATED_KINDS` on `PostgresEntityStore` and from the same two names
    hardcoded in SQL text on `SpannerEntityStore` — and the walk reads
    envelopes only out of the field names in `MIGRATED_SURFACES`. This fixture
    models the shared list, which is the property that matters; it does not
    model Spanner's duplicate copy of it, whose only failure mode is an
    undercount, which is a refusal. So a test cannot make those two
    disagree by renaming a kind — renaming it renames it in both — and any
    fixture that pretends otherwise is testing a decoupling production cannot
    exhibit. `scan_returns_nothing` is kept for the one class where they really
    do diverge in production: a cursor, ordering or pagination bug in the paged
    walk, where the count still sees rows the walk skipped.

    `v1_literal_rows` is computed from the serialised bodies of ALL rows, of any
    kind, which is what `STRPOS(body, …)` / `body::text LIKE` do. That is the
    only question here whose answer does not pass through `MIGRATED_KINDS` or
    `MIGRATED_SURFACES`, and it is why a renamed kind and a renamed body field
    are now catchable at all.
    """

    def __init__(
        self,
        rows: dict[tuple[str, str], dict[str, Any]] | None = None,
        *,
        census: EntityCensus | None = None,
        scan_returns_nothing: bool = False,
        census_raises: Exception | None = None,
    ) -> None:
        self.rows = copy.deepcopy(rows or {})
        self.scan_returns_nothing = scan_returns_nothing
        self.census_raises = census_raises
        self._census = census

    def scan(self, *, after: tuple[str, str] | None, limit: int) -> list[EntityRow]:
        if self.scan_returns_nothing:
            return []
        keys = sorted(
            key for key in self.rows if key[0] in MIGRATED_KINDS and (after is None or key > after)
        )[:limit]
        return [
            EntityRow(
                kind=kind,
                entity_id=entity_id,
                body=copy.deepcopy(self.rows[(kind, entity_id)]),
                original_body=copy.deepcopy(self.rows[(kind, entity_id)]),
            )
            for kind, entity_id in keys
        ]

    def compare_and_swap(self, row: EntityRow, new_body: dict[str, Any]) -> bool:
        raise AssertionError("the precondition must never write")

    def census(self, *, sample_limit: int = 1000) -> EntityCensus:
        if self.census_raises is not None:
            raise self.census_raises
        if self._census is not None:
            return self._census
        counts: dict[str, int] = {}
        for kind, _entity_id in self.rows:
            if kind in MIGRATED_KINDS:
                counts[kind] = counts.get(kind, 0) + 1
        return EntityCensus(
            migrated_kind_counts=counts,
            sampled_kinds=tuple(sorted({kind for kind, _ in self.rows})),
            v1_literal_rows=sum(
                1 for body in self.rows.values() if V1_ALGORITHM_LITERAL in json.dumps(body)
            ),
            source=SOURCE,
        )


def _byok_row(algorithm: str) -> dict[str, Any]:
    return {
        "workspace_id": "11111111-2222-3333-4444-555555555555",
        "provider": "anthropic",
        "encrypted_secret": _envelope(algorithm),
    }


# ------------------------------------------------------ the four verdicts ---


def test_a_migrated_deployment_attests_clean() -> None:
    """The only outcome that attests by observation rather than by absence."""
    store = FakeCloudDatabase(
        {
            ("byok", "a"): _byok_row(V2),
            ("byok", "b"): _byok_row(V2),
        }
    )

    result = check_no_v1_envelopes(store, cloud="gcp", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_CLEAN
    assert result.passed
    assert result.stats.envelopes_seen == 2
    assert result.stats.v2_envelopes == 2
    assert result.stats.rows_scanned_by_kind == {"byok": 2}


def test_a_single_surviving_v1_envelope_blocks_the_cloud() -> None:
    store = FakeCloudDatabase(
        {
            ("byok", "a"): _byok_row(V2),
            ("byok", "b"): _byok_row(V1),
        }
    )

    result = check_no_v1_envelopes(store, cloud="gcp", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_V1_REMAINS
    assert not result.passed
    assert "1 v1 envelopes are still stored" in result.detail


def test_rows_the_audit_cannot_classify_are_not_a_pass() -> None:
    """An unknown algorithm might be a v1 row under a name we do not know.

    Counting it as "not v1" is the assumption that gets a key deleted.
    """
    store = FakeCloudDatabase({("byok", "a"): _byok_row("TR-BYOK-ENVELOPE-AES-256-GCM-V9")})

    result = check_no_v1_envelopes(store, cloud="azure", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_DIRTY
    assert not result.passed


# ------------------------------------- the distinction that motivated this ---


def test_a_zero_scan_is_not_reportable_as_zero_v1() -> None:
    """The AWS/Azure shape, and the whole reason this file exists.

    An empty database and a broken query produce the same BackfillStats:
    rows_scanned=0, v1_envelopes=0. The audit alone therefore reports success
    for a run that established nothing. Here nothing corroborates the scan, so
    the outcome is `zero_scan` — a distinct, loud, unrecordable result.
    """
    store = FakeCloudDatabase({})

    result = check_no_v1_envelopes(store, cloud="aws", reporter=lambda _m: None)

    assert result.stats.v1_envelopes == 0  # the tempting half
    assert result.outcome == OUTCOME_ZERO_SCAN
    assert not result.passed
    assert "zero evidence" in result.detail

    with pytest.raises(ValueError, match="does not attest zero v1"):
        attestation_for(result, backend="postgres", operator="you@lorehex.co")


def test_an_empty_deployment_passes_only_with_a_census_witness() -> None:
    """Same scan result as the test above; different, corroborated conclusion.

    A deployment with no BYOK customers must still be able to reach step 4 or
    the gate becomes something people route around. What makes this a pass is
    not the empty scan — it is the census reaching the same table with the same
    credentials, finding rows of other kinds, and finding no row anywhere in
    the table that carries the v1 algorithm literal.
    """
    store = FakeCloudDatabase(
        {("workspace", "w"): {"name": "acme"}, ("generation", "g"): {"model": "gpt"}},
    )

    result = check_no_v1_envelopes(store, cloud="aws", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_EMPTY_WITNESSED
    assert result.passed
    assert result.outcome != OUTCOME_CLEAN, "an absence is a weaker claim and is named as one"


def test_rows_of_a_migrated_kind_that_hold_no_secret_are_not_a_failure() -> None:
    """The false positive that would have made this check unusable.

    Both broadcast secret fields and the BYOK secret are optional
    (`storage_models.py`), so a destination that posts to an unauthenticated
    webhook is a row of a migrated kind carrying no envelope at all. Blocking
    on that — "rows exist but I recognised no envelope" — would be fail-closed
    and wrong, and a precondition nobody can ever satisfy is a precondition
    somebody deletes. The literal census is what lets this pass safely: those
    rows contain no v1 marker.
    """
    store = FakeCloudDatabase(
        {
            ("broadcast_destination", "d1"): {"workspace_id": "w", "endpoint": "https://x"},
            ("workspace", "w"): {"name": "acme"},
        }
    )

    result = check_no_v1_envelopes(store, cloud="aws", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_EMPTY_WITNESSED
    assert result.stats.missing_envelopes == 2
    assert result.census.migrated_kind_counts == {"broadcast_destination": 1}


def test_a_broken_cursor_is_caught_by_the_per_kind_census() -> None:
    """The one divergence the paged walk and the aggregate count really have.

    The scan returns nothing — a bad cursor, an ordering bug, a page boundary
    mishandled — while the rows are sitting right there. Note what this does
    NOT cover: a renamed kind in the WHERE clause cannot be modelled this way,
    because both queries restrict to the same migrated kind list and a rename
    renames it in both. See the next two tests for the check that does cover it.
    """
    store = FakeCloudDatabase(
        {("byok", "a"): _byok_row(V1)},
        scan_returns_nothing=True,
    )

    result = check_no_v1_envelopes(store, cloud="gcp", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_SCAN_DISAGREES
    assert not result.passed
    assert "census counted 1 rows, the scan returned 0" in result.detail


def test_a_v1_envelope_under_a_renamed_body_field_is_not_an_empty_deployment() -> None:
    """The bug this file was written to prevent, one layer up, reproduced.

    `encrypted_secret` renamed to `secret_envelope` — schema drift, a partial
    refactor, a surface this repository never knew about. The walk reads only
    the field names in `MIGRATED_SURFACES`, so it sees the rows and finds
    nothing in them: `envelopes_seen=0`, and the per-kind census agrees with
    the walk exactly because both were told the same thing. That combination
    used to be reported as `empty_witnessed`, exit 0, recorded — over two live
    v1 envelopes, with `missing_envelopes: 2` printed in the same output.

    "I could not see" is not "there is nothing", and the only question here
    that can tell them apart is the one that does not know any field names.
    """
    store = FakeCloudDatabase(
        {
            ("byok", "a"): {
                "workspace_id": "w",
                "provider": "anthropic",
                "secret_envelope": _envelope(V1),
            },
            ("byok", "b"): {
                "workspace_id": "w",
                "provider": "openai",
                "secret_envelope": _envelope(V1),
            },
            ("workspace", "w"): {"name": "acme"},
        }
    )

    result = check_no_v1_envelopes(store, cloud="aws", reporter=lambda _m: None)

    assert result.stats.envelopes_seen == 0  # the tempting half, again
    assert result.stats.missing_envelopes == 2
    assert result.census.migrated_kind_counts == {"byok": 2}  # and the census agreed
    assert result.outcome == OUTCOME_SCAN_DISAGREES
    assert not result.passed
    assert "2 rows in the table carry the v1 algorithm literal" in result.detail

    with pytest.raises(ValueError, match="does not attest zero v1"):
        attestation_for(result, backend="postgres", operator="you@lorehex.co")


def test_a_renamed_entity_kind_is_not_an_empty_deployment() -> None:
    """The other half of the same blindness, and the one the docstrings claimed.

    The rows are under `byok_provider_config` rather than `byok`. Both the walk
    and the per-kind census are restricted to the migrated kinds, so BOTH miss
    them and corroborate each other's silence — the exact shape a census is
    supposed to break. It does not break it; the literal search does.
    """
    store = FakeCloudDatabase(
        {
            ("byok_provider_config", "a"): _byok_row(V1),
            ("workspace", "w"): {"name": "acme"},
        }
    )

    result = check_no_v1_envelopes(store, cloud="azure", reporter=lambda _m: None)

    assert result.stats.rows_scanned == 0
    assert result.census.migrated_kind_counts == {}
    assert result.outcome == OUTCOME_SCAN_DISAGREES
    assert not result.passed
    assert "1 rows in the table carry the v1 algorithm literal" in result.detail


def test_a_row_that_only_mentions_the_v1_literal_blocks_the_cloud() -> None:
    """The price of the literal search, paid here so nobody pays it at 2am.

    `STRPOS(body, …)` / `body::text LIKE` know nothing about envelopes. A
    generation row that captured an upstream error naming the v1 algorithm is
    counted exactly like a v1 envelope, and this cloud cannot attest until that
    row is removed or rewritten — even though every envelope it holds is v2.

    This is the deliberate direction. A search narrowed to a JSON-shaped match
    would have to assume a serialisation, and the two adapters store different
    ones; a literal search that stops matching fails OPEN, which is how a
    customer's key gets deleted. So: fail closed, say where to look, and write
    the cost down. If someone later narrows the match, this test is what has to
    change, and changing it is the moment to re-argue the direction.
    """
    store = FakeCloudDatabase(
        {
            ("byok", "a"): _byok_row(V2),
            ("generation", "g"): {
                "model": "gpt",
                "error": f"unsupported algorithm {V1_ALGORITHM_LITERAL}",
            },
        }
    )

    result = check_no_v1_envelopes(store, cloud="gcp", reporter=lambda _m: None)

    assert result.stats.v1_envelopes == 0, "no v1 envelope exists on this deployment"
    assert result.census.v1_literal_rows == 1
    assert result.outcome == OUTCOME_SCAN_DISAGREES
    assert not result.passed
    assert "free text that merely mentions the literal" in result.detail

    with pytest.raises(ValueError, match="does not attest zero v1"):
        attestation_for(result, backend="postgres", operator="you@lorehex.co")


def test_a_wrong_but_populated_database_still_passes_and_says_which_one_it_read() -> None:
    """A negative result, stated rather than hidden.

    Point the credential at another project's `tr_entities`. It is reachable,
    non-empty, and holds no v1 envelope — indistinguishable from success, and
    nothing offline can distinguish it. This test exists so that the limit is
    executable documentation rather than a sentence someone can forget: if a
    future change claims to close this, this test is what has to change.

    What the run does instead is record where it read, so the mismatch is
    visible to whoever reviews the ledger.
    """
    store = FakeCloudDatabase({("workspace", "someone-elses"): {"name": "not-ours"}})

    result = check_no_v1_envelopes(store, cloud="aws", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_EMPTY_WITNESSED
    assert result.passed, "this is the gap; it is named, not closed"
    assert result.census.source == SOURCE
    assert SOURCE in result.detail
    assert "does not establish that this was the right database" in result.detail


def test_a_credentials_failure_cannot_be_mistaken_for_an_empty_database() -> None:
    """A store that cannot answer must raise, never return an empty census.

    The script turns this into exit 3 and the word INCONCLUSIVE; what matters
    here is that no code path converts "the query failed" into "there is
    nothing there".
    """
    store = FakeCloudDatabase({}, census_raises=PermissionError("caller lacks spanner.read"))

    with pytest.raises(PermissionError):
        check_no_v1_envelopes(store, cloud="azure", reporter=lambda _m: None)


# ------------------------------------------------------------- the ledger ---


def _passing_attestation(cloud: str, **overrides: Any) -> Attestation:
    base = {
        "cloud": cloud,
        "outcome": OUTCOME_CLEAN,
        "recorded_at": "2026-08-15T12:00:00+00:00",
        "backend": "spanner",
        "surface_fingerprint": surface_fingerprint(),
        "rows_scanned": 7,
        "rows_scanned_by_kind": {"byok": 7},
        "envelopes_seen": 7,
        "v1_envelopes": 0,
        "v2_envelopes": 7,
        "missing_envelopes": 0,
        "census_migrated_kind_counts": {"byok": 7},
        "census_sampled_kinds": ["byok", "workspace"],
        "census_v1_literal_rows": 0,
        "census_source": SOURCE,
        "operator": "you@lorehex.co",
        "note": "",
    }
    base.update(overrides)
    return Attestation(**base)


def test_one_cloud_attested_is_not_three_clouds_attested(tmp_path: Path) -> None:
    """Each cloud is a separate database (multi-cloud-separation.md), so the
    cloud you happened to hold credentials for is the one you can speak for."""
    ledger_path = tmp_path / "attestations.json"
    record_attestation(_passing_attestation("gcp"), path=ledger_path)

    blockers = zero_v1_blockers(load_ledger(ledger_path))

    assert any("aws: no zero-v1 attestation" in blocker for blocker in blockers)
    assert any("azure: no zero-v1 attestation" in blocker for blocker in blockers)
    assert not any(blocker.startswith("gcp:") for blocker in blockers)


def test_a_full_ledger_clears_the_gate(tmp_path: Path) -> None:
    ledger_path = tmp_path / "attestations.json"
    for cloud in STANDALONE_CLOUDS:
        record_attestation(_passing_attestation(cloud), path=ledger_path)

    assert zero_v1_blockers(load_ledger(ledger_path)) == []


def test_a_missing_ledger_blocks_every_cloud(tmp_path: Path) -> None:
    """Deleting the ledger is the cheapest possible forgery. It must read as
    "nothing is attested", never as "nothing blocks"."""
    blockers = zero_v1_blockers(load_ledger(tmp_path / "does-not-exist.json"))

    assert len(blockers) == len(STANDALONE_CLOUDS)


def test_the_ledger_refuses_to_record_what_was_not_established(tmp_path: Path) -> None:
    ledger_path = tmp_path / "attestations.json"

    with pytest.raises(ValueError, match="refusing to record outcome 'zero_scan'"):
        record_attestation(_passing_attestation("aws", outcome=OUTCOME_ZERO_SCAN), path=ledger_path)
    with pytest.raises(ValueError, match="unknown cloud"):
        record_attestation(_passing_attestation("gcp2"), path=ledger_path)
    assert not ledger_path.exists()


def test_a_hand_written_non_passing_entry_is_reported_as_a_defect() -> None:
    """`record_attestation` refuses these, so an entry like this arrived by
    someone editing the JSON. The reader has to catch what the writer refused."""
    ledger = empty_ledger()
    ledger["attestations"]["aws"] = _passing_attestation("aws", outcome=OUTCOME_ZERO_SCAN).to_dict()

    defects = ledger_defects(ledger)

    assert any("is not an attestation and must not be recorded" in defect for defect in defects)
    assert any("aws" in blocker for blocker in zero_v1_blockers(ledger))


def test_a_hand_written_pass_that_counted_v1_rows_is_reported_as_a_defect() -> None:
    """The structural half of the literal census.

    `check_no_v1_envelopes` cannot produce this and `record_attestation`
    refuses it, so an entry like this arrived by someone editing the JSON —
    plausibly by copying a real run and trimming the outcome. The reader has to
    catch it too, or the only enforcement lives in the writer.
    """
    ledger = empty_ledger()
    ledger["attestations"]["gcp"] = _passing_attestation("gcp", census_v1_literal_rows=3).to_dict()

    defects = ledger_defects(ledger)

    assert any("census_v1_literal_rows=3" in defect for defect in defects)
    assert zero_v1_blockers(ledger) != []


def test_an_attestation_that_does_not_name_the_database_it_read_is_a_defect() -> None:
    """Since a wrong-but-populated database passes, the one mitigation is that
    the ledger says which database answered. An entry without it removes the
    only thing a reviewer could have checked."""
    ledger = empty_ledger()
    ledger["attestations"]["gcp"] = _passing_attestation("gcp", census_source="  ").to_dict()

    assert any("no census_source" in defect for defect in ledger_defects(ledger))

    with pytest.raises(ValueError, match="does not name the database"):
        record_attestation(_passing_attestation("gcp", census_source=""), ledger=empty_ledger())


def test_counts_written_as_strings_do_not_read_as_zero() -> None:
    """`not "0"` is False and `not 0` is True, so a hand edit that quotes the
    numbers flips several of the checks below it. Cheap to reject, so rejected
    before any of them run."""
    ledger = empty_ledger()
    ledger["attestations"]["aws"] = _passing_attestation("aws").to_dict()
    ledger["attestations"]["aws"]["v1_envelopes"] = "0"
    ledger["attestations"]["aws"]["census_v1_literal_rows"] = "0"

    defects = ledger_defects(ledger)

    assert any("are not integers" in defect for defect in defects)
    assert any("v1_envelopes" in defect for defect in defects)


# ------------------------------------------------ one fleet, not three clouds ---


def test_the_required_clouds_are_derived_from_the_enclave_failover_topology() -> None:
    """Why every cloud must attest, as code rather than as a claim.

    §4.0 of the migration doc used to say a v2 envelope written by one cloud's
    control plane "never reaches another cloud's enclave". It does: AWS and
    Azure enclaves are deployed with an ordered control-plane list ending in
    the home plane and fail over to it on a dial failure
    (quill-cloud-proxy tools/deploy-aws-nitro.sh:888,
    tools/deploy-azure-aci.sh:269,
    enclave-go/internal/trustedrouter/client.go:41-44). So a v1 envelope in the
    GCP database can be handed to an AWS enclave — during a control-plane
    outage, which is when nobody wants a second incident.

    The required set is therefore the union of every enclave's sources, and
    that is what `zero_v1_blockers` iterates.
    """
    assert set(clouds_that_must_attest()) == set(STANDALONE_CLOUDS)
    assert ENCLAVE_CONTROL_PLANE_SOURCES["aws"][-1] == "gcp"
    assert ENCLAVE_CONTROL_PLANE_SOURCES["azure"][-1] == "gcp"
    for cloud, sources in ENCLAVE_CONTROL_PLANE_SOURCES.items():
        assert sources[0] == cloud, "index 0 is the cloud's own control plane"
        assert set(sources) <= set(STANDALONE_CLOUDS)


def test_no_proper_subset_of_the_ledger_clears_the_gate(tmp_path: Path) -> None:
    """There is no per-cloud permission, however complete one cloud's evidence."""
    for absent in STANDALONE_CLOUDS:
        ledger_path = tmp_path / f"without-{absent}.json"
        for cloud in STANDALONE_CLOUDS:
            if cloud != absent:
                record_attestation(_passing_attestation(cloud), path=ledger_path)

        blockers = zero_v1_blockers(load_ledger(ledger_path))

        assert blockers, f"the gate cleared with {absent} unattested"
        assert all(absent in blocker for blocker in blockers)


def test_an_empty_witnessed_entry_with_no_witness_is_a_zero_scan() -> None:
    """The one field that carries the whole weight of `empty_witnessed`."""
    ledger = empty_ledger()
    ledger["attestations"]["azure"] = _passing_attestation(
        "azure",
        outcome=OUTCOME_EMPTY_WITNESSED,
        envelopes_seen=0,
        v2_envelopes=0,
        census_sampled_kinds=[],
        census_migrated_kind_counts={},
        rows_scanned=0,
        rows_scanned_by_kind={},
    ).to_dict()

    assert any("is a zero scan" in defect for defect in ledger_defects(ledger))


def test_adding_an_encrypted_surface_invalidates_every_attestation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Open question #2 in the migration doc, made executable.

    "Are there envelopes outside the three surfaces?" — if the answer ever
    becomes "yes, here is a fourth", every attestation recorded before it was
    taken by a run that never walked that surface. The fingerprint makes those
    attestations stale rather than silently reusable.
    """
    from trusted_router import byok_v1_attestations as attestations_module

    ledger_path = tmp_path / "attestations.json"
    for cloud in STANDALONE_CLOUDS:
        record_attestation(_passing_attestation(cloud), path=ledger_path)
    assert zero_v1_blockers(load_ledger(ledger_path)) == []

    monkeypatch.setattr(
        attestations_module,
        "MIGRATED_SURFACES",
        (
            *attestations_module.MIGRATED_SURFACES,
            ("smtp_credential", "encrypted_password", "control"),
        ),
    )

    blockers = zero_v1_blockers(load_ledger(ledger_path))

    assert len(blockers) == len(STANDALONE_CLOUDS)
    assert all("re-run the precondition" in blocker for blocker in blockers)


def test_the_committed_ledger_is_well_formed() -> None:
    """The real file, as committed. Empty is fine — forged is not.

    Recording nothing is the honest state today: nobody has run this against a
    real database. What this asserts is that whatever is in there parses, is
    for a known cloud, carries a passing outcome, and covers the surfaces this
    repository writes now.

    The path assertion is not ceremony. A missing ledger reads as the empty
    ledger — the fail-closed choice — so a default path that quietly stopped
    pointing at the committed file would leave every test here green while the
    gate judged a file nobody maintains.
    """
    assert DEFAULT_LEDGER_PATH.name == "byok-aad-v2-attestations.json"
    assert DEFAULT_LEDGER_PATH.exists(), f"the committed ledger is not at {DEFAULT_LEDGER_PATH}"
    assert ledger_defects(load_ledger()) == []


# ---------------------------------------------------------------- the CLI ---


def test_the_script_exits_loudly_on_a_zero_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "attestations.json"
    monkeypatch.setattr(check_script, "_store", lambda _args: FakeCloudDatabase({}))

    code = check_script.main(
        [
            "--cloud",
            "aws",
            "--backend",
            "postgres",
            "--ledger",
            str(ledger_path),
            "--record",
            "--operator",
            "you@lorehex.co",
        ]
    )

    out = capsys.readouterr().out
    assert code == 2
    assert "NOT AN ATTESTATION [zero_scan]" in out
    assert not ledger_path.exists(), "a zero scan must not reach the ledger"


def test_the_script_records_a_pass_and_then_reports_what_is_still_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "attestations.json"
    store = FakeCloudDatabase({("byok", "a"): _byok_row(V2)})
    monkeypatch.setattr(check_script, "_store", lambda _args: store)

    code = check_script.main(
        [
            "--cloud",
            "gcp",
            "--backend",
            "spanner",
            "--ledger",
            str(ledger_path),
            "--record",
            "--operator",
            "you@lorehex.co",
            "--note",
            "post-backfill re-audit",
        ]
    )

    out = capsys.readouterr().out
    assert code == 1, "exit 0 means the FLEET is clear; one cloud recorded is not that"
    assert "ATTESTS ZERO V1 [clean]" in out
    assert "step 4 is blocked" in out, "one cloud recorded is not the gate cleared"
    assert "EXIT 1, NOT 0" in out
    recorded = json.loads(ledger_path.read_text())["attestations"]["gcp"]
    assert recorded["outcome"] == OUTCOME_CLEAN
    assert recorded["surface_fingerprint"] == surface_fingerprint()
    assert recorded["note"] == "post-backfill re-audit"
    assert recorded["census_v1_literal_rows"] == 0
    assert recorded["census_source"] == SOURCE


def test_status_only_exits_nonzero_while_the_ledger_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`check_no_v1_envelopes.py --status-only && <proceed>` must not proceed.

    It returned 0 unconditionally, including while printing three blockers, so
    the shell form the migration doc itself suggests would have run the next
    command on a fully blocked ledger. A refusal that only humans can read is
    not a refusal.
    """
    ledger_path = tmp_path / "attestations.json"

    blocked = check_script.main(["--status-only", "--ledger", str(ledger_path)])
    assert blocked == 2
    assert "step 4 is blocked" in capsys.readouterr().out

    for cloud in STANDALONE_CLOUDS:
        record_attestation(_passing_attestation(cloud), path=ledger_path)

    cleared = check_script.main(["--status-only", "--ledger", str(ledger_path)])
    assert cleared == 0
    assert "every standalone cloud attests" in capsys.readouterr().out


def test_the_pinned_v1_literal_is_the_one_the_module_writes() -> None:
    """The census searches for `V1_ALGORITHM_LITERAL`, which is a copy.

    It is pinned rather than imported so that the check survives step 4
    deleting `byok_crypto.ALGORITHM` — but a copy that drifts would search for
    a string no row contains and report zero forever, which is the quietest
    possible way for this whole file to stop meaning anything. Held equal while
    both exist; delete this assertion with v1, not before.
    """
    from trusted_router.byok_crypto import ALGORITHM

    assert V1_ALGORITHM_LITERAL == ALGORITHM == V1


def test_the_script_reports_a_failed_run_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 3, not exit 0, and not exit 2: the run did not happen."""
    monkeypatch.setattr(
        check_script,
        "_store",
        lambda _args: FakeCloudDatabase({}, census_raises=PermissionError("no spanner.read")),
    )

    code = check_script.main(
        ["--cloud", "azure", "--backend", "spanner", "--ledger", str(tmp_path / "l.json")]
    )

    assert code == 3
    assert "INCONCLUSIVE" in capsys.readouterr().out
