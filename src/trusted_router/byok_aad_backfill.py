"""Read-only scanner for the completed BYOK AAD v1-to-v2 migration.

The mutating backfill retired with Step 4. This module intentionally retains
only the database census and envelope classifier used to prove that no V1 row
survived. It has no decrypt path, KMS dependency, or database write method.
See `trusted_router.byok_v1_attestations` for the law and the audit ledger.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from trusted_router.byok_crypto import ALGORITHM_V2
from trusted_router.byok_v1_attestations import (
    MIGRATED_KINDS,
    MIGRATED_SURFACES,
    OUTCOME_CLEAN,
    OUTCOME_DIRTY,
    OUTCOME_EMPTY_WITNESSED,
    OUTCOME_SCAN_DISAGREES,
    OUTCOME_V1_REMAINS,
    OUTCOME_ZERO_SCAN,
    PASSING_OUTCOMES,
    V1_ALGORITHM_LITERAL,
    Attestation,
    surface_fingerprint,
    utc_now,
)

CENSUS_POSITIVE_LITERAL = "{"


@dataclass(frozen=True)
class EntityRow:
    kind: str
    entity_id: str
    body: dict[str, Any]


@dataclass(frozen=True)
class EntityCensus:
    """Three differently shaped questions about the same table.

    `migrated_kind_counts` is an aggregate count per migrated kind; the scan is
    a paged cursor walk. They are computed by different SQL and can therefore
    disagree, which is the point: a cursor bug returns no rows while the count
    still sees them. `sampled_kinds` is a bounded peek at the table's contents
    of any kind at all, so that "the audit found nothing" can be distinguished
    from "the audit could not have found anything".

    `v1_literal_rows` is the one that does not share the scan's assumptions.
    The count and the walk restrict to the same registered kinds — all from
    `MIGRATED_KINDS`, on both adapters, since `SpannerEntityStore.scan` now
    binds `@kinds` the way its own census already did rather than writing the
    names out as SQL text — and the walk reads
    envelopes only out of the field names in `MIGRATED_SURFACES`. So a renamed
    entity kind or a renamed body field hides the same rows from the walk AND
    from the count, and the disagreement they exist to expose never happens.
    This one searches whole row bodies for `V1_ALGORITHM_LITERAL` with no kind
    filter and no field-name assumption, which is why it is the clause
    `empty_witnessed` actually rests on.

    That breadth is also its one false positive: it counts any row whose body
    text merely CONTAINS the literal, including a row of a kind nothing here
    migrates that only mentions it — a captured upstream error string, a stored
    audit line. Such a row makes the cloud report `scan_disagrees_with_census`
    until it is dealt with, even though no v1 envelope exists. That is
    fail-closed and the message says where to look, but it is a real way for a
    healthy deployment to be unattestable, and it is named as a scope limit in
    `byok_v1_attestations` rather than narrowed: narrowing the pattern to a
    JSON-shaped match would tie it to the exact serialisation each adapter
    happens to store, and a pattern that stops matching fails OPEN.

    `source` names the database the census was taken from, and the two adapters
    know it to different depths. Postgres asks the server for
    `current_database()` and `current_user`, then reads the negotiated host and
    port from the connection object (Aurora DSQL does not implement
    `inet_server_addr()`). Spanner composes
    `projects/…/instances/…/databases/…` client-side
    from the arguments the CLI was given: `Database.name` is string
    concatenation in `google.cloud.spanner` and issues no RPC, so that value
    records what was ASKED FOR, not what answered, and reads identically against
    an emulator. Neither is a proof of anything. Both are recorded so a reviewer
    of the ledger can compare the database against the cloud the entry claims to
    speak for, since nothing offline can tell a correct database from a
    wrong-but-populated one.
    """

    migrated_kind_counts: dict[str, int]
    sampled_kinds: tuple[str, ...]
    v1_literal_rows: int
    positive_control_rows: int
    source: str

    @property
    def reachable(self) -> bool:
        """True when the table answered with real rows of some kind.

        A credentials failure raises rather than reaching here; a wrong table
        name raises; an empty answer leaves this False, which is never a pass.
        """
        return bool(self.sampled_kinds)


class EntityStore(Protocol):
    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]: ...

class CensusStore(Protocol):
    def census(self, *, sample_limit: int = 1000) -> EntityCensus: ...


class AuditableStore(EntityStore, CensusStore, Protocol):
    """A store that can be both walked and counted. Required by the precondition."""


@dataclass
class BackfillStats:
    rows_scanned: int = 0
    envelopes_seen: int = 0
    v1_envelopes: int = 0
    v2_envelopes: int = 0
    missing_envelopes: int = 0
    failures: int = 0
    unsupported_algorithms: int = 0
    # Per-kind scan counts. A single total cannot be cross-checked against a
    # per-kind census, and "we scanned 4000 rows" says nothing about whether
    # any of them were the kind that holds envelopes.
    rows_scanned_by_kind: dict[str, int] = field(default_factory=dict)
    # Rows carrying at least one v1 envelope. Counted separately from
    # `v1_envelopes` because the census counts ROWS containing the v1 literal,
    # and a broadcast destination can hold two v1 envelopes in one row; the
    # cross-check has to compare like with like.
    rows_with_v1: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class BackfillRunner:
    """Read every registered encrypted field in stable key order.

    The historical name is retained for operator-script compatibility. Step 4
    removed every V1 decryptor, so this class can only classify stored formats.
    """

    def __init__(
        self,
        store: EntityStore,
        *,
        reporter: Callable[[str], None] = print,
    ) -> None:
        self._store = store
        self._report = reporter

    def run(
        self,
        *,
        batch_size: int = 100,
        after: tuple[str, str] | None = None,
    ) -> BackfillStats:
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch size must be between 1 and 1000")
        stats = BackfillStats()
        cursor = after
        while True:
            rows = self._store.scan(after=cursor, limit=batch_size)
            if not rows:
                return stats
            for row in rows:
                self._process_row(row, stats)
                cursor = (row.kind, row.entity_id)
            last_row = rows[-1]
            self._report(
                "checkpoint "
                f"kind={last_row.kind} row={_row_ref(last_row.kind, last_row.entity_id)} "
                f"rows_scanned={stats.rows_scanned}"
            )

    def _process_row(self, row: EntityRow, stats: BackfillStats) -> None:
        stats.rows_scanned += 1
        stats.rows_scanned_by_kind[row.kind] = stats.rows_scanned_by_kind.get(row.kind, 0) + 1
        row_v1 = 0
        for field_name, _envelope_family in _fields_for_kind(row.kind):
            raw_envelope = row.body.get(field_name)
            if raw_envelope is None:
                stats.missing_envelopes += 1
                continue
            stats.envelopes_seen += 1
            if not isinstance(raw_envelope, dict):
                stats.failures += 1
                self._error(row, field_name, "invalid_envelope_shape")
                continue
            algorithm = raw_envelope.get("algorithm")
            if algorithm == ALGORITHM_V2:
                stats.v2_envelopes += 1
                continue
            if algorithm != V1_ALGORITHM_LITERAL:
                stats.unsupported_algorithms += 1
                self._error(row, field_name, "unsupported_algorithm")
                continue
            stats.v1_envelopes += 1
            row_v1 += 1

        if row_v1:
            stats.rows_with_v1 += 1

    def _error(self, row: EntityRow, field: str, reason: str) -> None:
        self._report(
            "ERROR "
            f"kind={row.kind} row={_row_ref(row.kind, row.entity_id)} "
            f"field={field} reason={reason}"
        )


class SpannerEntityStore:
    def __init__(self, database: Any, param_types: Any) -> None:
        self._database = database
        self._param_types = param_types

    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]:
        after_kind, after_id = after or ("", "")
        sql = (
            "SELECT kind, id, body FROM tr_entities "
            "WHERE kind IN UNNEST(@kinds) "
            "AND (kind > @after_kind OR (kind = @after_kind AND id > @after_id)) "
            "ORDER BY kind, id LIMIT @limit"
        )
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                sql,
                params={
                    "kinds": list(MIGRATED_KINDS),
                    "after_kind": after_kind,
                    "after_id": after_id,
                    "limit": limit,
                },
                param_types={
                    "kinds": self._param_types.Array(self._param_types.STRING),
                    "after_kind": self._param_types.STRING,
                    "after_id": self._param_types.STRING,
                    "limit": self._param_types.INT64,
                },
            )
            return [
                EntityRow(
                    kind=kind,
                    entity_id=entity_id,
                    body=json.loads(body),
                )
                for kind, entity_id, body in rows
            ]

    def census(self, *, sample_limit: int = 1000) -> EntityCensus:
        """Counts per migrated kind, a bounded liveness peek, and a literal search.

        The per-kind count is deliberately not `GROUP BY kind` over the whole
        table: `tr_entities` holds every entity in the deployment and a full
        group-by is an expensive scan on a live database. The peek is a `LIMIT`
        read, so its cost does not grow with the table.

        The v1 literal search IS a full scan, and there is no way around it —
        that is the whole point of it. It filters on neither the kind list nor
        the field map the walk uses, so it is the only question here that a
        renamed entity kind or a renamed body field cannot hide from. This runs
        once, before an irreversible step, on a snapshot read; it is not on any
        request path. On a large table expect it to take a while and let it.

        Unlike the Postgres adapter, `source` here is NOT asked of the server.
        `Database.name` is assembled locally from `--project`,
        `--spanner-instance` and `--spanner-database`, so it names the database
        that was addressed and would read the same against an emulator. The
        returned string says so out loud, because the reviewer comparing the
        ledger against the cloud it claims is the person that distinction is for.
        """
        counts: dict[str, int] = {}
        sampled: set[str] = set()
        # This method deliberately executes three queries against one
        # consistent read-only view. Spanner snapshots are single-use unless
        # requested otherwise, so the default fails on the second query.
        with self._database.snapshot(multi_use=True) as snapshot:
            rows = snapshot.execute_sql(
                "SELECT kind, COUNT(*) FROM tr_entities WHERE kind IN UNNEST(@kinds) GROUP BY kind",
                params={"kinds": list(MIGRATED_KINDS)},
                param_types={"kinds": self._param_types.Array(self._param_types.STRING)},
            )
            for kind, count in rows:
                counts[kind] = int(count)
            sample = snapshot.execute_sql(
                "SELECT kind FROM tr_entities LIMIT @limit",
                params={"limit": sample_limit},
                param_types={"limit": self._param_types.INT64},
            )
            for (kind,) in sample:
                sampled.add(kind)
            # Not initialised to zero: an aggregate that returned no row at all
            # must raise, exactly as the Postgres adapter does. "The server told
            # me nothing" must never become "there is nothing", and zero is the
            # passing value. A GROUP BY or a LIMIT peek may legitimately be
            # empty — those fail closed on their own, via `reachable` and via
            # the undercount check — but a COUNT(*) returning no row is broken.
            literal_rows: int | None = None
            for (count,) in snapshot.execute_sql(
                "SELECT COUNT(*) FROM tr_entities WHERE STRPOS(body, @literal) > 0",
                params={"literal": V1_ALGORITHM_LITERAL},
                param_types={"literal": self._param_types.STRING},
            ):
                literal_rows = int(count)
            if literal_rows is None:
                raise ValueError("the v1 literal count returned no row")
            positive_rows: int | None = None
            for (count,) in snapshot.execute_sql(
                "SELECT COUNT(*) FROM tr_entities WHERE STRPOS(body, @literal) > 0",
                params={"literal": CENSUS_POSITIVE_LITERAL},
                param_types={"literal": self._param_types.STRING},
            ):
                positive_rows = int(count)
            if positive_rows is None:
                raise ValueError("the literal-search positive control returned no row")
        return EntityCensus(
            migrated_kind_counts=counts,
            sampled_kinds=tuple(sorted(sampled)),
            v1_literal_rows=literal_rows,
            positive_control_rows=positive_rows,
            # Spelled out because it is not what it looks like: `Database.name`
            # is built by concatenating the project, instance and database the
            # CLI was told to use, with no RPC. This says which database was
            # addressed, not which one answered.
            source=f"spanner:{self._database.name} (from CLI arguments, not asked of the server)",
        )

class PostgresEntityStore:
    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Postgres DSN is required")
        self._dsn = dsn

    def scan(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[EntityRow]:
        import psycopg

        after_kind, after_id = after or ("", "")
        with psycopg.connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT kind, id, body FROM tr_entities "
                "WHERE kind = ANY(%s) "
                "AND (kind > %s OR (kind = %s AND id > %s)) "
                "ORDER BY kind, id LIMIT %s",
                (
                    list(MIGRATED_KINDS),
                    after_kind,
                    after_kind,
                    after_id,
                    limit,
                ),
            ).fetchall()
        result: list[EntityRow] = []
        for kind, entity_id, raw_body in rows:
            body = json.loads(raw_body) if isinstance(raw_body, str) else dict(raw_body)
            result.append(
                EntityRow(
                    kind=kind,
                    entity_id=entity_id,
                    body=body,
                )
            )
        return result

    def census(self, *, sample_limit: int = 1000) -> EntityCensus:
        """See SpannerEntityStore.census. Same three questions, same reasons.

        `body::text LIKE` rather than a JSON path, so the search does not
        depend on where in the body an envelope sits or what the field holding
        it is called — the two things the walk assumes and the two things that
        made an earlier version of this check blind.
        """
        import psycopg

        with psycopg.connect(self._dsn) as conn:
            # All census facts come from one SQL statement and therefore one
            # database snapshot. Aurora DSQL rejects SET TRANSACTION outright,
            # so relying on a repeatable-read GUC makes the very cloud this
            # precondition protects unauditable. Keeping the three questions in
            # one UNION is both stronger than separate READ COMMITTED queries
            # and portable across PostgreSQL, Azure Flexible Server, and DSQL.
            facts = conn.execute(
                "WITH sampled AS ("
                "  SELECT DISTINCT kind FROM ("
                "    SELECT kind FROM tr_entities LIMIT %s"
                "  ) AS peek"
                ") "
                "SELECT 'count' AS metric, kind, COUNT(*) AS value "
                "FROM tr_entities WHERE kind = ANY(%s) GROUP BY kind "
                "UNION ALL "
                "SELECT 'sample' AS metric, kind, 0 AS value FROM sampled "
                "UNION ALL "
                "SELECT 'v1_literal' AS metric, '' AS kind, COUNT(*) AS value "
                "FROM tr_entities WHERE body::text LIKE %s "
                "UNION ALL "
                "SELECT 'positive_control' AS metric, '' AS kind, COUNT(*) AS value "
                "FROM tr_entities WHERE body::text LIKE %s",
                (
                    sample_limit,
                    list(MIGRATED_KINDS),
                    f"%{V1_ALGORITHM_LITERAL}%",
                    f"%{CENSUS_POSITIVE_LITERAL}%",
                ),
            ).fetchall()
            counts: dict[str, int] = {}
            sampled: set[str] = set()
            literal_rows: int | None = None
            positive_rows: int | None = None
            for metric, kind, value in facts:
                if metric == "count":
                    counts[str(kind)] = int(value)
                elif metric == "sample":
                    sampled.add(str(kind))
                elif metric == "v1_literal":
                    if literal_rows is not None:
                        raise ValueError("the v1 literal count returned more than one row")
                    literal_rows = int(value)
                elif metric == "positive_control":
                    if positive_rows is not None:
                        raise ValueError("the literal-search positive control returned more than one row")
                    positive_rows = int(value)
                else:
                    raise ValueError(f"the census returned an unknown metric: {metric!r}")
            # A missing aggregate raises rather than defaulting to zero: "the
            # server told me nothing" must never become "there is nothing".
            if literal_rows is None:
                raise ValueError("the v1 literal count returned no row")
            if positive_rows is None:
                raise ValueError("the literal-search positive control returned no row")
            source_row = conn.execute("SELECT current_database(), current_user").fetchone()
            if source_row is None:
                raise ValueError("the database could not identify itself")
            host = str(conn.info.host or "unknown")
            port = int(conn.info.port or 0)
        database, user = source_row
        return EntityCensus(
            migrated_kind_counts=counts,
            sampled_kinds=tuple(sorted(sampled)),
            v1_literal_rows=literal_rows,
            positive_control_rows=positive_rows,
            # Asked of the server rather than parsed out of the DSN, and the
            # DSN never appears here: it carries the password.
            source=(
                f"postgres:{database}@{host}:{port} as {user} "
                "(database/user from server; host/port from negotiated connection)"
            ),
        )

def _fields_for_kind(kind: str) -> tuple[tuple[str, str], ...]:
    """Derived from MIGRATED_SURFACES so the surface list has exactly one home.

    A new encrypted surface added there is walked by the backfill AND changes
    the attestation fingerprint, which invalidates every zero-v1 attestation
    recorded before the surface existed. Two copies of this list is how open
    question #2 in the migration doc turns into an outage.
    """
    fields = tuple(
        (field_name, family)
        for surface_kind, field_name, family in MIGRATED_SURFACES
        if surface_kind == kind
    )
    if not fields:
        raise ValueError(f"unsupported entity kind: {kind}")
    return fields


def _row_ref(kind: str, entity_id: str) -> str:
    return hashlib.sha256(f"{kind}\x00{entity_id}".encode()).hexdigest()[:12]


# ------------------------------------------------- the step-4 precondition ---


@dataclass(frozen=True)
class PreconditionResult:
    """One cloud's answer to "may v1 be deleted?", with its working shown."""

    cloud: str
    outcome: str
    detail: str
    stats: BackfillStats
    census: EntityCensus

    @property
    def passed(self) -> bool:
        return self.outcome in PASSING_OUTCOMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "cloud": self.cloud,
            "outcome": self.outcome,
            "detail": self.detail,
            "audit": self.stats.as_dict(),
            "census": {
                "migrated_kind_counts": dict(sorted(self.census.migrated_kind_counts.items())),
                "sampled_kinds": list(self.census.sampled_kinds),
                "v1_literal_rows": self.census.v1_literal_rows,
                "positive_control_rows": self.census.positive_control_rows,
                "source": self.census.source,
            },
        }


