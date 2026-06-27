# Typed-billing cutover handoff — resolved synthetic cohort

**Status as of 2026-06-21 10:55 Pacific.**  
Owner handing back: Codex. Next reader: Claude.

## TL;DR

The typed-column conditional-DML billing path is now active in production for the
synthetic monitor workspace:

`45819281-0ce9-4811-a0cd-c660ab3a116d`

The earlier "typed authorize path is deployed but not engaging" symptom is
resolved. The real cause was not a bad cohort flag or stale image. The cohort
runtime flags were correct, but the synthetic workspace's **typed credit mirror**
was stale/exhausted. Typed authorize correctly read the typed counters, found no
available balance, returned `402`, and therefore created no `tr_reservation`
rows.

Prod state was repaired through the app ledger path, not raw DML:

1. Applied an idempotent synthetic top-up event.
2. Ran a zero-dollar app-level mirror repair with `TR_TYPED_COUNTER_MIRROR=1`.
3. Forced/observed synthetic monitor traffic.
4. Verified typed reservations are now created and settled.

## Current production verification

Last verified by Codex on 2026-06-21:

```text
tr_reservation for workspace 45819281-0ce9-4811-a0cd-c660ab3a116d
n=1932
settled=1925
open=7
```

Typed balance:

```text
total_credits=8687000000
reserved=236803676
total_usage=8333057599
available=117138725
```

Recent `us-central1` gateway authorize logs showed only `200` responses in the
sample window, including both:

```text
https://trustedrouter.com/internal/gateway/authorize
https://trustedrouter.com/v1/internal/gateway/authorize
```

Control-plane `/health` also returned:

```json
{"status":"ok"}
```

Note: `https://api.trustedrouter.com/health` returned `401` without auth. That is
not the signal used here; the gateway authorize logs and synthetic monitor
reservations are the real typed-billing signal.

## Root cause

The synthetic monitor workspace had effectively no available balance in the typed
counter table:

```text
total_credits=8567000000
total_usage=8330197429
reserved=236802847
available=-276
```

An operator top-up was first run through `STORE.credit_workspace_once`, but that
operator environment did **not** include `TR_TYPED_COUNTER_MIRROR=1`. That updated
the canonical JSON credit state but not the typed mirror that the cohort-enforced
authorize path reads.

After a zero-dollar app-level credit event was run with `TR_TYPED_COUNTER_MIRROR=1`,
the typed mirror matched the canonical state and authorizes immediately began
creating `tr_reservation` rows.

## Production actions taken

### 1. Diagnostic PR

PR: `https://github.com/Lore-Hex/quill-router/pull/68`  
Commit deployed: `3c9242017e0c8d1fb7a8c95d6802be63069c6a23`

Added scoped runtime diagnostics around the typed billing decision in:

```text
src/trusted_router/routes/internal/gateway.py
```

Important note: the diagnostic logger did not produce useful Cloud Logging lines
in the expected query path, likely due app logger/root handler wiring. Runtime
proof came from actual Spanner reservation rows and gateway HTTP status logs.

Deploy run:

```text
GitHub Actions run 27910487991
```

Result: success.

It passed:

- us-central1 deploy, staged traffic, canary
- europe-west4 deploy, staged traffic, canary
- us-east4 deploy, staged traffic, canary
- cold-region deploy
- synthetic monitor job redeploy
- prod smoke

### 2. Production mirror repair

Top-up event:

```text
manual_synthetic_billing_typed_cutover_2026-06-21_20_dollars
```

Then mirror-repair event:

```text
manual_synthetic_billing_typed_cutover_2026-06-21_mirror_repair_zero
```

The mirror repair was intentionally run through the normal store/app path with
`TR_TYPED_COUNTER_MIRROR=1`, not raw SQL/DML.

### 3. Operator guard PR

PR: `https://github.com/Lore-Hex/quill-router/pull/69`  
Merge commit: `37d023c55fbd2c8b3c528a786e920f64eca0a860`

Files changed:

```text
scripts/credit_makeup.py
scripts/credit_grant_joseph.py
tests/test_operator_credit_scripts.py
```

The manual credit scripts now force:

```python
os.environ["TR_TYPED_COUNTER_MIRROR"] = "1"
```

before calling:

```python
create_store(Settings())
```

This prevents future manual operator credits from updating canonical credit JSON
without also updating typed counters during the cutover.

Main CI for `37d023c` passed fully:

- ruff
- mypy
- TypeScript build
- ESLint
- Stylelint
- pytest
- coverage
- Playwright/browser smoke

No runtime deploy was triggered for PR #69 because the deploy workflow is path
filtered and this PR only touched operator scripts/tests, not runtime service
paths. That is expected and safe; the runtime deploy from PR #68 is already live.

## Commands to re-check state

Use:

```bash
--account=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com
```

### Reservation growth

