# Durable async obligations (PR 4)

This is an additive, dormant adapter stacked on journal PR #1403. No route
constructs the registry, issues an async ticket, or creates an obligation.
`TR_ASYNC_SETTLEMENT_JOURNAL_ENABLED` remains false. Recovery guards do not read
that flag. Snapshot hash/version are ticket inputs; this adapter does not read
PR 3 authorization snapshot fields.

## Deployment and enablement order

Code may deploy before the migration with admission disabled. Each process probes
`tr_async_settlement_obligation` by an empty complete authorization key. Confirmed
absence selects the exact parent SQL variants for settlement, reaper scans and
transactional rechecks, snapshot booking, and all retention/outbox completion
paths. No obligation can exist before its table exists. The cache is keyed by
fully qualified database name, separate from outbox availability; concurrent
misses serialize. Presence is cached for the process lifetime. Absence expires
in **five seconds**. Other errors retain guarded SQL and are never cached.

Once present, obligation guards apply independently of
`TR_ASYNC_SETTLEMENT_JOURNAL_ENABLED`. The existing outbox probe, cache and
`guard_outbox=guard_active` semantics are preserved. In the legacy reaper,
`expires_before` identifies the reclaim operation even when the outbox table
is absent, so its obligation recheck remains armed. Normal settle still books
money while retaining records with unresolved obligations.

Registry/journal operations never use the pre-migration fallback. Grant-only
registry reads also probe the obligation table inside their transaction/snapshot,
so a partial migration cannot issue a grant. Missing schema raises even when the
flag is on; async admission cannot proceed and must stay synchronous.

**Enablement requires the migration AND at least one five-second TTL before
turning on admission, or a new revision with fresh processes.** A fresh process
probes before using settlement/reaper SQL. Do not enable on an old revision
immediately after DDL: it may still hold a negative cache entry. Apply migrations
outside a rolling deploy using the established operational sequence.

For the repository's default GCP deployment, the exact operator commands are:

```bash
GCP_PROJECT_ID=quill-cloud-proxy \
SPANNER_INSTANCE_ID=trusted-router-nam6 \
SPANNER_DATABASE_ID=trusted-router \
bash scripts/deploy/migrate_async_settlement.sh

GCP_PROJECT_ID=quill-cloud-proxy \
SPANNER_INSTANCE_ID=trusted-router-nam6 \
SPANNER_DATABASE_ID=trusted-router \
bash scripts/deploy/check_async_settlement_schema.sh --enablement
```

The second command is a read-only enablement gate that tooling can call. It
compiles zero-row queries naming every required column, requires the unique
slot index to be `READ_WRITE`, and then waits five seconds so earlier negative
caches have expired. It exits nonzero on schema failure without waiting and
never applies schema. `--schema` (the default) checks only schema readiness;
it does not satisfy the old-process cache precondition. Ordinary code rollout
does not depend on this script being wired into the deploy pipeline. Substitute
all three deployment identifiers together for other installations. Neither
command was run against a real database during implementation.

The exact executable DDL is in
[`migrate_async_settlement.sh`](../scripts/deploy/migrate_async_settlement.sh).
It uses the established `table_exists` / `index_exists` / `apply_ddl` machinery.
The conformance schema is regenerated from that script, not separately authored.

| Object | Key and contents |
|---|---|
| `tr_async_settlement_budget` | PK `(workspace_id, epoch)`; region, cap, immutable shards/slots, active/retiring/closed state, recorded bound, successor epoch, retention deadline, created/updated timestamps |
| `tr_async_settlement_obligation` | PK `(authorization_id)`; workspace/region/epoch, shard/slot, generation, key, nonce, snapshot hash/version, idempotency deadline, pending/accepted/sync_required/fenced/acknowledged state, payload hash, amount, ledger receipt, timestamps |
| `tr_async_obligation_by_slot` | UNIQUE `(workspace_id, epoch, shard, slot)`; indexed epoch enumeration and an independent slot-uniqueness constraint |