def check_no_v1_envelopes(
    store: AuditableStore,
    *,
    cloud: str,
    batch_size: int = 100,
    sample_limit: int = 1000,
    reporter: Callable[[str], None] = print,
) -> PreconditionResult:
    """Audit one cloud's database and say whether it attests zero v1 envelopes.

    The audit alone cannot do this. `BackfillRunner` reports
    `v1_envelopes == 0` just as happily for a migrated database as for a run
    that scanned nothing at all. Both render as a green check, and on AWS and
    Azure a green check is exactly what a zero-row audit produced. So this
    function pairs the walk with a census and refuses to collapse the cases.

    WHAT THE CENSUS ACTUALLY SEPARATES, AND WHAT IT DOES NOT
        * A cursor, ordering or pagination bug in the paged walk — caught by
          the per-kind counts, which are computed by different SQL from the
          same kind list.
        * A renamed entity kind, or an envelope moved to a differently named
          body field — caught by `census.v1_literal_rows`, and by nothing else
          here. The per-kind counts cannot catch either: the count and the walk
          restrict to the same two kind names, so a renamed kind is hidden from
          both, and neither reads a field name the surface map does not list.
          (The count and walk both take those names from `MIGRATED_KINDS` on
          both adapters; a divergence would show up as an undercount, which is
          a refusal.) An earlier
          revision of this docstring claimed the per-kind census covered a
          renamed kind. It did not, and a live v1 envelope under a renamed
          field passed as `empty_witnessed`.
        * A credential pointed at a wrong-but-populated database — NOT caught,
          and not catchable from here. Such a database is reachable, non-empty,
          and holds no v1 envelope, which is indistinguishable from success.
          `census.source` is recorded so the mismatch is at least visible in
          the ledger afterwards — on Postgres that string is the server's own
          answer, on Spanner it is the database the CLI was pointed at, and it
          says which it is.

    The literal search is a text search, so it also counts rows that only
    MENTION `V1_ALGORITHM_LITERAL` — a stored upstream error message, an audit
    line — and one of those blocks the cloud with `scan_disagrees_with_census`
    until that row is removed or rewritten. Fail-closed, and a real reason a
    deployment holding no v1 envelope can be unattestable. See the scope limits
    in `byok_v1_attestations` for why it is not narrowed.

    The census is taken FIRST, on purpose. Taken last, a BYOK key registered
    while the scan was running would be counted by the census and missed by the
    walk, and would report as a scan disagreement — a false alarm on the most
    ordinary event there is. Taken first, that same registration is scanned but
    not counted, which is harmless. The remaining false-alarm mode is a row
    deleted mid-run; the answer to that one is to re-run.

    There is no resume cursor here, unlike the backfill. Resuming is what makes
    a long mutating job survivable and it is exactly what makes an audit
    unfalsifiable: a precondition that starts halfway through cannot claim to
    have covered the beginning. This walks the whole table or reports nothing.
    """
    census = store.census(sample_limit=sample_limit)
    stats = BackfillRunner(store, reporter=reporter).run(batch_size=batch_size)

    if not census.reachable:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_ZERO_SCAN,
            detail=(
                "the independent whole-table census found no rows of any kind. Even if the "
                "paged migrated-kind walk returned envelopes, there is no independent witness "
                "that the census could have found a retired format. This is zero evidence, not "
                "an attestation."
            ),
            stats=stats,
            census=census,
        )

    if census.positive_control_rows <= 0:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_SCAN_DISAGREES,
            detail=(
                "the census reached populated rows, but its whole-body literal-search positive "
                "control matched none. A zero V1-literal count is not evidence until the same "
                "column, cast, operator, and parameter path demonstrates that it can match a "
                "known JSON-object marker."
            ),
            stats=stats,
            census=census,
        )

    undercounted = {
        kind: (counted, stats.rows_scanned_by_kind.get(kind, 0))
        for kind, counted in sorted(census.migrated_kind_counts.items())
        if counted > stats.rows_scanned_by_kind.get(kind, 0)
    }
    if undercounted:
        detail = ", ".join(
            f"{kind}: census counted {counted} rows, the scan returned {scanned}"
            for kind, (counted, scanned) in undercounted.items()
        )
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_SCAN_DISAGREES,
            detail=(
                f"the scan did not see rows the census can count ({detail}). A resume cursor, "
                "filter, or ordering bug — or a row deleted mid-run. Re-run before believing "
                "anything else this reported."
            ),
            stats=stats,
            census=census,
        )
    if census.v1_literal_rows > stats.rows_with_v1:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_SCAN_DISAGREES,
            detail=(
                f"{census.v1_literal_rows} rows in the table carry the v1 algorithm literal "
                f"{V1_ALGORITHM_LITERAL!r} somewhere in their body, but the audit classified "
                f"only {stats.rows_with_v1} rows as v1. The literal search uses no kind filter "
                "and no field-name map, so at least "
                f"{census.v1_literal_rows - stats.rows_with_v1} rows carry that string where the "
                "walk cannot see it: a renamed entity kind, an envelope under a body field this "
                "repository does not know, or a surface missing from MIGRATED_SURFACES. It can "
                "also be free text that merely mentions the literal — a stored provider error, "
                "an audit line — which is not a v1 envelope but is not distinguishable from "
                "here. Find those rows before believing anything else this reported."
            ),
            stats=stats,
            census=census,
        )
    if stats.failures or stats.unsupported_algorithms:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_DIRTY,
            detail=(
                f"{stats.failures} unreadable and {stats.unsupported_algorithms} "
                "unknown-algorithm envelopes. Something other than the migration is wrong; "
                "a row that cannot be classified cannot be attested as v2."
            ),
            stats=stats,
            census=census,
        )
    if stats.v1_envelopes:
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_V1_REMAINS,
            detail=(
                f"{stats.v1_envelopes} v1 envelopes are still stored. V1 decrypt support "
                "has been retired; stop the rollout and use reviewed pre-Step-4 recovery code "
                "rather than attempting to overwrite these rows."
            ),
            stats=stats,
            census=census,
        )
    if stats.envelopes_seen == 0:
        # Every clause of the sentence below is evaluated above: the literal
        # search returned zero (checked at the top), the census reached rows
        # (checked immediately above), and envelopes_seen is zero by this
        # branch. Nothing here asserts a condition the code did not test —
        # that is the defect this branch used to have, and it is the same
        # defect the whole file exists to prevent one layer down.
        migrated_rows = sum(census.migrated_kind_counts.values())
        return PreconditionResult(
            cloud=cloud,
            outcome=OUTCOME_EMPTY_WITNESSED,
            detail=(
                f"the walk read {stats.rows_scanned} rows of the migrated kinds and found no "
                f"envelope in any of them ({stats.missing_envelopes} expected envelope fields "
                "were absent, which is ordinary — both broadcast secret fields and the BYOK "
                "secret are optional). What makes this an attestation rather than an empty "
                f"result is the census: it reached {len(census.sampled_kinds)} entity kinds in "
                f"the same table with the same credentials, counted {migrated_rows} rows of the "
                "migrated kinds, and found ZERO rows anywhere in the table carrying "
                f"{V1_ALGORITHM_LITERAL!r} — a search that filters on no kind and assumes no "
                "field name, so a renamed kind or a renamed body field cannot hide from it. "
                "This does not establish that this was the right database; the census "
                f"records it as {census.source!r}."
            ),
            stats=stats,
            census=census,
        )
    return PreconditionResult(
        cloud=cloud,
        outcome=OUTCOME_CLEAN,
        detail=(
            f"{stats.envelopes_seen} envelopes examined across {stats.rows_scanned} rows; "
            f"all {stats.v2_envelopes} are v2, and no row anywhere in the table carries "
            f"{V1_ALGORITHM_LITERAL!r}. The census records the database as {census.source!r}."
        ),
        stats=stats,
        census=census,
    )