```bash
gcloud spanner databases execute-sql trusted-router \
  --instance=trusted-router-nam6 \
  --project=quill-cloud-proxy \
  --account=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com \
  --sql="SELECT COUNT(*) n, COUNTIF(settled) settled, COUNTIF(NOT settled) open, MAX(created_at) latest FROM tr_reservation WHERE workspace_id='45819281-0ce9-4811-a0cd-c660ab3a116d'"
```

Success signal: `n > 0` and increasing; most rows settled; open count small and
bounded.

### Typed credit balance

```bash
gcloud spanner databases execute-sql trusted-router \
  --instance=trusted-router-nam6 \
  --project=quill-cloud-proxy \
  --account=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com \
  --sql="SELECT total_credits, reserved, total_usage, total_credits-total_usage-reserved AS available FROM tr_credit_balance WHERE workspace_id='45819281-0ce9-4811-a0cd-c660ab3a116d'"
```

Success signal: `available > 0`.

### Recent authorize statuses

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="trusted-router" AND resource.labels.location="us-central1" AND timestamp>="2026-06-21T17:12:00Z" AND (httpRequest.requestUrl:"/internal/gateway/authorize" OR httpRequest.requestUrl:"/v1/internal/gateway/authorize")' \
  --project quill-cloud-proxy \
  --account=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com \
  --limit=30 \
  --format=json \
  | jq -r '.[] | [.timestamp, .httpRequest.status, .httpRequest.requestMethod, .httpRequest.requestUrl] | @tsv'
```

Success signal: recent synthetic authorizes are `200`, not `402`.

### Force one synthetic pass

```bash
gcloud run jobs execute trusted-router-synthetic-us-central1 \
  --region=us-central1 \
  --project=quill-cloud-proxy \
  --account=tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com \
  --wait