Neither table has an age-based deletion policy. Authorization bindings and epoch
identities are permanent tombstones, including after journal retention expires.
There is no budget per region: every workspace transition reads the complete
workspace primary-key prefix, including older unclosed epochs in all regions.

## Transactions and trust boundaries

All registry writes are Spanner mutation transactions; they do not mix mutations
with DML. The SDK retries aborted transactions. No registry call was added to the
regional handoff path.

| Operation | One read-write transaction |
|---|---|
| `register` | Read workspace epochs; enforce eligibility, tier cap, minimum shard capacity, no open epoch and no recycled epoch; write grant. Exact grant replay is a no-op. |
| `bind` | Read exact grant, epoch slots and globally keyed authorization tombstone; reject changed binding/occupied slot/inactive grant; read authorization and indexed reservations' settled state; write pending obligation. |
| `allocate` | Same transaction as bind, choosing the first unused slot only in the stable hashed shard. Replay preserves the ordinal. |
| `retire` | Read exact grant and change active to retiring. Allocation stops before journal sealing. Closed remains closed. |
| `record_bound` | Validate trusted, sealed metadata for every shard; read exact retired grant and persist the minimum of prior bound and observed outstanding. |
| `successor` | Read all workspace epochs; replay the immutable link or require all open epochs retired with sealed bounds; subtract **all** their bounds from `tier_cap`; write new grant and predecessor link together. No region migration. |
| `reconcile` | Read terminal evidence from pinned journal storage, then transactionally validate the exact durable binding and immutable payload; persist terminal/acknowledged projection. Pending or unavailable evidence never clears a guard. |
| `close` | Before the transaction, independently read pinned sealed journal shards/pages, zero outstanding and acknowledgment for every registered slot; enforce each idempotency deadline. Inside the transaction, revalidate the retired grant and exact binding set, validate immutable payloads, and persist every acknowledgment projection together with closed state, zero bound and monotonic retention deadline. Closed replay checks durable terminal states and needs no journal slots. Missing storage before initial close is a refusal. |

Read ports `grants`, `tickets`, `registered`, `is_retired`, and
`retention_deadline` use strong snapshots. K/S are checked as part of the exact
Grant value, not inferred from current settings.

`SpannerReceiptVerifier` uses a strong snapshot of the obligation and grant. It
requires the full ticket binding, matching payload hash, terminal state and an
exact durable `ledger_receipt` document:

```json
{"kind":"ledger_finalization","ticket":"<full Ticket object>","receipt":"<Receipt object>","amount":"<obligation amount>"}
```

The schematic strings above stand for actual JSON objects and an integer.
Serialization is sorted keys with compact separators. An import receipt, a
changed ticket/hash/epoch, or an absent durable ledger receipt is rejected.
There is deliberately no standalone production receipt minting API. PR 7 must
write this document in the **same transaction** as authoritative ledger
finalization. Tests seed that transaction boundary explicitly; this PR does not
implement PR 7's ledger worker or claim end-to-end drain completion.

## Reaper and retention guard

The added correlated predicate is:

```sql
AND NOT EXISTS (
  SELECT 1 FROM tr_async_settlement_obligation a
  WHERE a.authorization_id = tr_reservation.authorization_id
    AND a.state NOT IN ('acknowledged', 'fenced')
)
```

Authorization retention uses the same predicate correlated to
`tr_gateway_authorization.authorization_id`. `fenced` is written only from an
irrevocable journal terminal CAS. `sync_required`, `accepted`, and `pending`
remain guarded, regardless of age or the current admission flag.

* When the obligation table exists, both advisory scan variants apply the predicate **before LIMIT**, so guarded
  holds cannot starve later candidates, including when the old outbox is absent.
* Both transactional reaper paths read `ASYNC_GUARD_COUNT_SQL` by complete
  authorization primary key before claiming. This is the existing MF2 pattern.
* Binding performs the inverse read of settled authorization/reservations, so a
  reaper that wins first prevents subsequent admission.
* Typed snapshot booking shares the same transactional guard.
* Reservation claim TTL, reservation retention, authorization retention, and
  outbox-completion retention all preserve unresolved obligations.

