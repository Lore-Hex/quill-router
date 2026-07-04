# Durable settle outbox

Status: **design v2 hardened + Increment 1 (storage) landed; Increments 2–4
remain.** The fast kill-switch half of #43/#33 shipped (#109/#110). A multi-agent
adversarial review of design v1 found 4 critical correctness defects (v1 would
have silently lost charges while reporting them "recovered"); v2 folds in the 7
must-fix items. Joseph approved building it in codex-gated increments; the
prod-enable flag flip stays owner-gated. **See §11 for the implementation status
& handoff — read that first if you are picking this up.**

## 1. Problem

When the attested gateway (quill-cloud-proxy enclave) finishes a request it calls
`POST /internal/gateway/settle` with token actuals; `_settle_gateway_authorization`
books the real cost against the reservation (`typed_finalize_gateway_authorization`
for typed reservations, the legacy path otherwise). The reservation is one-shot:
a second settle on an already-settled authorization returns `already_settled`.

The gap is a **settle that never lands**: the enclave computed actuals but the
settle failed and retries exhausted (Spanner blip during finalize, a crash after
computing cost but before commit, a partition outliving the enclave's retry
budget). The hold then sits until `reap_expired_reservations` reclaims it — and
the reaper **releases the hold WITHOUT booking a charge** (`actual_micro=0`,
`success=False`): a completed request served **for free**. Bounded and rare today,
but a whale multiplies the rare loss into real dollars and a correlated Spanner
incident loses a burst at once. `reap_expired_reservations`'s own docstring names
the outbox as the planned fix.

## 2. Goal

Recover completed-but-settle-lost charges — no request that ran is released free —
without ever double-charging, without a new hot-path deadlock, and with **zero
behavior change until deliberately enabled** (default-off).

## 3. The correctness spine (READ FIRST — v1 got this wrong)

The single most important fact, established by the review against the source:

> **When the reaper free-releases, it flips `tr_reservation.settled=true` but does
> NOT mark the `gateway_authorization` entity settled** (only
> `typed_finalize_atomic` does that, `storage_gcp_authorize.py:422-425`). So a
> post-reaper drain that re-derives "settled?" from `authorization.settled`
> (`gateway.py:714`) sees **False**, proceeds, loses the `claim_reservation`
> race (`won=False`), books **nothing**, returns `finalized=False`, and — in v1 —
> marks the outbox row "done" and increments a "recovered" metric. **The charge
> is permanently lost and the dashboard calls it healthy.**

Consequences that shape the whole v2 design:

- **The reaper guard is the SOLE correctness mechanism.** "Drain cadence < reaper
  `expires_at`" buys *latency/liveness only* — it is NOT a safety guarantee. Once
  the reaper wins, the charge is gone. Correctness must not depend on the drain
  winning a race.
- **`finalized=False` is ambiguous** — it means (a) already charged inline, (b)
  reaper released it free (charge lost), or (c) no credit reservation. The drain
  MUST NOT collapse these to "done". It needs a **richer finalize outcome**
  (see the §6 `ApplyOutcome` contract) and must treat
  `already_released_free` on a row that intended a charge as an **invariant
  violation → alert, never silently done**.
- The **double-charge direction is already safe**: `claim_reservation` is
  first-writer-wins, so N inline+drain replays book at most once. v2 must preserve
  that and only fix the lost-charge direction.

## 4. Reaper guard — the real interlock (fixes MF1/MF2/MF3)

The reaper must not free-release a hold the outbox still intends to charge.