```

Then re-check reservation growth.

## What not to do

- Do not raw-DML prod billing tables.
- Do not use a one-off manual credit script unless it sets
  `TR_TYPED_COUNTER_MIRROR=1` before store creation.
- Do not broaden the typed billing cohort beyond the synthetic monitor workspace
  without Joseph's explicit approval.
- Do not treat missing diagnostic log lines as proof that typed billing is off.
  Use `tr_reservation` rows plus authorize statuses as the source of truth.

## Rollback

If the synthetic cohort starts failing:

1. Prefer removing the synthetic workspace from
   `TR_TYPED_BILLING_WORKSPACE_IDS` in `scripts/deploy/rollout.sh` and redeploying.
2. If a denylist env is wired into the deploy path, adding the workspace to
   `TR_TYPED_BILLING_WORKSPACE_DENYLIST` is also safe because denylist wins.
3. Existing typed reservations can still finalize because settle/refund routes by
   reservation origin through `is_typed_reservation`.

## Next cutover step

Leave the synthetic cohort running and observe:

- reservation growth
- settle/open ratio
- comparator drift
- deadlock rate
- 402 rate

After a clean observation window, propose the next small allowlist cohort. Do not
add more workspaces in this handoff without a fresh approval.

## Local workspace note

This file is untracked in `/Users/jperla/claude/qr-billing`.

The `qr-billing` worktree is currently on:

```text
billing-typed-operator-mirror
```

That remote branch was deleted after PR #69 was squash-merged. The `main`
worktree is already occupied by:

```text
/Users/jperla/claude/quill-router
```

So do not be surprised if `git switch main` fails from `qr-billing` with:

```text
fatal: 'main' is already used by worktree at '/Users/jperla/claude/quill-router'
```

---

## Claude independent verification (2026-06-21, post-codex) — VERDICT: WORKING & HEALTHY

Independently re-checked every dimension (not trusting pasted numbers). **The typed path is
genuinely engaging and prod is healthy.** Evidence:

- **`tr_reservation` growing live:** 0 (pre-fix) → 1932 (codex) → 3799 → 4046, settled tracking
  (4031), open bounded (15). A forced synthetic probe incremented it in real time → typed
  authorize is *actively* creating reservations now, not just historically.
- **Typed balance positive:** `available = 116,988,687` (> 0). No re-exhaustion.
- **Authorize statuses:** last 40 gateway authorizes all **200** (zero 402s).
- **Deadlocks:** **0** `Deadlock`/`Aborted` errors in the trailing 2h (whole service).
- **Infra:** all 3 regions still carry the cohort flag; `trustedrouter.com/health` = 200;
  deploy run `27910487991` (PR #68) = success; PRs #68 + #69 merged; #69 operator-mirror guard
  present in `scripts/credit_makeup.py`.

### Correction to "comparator should be clean" — the drift is EXPECTED, not a bug
The comparator now reports drift for the cohort workspace:
`credit:45819281 total_usage JSON=8330197429 vs typed=8333200595` (typed ahead ~3.0M),
`reserved` typed ahead ~4.6k, and its two API keys' `usage` typed ahead. **This is the correct
post-flip signature, not a money bug:** the typed DML path books usage into the *typed*
counters only, and the mirror is one-way (JSON→typed), so JSON usage **freezes** for a
cohort workspace while typed advances. **`total_credits` IS consistent** between JSON and typed
(codex's top-up mirrored correctly), so no credit was lost. Typed is the source of truth and
it is correct. ⇒ Do NOT expect the comparator to be clean for active cohort workspaces; "typed
ahead on usage/reserved, equal on total_credits" is the healthy state. **Implication for the
ramp:** rolling a workspace back to legacy after a long typed period would leave JSON
*under-counted* by the typed-era usage. Negligible for the synthetic monitor; decide a
JSON-backfill-on-rollback policy before broadening the cohort to real whales.

### Note on the root-cause narrative
Codex's "exhausted typed balance → 402 → no reservation" explains the 402s codex saw, but does
NOT explain the *earlier* state I observed (synthetic succeeding on the **legacy** path: HTTP
200 + real settles `recorded:N` + `tr_reservation=0`). The thing that actually flipped
engagement legacy→typed was the **PR #68 redeploy** (fresh build/restart), not the balance
top-up. The balance exhaustion was a *second*, real issue that only surfaced once typed
started engaging. The diagnostic logger codex added "did not produce useful lines," so the
*original* reason the cohort branch wasn't taken pre-#68 was never definitively pinned down —
it's working and stable now, but be aware a future redeploy is the only thing proven to have
fixed it.

### Two minor follow-ups (non-blocking)
1. **Remove the dead diagnostic code** from `routes/internal/gateway.py`
   (`_log_typed_billing_decision`, called at ~line 230, defined ~428, logger at ~57) — codex
   confirmed it doesn't emit useful Cloud Logging lines. Small cleanup PR.
2. Decide the **JSON-backfill-on-rollback** policy above before the whale ramp.

---

## Ramp-to-fleet strategy + decision (2026-06-21)

**Goal / end state:** typed conditional-DML billing for **every** workspace, **new ones
included**, with the allowlist gone. The `TR_TYPED_BILLING_WORKSPACE_IDS` allowlist is a
*transitional safety valve*, not the architecture. The final form is the gate returning
"typed unless denylisted" (universal default) + new workspaces starting on typed at creation.

**Where we are:** synthetic monitor (45819281) + first real workspace (063e9fb9, deploy in
flight) on typed. The dominant deadlock source (the synthetic, ~8000× the traffic of any real
ws — the only ws in deadlock payloads) is already fixed → **0 deadlocks service-wide since its
cutover.** So there is **no urgency** forcing a big-bang.

**Why staged, not big-bang (the actual reasoning).** Two distinct failure modes:
1. *Stale/wrong typed counters* → would 402 a customer. **Already de-risked fleet-wide:** the
   drift comparator shows every workspace except the synthetic is CLEAN (typed == JSON); the
   one-way mirror has kept all ~46 workspaces in sync. So data-wise the fleet is ready.
2. *A billing code path the typed engine handles wrong* → **this is the gate.** The typed path
   has run in prod for only two workspaces, one (synthetic) on the non-representative internal
   monitor model. We have NOT yet watched the typed path handle, on real traffic: **BYOK
   billing, refunds / failed-settles, mid-flight credit grants or monthly resets, or genuinely
   high real-workspace concurrency.** A big-bang flip would expose every customer to any such
   bug simultaneously (fleet-wide 402s until rollback, which is minutes→90min).

The asymmetry decides it: going slow costs ~nothing now (no deadlocks happening); going
fast-and-wrong rejects real paid traffic fleet-wide. For billing, stage it.

**Accelerated plan (≈46 workspaces total → days, not a one-at-a-time grind):**
1. **Validate 063e9fb9** (in flight) — first real workspace, real models.
2. **Fleet pre-flight sweep** — run the comparator + the per-key `tr_key_limit` existence/funded
   check (the one done for 063e9fb9) across ALL workspaces, programmatically; flag any not safe
   to flip. (Build this; not built yet.)
3. **One diverse batch** deliberately covering the untested traffic *types* — a BYOK workspace,
   a refund-active one, the top few by volume — watch ~a day.
4. **Flip the universal default** — gate → "typed unless denylisted" (= all existing + all new
   in one move) + a small code change so new workspaces start on typed at creation (lowest risk,
   no migration state). Only after step 3 is clean across traffic types.

**DECISION (2026-06-21, Joseph):** **HOLD at 063e9fb9 until it reports.** Do NOT widen the
cohort or build the sweep yet. Once 063e9fb9 is validated clean (reservations growing, no 402s,
comparator behaving), resume at step 2 (build the fleet pre-flight sweep), then step 3, then the
universal-default flip. Background monitor for the 063e9fb9 us-central1 engagement is running.
