# Billing / Metering / Quota plane — typed-column conditional DML

Status: **design, reviewed** (panel synthesis + 2× codex adversarial review)
Owner: billing critical path. Changes ship incrementally; each step is
independently revertible and gated on tests + codex review (+ Joseph's go for
any cutover that changes live enforcement).

## 1. Problem

Per request the control plane synchronously does:

- **authorize** → reserve estimated credit: read-modify-write the per-workspace
  `credit:{workspace_id}` row *and* the per-key `api_key:{key_hash}` row;
- run the LLM call;
- **settle** → `finalize_gateway_authorization`: RMW the same two rows with the
  actual cost (plus reservation / generation / auth rows).

Every entity is a JSON blob in one Spanner table `tr_entities(kind,id,body)`.
The RMW pattern takes a shared read lock on the counter row, then upgrades to an
exclusive write lock. Two concurrent requests for the same hot workspace/key
both hold the shared lock and both try to upgrade → **wound-wait deadlock**
(`Aborted: Deadlock with higher priority transaction`). An 8-attempt retry
wrapper (`storage_gcp_io.run_in_transaction_with_retry`) absorbs normal
contention but a sustained hot tenant exhausts it, and the retries add latency
on the user-blocking authorize path.

## 2. Decision

Adopt the **typed-column atomic conditional-DML** design. Move the four hot
counters out of the JSON blob into native `INT64` columns on dedicated tables,
and replace each read-modify-write with a **single conditional DML**:

```sql
UPDATE tr_credit_balance
   SET reserved = reserved + @est
 WHERE workspace_id=@ws AND shard=0
   AND (total_credits - total_usage - reserved) >= @est
```

`transaction.execute_update()` returns the exact modified-row count
(verified on `google-cloud-spanner==3.65.0`): **1 = accepted, 0 = rejected**
(insufficient) — disambiguated from "no such row / uncapped" by a cheap
point-read only on the `0` path. The conditional UPDATE takes the row write
lock **directly**, so the shared-read→exclusive-upgrade cycle that deadlocks
today *cannot form*. Concurrent reservers serialize in microseconds; the loser
re-evaluates the predicate against committed state.

### Why this over leases / ledger

Considered: lease/token-bucket quota, async-metering + soft limit, event-sourced
ledger, sharded counters. The lease/ledger family scales a single key past the
single-row write ceiling, but pays for it by demoting Spanner from
system-of-record on the hot path, adding a second authoritative store (Redis) +
async reconciliation + bounded-overshoot reasoning — buying throughput we do not
yet have, at the cost of the two higher-ranked priorities (exact correctness,
simplicity). Typed-column DML fixes the deadlock at the root, keeps **exact,
strongly-consistent, multi-region** enforcement with **no new datastore**, and
is the shared foundation those designs need anyway. The door stays open:
per-whale sharding (built in, dormant) and a change-stream audit ledger are
additive, non-load-bearing escalations if a tenant ever proves it needs them.

## 3. Data model

Three typed tables. `shard` is in the PK from day one with `DEFAULT(0)` — the
long tail lives entirely on shard 0 (one row, exact, zero scan), and turning a
whale into N shards is a *data* change, not a schema migration.