1. **In-transaction re-check, not a snapshot filter (MF2).** `reap_expired_reservations`
   scans candidates on a read-only snapshot and then settles each in a *separate*
   `settle_atomic` txn. Adding `NOT EXISTS(tr_settle_outbox …)` to the snapshot
   SELECT evaluates the guard at snapshot time — an enqueue committing between the
   scan and the per-row claim would be missed and the hold free-released. So the
   guard is re-checked **inside `settle_atomic`'s read-write claim transaction**
   (a strong read of `tr_settle_outbox` by `authorization_id`) when invoked from
   the reaper path; if an intent row exists, **abort the free-release**. The
   snapshot-scan check is an advisory optimization; the in-txn re-check is the
   interlock, serialized by Spanner against a concurrent enqueue. (The reaper
   SELECT must also project `authorization_id` — it currently projects only
   `reservation_id`.)
2. **Dead rows FREEZE the hold, they don't release it (MF3).** The guard suppresses
   the reaper for `status IN ('pending','dead')`. A `dead` row (drain gave up)
   means "we have actuals for a request that ran but can't apply them" — the hold
   must be **frozen for a human**, never auto-freed at `actual=0`. A distinct,
   human-set terminal state `release_approved` is the ONLY status the reaper may
   free on. Dead-row runbook: engineer inspects, usually applies a manual
   charge/adjustment (replay no-ops once settled), then sets `release_approved`.
3. **The fake must model the guard or the tests are theater (MF6).** The fake
   Spanner matches the reaper by the substring `FROM tr_reservation WHERE
   settled=false AND expires_at` (`tests/fakes/spanner.py:523`). Adding a
   `NOT EXISTS` subquery leaves that substring intact, so the fake still matches
   the generic branch and **silently ignores the guard** — every guard test would
   pass even with the guard removed. Required: a dedicated fake matcher that
   detects `tr_settle_outbox` + the status predicate and filters candidates
   against a fake outbox table *before* the generic reaper branch, and tests that
   assert **both** directions (a reservation WITH a pending/dead row is skipped;
   a guard-column/status typo makes a test FAIL). General rule (add to the fake's
   header): *any predicate added to an already-matched SQL string needs a
   corresponding fake change or the fake masks it.*

## 5. Table + enqueue — native INSERT-as-claim, frozen inputs (fixes MF4/MF5/MF7)

### 5.1 `tr_settle_outbox` is a NATIVE Spanner table, not the broadcast upsert (MF7)

v1 said "mirror the broadcast durable-job pattern." That is right for the **state
machine** (pending/done/dead + lease + exponential backoff + max_attempts) but
WRONG for persistence: the broadcast store writes jobs via `write_entity` — a
last-write-wins UPSERT with a per-call `bdel_{uuid}` id — which has **no PK
uniqueness and cannot raise `ALREADY_EXISTS`**. The exactly-once argument depends
on **INSERT-as-claim**, which is the *typed-counter* mechanism
(`storage_gcp_counter_dml` INSERT-DML raising `AlreadyExists`), not broadcast.
So: `tr_settle_outbox` is a native Spanner table with a real PRIMARY KEY, enqueued
via INSERT DML that raises `AlreadyExists`. Mirror broadcast's *state machine*,
not its storage. **InMemory backend: no-op/unsupported** (durability needs
Spanner); the mechanism is only active on `spanner-bigtable`.

### 5.2 Primary key handles settle-vs-refund polarity (SF1)

One authorization can be targeted by a **settle** (`success=True`, `/settle`) and
a **refund** (`success=False`, `/refund`); `success` is not in the request body,
it's decided by the route. Key the row on **`(authorization_id, intent_kind)`**
where `intent_kind ∈ {settle, refund}`, so the two intents never clobber each
other. On `ALREADY_EXISTS` for a still-`pending` row of the same kind, **UPDATE
the stored body/frozen inputs to the latest delivery** (the enclave may retry with
corrected token counts, SF9) rather than freezing the first delivery.

### 5.3 Columns — store FROZEN, fully-resolved settle inputs (MF4/MF5)

Do **not** store only the raw token counts and re-run routing+pricing at drain
time — a drain that runs after a pricing/endpoint change (common: the recovery
drain often runs across the very deploy following an incident) would book a
different amount, and a retired endpoint would 400/500 → churn to dead. Persist
what the inline attempt already resolved:

| column               | type         | note                                              |
|----------------------|--------------|---------------------------------------------------|
| `authorization_id`   | STRING       | PK part 1                                          |
| `intent_kind`        | STRING       | PK part 2: `settle` / `refund`                     |
| `settle_origin`      | STRING       | **`typed` / `legacy`, captured from the SAME `is_typed_reservation` decision the inline attempt used** (MF4) |
| `reservation_id`     | STRING       | the credit reservation id                          |
| `actual_cost_micro`  | INT64        | **frozen** cost computed by the inline attempt     |
| `selected_endpoint_id` | STRING     | frozen (for the generation record)                 |
| `model_id`           | STRING       | frozen                                             |
| `selected_usage_type`| STRING       | frozen                                             |
| `settle_body`        | STRING(MAX)  | raw `GatewaySettleRequest` JSON (audit/generation) |
| `status`             | STRING       | `pending` / `done` / `dead` / `release_approved`   |
| `attempts`           | INT64        |                                                    |
| `next_attempt_at`    | TIMESTAMP    |                                                    |
| `lease_owner` / `leased_until` | STRING / TIMESTAMP | drain lease                          |
| `created_at` / `updated_at` | TIMESTAMP |                                                   |

DDL: guarded `CREATE TABLE` **and** a `CREATE INDEX` on `(status, next_attempt_at)`
for the due-scan (a full-table scan under a whale burst is the very load the
feature targets) — using the same `table_exists`/`index_exists` guards the
typed-counter migration actually uses (SF10), not a hand-wave.

### 5.4 Enqueue ordering