With the obligation table absent or present with no obligation rows, returned values and stored billing/retention payloads
match the parent. The only runtime change is guard evaluation.

## Evidence and limits

Parent capture revision: `54914fb2a668ba57386c8f5ab624f1f9aecb5cfe`.
`tests/async_settlement_dormancy.py` captures canonical results and durable rows;
the tests pin the SHA-256 of bytes independently captured from that parent.
Eight typed combinations cover cohort, heartbeat and snapshot booking; two more
cover missing typed authorizations and the legacy reaper fallback. Round 2 runs
all ten with the obligation table absent and present. Seven more byte captures
cover ordinary settle, sequential and speculative finalization, and standalone
outbox completion with each applicable outbox-table availability. They run with
the obligation table both absent and present, for 34 comparisons to the parent. Generation
identity and the legacy settle clock are fixed at input, with no output fields
removed or normalized away.

The existing fake now models obligation PK reads, registry range conflicts,
and SQL-sensitive guard predicates. Tests cover all registry transitions,
crashes after each transaction statement, restart/replay, both commit orderings
of guard vs reaper for both entry points, snapshot booking, retention, starvation,
and all 30 PR 5 randomized three-epoch schedules with the durable registry.
The conformance test also executes the registry lifecycle on native GoogleSQL
when its emulator fixtures are available.

Round-1 call-count capture on the repository fake (before the round-2 availability
probe; not server latency or scanned rows):

| Typed reaper candidate | Parent | This PR |
|---|---:|---:|
| Snapshot execute calls | 2 | 2 |
| Transaction SELECT calls | 4 | 5 |
| DML calls, refund | 7 | 7 |
| DML calls, snapshot booking | 8 | 8 |
| Legacy fallback transaction SELECT calls | 6 | 8 |
| Legacy fallback DML calls | 4 | 4 |

The additional transaction read is a complete `authorization_id` PK lookup. Advisory and
retention guards are correlated on that same complete PK; the source manifest
registers these exact SQL expressions and the registry's filtered query forms.

**Native execution and measured server cost remain blocked locally.** There is
no Docker/Podman runtime, configured emulator, or loopback listener on 9010/8086.
The native command skips server tests without opt-in, and with CI's opt-in it
fails connecting to loopback. Neither a green offline inventory nor fake counts
prove the emulator gate or an execution plan. Before landing, run CI's two gates
with both emulators available:

```bash
export SPANNER_EMULATOR_HOST=127.0.0.1:9010
export BIGTABLE_EMULATOR_HOST=127.0.0.1:8086
export TR_CONFORMANCE_EMULATOR_SCHEMA=1
export TR_SPANNER_POOL_SIZE=2
uv run pytest -q -rsx tests/conformance -k spanner-emulator
uv run pytest -q -rsx tests/conformance/test_spanner_schema.py tests/conformance/test_spanner_sql_acceptance.py
```

Temporary-copy mutations (`python -m tests.async_settlement_mutations`) all
produce actual failing test assertions; collection errors are not counted:

| Mutation | Result |
|---|---|
| Remove transactional guard (advisory only) | RED |
| Condition guards on current enable flag | RED |
| Arm terminal_at with unresolved obligation | RED |
| Permit guarded snapshot booking | RED |
| Ignore older unclosed epoch bound | RED |
| Non-idempotent successor | RED |
| Recycle authorization across epochs | RED |
| Accept import receipt as finalization | RED |
| Permit mutable K/S | RED |

## Round-1 local validation (2026-09-29)

Use the supplied repository venv and a writable cache. In this worktree a bare
shell `python3` resolves to Conda 3.10; `uv run --no-sync` also gives shell-test
subprocesses the correct venv PATH without modifying that external venv:

```bash
export UV_CACHE_DIR=/private/tmp/pr4-uv
export UV_NO_SYNC=1
export UV_PROJECT_ENVIRONMENT=/Users/jperla/josh/repos/tr/quill-router/.venv
export PYTHONPATH=src
export PYTHONDONTWRITEBYTECODE=1
```

