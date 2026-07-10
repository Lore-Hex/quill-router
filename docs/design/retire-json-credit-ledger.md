# Design: retire the JSON credit ledger

Owner: Joseph. Author: Claude (from the 2026-07 billing-review retro; Joseph approved the
direction: "simplify those 2"). Companion simplification — single-sourced request-rate
resolution — shipped separately (#144).

## Verified current state (2026-07-10)

- `scripts/deploy/rollout.sh` sets `TR_TYPED_COUNTER_MIRROR=1` and does **not** set
  `TR_TYPED_BILLING_WORKSPACE_IDS` (config default `""`). Prod is therefore
  **mirror-on, enforcement-legacy for every workspace**: the JSON `credit` entity is the
  authoritative money book everywhere; typed `tr_credit_balance` rows carry a mirrored
  `total_credits` and zero/unseeded `reserved`/`total_usage`.
  - Checklist item before Phase A: confirm the SERVING revision's env agrees (bare env
    edits don't survive here — traffic is pinned to named revisions; see the settle-outbox
    rollout learnings in docs/runbook.md).
- Ownership split (2026-06-25 incident fallout) is already in code: the mirror writes
  ONLY `total_credits` (+ key config), never `reserved`/`total_usage`.
- Full inventory of every reader/writer of both books, the mirror, and the repair
  tooling: see §Appendix.

## Why retire it

Two books for the same money is the single biggest standing complexity in billing:
per-column ownership rules, a mirror that must fire on every JSON write, documented
"stale-low by design" fields, `backsync`/`backfill`/`compare`/`repair` operator tooling,
and an ordering invariant that only holds within a process lifetime. All of it exists to
support a migration that is 40% complete. Finishing the migration deletes the machinery.

## End state

- `tr_credit_balance` / `tr_key_limit` are the ONLY money books. Conditional DML
  (reserve/release) is the only writer of usage/holds; grants/top-ups write
  `total_credits` typed-directly (same idempotent `stripe_event` gate, same txn).
- The JSON `credit` entity survives ONLY as non-money metadata: auto-refill config,
  stripe customer/payment-method ids, last-refill audit fields. Money fields deleted
  from `CreditAccount` (and the memory twin models the single typed book).
- Deleted code: legacy `_require_credit_tx`/JSON finalize path, the mirror
  (`mirror_write`/`mirror_delete`), `backsync_typed_to_json`, `backfill`, `compare`,
  and their tests. `repair_typed_reserved` + `audit_typed_invariants` REMAIN (they audit
  the surviving book).
- Console/display and `signup()` read `typed_credit_snapshot`.

## Phases

### Phase A — finish the enforcement cutover (operational; Joseph gates each ramp)
A1. Flip tooling: a batch script wrapping the existing per-workspace runbook
    (`billing_paused` → drain to zero open holds → `reconcile_for_flip(apply=True)` →
    add to allowlist → unpause), plus a dry-run mode that reports per-workspace
    readiness (open holds, drift). Uses existing primitives only.
A2. Canary: flip 1-3 internal/low-traffic workspaces. Run `audit_typed_invariants` +
    `compare` daily. Bake ≥ a few days.
A3. Ramp cohorts (10% → 50% → all) with the same audits between batches. New signups
    flip at creation (allowlist additions or a "typed-by-default-for-new" flag).
