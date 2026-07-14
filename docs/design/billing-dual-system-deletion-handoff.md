# Billing — Dual-System Deletion + Cleanup (handoff for codex)

**Written 2026-06-26 for execution 2026-06-27+. Audience: codex-cli with fresh context.**
**Read this whole doc before touching anything. Prod billing is involved.**

---

## 0. Where we are (one paragraph)

The TrustedRouter Spanner billing **deadlock is fixed** and the **universal typed-billing flip is LIVE fleet-wide** as of 2026-06-26: every workspace authorizes/settles through the typed conditional-DML path (`storage_gcp_counter_dml.py`, `storage_gcp_authorize.py`). C1 removed the legacy reserve/finalize path and C2a removed the generic JSON→typed mirror plus typed-to-JSON rollback helpers. The end state (codex-endorsed) is **ONE source of truth**: typed owns all counters, JSON holds metadata/config only. **This doc = historical deletion handoff plus residual cleanup notes.**

Two residuals that DISSOLVE once legacy is gone (do NOT try to fix them before Step 1b):
- **`ea7dd3d8` $3.42 usage under-count** (a `tmxubench` test workspace). Blocked because it still has live/abandoned **legacy** reservations (`reserved_microdollars=141485`); reconciling its usage while a legacy settle can still fire would re-stale the fix.
- **PR #89 `reconcile_typed_credit_usage`** (the usage-catch-up tool) — codex FAILed it because its drain guard can't *prove* no pending legacy settle. That proof is free once the legacy settle path no longer exists.

---

## 1. NON-NEGOTIABLE guardrails (the same discipline that got us here safely)

1. **NEVER raw-DML / ad-hoc-script prod billing tables.** Use tested tooling only: the operator CLI `scripts/deploy/ramp_typed_billing.py` (subcommands `audit｜status｜pause｜unpause｜reconcile｜repair｜usage`) and merged store methods. An ad-hoc `settle_atomic` loop was correctly **blocked by the safety classifier** — don't repeat it. If you need a new prod operation, add it as a reviewed CLI subcommand first.
2. **Every code change: its own PR → codex-cli review → CI green → `gh pr merge --squash`. Keep main green.** One reviewable increment per PR. Invoke review with `codex exec --skip-git-repo-check -c service_tier="fast" '<prompt>'` (backgrounds; read its output file).
3. **The deploy is automatic and ~1h.** `.github/workflows/deploy.yml` fires on **push-to-main touching `src/**` or `scripts/deploy/**`** (Workload Identity Federation — no gcloud needed), gated on `ci.yml` green, ~1h canary across regions. **Docs-only changes do NOT deploy.** Plan for the ~1h tail on any enforcement-affecting merge.
4. **ADC expires ~1h.** Python prod-Spanner **writes/reads** use ADC. It WILL expire mid-session → `503 Reauthentication is needed`. Fix: ask Joseph to run `gcloud auth application-default login`. (This bit us mid-flip and stranded 45 paused workspaces.) The deploy itself uses WIF, not ADC, so deploys are unaffected.
5. **`billing_paused` causes outages + a Sentry 503 alert.** Pausing an *active* workspace 503s its users ("Workspace billing is paused", notifies the team). The ~1h flip pause window is what made a real user hit it. For this effort you should **NOT need to pause anything except `ea7dd3d8` (idle test ws) briefly** — keep it that way.
6. **Verify-after EVERY prod mutation:** run `ramp_typed_billing.py audit` (expect CLEAN) and a paused-scan (expect 0): `[w['id'] for w in store._list_entities("workspace", cls=dict) if w.get("billing_paused")]`.
7. **Keep the auditor running the whole time** — it is the standing money tripwire (`typed.reserved == Σ open typed holds`, both directions, shard-aware). If it ever goes non-clean mid-deletion, STOP.

### Env + worktree boilerplate (every prod command)
```bash
# Dedicated worktree (shared quill-router tree is dirtied by other agents — branch off origin/main):
git -C /Users/jperla/claude/quill-router worktree add /Users/jperla/claude/qr-billing -b <slug> origin/main
cd /Users/jperla/claude/qr-billing
export VIRTUAL_ENV=/Users/jperla/claude/quill-router/.venv; export PATH="$VIRTUAL_ENV/bin:$PATH"
export TR_STORAGE_BACKEND=spanner-bigtable TR_GCP_PROJECT_ID=quill-cloud-proxy \
  TR_SPANNER_INSTANCE_ID=trusted-router-nam6 TR_SPANNER_DATABASE_ID=trusted-router \
  TR_BIGTABLE_INSTANCE_ID=trusted-router-logs TR_BIGTABLE_GENERATION_TABLE=trustedrouter-generations \
  PYTHONPATH=src
# Tests: PYTHONPATH=src python3 -m pytest tests/ -q -k billing
```

---

## 2. STEP 0 — Verify the soak held (DO THIS FIRST, gate everything on it)

`*` had only been live a few hours when this was written. Before deleting anything, confirm it stayed healthy overnight:

```bash
python3 scripts/deploy/ramp_typed_billing.py audit          # expect: credit 0/N, key 0/M, CLEAN
```
And inline (read-only) confirm: **open holds still bounded** (no monotonic growth = no deadlock/leak), **0 paused**, **drift still only `ea7dd3d8` ≈ $3.42** (nothing new), and **no `release row-count != 1` errors** in logs/Sentry since the flip.

```python
from trusted_router.config import Settings
from trusted_router.storage import create_store
s = create_store(Settings())
with s._database.snapshot(multi_use=True) as snap:
    tot, opn = list(snap.execute_sql("SELECT COUNT(*), COUNTIF(settled=false) FROM tr_reservation"))[0]
    top = list(snap.execute_sql("SELECT workspace_id, COUNTIF(settled=false) c FROM tr_reservation GROUP BY workspace_id ORDER BY c DESC LIMIT 5"))
print("tr_reservation total", tot, "open", opn, "topopen", [(w[:8], c) for w, c in top])
```

**If anything regressed** (audit non-clean, open holds growing without bound, new drifted workspaces, any `release row-count`/`typed finalize failed` errors): **STOP. Do not delete the dual-system.** Rollback lever is still intact at this point (denylist a workspace, or revert the gate). Investigate first.

**Also confirm the row-count log alert exists** (it needs Joseph's gcloud — `tr-deploy` SA lacks `logging.logMetrics.create`): `gcloud logging metrics create typed_finalize_release_rowcount_fail --... 'textPayload:"release row-count" OR "typed finalize failed"'`. If missing, ask Joseph to create it before proceeding — it's the early-warning for a clobber regression.

---

## 3. STEP 1 — Delete the dual-system (the main work; one PR per sub-step)

**Ordering is load-bearing. Do them in this order, codex-review + merge + (where it deploys) let it converge + re-audit between each.** The point: remove the *legacy settle path first* so JSON usage freezes, which is what makes Step 2 safe and removes the only remaining cross-system race.

> Before each enforcement-affecting merge, remember the ~1h deploy + re-audit after convergence.

### 1a — Remove the env gate; typed is always-on
- `src/trusted_router/config.py`: `typed_billing_workspace_ids` / denylist parsing.
- `src/trusted_router/routes/internal/gateway.py`: the cohort gate check in authorize (and the denylist short-circuit). Make `authorize` unconditionally typed.
- `scripts/deploy/rollout.sh`: drop the `TR_TYPED_BILLING_WORKSPACE_IDS` env var (and the big comment block).
- Net effect: no per-request typed-vs-legacy branch on authorize. **Keep** `is_typed_reservation` origin-routing on settle/refund for now (1b removes its other arm).
- Deploys. Re-audit after convergence.

### 1b — Remove the legacy settle/finalize path (THE keystone — freezes JSON usage)
- `src/trusted_router/routes/internal/gateway.py` settle/refund: currently routes by reservation origin (`is_typed_reservation`) → make settle/refund **always** the typed path (`typed_finalize_atomic`). Delete the legacy arm.
- `src/trusted_router/storage_gcp.py`: the legacy `finalize_gateway_authorization` (the JSON read-modify-write of `reserved_microdollars` / `total_usage_microdollars`, ~`storage_gcp.py:862`) — this is the function that can still bump JSON usage. Once nothing calls it, JSON usage is **permanently frozen**.
- ⚠️ **In-flight legacy reservations at cutover:** any `GatewayAuthorization` with `settled=false` and a legacy `credit_reservation_id` (e.g. `ea7dd3d8`'s `reserved_microdollars=141485`) will now never settle legacy. That is FINE and intended — they become abandoned, JSON freezes. But confirm there are no *active high-value* legacy auths in flight first (idle test workspaces only is the expectation). Query unsettled non-typed `gateway_authorization` entities to be sure.
- After this merges + converges: **JSON `total_usage_microdollars` can no longer change** anywhere → Step 2 is now provably safe.

### 1c — Remove the typed-aware read overlays
- `src/trusted_router/typed_balance.py` (`typed_aware_credit_account`, `_read_typed_credit`, etc.) — now that typed is universal + authoritative, read typed counters directly instead of overlaying.
- Call sites to switch to a plain typed read: `auto_refill.py` (`maybe_charge_after_settle`), `routes/billing.py` `/credits`, `routes/console/credits.py`.

### 1d — Removed the one-way mirror and rollback helpers
- `storage_gcp_counter_reconcile.py`: the typed-to-JSON helper and drift/backfill helpers are gone.
- `storage_gcp_counters.py` generic mirror + its chokepoint calls in `storage_gcp.py` are gone.
- New-workspace/new-key typed-row seeding is explicit on entity create, and top-ups write typed `total_credits` directly.

### 1e — Retire legacy JSON counter fields
- `CreditAccount.reserved_microdollars` / `total_usage_microdollars`, `ApiKey.usage` etc. (`storage_models.py`): stop reading/writing them. Leave the columns in place for one release (don't DROP COLUMN in the same wave), then a follow-up removes them.
- Also split the `storage_gcp_counter_reconcile.py` god-module here (compare/backfill vs flip-seed vs audit vs repair/usage) — it's grown large; codex flagged it.

---

## 4. STEP 2 — Fix `ea7dd3d8` $3.42 + finish #89 (only AFTER 1b)

Now JSON usage is frozen, so `typed total_usage = JSON.total_usage + Σ settled-Credits actual_micro` is permanently correct.

1. **Reap `ea7dd3d8`'s 18 dead typed holds.** They expired 2026-06-25 15:36+ (settles failed in the clobber incident). Use **reviewed tooling** — either add a `reap` subcommand to `ramp_typed_billing.py` (thin wrapper over the tested `store.reap_expired_reservations(now, limit)`, `storage_gcp_authorize.py:238`; fleet-wide, oldest-expired first, claim-gated so a racing settle is safe) — codex-review + merge it — or rely on the scheduled reaper if one is wired. Verify `ea7dd3d8` open holds → 0 and its typed reserved → 0; auditor stays CLEAN.
2. **Finish PR #89** (`billing-usage-reconcile` branch): its blocker was "can't prove no pending legacy settle" — now there *is* no legacy settle path, so the concern is moot. Either drop the JSON-stability worry entirely (document why: legacy settle path deleted in 1b) or keep the existing **drain guard** (no open typed holds; already added). Re-run codex; it should PASS now. Merge.
3. **Reconcile the usage:** `ramp_typed_billing.py pause ea7dd3d8… --apply` → `usage ea7dd3d8… --apply` → `unpause ea7dd3d8… --apply`. Expect `total_usage` to jump +~$3.42. Re-run the drift sweep → **0 drifted workspaces**. Re-audit CLEAN, 0 paused.

`ea7dd3d8` = `ea7dd3d8-b655-4522-9f44-a08627a364eb`. (For reference, `1d4d7128-ec67-4a46-a2e1-421f3dc475b5` was already fixed this session: +$7.21.)

---

## 5. STEP 3 — Remaining follow-ups (lower priority, independent)

- **#29 API-key deletion drain/tombstone.** Deleting a key with open typed holds leaves an unreapable orphan. The fix needs an **atomic count+delete in ONE read-write txn** (count `tr_reservation` for the key `settled=false` + delete entities + typed tombstone/delete handling together; the count's read-lock serializes vs a concurrent authorize's tr_reservation insert). Disable-first-then-check is INSUFFICIENT (TOCTOU → unreapable orphan; reaper only reclaims SETTLED). Prior WIP on closed PR #85 / branch `billing-key-delete-drain`. Also fix `scripts/cleanup_smoke_signups.py` (deletes api_key+tr_key_limit directly, bypassing any guard).
- **#33 durable per-ws kill-switch + settle outbox** (before any whale/high-value workspace). The fast kill-switch avoids the 25-min–1h deploy for an emergency single-workspace revert; the outbox recovers a completed-but-settle-lost charge instead of the reaper releasing it free.
- **Fake-fidelity gaps** (each masked a real prod bug this session — see the spawned task): make the fake `database.snapshot()` **single-use** by default (real Spanner raises `Cannot re-use single-use snapshot`; only `multi_use=True` allows multiple reads — caught `repair`'s 3-read snapshot bug); make a missing `tr_credit_balance` point-read return `[]` not a default-0 row (real Spanner returns no rows).
- **Step 5 sharding** (`shard` in PKs, dormant) — only if per-whale contention metrics ever demand it. The repair/usage/reconcile tools are shard-0-only and abort on nonzero shard, so revisit them if sharding is ever turned on.

---

## 6. What to KEEP (do not delete)

- The typed conditional-DML core (`storage_gcp_counter_dml.py`, `storage_gcp_authorize.py`): authorize/settle/reap/typed_finalize. **This is the product now.**
- `tr_reservation` (the typed holds ledger) + the crash reaper.
- The invariant auditor (`audit_typed_invariants`) + the `audit` CLI — standing money tripwire, run forever.
- `workspace.billing_paused` + the quiesce primitive — still the safe way to do any future maintenance on a workspace.
- The `repair_typed_reserved` / `reconcile_typed_credit_usage` tools — keep as operator repair tools.

---

## 7. Risk model / rollback

- **Before Step 1**, rollback is easy (gate to a cohort / denylist / revert rollout.sh). **After 1b** (legacy settle path deleted), there is no legacy fallback — that's the point, but it means 1b is the commitment. Do it only after Step 0 confirms a clean soak. Incremental sub-PRs + re-audit between each = a regression is caught one step in, not all at once.
- The standing money invariant (`auditor CLEAN`) is your continuous proof. If it ever goes non-clean, STOP and investigate before merging the next sub-step.
- Memory for the full saga + every gotcha: `~/.claude/.../memory/project_trustedrouter_billing_redesign.md`. Cutover handoff: `docs/design/billing-typed-cutover-handoff.md`. Design: `docs/design/billing-typed-counters.md`.