| Check | Result |
|---|---|
| Requested journal / stage D / outbox / reservation / quota / SDK subset | 2,065 passed, 3 skipped |
| Final current obligation + migration + outbox guard tests | 144 passed |
| Offline SQL acceptance/inventory/schema/DDL checks | 426 passed, 556 server-dependent skips |
| Native conformance without configured emulators | 8 passed, 137 skipped, 1,677 deselected; not a native gate pass |
| Native registry lifecycle with CI opt-in | Setup error: connection refused on localhost:9010 |
| Nine temporary-copy mutations | All RED |
| Full-suite cases (all 17,676 progress outcomes) | 16,567 passed, 48 failed, 1,049 skipped, 12 xfailed |
| Saved full-run coverage, independently reported with `--fail-under=70` | 85% |
| Repository-wide `ruff check .` | All checks passed |
| `mypy src/trusted_router` | No issues in 404 source files |
| Migration/preflight `bash -n`; `git diff --check` | Passed |

The raw full-suite invocation uses `--cov=trusted_router --cov-report=term
--cov-fail-under=70`, `-p no:cacheprovider`, and a `/private/tmp` basetemp.
Its 46 failing ledger-retirement shell cases picked Conda 3.10 and failed on `datetime.UTC`;
the same failure reproduces on the unchanged parent with that PATH. The entire
file passes with the venv PATH (57 passed). Two OAuth cases returned HTTP 408 in
the full run; both pass together on isolated rerun (2 passed). These reruns do
not turn the raw full-suite result into a green run.

Evidence logs are retained outside the worktree: `/private/tmp/pr4-requested.log`,
`pr4-final-current.log`, `pr4-acceptance-final.log`, `pr4-native.log`,
`pr4-native-required.log`, `pr4-mutations-final.log`,
`pr4-snapshot-mutation-final.log`, `pr4-full-serial.log`,
`pr4-parent-same-python.log`, `pr4-ledger-correct-env.log`,
`pr4-oauth-final.log`, `pr4-coverage-report.log`, `pr4-ruff-final.log`, and
`pr4-mypy-final.log`. Full-run outcome counts above are reconstructed from all
17,676 progress characters, including characters sharing a line with SDK
warnings; the original pytest process stalled in coverage reporting after all
test outcomes and failure tracebacks were emitted. An independent coverage
report reads its completed `/private/tmp/pr4-coverage-serial` database.


## Round-2 deploy-order validation (2026-09-29)

The fake now rejects every SELECT/DML referencing an explicitly absent table,
including correlated retention predicates. Tests cover separate outbox/obligation
availability in all four combinations, false-to-true TTL transition, uncached
non-table probe errors, and enabled-registry refusal on a partial migration.
The cold-settle RPC-order tests pin **both** cache resets and assert the extra
obligation probe with its exact empty-key parameters; no existing money/order
assertion was removed. Warm production processes reuse the positive cache.

A temporary parent package assembled with read-only `git show HEAD:<path>`
re-executed all capture scenarios. No git writes, real migration, or Docker
installation was used. Both table states have the same independent parent hashes:

| Capture | Cases per schema state | SHA-256 |
|---|---:|---|
| Typed reaper | 8 | `c4ea6650a4aa64dff23ec2a4025010ca82f1c2347534b76e99acfb35fac04bfe` |
| Legacy reaper | 2 | `78873e0382ad4659e03c002c21141c5f99a6510b38305c7914c39db5738cec8c` |
| Standalone settle/finalize/outbox | 7 | `b13efc8fad36629d1394a0e642e18b8c38547bb3f4fa1ef80308bb12e47ef8ac` |

The four added temporary-copy mutations all fail assertions: always-present
availability breaks pre-migration settle; always-absent skips post-migration
guards; flag-conditioned availability skips disabled-but-live obligations;
conflating availability fails the independent-table matrix. All nine round-1
mutations also remain red (13/13). Collection errors do not count as kills.

Offline schema coverage includes every new literal SQL variant and builder
combinations for outbox/obligation availability. Native emulator execution is
reserved for CI's workflow-dispatch gate, as requested; offline skips do not
constitute native execution evidence.