Enqueue (INSERT) **before** the inline settle, gated on `settle_outbox_enabled`
(default **False**). Durable the instant the INSERT commits; a crash anywhere
after is recoverable. On inline success, `UPDATE status='done'`. When the flag is
off, the settle path is byte-identical to today (the enqueue is the only added
statement and it's skipped). Honest scope (MF/SF5): this recovers losses **after
the enqueue INSERT commits** — a crash between receiving the POST and the enqueue
still relies on the enclave re-delivering; quantify recovery as "finalize-after-
enqueue failures", not "all losses".

## 6. Drain — apply the FROZEN amount via a narrow primitive (fixes MF4/MF5/SF7)

`POST /internal/gateway/settle-outbox/drain?limit=N` (internal-token auth),
lease-claims due `pending` rows and for each:

- Routes on the **stored `settle_origin`**, immune to a `TR_TYPED_COUNTER_MIRROR`
  kill-switch flip after enqueue (MF4). If `settle_origin='typed'` but the typed
  store is currently unavailable, **PARK** the row (retry later) — never reroute to
  legacy, never dead-letter.
- Applies the **frozen `actual_cost_micro`** through a **narrow finalize primitive**
  (counter claim + `gateway_authorization` finalize + generation write), NOT the
  full `_settle_gateway_authorization` HTTP handler — which would re-run pricing
  and re-fire non-idempotent side effects (budget alerts, auto-refill, metadata
  broadcast, provider-benchmark samples) on every replay (SF7).
- Interprets the **richer finalize outcome** (§3): `settled_now` or
  `already_settled_with_charge` → `status='done'`; `already_released_free` on a
  row that intended a charge → **`dead` + alert** (the reaper beat us — invariant
  violation, do not report "recovered"); deterministic non-retryable errors →
  `dead` (no page); transient → backoff. After `max_attempts` → `dead` + alert.
- Final `ApplyOutcome` contract for the drain:
  `settled_now` → done. `already_settled_with_charge` means done for settle
  intent; for refund intent with a charged reservation, done plus the same
  low-priority review flag as legacy (a kept charge beat a refund intent, and
  typed has strictly better information so it must not alert less). A frozen
  `actual_cost_micro=0` replay is in this benign bucket even when the typed
  reservation booked 0. `already_released_free` means a real settle charge was
  lost unless the row intent is refund. `already_settled_legacy` → done, plus a
  low-priority review flag if a sibling refund-intent row exists for that
  authorization. `reservation_missing` → dead + alert: the row references an
  authorization/reservation that does not exist, so replay cannot repair the
  deterministic broken reference. `park_typed_unavailable` covers missing typed
  capability and transient typed-store outages at all three store touchpoints:
  pre-read, finalize, and disambiguation read; release the lease and do not
  increment attempts. `invalid_row` is deterministic dead; `error` uses transient
  backoff.
- `mark` updates status/lease/next_attempt_at/attempts in a **single lease-fenced
  conditional-DML transaction** on the native row (SF8) — do NOT copy broadcast's
  non-atomic delete-then-write index rewrite.

## 7. Exactly-once argument (corrected)

- **INSERT-as-claim** keyed by `(authorization_id, intent_kind)` → at most one
  intent per (auth, kind).
- **Apply** (inline or drain) goes through the first-writer-wins
  `claim_reservation` gate → N replays book at most once (double-charge safe).
- **Lost-charge safety rests ENTIRELY on the reaper guard** (§4): the reaper never
  frees a hold with a `pending`/`dead` outbox row, re-checked in-transaction. It is
  NOT provided by "drain wins the race" (v1's false claim).
- Residual, explicitly accepted: a crash between the inline finalize commit and the
  `status='done'` UPDATE leaves a `pending` row for an already-charged auth — safe
  (drain replay → `already_settled_with_charge` → done), just a redundant replay
  (SF4). If the outbox shares the Spanner instance, prefer writing `status='done'`
  in the same txn as the finalize to close even that.

## 8. Rollout (default-off, Joseph-gated)

1. Guarded additive DDL (table + `(status,next_attempt_at)` index) — safe pre-code.
2. Merge the mechanism with `settle_outbox_enabled=False` **and the reaper guard
   active-but-inert** (an empty `tr_settle_outbox` makes `NOT EXISTS` always true,
   so the reaper is byte-identical to today). Dead code otherwise.
3. Shadow: enable enqueue only (write rows, still rely on inline settle + guard),
   watch `dead`-row count + a **truthful** "recovered vs lost-to-reaper" metric
   split (§3).
4. Flip `settle_outbox_enabled=True` fully once shadow is clean. **Billing
   prod-behavior flip → Joseph's explicit go** (typed-cutover bar).

## 9. Open question for the owner (unchanged — §6 of v1)

The in-repo outbox recovers **control-plane-side** losses (settle reached the
control plane at least once, then finalize failed/crashed). It does **not** cover
"enclave computed actuals but never delivered" (total partition beyond the
enclave's retry budget) — that needs a quill-cloud-proxy change (cross-repo,
`NO PRs` / push-to-main with Joseph's go). Recommendation: ship the control-plane
outbox first, then decide enclave-side durability from the observed lost/`dead`
rate.

## 10. Test plan (full pyramid, guard-aware)

- **Unit (fake Spanner, guard actually modeled — MF6):** INSERT-as-claim idempotency
  (double-INSERT → one row, `ALREADY_EXISTS` updates a pending row's frozen inputs);
  drain applies frozen cost via the narrow primitive; the richer finalize outcome
  distinguishes already-charged / released-free / missing; backoff → dead; the fake
  filters reaper candidates by the outbox guard and a guard typo makes a test FAIL.
- **Reaper interlock:** a reservation with a `pending` OR `dead` row is NOT
  free-released; one with `release_approved` IS; the in-txn re-check beats an
  enqueue that commits between the reaper snapshot and its claim txn (MF2).
- **Origin fidelity (MF4):** a typed row drains typed even with the mirror flag
  flipped off after enqueue; parks (not dead) when the typed store is unavailable.
- **Cost determinism (MF5):** a drain after a simulated pricing/endpoint change
  books the frozen amount, not the recomputed one; a retired endpoint does not 400.
- **Integration:** authorize → inline settle fails → reaper suppressed by the
  pending row → drain recovers the frozen charge → balance reflects real cost, not
  a free release; and the lost-to-reaper path alerts rather than silently "done".

## Appendix — review provenance

v2 folds a 7-item must-fix set (4 critical) plus 10 should-fix items from a
6-critic + synthesizer adversarial review of v1 (each finding confirmed against
source). Headline defect caught: v1's "even if the reaper wins, the drain recovers
the charge" was **false** — it would silently lose charges and rate the shadow gate
healthy. The underlying mechanism (durable intent + idempotent claim-gated apply +
reaper guard) is sound; the corrections make the guard the sole in-transaction
correctness authority, freeze cost+origin in the row, use a native INSERT-as-claim
table, and make the fake actually model the guard.

## 11. Implementation status & handoff (2026-07-03)

Read this first if you are continuing the outbox build.

### What is DONE and merged
- **Design v2** (§1–§10) — hardened by a 6-critic adversarial review; the 4
  critical + 3 other must-fix items are folded in. Trust §3–§4 as the correctness
  spine: the reaper `NOT-EXISTS(pending|dead)` guard is the SOLE lost-charge
  authority; drain cadence is latency only.
- **Increment 1 — native-table storage (dormant)** = PR #113 (branch
  `settle-outbox-storage`), codex **PASS** after 3 rounds, full suite 1216 green.
  Adds:
  - `tr_settle_outbox` DDL (PK `(authorization_id, intent_kind)` + `(status,
    next_attempt_at)` index) in `scripts/deploy/migrate_typed_counters.sh`
    (idempotent/guarded; NOT auto-applied — an operator runs it).
  - `settle_outbox_enabled: bool = False` in `config.py`.
  - `SettleOutboxRow` in `storage_models.py` (frozen inputs: `actual_cost_micro`,
    `settle_origin`, `reservation_id`, `selected_endpoint_id`, `model_id`,
    `selected_usage_type`).
  - `SpannerSettleOutbox` in `storage_gcp_settle_outbox.py`: `enqueue`
    (INSERT-as-claim, refresh-latest ONLY on an unclaimed pending row → returns
    `ENQ_INSERTED|ENQ_REFRESHED|ENQ_LEASED|ENQ_EXISTS_TERMINAL`), `due`/`claim`
    (lease-fenced), `mark` (backoff→`dead` at max_attempts), `has_intent`
    (the reaper-guard predicate: freezes on `pending`/`dead`, NOT
    `done`/`release_approved`), `get`.
  - Wired as `self.settle_outbox` on `SpannerBigtableStore` only. **No live
    caller yet** — dormant.
  - Fake Spanner models the table AND asserts every load-bearing SQL predicate
    (`_require_pred`) so a dropped predicate FAILS a test (the MF6 guarantee).
  - Tests: `tests/test_settle_outbox_storage.py` (11).

### What REMAINS — Increments 2–4 (each its own codex-gated PR, default-off)
2. **Reaper in-txn guard (MF1/MF2/MF3) — the correctness spine, the one live
   change.** In `storage_gcp_authorize.py`:
   - `settle_atomic` gains `guard_outbox: bool = False`. When True, INSIDE its
     read-write txn after `read_reservation` (which returns `authorization_id` —
     confirmed), do an in-txn `SELECT COUNT(*) FROM tr_settle_outbox WHERE
     authorization_id=@aid AND status IN ('pending','dead')`; if > 0, ABORT the
     free-release (return a new `SettleOutcome.OUTBOX_GUARDED`, do NOT claim). The
     in-txn re-check is the interlock (a snapshot pre-filter alone has the MF2
     TOCTOU).
   - `reap_expired_reservations` must project `authorization_id` (today it
     projects only `reservation_id`), advisory-skip candidates where
     `settle_outbox.has_intent(aid)`, and call `settle_atomic(..., guard_outbox=True)`.
   - INERT WHEN THE TABLE IS EMPTY → reaper is byte-identical to today when
     disabled. Fake: the reaper query gains the guard subquery, so update the fake
     reaper handler (`tests/fakes/spanner.py`, `"FROM tr_reservation WHERE
     settled=false AND expires_at"` branch) to filter by the fake `settle_outbox`,
     and add a `_require_pred` for the guard predicate (MF6 — a guard typo must
     fail a test). Test both directions (row present → skipped; absent → reaped).
3. **Frozen-cost finalize primitive + richer outcome (MF5/SF3/SF7).** A narrow
   apply function (counter claim + gateway_authorization finalize + generation
   write) that applies the STORED `actual_cost_micro` and returns the final
   8-value §6 `ApplyOutcome` contract — NOT the full
   `_settle_gateway_authorization` handler (which re-runs pricing + re-fires
   alerts/refill/broadcast). The drain marks `done` on charged, `dead`+ALERT on
   `already_released_free` (reaper beat us — never silently "recovered").
4. **Enqueue-at-settle + drain endpoint.** In `_settle_gateway_authorization`
   (`routes/internal/gateway.py`), gated on `settings.settle_outbox_enabled`:
   enqueue BEFORE the inline finalize (capturing the resolved origin +
   frozen cost the inline attempt computed), mark `done` after. Add
   `POST /internal/gateway/settle-outbox/drain` (internal-token auth, mirror
   `routes/internal/broadcast_queue.py`) that claims due rows and applies via the
   Increment-3 primitive, routing on the STORED origin (MF4 — immune to a
   `TR_TYPED_COUNTER_MIRROR` flip; PARK, don't dead-letter, if the typed store is
   unavailable).

### HARD constraints (do not violate)
- **codex-review BEFORE merge on every billing PR. Do NOT arm auto-merge-on-green
  in parallel with codex** — CI can merge before codex's verdict returns (this
  happened once this session, PR #109; the merged code was dormant so no harm, but
  gate the merge trigger on codex PASS).
- **Default-off. The prod-enable flip (`settle_outbox_enabled=True`) is Joseph's
  call** — same bar as the typed-enforcement cutover. Increments 2–4 must be inert
  when off / when the table is empty.
- `gh pr merge --squash` only; keep main green.
- **CI runs `uv run ruff check .` and mypy REPO-WIDE** — run those (not just
  file-targeted) before pushing (an unsorted import in `storage_gcp.py` red-failed
  #113's CI even though targeted ruff passed).
- Never raw-DML prod billing tables; DDL goes through the guarded migrate script.
- Scope: **control-plane-only** (§9). The enclave-durability half is cross-repo
  (`quill-cloud-proxy`, NO PRs) and needs Joseph's separate go — do not start it.

### Gotchas learned this session
- **Fake fidelity (MF6):** the fake is a substring SQL interpreter; adding a
  predicate to an already-matched query is invisible unless the fake asserts it.
  Always `_require_pred` load-bearing predicates (incl. the PK) so a typo fails.
- **Live-lease races:** a claimed row stays `status='pending'`; any UPDATE that
  should not touch an in-flight row must also fence on `(leased_until IS NULL OR
  leased_until < @now)`.
- **Test pollution:** never mutate `sys.modules` in a test (poisons later tests);
  do fresh-import checks in a subprocess.
- Branch hygiene: verify a clean working tree before `git rebase` (stash-juggling
  left conflict cruft this session; `git reset --hard HEAD` recovered it).

### Session ledger (all merged unless noted)
#106 gateway_authorize extract · #107+#108 catalog split (1592→620) · #109
kill-switch + #110 honesty · #111 design v1 + #112 design v2 · **#113 outbox
Increment 1 (landing)** · #114 closed (dup of main's makora fix `2c03e20`).