```sql
CREATE TABLE tr_credit_balance (
  workspace_id      STRING(64) NOT NULL,
  shard             INT64 NOT NULL DEFAULT (0),
  total_credits     INT64 NOT NULL DEFAULT (0),
  total_usage       INT64 NOT NULL DEFAULT (0),
  reserved          INT64 NOT NULL DEFAULT (0),
  source_updated_at TIMESTAMP,   -- the JSON row's updated_at this mirror reflects
  updated_at        TIMESTAMP OPTIONS (allow_commit_timestamp=true),
) PRIMARY KEY (workspace_id, shard);

CREATE TABLE tr_key_limit (
  key_hash          STRING(64) NOT NULL,
  shard             INT64 NOT NULL DEFAULT (0),
  limit_micro       INT64,                 -- NULL = uncapped key
  usage             INT64 NOT NULL DEFAULT (0),
  byok_usage        INT64 NOT NULL DEFAULT (0),
  reserved          INT64 NOT NULL DEFAULT (0),
  include_byok      BOOL  NOT NULL DEFAULT (true),
  source_updated_at TIMESTAMP,
  updated_at        TIMESTAMP OPTIONS (allow_commit_timestamp=true),
) PRIMARY KEY (key_hash, shard);

CREATE TABLE tr_reservation (
  reservation_id        STRING(64) NOT NULL,
  workspace_id          STRING(64),
  key_hash              STRING(64),
  ws_shard              INT64,
  key_shard             INT64,
  credit_reserved_micro INT64,   -- EXACT credit hold taken at reserve
  key_reserved_micro    INT64,   -- EXACT key hold taken at reserve (0 if none)
  actual_micro          INT64,   -- booked at settle
  hold_usage_type       STRING(16),  -- usage_type the HOLD was taken under (Credits if any credit candidate)
  settled_usage_type    STRING(16),  -- usage_type the ACTUAL selected endpoint resolved to, set at settle
  settled               BOOL NOT NULL DEFAULT (false),
  idempotency_scope     STRING(256), -- workspace#key#sha256(idem) ; NULL if none
  idempotency_fingerprint STRING(64),
  created_at            TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  expires_at            TIMESTAMP,   -- for the crash reaper
) PRIMARY KEY (reservation_id);

-- scoped idempotency: NOT a global UNIQUE(idempotency_key)
CREATE UNIQUE NULL_FILTERED INDEX tr_reservation_by_idemp
  ON tr_reservation (idempotency_scope);
CREATE INDEX tr_reservation_by_expiry ON tr_reservation (settled, expires_at);
```

`tr_entities` stays the home of everything else; Bigtable generation rows are
unchanged. No Redis on the enforcement path.

## 4. Flows (final enforcement shape — Step 3+)

### Lock order — ONE order everywhere: **key, then credit**

codex finding #2: the panel had authorize=key→credit but settle=credit→key,
which still deadlocks. Both authorize and settle acquire **key first, then
credit**.

### authorize — ONE atomic read-write transaction

codex finding #1: today key reserve, credit reserve, and auth-create are three
separate operations; a crash between them leaks a hold. The typed design does
the whole authorize decision in one transaction:

1. **Idempotency (scoped):** point-read `tr_reservation_by_idemp` on
   `idempotency_scope = workspace#key#sha256(idem)`. On hit, verify
   `idempotency_fingerprint` matches the request; mismatch → reject (body
   changed under same key); match → replay the existing authorization, **no
   second debit**. (Preserves the current `(workspace_id, key_hash,
   hash(idempotency_key))` scope; does **not** use a global unique key, and does
   not `insert_or_update`-overwrite an existing mapping.)
   - **Concurrent first calls** (codex#2 #4 / red-team P4): two requests with the
     same key may both miss the index read before either commits; the
     `UNIQUE NULL_FILTERED` index makes the second committer's INSERT fail. The
     conflict surfaces as **`ALREADY_EXISTS` (an IntegrityError), which
     `run_in_transaction` does NOT retry — it only retries `Aborted`.** So the
     code must **explicitly catch `ALREADY_EXISTS`** on the reservation INSERT
     and convert it to the **replay path** (re-read by `idempotency_scope`, now
     guaranteed to hit the committed winner, run the fingerprint check → return
     the winner's auth on match, deterministic 4xx on mismatch). It must **never
     surface a 500**. (The loser's own holds rolled back with its aborted txn, so
     money-safety already holds; this closes the API contract.)
   - **Replay is resume / no-execute** (codex#2 #4): a replay returns the same
     authorization but the gateway must **not re-run the LLM call** — the
     reservation already holds budget and claim-gated settle charges exactly
     once, so a second provider call would be uncharged (we eat its cost).
     `idempotent_replay=True` means "resume the in-flight/finished
     authorization," not "execute again." (The route already returns
     `idempotent_replay`; the contract is that the caller resumes, not
     re-executes.)
2. **Key cap** (only if the key is capped and the hold applies — see BYOK):
   `UPDATE tr_key_limit SET reserved = reserved + @est
      WHERE key_hash=@kh AND shard=@ks AND limit_micro IS NOT NULL
        AND (limit_micro - usage - IF(include_byok, byok_usage, 0) - reserved) >= @est`.
   On **row-count 0**, classify with a point-read **inside the same
   transaction** after the failed DML (codex#2 #3) — do NOT decide from the
   pre-authorize `ApiKey` read:
   - typed row **missing** → fail closed (drift/not-yet-backfilled; alarm);
   - `limit_micro IS NULL` → uncapped → no hold, proceed;
   - BYOK-excluded (`usage_type==BYOK AND include_byok=false`) → no hold (the
     statement is skipped up front for this case);
   - capped & insufficient → 402.
3. **Credit balance** (CREDITS usage only):
   `UPDATE tr_credit_balance SET reserved = reserved + @est
      WHERE workspace_id=@ws AND shard=@ws_shard
        AND (total_credits - total_usage - reserved) >= @est`.
   row-count 0 → insufficient credits → 402. (We already grabbed the key hold in
   the SAME transaction, so a credit reject simply aborts the txn and releases
   the key hold atomically — no compensation DML needed.)
4. Insert `tr_reservation` recording **the exact holds taken** (`credit_reserved_micro`,
   `key_reserved_micro`), the resolved `usage_type`, shards, `expires_at`, and
   the idempotency scope/fingerprint; insert the `gateway_authorization`.
5. Commit. Run the LLM call.

All five steps commit in one `run_in_transaction` callback → atomic across both
counters; the ABORTED-retry wrapper still wraps it for the (now rare) genuine
contention.

### settle / refund — claim-gated, exact release

codex findings #4 & #5:

1. **Claim:** `UPDATE tr_reservation SET settled=true, actual_micro=@actual,
      settled_usage_type=@settled_ut
      WHERE reservation_id=@rid AND settled=false`. row-count **1 = this caller
   won**, **0 = already settled (replay) → return without touching counters.**
   First-writer-wins, preserved from today's `reservation.settled` /
   `authorization.settled` contract.
2. Winner releases **the exact recorded holds** (never `GREATEST(0, reserved -
   est)`, which masks double-release/drift):
   - key: `UPDATE tr_key_limit SET reserved = reserved - @key_reserved_micro
       [, usage += @actual | byok_usage += @actual]  WHERE key_hash=@kh AND shard=@ks`
   - credit: `UPDATE tr_credit_balance SET reserved = reserved - @credit_reserved_micro
       [, total_usage += @actual]  WHERE workspace_id=@ws AND shard=@ws_shard`
   Release the hold using the recorded **`hold_usage_type`** (so the exact
   key/credit holds taken at reserve are released even if the selected endpoint
   differs); book the **actual** to the column chosen by **`settled_usage_type`**
   (codex#2 #2: a request may take a CREDITS hold — because a credit candidate
   existed — yet actually run a BYOK endpoint; the hold must be released as
   CREDITS while the actual is bucketed as BYOK `byok_usage`, and credit
   `total_usage` is incremented only when `settled_usage_type == CREDITS`).
   key first, then credit; usage increment only on `success`.
   **Assert each release UPDATE returns row-count == 1** (red-team polish): a
   0-row release (e.g. the recorded shard no longer exists after a future Step-4
   rebalance, or a missing counter row) must **not** silently commit
   `settled=true` while booking nothing — that strands the hold *and* loses the
   charge. On a 0-row release, abort the settle txn (so the claim rolls back and
   the reaper/outbox re-drives it) and alarm. At N=1 (shard 0 always exists) this
   never fires, but landing the assertion now hardens the reaper and the Step-4
   sharding cutover.
3. refund = same claim, then release holds with **no** usage increment.

### Overdraft semantics (codex finding #6) — explicit decision

Conditional reserve guarantees accepted *estimates* never exceed available. It
does **not** prevent the final balance going slightly negative when the
**actual** cost of a request exceeds its estimate (streaming output longer than
predicted). We **preserve today's behavior**: settle books `actual` via
`total_usage += actual` and **permits bounded overdraft** of at most one
in-flight request's (actual − estimate) per concurrent request. For prepaid
credit this is a tiny, bounded, self-liquidating loss — acceptable, and what the
system already does. (Alternative — reserve a true upper bound from
`max_output_tokens` — over-reserves and rejects more; not adopted. Revisit only
if overdraft is observed to matter.)

The same bounded-overdraft acceptance covers **cache tokens** (codex#2 #5):
authorize estimates only normal input/output cost, but settle may add
provider-specific cache read/write charges (`gateway.py` settle pricing) that the
estimate did not predict. We explicitly **accept and bound** cache-token
overdraft on the same terms as output overdraft, rather than adding cache-token
estimate fields. (If cache charges ever dominate, add an estimate term then.)

### BYOK (codex finding #7)

The key hold is **skipped** when `usage_type == BYOK AND include_byok=false`
(no `tr_key_limit` reserve statement issued). At settle, actual BYOK usage is
still recorded to `byok_usage` (it counts toward the cap for keys with
`include_byok=true`). The SQL therefore needs a **usage-type predicate at
reserve** (skip the statement), and **usage-column selection by usage_type at
settle** (`byok_usage` vs `usage`), not merely `IF(include_byok, byok_usage,0)`
inside the available expression.

## 5. Spanner mechanics (verified on 3.65.0)

- `transaction.execute_update(dml, params, param_types)` returns the exact
  affected row count — the accept/reject signal. Use **standard DML**, not
  partitioned DML, on the hot authorize path.
- Multiple DML statements in one `run_in_transaction` callback are fine; the
  client retries `ABORTED` by re-invoking the callback (our wrapper adds more
  attempts of that same safe re-run).
- **Do not mix DML and mutations in the same transaction.** Spanner executes DML
  before buffered mutations, and mutations are invisible to later SQL/DML in the
  same txn (Google DML best-practices). The authorize/settle transactions
  therefore do **all** writes as DML (`INSERT`/`UPDATE` via `execute_update`),
  not `transaction.insert_or_update`. (Non-counter JSON writers elsewhere keep
  their mutation path.)
- Do not set `last_statement=True` unless it is genuinely the last statement and
  there are no mutations.
- **`PENDING_COMMIT_TIMESTAMP()` poisons the WHOLE table + its indexes**, not
  just the column (red-team P3): after a statement writes PCT to a table, *any*
  later SQL in the same transaction that touches that table (SELECT/INSERT/
  UPDATE/DELETE) fails the transaction. So the PCT-writing statement must be the
  **last touch of that table** in the txn. Since each authorize/settle txn
  already touches each counter table exactly once, this is a guardrail: add a
  review/CI invariant "no two same-table SQL statements in one txn when the first
  wrote PCT." (Alternatively skip PCT on the hot counter `updated_at` and set it
  from a read-free expression, keeping the single-touch rule simplest.)

## 6. Migration — incremental, each step revertible

**Step 0 — skipped.** The retry wrapper holds; "conditional JSON DML" isn't real
on a STRING body; async settle without a durable outbox/reaper risks lost
charges. Go straight to schema → exact dual-write → backfill → typed
enforcement.

**Step 1 — typed tables + EXACT-MIRROR dual-write.** `update_ddl` the three
tables. Every JSON writer of these counters (key create/update, credit grants,
reserve, settle, refund, finalize) additionally writes the typed rows. The JSON
path stays **authoritative**. Critical correction (codex): the shadow writes
mirror the **exact post-transaction counter values + `source_updated_at`** — they
do **not** run conditional accept/reject predicates (a shadow predicate would
manufacture drift by design). Emit a divergence metric (typed value vs JSON
value per write). Zero behavior change.

**Step 2 — backfill + reconcile to zero drift.** One-shot, **idempotent**,
guarded by source `updated_at` (never overwrite a newer dual-write). Hold until
drift is zero. Critical (red-team P2): the per-write divergence metric is
**structurally blind to a torn dual-write** — if the JSON leg commits but the
typed-mirror write aborts/OOMs, no sample is emitted, so "flat at zero" can pass
over real drift that then enforces real money at the flip. So add an
**independent periodic full-row comparator** (scan typed vs JSON with
`updated_at`-staleness tolerance), and **re-scan each cohort at flip time**
(not just the one-shot backfill); gate the Step-3 flip for a cohort on *that
comparator* reading zero, not on the per-write metric alone.

**Step 3 — flip enforcement to conditional DML (the deadlock fix). Includes
typed reservations + scoped idempotency index + reaper** (codex#2 #1: these
cannot lag behind the live cutover, or crash-after-authorize holds strand with
nothing to reclaim them). The cutover ships together:
- the one-shot atomic `gateway_authorize` and claim-gated `finalize` transactions
  (conditional-UPDATE row-count = authoritative accept/reject);
- `tr_reservation` + the scoped `tr_reservation_by_idemp` unique index (collapses
  the three JSON idempotency entities into one indexed point-read);
- the Cloud Scheduler **reaper** over `tr_reservation_by_expiry`, racing late
  settles safely via the `settled` claim (one wins, the other no-ops), claim +
  release in **one transaction**.
  **`expires_at` is an execution deadline = max stream duration + settle-retry
  window + margin** — it MUST be beyond the longest legitimate run, or the reaper
  could win the `settled=false` claim and leave a successful LLM call uncharged
  (codex#2 #1 / red-team P1).
  The reaper must distinguish **crashed-before-completion** (no provider cost →
  release the hold, book nothing — correct) from **completed-but-settle-lost**
  (a real provider call whose settle never landed → must book actual, not free).
  To make the latter recoverable, settle actuals are written to a **durable
  outbox** when the gateway receives the response, so a lost settle is re-driven
  from the outbox rather than silently reaped to zero. (This is the one piece
  that must exist before the reaper can safely release holds.)

Cutover flips **reserve and settle together, per cohort** (typed-reserve +
JSON-settle leaks holds; JSON-reserve + typed-settle invents refunds). An
in-flight request that reserved under JSON must settle under JSON — gate on the
reservation's own origin (a flag/marker on the reservation), not the cohort's
current state, so a request straddling the flip settles where it reserved. Keep a
lazy exact JSON mirror for ≥1 release so rollback is a flag flip, not a repair.
Ramp by workspace cohort; credit first, then key cap. The ABORTED-retry should
now essentially never fire.

**Step 4 — per-whale shard fan-out (DEFERRED, metrics-driven).** Only when a
specific key/workspace demonstrates the single-row ceiling. Reserve targets
`hash(request_id) % N`; settle hits the recorded shard; available *display* is a
lock-free `SUM` over shards; on a per-shard `0`-row deny, do an exact
sum-on-deny read before rejecting. A single-writer-per-tenant,
conservation-invariant rebalancer (`SUM(slices) == total_credits`, moved in one
txn) ships **observe-only** first; enable apply only with the invariant auditor
green. Everyone else stays N=1/exact.

**Step 5 — (optional) change streams for audit + soft display cache.** Spanner
change streams on the counter tables give a native ordered audit ledger; an
optional Redis cache may serve available-balance *display* reads (never the
gate).

Rollback at any step = flip the enforcement source back to the dual-written JSON
path.

## 7. Single-row write ceiling — measure, don't assume

Typed DML removes the *deadlock*, not physics: one row tops out at
~hundreds–low-thousands of conditional-UPDATE commits/sec (single-writer-per-
commit + cross-region Paxos). One request ≈ two writes to the key row and (for
credit routes) two to the credit row (reserve + settle/refund). **Before/after
Step 3, measure hot `(workspace_id, shard=0)` and `(key_hash, shard=0)` commit
QPS + the abort-rate metric.** If the production hot key is already several
hundred req/s, build Step 5 sooner; if the incident is abort storms at lower QPS,
Step 3 alone fixes it. The shard flag is pre-wired so a whale can be sharded
*before* it saturates.

## 8. Test plan (must pass before each cutover)

Concurrency + correctness (extend `tests/test_storage_gcp_concurrency.py`,
`test_credit_ledger_idempotency*.py`, `test_billing.py`,
`test_storage_gcp_features.py`):

- N concurrent reserves on one workspace: exactly `floor(available/est)` accept,
  rest 402; `reserved` never exceeds available; **no deadlock / no ABORTED
  surfaced**.
- Concurrent same-idempotency-key authorize → one reservation, one debit.
- Idempotency-key body mismatch → rejected.
- settle/refund race (half each) → first-writer-wins, charged exactly once,
  ledger never negative beyond bounded overdraft, never double-applied.
- BYOK with `include_byok=false` → no key hold at reserve, `byok_usage` recorded
  at settle; with `include_byok=true` → counts toward cap.
- Key limit removed/changed between reserve and settle → releases the **exact**
  recorded hold (no leak, no phantom refund).
- actual > estimate → bounded overdraft booked, accounted once.
- Step 1/2: per-write divergence metric == 0 AND the independent full-row
  comparator == 0 across a replay of recorded traffic; a deliberately torn
  dual-write (typed leg skipped) is caught by the comparator, not the per-write
  metric.
- Concurrent same-key first calls: the index loser hits `ALREADY_EXISTS` and is
  converted to a successful replay (never a 500); fingerprint mismatch → 4xx.
- N concurrent same-key+same-workspace reserves on the uncapped 0-row path raise
  **no ABORTED** (the disambiguation read must not introduce a lock-upgrade).

InMemory store mirrors the same contracts so the suite runs without Spanner.

## 9. Review log

- **Design panel** (5 architects + synthesis): chose typed-column conditional DML
  over lease/ledger/sharded/async for a high-scale router at current scale.
- **codex review #1** (REVISE): 7 money-safety findings (atomic authorize;
  single key→credit lock order; scoped idempotency+fingerprint; claim-gated
  settle; exact recorded-hold release; explicit overdraft decision; BYOK
  predicate) + Spanner mechanics (execute_update row-count; no DML/mutation
  mixing; exact-mirror dual-write not shadow-conditional). All folded in.
- **codex review #2** (REVISE): 5 specs — merge reaper/idempotency into the Step-3
  gate + define `expires_at`; split hold-usage-type vs settled-usage-type;
  stricter 0-row key classification; replay = resume/no-execute + concurrent
  unique-conflict→replay; cache-token overdraft. All folded in.
- **Money-safety red team** (7 attackers + synthesis): **no blockers** — Step 1
  ready now; double-charge and deadlock-reintroduction are FALSE alarms (claim +
  releases in one atomic txn = exactly-once; key→credit order holds). Pre-cutover
  fixes P1 (reaper books actual + durable outbox), P2 (independent comparator +
  flip-time rescan), P3 (PCT poisons whole table), P4 (`ALREADY_EXISTS` not
  retried → catch→replay), plus row-count==1 on releases. All folded in.
```