def attestation_for(
    result: PreconditionResult,
    *,
    backend: str,
    operator: str,
    note: str = "",
    recorded_at: str | None = None,
) -> Attestation:
    """Turn a passing precondition run into a ledger entry.

    Refuses non-passing outcomes here rather than only at write time, so that
    no caller can construct a green-looking Attestation from a zero scan and
    then hand it to something less careful than `record_attestation`.
    """
    if not result.passed:
        raise ValueError(
            f"outcome {result.outcome!r} does not attest zero v1 envelopes: {result.detail}"
        )
    return Attestation(
        cloud=result.cloud,
        outcome=result.outcome,
        recorded_at=utc_now() if recorded_at is None else recorded_at,
        backend=backend,
        surface_fingerprint=surface_fingerprint(),
        rows_scanned=result.stats.rows_scanned,
        rows_scanned_by_kind=dict(result.stats.rows_scanned_by_kind),
        envelopes_seen=result.stats.envelopes_seen,
        v1_envelopes=result.stats.v1_envelopes,
        v2_envelopes=result.stats.v2_envelopes,
        missing_envelopes=result.stats.missing_envelopes,
        census_migrated_kind_counts=dict(result.census.migrated_kind_counts),
        census_sampled_kinds=list(result.census.sampled_kinds),
        census_v1_literal_rows=result.census.v1_literal_rows,
        census_positive_control_rows=result.census.positive_control_rows,
        census_source=result.census.source,
        operator=operator,
        note=note,
    )
