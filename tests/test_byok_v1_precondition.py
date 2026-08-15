"""The step-4 precondition: "no v1 envelopes remain" must be earned, per cloud.

THE LAW
    For each standalone cloud deployment, `check_no_v1_envelopes` reports that
    the deployment holds no v1 BYOK envelope only when the audit both (a) found
    zero v1 envelopes and (b) is corroborated by an independently shaped census
    of the same table showing the scan could have seen rows if any existed.
    Every other shape — v1 rows found, rows it could not classify, a scan that
    disagrees with the census, a scan with nothing behind it — is its own named
    outcome and none of them may be written into the attestation ledger.

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
    are identical. That is the specific confusion these tests exist to make
    impossible to commit.

SCOPE LIMIT — what these tests do NOT establish
    * They exercise the classification and the ledger, against in-memory
      stores. They do not exercise Spanner or Postgres SQL. `census()` on
      either real adapter is unproven here, and a census that always returned
      an empty result would fail closed (zero_scan), not open — that is the
      direction this gets wrong safely.
    * A passing precondition is about one moment on one cloud. It says nothing
      about the enclave side (`quill-cloud-proxy` has its own v1 branch), and
      nothing about a surface no code path in this repo knows about.
    * `empty_witnessed` rests on the census reaching the same table as the
      scan through the same store object. It corroborates credentials,
      reachability and the scan's cursor. It does NOT prove `tr_entities` is
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
    OUTCOME_CLEAN,
    OUTCOME_DIRTY,
    OUTCOME_EMPTY_WITNESSED,
    OUTCOME_SCAN_DISAGREES,
    OUTCOME_V1_REMAINS,
    OUTCOME_ZERO_SCAN,
    STANDALONE_CLOUDS,
    Attestation,
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


class FakeCloudDatabase:
    """One cloud's entity table, with the scan and the census as separate
    answers so a test can make them disagree — which is exactly what a resume
    cursor bug does in production."""

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
        keys = sorted(key for key in self.rows if after is None or key > after)[:limit]
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
            counts[kind] = counts.get(kind, 0) + 1
        return EntityCensus(
            migrated_kind_counts=counts,
            sampled_kinds=tuple(sorted({kind for kind, _ in self.rows})),
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
    credentials, finding rows of other kinds, and counting zero rows of every
    migrated kind.
    """
    store = FakeCloudDatabase(
        {},
        census=EntityCensus(
            migrated_kind_counts={},
            sampled_kinds=("api_key", "generation", "workspace"),
        ),
    )

    result = check_no_v1_envelopes(store, cloud="aws", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_EMPTY_WITNESSED
    assert result.passed
    assert result.outcome != OUTCOME_CLEAN, "an absence is a weaker claim and is named as one"


def test_a_broken_cursor_is_caught_by_the_census() -> None:
    """The failure mode a self-reported audit cannot see.

    The scan returns nothing — a bad `--after` cursor, an ordering bug, a
    renamed kind in the WHERE clause — while the rows are sitting right there.
    Without the census this is indistinguishable from a migrated deployment.
    """
    store = FakeCloudDatabase(
        {("byok", "a"): _byok_row(V1)},
        scan_returns_nothing=True,
    )

    result = check_no_v1_envelopes(store, cloud="gcp", reporter=lambda _m: None)

    assert result.outcome == OUTCOME_SCAN_DISAGREES
    assert not result.passed
    assert "census counted 1 rows, the scan returned 0" in result.detail


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
        "census_migrated_kind_counts": {"byok": 7},
        "census_sampled_kinds": ["byok", "workspace"],
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
    assert code == 0
    assert "ATTESTS ZERO V1 [clean]" in out
    assert "step 4 is blocked" in out, "one cloud recorded is not the gate cleared"
    recorded = json.loads(ledger_path.read_text())["attestations"]["gcp"]
    assert recorded["outcome"] == OUTCOME_CLEAN
    assert recorded["surface_fingerprint"] == surface_fingerprint()
    assert recorded["note"] == "post-backfill re-audit"


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