A4. Set `TR_TYPED_BILLING_WORKSPACE_IDS=*` in rollout.sh (config-as-code, like the
    #120 outbox flip). The denylist kill-switch semantics are unchanged throughout
    (break-glass availability brake; under-bills until backsync — acceptable and
    already documented).
Rollback story: per-workspace `pause → drain → backsync_typed_to_json → remove from
allowlist → unpause` — the existing, tested runbook. This is why Phase A deletes nothing.

### Phase B — move the remaining JSON-money surfaces (code; one PR per step)
B1. Reads: `signup()` trial-credit report, console credits/billing display, AND the MCP
    `credits-get` tool (routes/mcp.py:172-187 — reads JSON total_credits/total_usage/
    reserved/available; found in adversarial review) → `typed_credit_snapshot`. (Safe
    once the workspace is typed; during the ramp, gate on membership like the routes do.)
B2. Writes: `credit_workspace_once`, stripe settlement, auto-refill outcome → typed
    `total_credits` becomes AUTHORITATIVE (idempotency event + typed write in one txn),
    but the JSON `total_credits` field is STILL written in the same txn ("kept warm").
    Rationale (P1 from adversarial review): `backsync_typed_to_json` copies only
    total_usage/reserved — never total_credits (counter_reconcile.py:491,550-553; pinned
    by tests/test_billing_rollback_backsync.py:51-52) — so a denylisted/rolled-back
    workspace's legacy authorize (storage_gcp_keys.py:278-286) would otherwise read a
    stale JSON balance after a typed-direct top-up. Keeping JSON warm preserves the
    per-workspace rollback unchanged; the JSON write (and the warm-keeping) is deleted
    only in Phase C when rollback-to-legacy is retired. (Alternative — extending
    backsync to copy total_credits — rejected: touches rollback semantics mid-migration.)
B3. Grant scripts (`scripts/credit_grant_*.py` pattern) switch their verification to the
    typed snapshot (they already cross-check it today).

### Phase C — delete (the payoff; only after A4 + B and clean audits for ~2 weeks)
C1. Delete the legacy JSON finalize path and `_require_credit_tx` enforcement — AND the
    legacy RESERVE side (missed in v1, found in adversarial review): the fallback
    authorize path's `STORE.reserve` call (routes/internal/gateway.py:343-351) and
    `SpannerApiKeys.reserve` (storage_gcp_keys.py:278-295), which reads JSON availability
    and writes `CreditAccount.reserved_microdollars`. Also delete B2's JSON
    total_credits warm-keeping write in the same step (rollback-to-legacy is retired
    here, so the warm copy loses its purpose).
C2. Delete the mirror, `backsync`, `backfill`, `compare`; slim `CreditAccount` to
    metadata; update the memory twin + every test that constructs money fields on it.
C3. Runbook rewrite: single-book operations; keep `repair_typed_reserved` +
    `audit_typed_invariants` as the standing tripwires.

## Invariants that must hold at every step
1. A workspace is enforced by EXACTLY ONE book at any instant (allowlist membership
   decides; settle routes by reservation ORIGIN — already true).
2. `total_credits` visible to the user never changes as a side effect of a flip
   (reconcile_for_flip seeds from JSON in the pause window; audited before unpause).
3. Every transition is reversible until Phase C (nothing deleted before then).
4. No raw DML against prod billing tables — flips go through the deployed primitives.

## Non-goals
- No new sharding work (shard-0 hot-row contention, issue #128, is orthogonal; revisit
  after single-book).
- No auto-refill/stripe metadata redesign — those fields just stay JSON metadata.

## Appendix: inventory (2026-07-10; v2 additions from adversarial review marked ⊕)
JSON readers: get_credit_account (storage_gcp.py:391) ← signup (280), console billing,
grants verification, ⊕ MCP credits-get (routes/mcp.py:172-187),
⊕ legacy authorize availability read (storage_gcp_keys.py:278-286).
⊕ JSON money writer missed in v1: SpannerApiKeys.reserve (storage_gcp_keys.py:278-295,
called from the fallback authorize at routes/internal/gateway.py:343-351) — increments
CreditAccount.reserved_microdollars on the legacy path; scheduled for deletion in C1. JSON writers (all mirror-firing via _write_entity_tx/batch):
credit_workspace_once (753), auto-refill settings (765), stripe customer (785), clear
payment method (804), refill outcome (818), legacy finalize (935: reserved/total_usage),
ensure_user (248), create_workspace (328), backsync (counter_reconcile.py:551). Typed
readers: typed_key_usage (1198), typed_credit_snapshot (1233), read_typed_reservation
(1252), check_key_window_limits (authorize.py:61). Typed writers: reserve/release_credit,
reserve/release_key (counter_dml.py). Mirror: counters.py:105-147. Repair/rollback:
counter_reconcile.py (compare 85, backfill 140, reconcile_for_flip 221, backsync 476,
repair_typed_reserved 596, audit_typed_invariants 405). JSON-only metadata (stays):
auto_refill_*, stripe_customer_id, stripe_payment_method_id, last_auto_refill_*.
Memory twin: single `credits` dict of CreditAccount (storage_models.py:245).

## Execution checklist (added 2026-07-10; update in place as steps complete)

Legend: [J] = Joseph's explicit go required · [C] = Claude runs autonomously (SA key)
· every mutating tool step uses `--apply` after a dry-run.

- [x] Design merged (#145, v2 hardened after adversarial review)
- [ ] A1 flip tool merged (#146)

### A2 — canary
1. [C] `PYTHONPATH=src uv run python scripts/typed_flip.py readiness --all`
   (read-only) → report + canary candidates (low-traffic, verdict READY).
2. [J] Pick the canary workspace(s).
3. [C] `typed_flip.py prepare --workspace <id> --apply` → workspace parked PAUSED
   (pause → drain → seed → verify). Exit 2 = still draining; re-run later.
4. [J] THE FLIP: one-line rollout.sh edit adding the id to
   TR_TYPED_BILLING_WORKSPACE_IDS → merge (deploy runs) → verify the SERVING
   revision env carries it (bare env edits do not survive; traffic is
   revision-pinned).
5. [C] `typed_flip.py finish --workspace <id> --allowlist-deployed --apply` →
   unpaused, typed-enforced.
6. Bake 3-7 days: daily `audit_typed_invariants` + compare() drift + the
   "release row-count != 1" alert. Any violation → rollback path below.

### A3 / A4 — ramp
7. Repeat 1-6 for cohorts (~10% → ~50% → all), audits clean between batches. [J]
   gates each cohort.
8. [J] A4: set TR_TYPED_BILLING_WORKSPACE_IDS=* in rollout.sh (config-as-code);
   covers new signups from then on.

### Phase B (each step: codex writes, Claude reviews, PR, CI, merge)
9. B1 reads → typed_credit_snapshot, gated on allowlist membership during the
   ramp: signup() trial report, console credits/billing, MCP credits-get.
10. B2 writes: typed total_credits authoritative; JSON total_credits KEPT WARM
    in the same txn (rollback safety — backsync never copies total_credits).
11. B3 grant scripts verify against the typed snapshot.

### Phase C (only after 100% flipped + 2 weeks clean audits) [J] go required
12. C1 delete: legacy finalize, _require_credit_tx, SpannerApiKeys.reserve +
    its gateway call site, and B2's warm-keeping write.
13. C2 delete: mirror, backsync, backfill, compare; slim CreditAccount to
    non-money metadata; memory-twin + test updates.
14. C3 runbook rewrite (single book; keep repair_typed_reserved +
    audit_typed_invariants as standing tripwires).

### Rollback (any point through B)
`typed_flip.py rollback --workspace <id> --apply` → allowlist-REMOVAL deploy →
`typed_flip.py finish --workspace <id> --allowlist-deployed --rollback --apply`.