| Round-2 check | Result |
|---|---|
| Journal / stage D / outbox / reservation / quota / SDK / obligation / migration suite | 2,131 passed, 3 skipped |
| Offline SQL/schema/DDL acceptance and carrier checks | 295 passed, 584 emulator-dependent skips |
| Parent captures | 17 scenarios × absent/present = 34 byte-identical comparisons |
| Temporary-copy mutations | 13/13 RED |
| `uv run ruff check .` | All checks passed |
| `uv run mypy` | No issues in 404 source files |
| `bash -n` migration/preflight; `git diff --check` | Passed |

Round-2 evidence logs are `/private/tmp/pr4-r2-requested-final.log`,
`pr4-r2-sql.log`, `pr4-r2-capture.log`, `pr4-r2-mutations.log`,
`pr4-r2-ruff.log`, and `pr4-r2-mypy.log`. The initial broad run's three cold
RPC-count assertions were updated to pin the added probe, then the entire
requested suite above was re-run successfully.


### Full-suite result and final follow-up

The completed serial full run reports **16,638 passed, 6 failed, 1,077 skipped,
12 xfailed** across all 17,733 cases. Branch coverage is **85.51%**, above the
70% gate. The raw full-suite result is not green. Five failures returned HTTP
408 (`Request body timed out`) instead of their expected status:

* `test_gateway_routing_state.py::test_gateway_settlement_affinity_performance_and_hard_filters`
* `test_internal_fetch_image.py::test_rejects_missing_auth`
* `test_oauth_apps_increment_b.py::test_registration_rejects_reserved_ids[admin]`
* `test_oauth_key_delegation.py::test_oauth_browser_approve_redirects_with_code_and_user_id`
* `test_wallet_only_billing.py::test_wallet_only_api_checkout_rejects_every_non_stablecoin_rail[card]`

The sixth was `test_settle_post_reply.py`'s exact RPC-count assertion. It now
pins the additional obligation probe's exact SQL, complete-key parameters and
types, and expects one extra snapshot read. All existing transaction, money,
reply-order and background-payload assertions remain intact. The **entire
post-reply file plus all five HTTP cases passed together: 26 passed**. These
reruns do not turn the earlier full-suite invocation into a green run. No
production code changed after that full run; the follow-up changed only the
post-reply test's cold-probe expectations.

The first parallel coverage attempt was stopped incomplete under severe memory
pressure, then restarted serially. The serial run emitted the complete pytest
summary and saved coverage but remained alive during shutdown; shutdown interrupts were requested
only after those artifacts were complete. The process did not exit after those
requests, and process-list-based termination was blocked by the sandbox. No test timeout or guard was relaxed.
The native emulator gate remains for CI workflow dispatch.

Final evidence: `/private/tmp/pr4-r2-full-serial.log`,
`/private/tmp/pr4-r2-coverage-serial`, `/private/tmp/pr4-r2-final-reruns.log`.
All test basetemps created by this round were removed after use; evidence logs
and the coverage database are retained. Repository-wide ruff, mypy, shell syntax
and `git diff --check` pass. No git writes, real migrations, Docker installation,
or edits to `tests/conftest.py` were made.


## Round-3 atomic closure (2026-09-29)

Chosen option **(b)**: closure persists verified acknowledgment projections in
**the same Spanner read-write transaction** as the closed state and retention
deadline. Journal metadata/pages are read before the transaction; acknowledged
slots are immutable. The transaction rereads the epoch and all its obligations,
requires the exact verified binding set, and uses reconciliation's immutable
payload validation while preserving ledger receipts. Failure rolls back all
projections and the purge-enabling deadline together. Replay after purge reads
only durable Spanner state and refuses any non-terminal obligation.

This fixes the accept → ledger finalization → journal acknowledgment → crash
before reconciliation window without adding a separate recovery dependency to
`Journal.close_epoch`. That flow already delegates final closure to the registry;
its existing acknowledgment check and the registry's independent check remain.
**No PR 5 file changed.** Round-2 table-availability code is unchanged.

The crash/restart regression retains two accepted obligations, recreates the
registry and journal, and closes through both entry points. Before close, purge
refuses. An injected crash after both obligation projections and the grant write
rolls back all changes; purge still refuses. A successful retry leaves both
obligations acknowledged before purge, and closed replay works after both slots
are removed. The test also rejects any journal I/O during a Spanner transaction.

The direct registry negative uses a sealed shard with zero outstanding, a fenced
but unacknowledged slot, and a matching durable fenced obligation. Fenced is
terminal but not acknowledged, so a terminal-state check cannot mask removal of
the independent acknowledgment check. Temporary-copy mutations remove that
check and separately remove the closure projection: both must fail assertions.


| Round-3 check | Result |
|---|---|
| Obligation suite, including five new close cases | 157 passed |
| Same round-2 requested suites, plus the full post-reply file | 2,157 passed, 3 skipped |
| Offline SQL/schema/DDL acceptance and carrier/safety checks | 295 passed, 585 emulator-dependent skips (including the native registry case) |
| Independent parent captures | 17 scenarios × absent/present = 34 byte-identical comparisons |
| Temporary-copy mutations | 15/15 RED; omitted ack check: `DID NOT RAISE Conflict`; omitted projection: `accepted != acknowledged` |
| `uv run ruff check .` | All checks passed |
| `uv run mypy` | No issues in 404 source files |
| Shell syntax; `git diff --check` | Passed |

Evidence logs: `/private/tmp/pr4-r3-focused.log`, `pr4-r3-requested.log`,
`pr4-r3-sql.log`, `pr4-r3-ddl-safety.log`, `pr4-r3-capture.log`,
`pr4-r3-mutations.log`, `pr4-r3-close-mutations.log`, `pr4-r3-ruff-final.log`,
and `pr4-r3-mypy.log`. Native Spanner acceptance and server cost remain
unverified. No git writes, real migrations, or `tests/conftest.py` changes.


The fresh registry coverage run passed all 157 obligation cases and measured
**86.36% branch coverage** (`/private/tmp/pr4-r3-registry-coverage.log`, data:
`/private/tmp/pr4-r3-coverage`). This is module coverage, not a fresh global
coverage claim. The two-worker full instrumentation attempt stalled around 49%
and was interrupted; it produced no complete summary or usable coverage data.
Its partial output contains five failure markers without final diagnostics and is retained in `/private/tmp/pr4-r3-full.log`. File-handle
and worktree-process inventories confirmed that its workers exited. A serial
full-suite rerun without coverage instrumentation follows below.


Incremental global branch coverage is **85.51%**, above 70%. The artifact
`/private/tmp/pr4-r3-incremental-coverage` copies the round-2 full-run data for
unchanged production files, purges **all** old registry coverage, and replaces
it with the fresh registry run. Round 3 changes no other production file. This
is explicitly an incremental measurement, not a new full instrumented run.
The recipe and report are `/private/tmp/pr4-r3-incremental-coverage.py`,
`pr4-r3-incremental-coverage.log`, and `pr4-r3-incremental-summary.log`.


The completed serial full suite (`uv run pytest -q`, with an external temporary
progress reporter and `/private/tmp` basetemp) reports **16,649 passed,
1,077 skipped, 12 xfailed** in 2,866.98 seconds. It exited normally with code 0;
this is a green full-suite run, separate from the incomplete parallel attempt.
The complete log is `/private/tmp/pr4-r3-full-serial.log`. The per-test reporter
recorded no failures. No tests or timeouts were weakened to obtain this result.

All test processes launched for round 3 exited. File-handle and worktree-process
inventories verified no remaining pytest parent or workers. Every round-3 test
basetemp was deleted; evidence logs and coverage artifacts remain under
`/private/tmp`. Initial `df -h /private/tmp` showed 104 GiB available. The final
worktree retains the pre-existing round-1/2 changes; round 3 edits only this
note, the registry, the obligation tests, and their mutation runner.
