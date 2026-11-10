# Widening the fast path to every eligible workspace

Status: decided; PRs 1 to 6 merged (#2354, #2356, #2359, #2385, #2394,
#2396) and PR 7 built (§11); the rollout is at WS4 (§8.2), with the
enabler in `apply` and all mode on since 2026-11-09, the wildcard row on
and `max_rank` 10 since 2026-11-10. This is the short note the production
rollout asks for before §8 step 9 of
[`fast-admission-and-batched-settlement.md`](fast-admission-and-batched-settlement.md)
(`fast-admission-production-rollout.md`, "P2 onward"): what widening
enables, what it checks and how it turns off. Joseph asked on 2026-11-05
for the Go edge to decide authorizes for every eligible workspace, not the
pilot set alone, and for the open questions to be decided here rather than
put to him. Each decision below states its reason.

Line citations are against `origin/main` at `b7bf9cfcf`. The widening's
steps are named WS0 to WS6, apart from the production rollout's W1 to W8.

## 1. The shape

The earlier memo's option 3, taken whole:

- **A policy-level "all" mode.** Python's pilot set, today an explicit list
  of at most 32 (`config.py:1624-1635`), can instead be every workspace but
  an exclude list: `fastpath_pilot_all` and `fastpath_pilot_exclude`. The
  edge policy carries both (§6.3).
- **An enabler job.** It bulk-enables eligible workspaces in
  `tr_fastpath_workspace` by tier, funding and plane, and disables them when
  they stop being eligible (§5).
- **A wildcard decide row.** One row of `tr_fastpath_edge_decide` decides
  for every workspace without a row of its own; a workspace's own row, on or
  off, wins over it (§6).

## 2. What holds throughout

- **W1 stays the allow-list** (production rollout §1, W1). Nothing is
  admitted or granted for a workspace whose `tr_fastpath_workspace` row is
  not enabled: the front door and the owner ask the node's switch copy
  (`service/switch.go`), and `Store.Grant` reads the row in its own
  transaction (`store/grant.go:103-111`). "All" widens who Python and the
  edge *ask*; the enabler's rows still decide who is *granted*. The enabler
  is the only new writer of the table.
- **The kill switches stay and win.** `fastpathctl disable-all` empties the
  allow-list and revokes every open lease in one transaction
  (`store/workspace.go:67-90`); `decide-off-all` turns every decide row off,
  the wildcard included (`store/decide.go:97-115`, `WHERE decide`). Neither
  changes what it stops. A kill is never undone by the enabler, by
  construction: every stop but the enabler's writes a hold, because the
  store writes it for every Go caller, a watch's trip included (PR 4);
  rows disabled before PR 4 are given one (`hold-backfill`); the enabler
  never writes a hold; and every enabler write is a transaction that
  stops on a fleet hold or a pause, every enable also on the workspace's
  own hold (§5.3, §5.6).
- **Fast-path workspaces stay outside async admission** (§4.13; G15 §4.5),
  through the same `Settings.async_settle_pilot_allows` rule, with the
  overlap list an operator-only exception (§9).
- **GCP `us-central1` only.** The AWS and Azure planes stay excluded
  (Joseph, 2026-11-05: hold off on that change). Their Postgres store has
  neither the debt rules nor the covering pass (design §8 step 4, #1574),
  which is reason enough on its own.

## 3. Membership, layer by layer

A request is leased only when every layer says so. Each layer exists today
except where marked new.

| Layer | Who writes it | What it says | Read by |
|---|---|---|---|
| Edge policy `fastpath_pilot_enabled`, the pilot set or **all minus exclude (new)**, `fastpath_pilot_regions` | `rollout.sh`, a Python deploy | whom Python and the edge consider | Python's `pilot_reason`, the edge's policy gates |
| `tr_fastpath_workspace` (W1) | **the enabler (new)**; `fastpathctl enable`, `disable`, `disable-all`; `fastpathwatch` when a ceiling trips (`cmd/fastpathwatch/main.go:287`) | who may be granted | the owner, the front door, `Store.Grant`; **the edge and Python, as a copy (new)** |
| `tr_fastpath_workspace_hold` **(new)** | every Go caller of `Store.SetWorkspace(false)`, which writes the hold itself: `fastpathctl disable` and `fastpathwatch`'s trip; and the operator's `disable-all`, `enabler pause`, `enabler arm`, `hold-backfill`, and `enabler resume`'s backfill; `enable`, `release`, `release-stamped`, `release-all` and `enabler resume` remove rows. Never the enabler | whom the enabler must not enable, and whether it may write at all | the enabler, in each of its write transactions |
| `tr_fastpath_kill` **(new)** | `fastpathctl disable-all` inserts a row; `release-all` deletes them all | the commit timestamp of each kill made while the fleet hold stands | `enabler resume`, the enabler's report |
| `tr_fastpath_edge_decide`, with **a wildcard row (new)** | `fastpathctl decide` | whether the edge answers itself or leaves the request to Python's front door call | the edge |
| `Store.Grant` refusals | the credit rows | tier, latch, pause, debt, allowance, floor | every grant |
| The node attribute `fastpath-edge-decide` | Terraform | whether the decide path runs at all | the node's startup |

An enabled row outside the explicit pilot set is inert today: Python's
`pilot_reason` passes it as `workspace` (`fastpath_frontdoor.py:633-634`)
and the edge as `policy.not_pilot_workspace`. Under all mode every enabled
row that is not excluded is live, whoever wrote it. WS2's gate (§8.2) is
there for that.

## 4. Decisions

### (a) A stale trust reconciliation

**Decision.** Leases do nothing about a stale reconciliation: no grant,
renewal or lease refuses on its age. The enabler alone acts on it, per
workspace: it does not newly enable a workspace whose
`trust_reconciled_through`, the oldest across its credit rows (the
reconciler writes it per workspace on every shard,
`storage_trust_reconciliation.py:509-514`), is older than
`trust_reconcile_max_age_seconds` (3,600 s) or absent. A workspace at its
tier by operator override counts as fresh; one whose tier 3 rests on an
identity bypass is enabled only from the include list ((c)). The freeze
stops enables only:
every disable, for any cause (§5.3), goes on. When more than half of a
run's otherwise-eligible candidates are stale, which points at the
reconciler rather than at workspaces, the enabler raises
`fastpath_enabler_trust_stale`.

**Why.**

- The watermark is written per workspace, but the reconciler that writes
  it is shared, and its ordinary lag reaches 45 minutes (§4.11). Refusing
  grants on it would let one stalled reconciler run stop every lease in the
  fleet, the outcome §4.11 already rejected for the grant.
- A dispute the reconciler has not seen leaves the workspace its tier on
  *both* paths. During that window today's path lets it spend its balance
  too. A lease reserves from that same balance, so leases add no exposure
  beyond the overrun windows §4.13 already bounds by the allowance.
- What a stale watermark does change is the evidence behind a *new* tier 3.
  Freezing that workspace's enable addresses exactly that, and costs
  nothing to the workspaces already on.
- Once the reconciler latches, the grant reads `trust_latched_at` at once
  (`grant.go:351-352`) and other owners stop within the state cache's age;
  the enabler then disables the workspace at its next run. So the window is
  the reconciler's lag, as it is today, plus the cache's age.
- A run against the wrong environment writes no watermark, which reads as
  absent, which freezes. An override has nothing to reconcile, which is why
  it counts as fresh. Disables keep running during a freeze because
  disabling is the safe direction: a rank or exclude change must take
  effect whatever the reconciler is doing.

### (b) Allowance, floor and required tier

**Decision.**

| Setting | Value | Node flag |
|---|---|---|
| Required tier | 3 | `-required-tier 3` (exists, `cmd/fastpath/main.go:73`; the node's unit passes none today) |
| Allowance, tier 3 | 1,000,000,000 microdollars ($1,000) per workspace | `-allowance` (new) |
| Allowance, tiers 0 to 2 | none: refused by the required tier | |
| Floor | 20,000,000 microdollars ($20) | `-floor` (new) |
| Enabler's balance to enable | unspent credit (`total_credits - total_usage`) at least $100 | enabler setting |
| Enabler's balance to disable | unspent credit under $40 | enabler setting |

**Why.**

- **Tier 3.** §4.11 and §8 step 8: tier 1 is one succeeded payment, which a
  stolen card that cleared also has. The design requires the grant to
  enforce the tier before any workspace but the operator's is leased. The
  nodes run tier 1 today (§10), so this lands first (PR 1).
- **One allowance, not a table.** The effective tier is at most 3
  (`trust_tiers.py:277`), so with the required tier at 3 only one tier ever
  holds a lease and `store.Config`'s single `Allowance` is the whole table.
  A per-tier table is built when the required tier is lowered, which stays
  Joseph's call (§9 of the design).
- **$1,000, as exposure.** §9 sizes an allowance by the exposure it
  permits, not by honest traffic: the allowance times the largest ratio of
  bill to hold a tier-3 workspace can route to. That ratio has no fixed
  bound below a model's own output limit. §4.13 lists sixty for an
  adaptive-thinking Anthropic request with no limit (32,000 tokens against
  a 512-token hold) and 83 from the effort mapping (a limit of 100 sent as
  8,292), but OpenAI-compatible upstreams and image-capable Gemini models
  are sent no output limit at all, so a request with the 512-token default
  hold on a model that writes 128,000 output tokens bills about 250 times
  its hold, and cache writes and price-tier boundaries add to that. The
  bound stated here is 250: $1,000 permits about $250,000 of charges
  against $1,000 of holds in the worst case, per workspace, each overrun
  reserved by its shortfall write and stopped by the debt mark (§4.7). A
  model with a larger output limit raises it in proportion.
  - **Why that is accepted.** The same workspace's synchronous holds are
    bounded only by its balance, and overrun by the same ratio (§4.13,
    "The amounts are today's"). A lease's holds are reserved from that
    same balance, less the floor, and are further capped by the allowance,
    so the holds that can overrun under leases are never more than today's
    path would allow the workspace. What leases add is the windows of
    §4.13 (one write, the auditor's lag on a lost write, the state cache's
    age), not a larger base. Only tier-3 workspaces get there: a 30-day
    payment history, $50 paid, an approved owner and no dispute, or an
    operator's override with an approved owner (the P1 synthetic workspace
    alone by identity bypass, §8.2).
  - **It must not throttle honest traffic.** A lease's allocation is a
    top-up of at most `TopUps.Max`, $10 (`service.go:156`), sized to about
    a minute of spend (`Horizon`), and a shard keeps several while holds
    are open, so honest exposure is about a minute of a workspace's spend.
    PR 1's precondition reads each pilot workspace's `LeaseReserved` with
    `fastpathctl status` (by its keys) at its busiest hour: if any is above
    a quarter of $1,000, the allowance is set to four times the highest
    instead, and this note is amended with the new exposure bound, not the
    figure read. `1 << 50`, today's value, bounds nothing.
- **$20 floor, and what it does not do.** The floor is the headroom a grant
  leaves for the same workspace's synchronous requests (streams the enclave
  does not heartbeat, service tiers, capped keys). No floor covers every
  hold: a hold prices the prompt at its estimate as well as the output
  (`authorize_candidates.py:483-485`), and the prompt is bounded only by
  the context window. Today a stream's hold is its full estimate, output
  budget unbounded by `HOLD_OUTPUT_TOKENS`; #2337 (open) gives streams the
  same bounded hold as any catalog request, which makes stream holds
  boundable by output but changes nothing about the prompt. The figure
  is a customer price from the runtime catalog, with the standard markup
  (5.5%, `pricing.py:126-129`, applied at ingest) and retirements applied,
  over credit endpoints, every price tier included. At `b7bf9cfcf` the
  highest output price on a credit endpoint of `catalog.MODEL_ENDPOINTS`
  is 79.125 $/M, about $1.30 for 16,384 output tokens. The priciest
  entries in the provider files are outside it: `openai/o1-pro`'s 600 $/M
  is a provider cost and the model retired on 2026-10-23
  (`provider_lifecycle.py:32`), and `openai/gpt-5.5-pro` lists 180 $/M,
  270 $/M in its long-context tier, which is 284.85 $/M to a customer,
  about $4.67 for 16,384 output tokens, should it be served on credits.
  $20 is four times that last figure, so an ordinary request's hold fits
  under either. PR 1's test computes the figure from the runtime catalog
  as above, not from the routing snapshot fixture or the provider files,
  and fails when it passes a quarter of the floor, so a price rise is
  reviewed rather than absorbed. A hold larger than the floor that meets
  a lease holding the rest gets 402 until PR 7's 503 is on
  (`fastpath_lease_headroom_503`), and 503 after it; WS6 waits for PR 7 for
  that reason.
- **$100 and $40.** Below about $40 a grant leaves at most $20 for leases
  past the floor, so most leases are refused and each request pays a front
  door hop to be passed. The gap between enabling and disabling keeps a
  workspace near the line from flapping each run.

### (c) The exclusion set

Each exclusion is enforced where its fact lives, and most twice. "Request"
is a per-request gate on both doors (Python's `pilot_reason`,
`services/fastpath_frontdoor.py:599-661`; the edge's §3 reasons in
`fast-admission-e2-decide.md`); "enabler" is the enabler's eligibility, so
the workspace is never in `tr_fastpath_workspace`; "grant" is a
`Store.Grant` refusal; "policy" is an edge policy gate.

| Excluded | Where | Notes |
|---|---|---|
| Effective tier below 3 | enabler, grant (`RefusedTier`), edge `workspace.tier` | the stored effective tier, override included |
| Tier 3 resting on an identity bypass, off the include list | enabler; counted in the report as `bypass` | the admin route sets `identity_bypass` for any workspace at any tier (`routes/internal/admin.py:54-72`), so the enabler does not trust it: a bypassed override reaches leases only for a workspace an operator also put on `fastpath_enabler_include` |
| Latched (`trust_latched_at`) | grant at once; enabler disables | a latch is tier 0 (`grant.go:351`) |
| Paused (any `billing_pause_causes`) | grant (`RefusedPaused`); enabler disables; edge `workspace.paused` | |
| In debt (any shard marked) | grant (`RefusedInDebt`); enabler disables; edge `workspace.in_debt` | |
| Unspent credit under $40 (under $100 to join) | enabler; grant (`RefusedFloor`) | (b) |
| Deleted workspaces | enabler (the entity's deletion); edge `workspace.deleted` | |
| BYOK candidates | request (`funding`; `candidates.byok`) | per request: the same workspace's credit requests stay in |
| Non-credit funding: partner billing, custom or user-provided models, priced extras | request (`pricing`; `body.priced_extra`, `body.custom_model`) | |
| Service tiers | request (`service_tier`; `body.service_tier`) | |
| Lifetime caps, windowed keys, `budget_strict` | request (`key_cap`; `key.*`) | windowed keys stay off (`TR_FASTPATH_PILOT_KEY_WINDOWS=off`) until e2-key-windows §8.3's gate, unchanged here |
| App markup | request (`markup`; `key.app`) | |
| Federated (deferred-settlement) keys and workspaces | edge `key.federated` and `workspace.federated`; enabler skips a workspace whose entity names a federated home; Python's `pilot_reason` has no such gate today, so PR 5 adds `federated` | |
| x402 | not excluded: x402 buys credits (`services/x402_billing.py`, `record_credit_purchase`) on the same credit rows, and counts as a qualifying payment for the tier (`TR_TRUST_QUALIFYING_PROVIDERS=stripe,x402`) | §10: there is no x402 key type for §4.11's exclusion to name |
| Synthetic probes | enabler: the workspaces of `synthetic_monitor_api_key`, `stage_d_probe_api_key` and `async_settle_shadow_probe_api_key`, and `client_events_synthetic_workspace_ids`; they also go on `fastpath_pilot_exclude` | the P1 synthetic workspace owns none of those keys and is on none of those lists, so it is not a probe here; it is eligible like any workspace at tier 3 by override (WS0) |
| Async-shadow workspaces | edge `policy.async_settle_shadow`; enabler; Python's `pilot_reason` has no such gate today, so PR 5 adds `async_shadow` | |
| The monitor key | edge `policy.monitor_key`; enabler skips its workspace; PR 5 adds `monitor_key` to `pilot_reason` | |
| Async-admission pilot workspaces outside the overlap list | `fastpath_pilot_exclude`, required by a validator (§9) | |
| Regions other than `us-central1` | policy (`fastpath_pilot_regions` stays `us-central1`); the wildcard row names only `us-central1` | |
| The AWS and Azure planes | the edge's `boot.signature`; their control planes leave the pilot off; the enabler runs only against GCP's database; `fastpath_pilot_all` refuses to start without the typed Spanner store | held, (2) |
| Held workspaces | enabler, in each enable's transaction (§5.6) | |
| Streams the enclave does not heartbeat, idempotency keys over the edge's bounds, speculation descriptors | request, unchanged | §4.11 |

Before PR 5, Python relied on the enabler and W1 alone for the
federated, async-shadow and monitor-key rows. PR 5 adds `federated`,
`async_shadow` and `monitor_key` to `pilot_reason`, read from the same
facts the edge reads (the key's and the workspace's `federated_home`, the
published `async_settle_shadow_workspaces`, the request's lookup hash
against `monitor_lookup_hash`), so all mode never relies on them.

### (d) The enabler job

**Decision.** A Python Cloud Run job, `fastpath_enabler`, every 15 minutes
at minutes 12, 27, 42 and 57, five minutes after the tier job
(`scripts/deploy/trust_tier_job.sh`, `7,22,37,52`). It is deployed by its
own script with its own environment, and on every control-plane deploy
rather than behind `TR_TRUST_JOBS_DEPLOY=1`. Details in §5.

The tier job can run for most of its fifteen minutes (one worker took
about 12 of its 14 minutes, which is why it now runs eight,
`trust_tier_job.sh:32-35`), so the enabler may read while it is still
writing. That is harmless: a
tier not yet written is read at the next run, and each enable rechecks
the workspace's credit rows in its own transaction (§5.3).

**Why Python and that cadence.** Every input is a Python fact: the
effective tier and its watermark, the settings that name probe, monitor,
async and excluded workspaces, the workspace entity's federated home and
deletion. The tier changes only when the tier job runs. Latches, pauses
and debt refuse grants at once without it, so its lag affects who joins
and the tidiness of the allow-list, never money.

### (e) The wildcard decide row

**Decision.** The row `('*', 'gcp', 'us-central1')` decides for every
workspace that has no row of its own for that cloud and region. A
workspace's own row wins over the wildcard in both directions, so an off
row is an override. With the wildcard off, the table means what it means
today. Details, the commands whose meaning changes, and the deploy order
in §6.

**Why.** It keeps the table a handful of rows that every node reads every
5 s whole (`ReadDecide`), rather than a row per workspace; and `*` can
never be a workspace's id, since `fastpathctl` takes only ids beginning with
a letter or digit.

### (f) The fleet audit

**Decision.** The hourly per-workspace script
(`/var/tmp/pilot-audit-hourly.sh` on one node, `fast-admission-pilot.md`
"What it checks") is replaced by an audit role inside the fast-path
service. Details in §7.

**Why.** The script runs from `/var/tmp` (kept in `scripts/fastpath/pilot/`
since a node's replacement would lose it), names its workspaces in a list
by hand, runs one `fastpathctl` process per workspace and one funding read
each. None of that survives a few thousand workspaces.

### (g) The widening sequence

**Decision.** Seven steps, WS0 to WS6, by tier and spend group, each gated
by counts read from that step's own traffic, with no calendar soak (§8).
The count-gate rule is Joseph's of 2026-10-30 for windowed keys, applied
here. That includes I2's own sample: its 24-hour span is replaced by a
count (§8.1 item 3).

**Why.** A soak measures time, not evidence. The I2 and I3 gates already
moved to samples (`fast-admission-e2-decide.md`, "I2 gates") for that
reason, keeping only the 24-hour span as a stand-in for the variety a day
of traffic brings; a count of distinct keys and digests measures that
variety directly.

### (h) Async admission

**Decision.** Workspaces the enabler turns on are outside async admission
by default, through `Settings.async_settle_pilot_allows`, rewritten so that
it reads membership as "all minus excluded" when that mode is on. The
overlap list stays an explicit, operator-only exception and is never
widened by this work. Details in §9.

## 5. The enabler

### 5.1 Inputs

One snapshot per run, read the way the tier job reads its own: one strong
read-only snapshot, read in chunks (`trust_tier_bulk.py:103-113`), at LOW
priority:

- `tr_credit_balance` for every workspace: per shard the effective
  `trust_tier`, `trust_latched_at`, `trust_override_tier`,
  `billing_pause_causes`, `in_debt`, `total_credits`, `total_usage`,
  `reserved` and `trust_reconciled_through`;
- the workspace entities of the candidates, by complete key in chunks
  (`kind` and `id IN UNNEST(@ids)`, as `BULK_ENTITIES_BY_ID_SQL`), for the
  federated home and deletion. Never a body scan (AGENTS.md);
- `tr_trust_override` of the candidates, by complete key in chunks, as the
  tier job reads it (`BULK_OVERRIDES_SQL`, `trust_tier_bulk.py:59-62`):
  `tier` and `identity_bypass`. A bypass leaves no trace on the credit
  rows (the admin route writes the shard tier alone,
  `storage_gcp.py:1368-1409`), so this is the only place to see it. A
  workspace's tier 3 "rests on a bypass" exactly when its override row has
  `identity_bypass = true`; no owner read is needed;
- `tr_fastpath_workspace` and `tr_fastpath_workspace_hold`, whole (one row
  per workspace ever enabled or held);
- the probe and monitor keys' workspaces, resolved by lookup hash with
  complete-key reads;
- its settings (§5.7).

A read that fails anywhere aborts the run before any write. A partial
snapshot never decides.

### 5.2 Eligibility

A workspace is eligible when none of (c)'s enabler rows excludes it, it is
not on `fastpath_pilot_exclude`, and it is ranked within `max_rank`. The
rank is by lifetime usage on its credit rows (the sum of `total_usage`),
largest first, ties by id, among workspaces otherwise eligible. Workspaces
on `fastpath_enabler_include` (at most 32: the current pilot set, carried
over) are exempt from the rank and from the bypass exclusion of (c), and
from nothing else.

The rank has a margin. A workspace joins at rank `max_rank` or better,
and leaves for its rank only past `max_rank + max(5, max_rank / 10)`, or
past `max_rank` itself when `max_rank` is 0: at 0 nobody off the include
list belongs, so there are no near-ties to absorb.
Lifetime usage moves slowly and workspaces near the boundary trade places
on small differences, so without a margin the enabler would enable and
disable them in turn, each disable revoking leases. A margin of a tenth,
at least five, absorbs those near-ties, while a real fall in rank still
leaves at the next run. It needs no memory of earlier runs, which a rule
of N runs out of rank would.

The rank uses lifetime usage because the snapshot already holds it and it
needs no analytics read; the largest spenders carry most of the traffic
(§4.11) and have the longest payment history.

### 5.3 What a run does

- **Every write is guarded.** Each enable and each disable is its own
  read-write transaction that first reads `*`, `#pause` and `#armed`
  (§5.6), and writes nothing if `*` or `#pause` exists or `#armed` does
  not. So a run that took its snapshot before a pause or a kill cannot
  commit any write after it: its transaction either commits before the
  pause's or kill's commit timestamp, or reads the row and stops.
- **Enables** each eligible workspace not enabled, at most 500 a run, so a
  group lands over a few runs. Each enable's transaction also reads the
  workspace's own hold, its credit rows and its `tr_trust_override` row by
  complete key, and writes the switch only if there is no hold and the rows
  and the override still make it eligible; else it writes nothing and
  counts why. Spanner orders that transaction
  against any `disable` or `disable-all` that writes a hold, so an enable
  is either before the kill, and the kill turns it off, or after, and sees
  the hold. A per-workspace freeze ((a)) skips the enable.
- **Disables** each enabled workspace that is latched, paused, in debt or
  held, with no cap: these are the safe direction and already refused by
  the grant. An uncapped cause takes precedence: a workspace with one is
  disabled uncapped and counted under that cause alone, even when it also
  has a capped one. A latch makes the effective tier 0
  (`trust_tiers.py:277`), so a latched workspace is also below tier 3, but
  it is disabled as latched; a mass latch never reaches the guard below
  and never raises `fastpath_enabler_mass_disable`. The guard and the pace
  count only workspaces whose every cause is capped. For any other cause
  that came from a change of facts (tier below 3, balance under $40, a
  federated home, a deletion, a probe, a bypass), the cap is a guard: when
  a run would disable more than 10% of the enabled set or 20 workspaces,
  whichever is larger, for those causes, it disables none of them that run
  and raises `fastpath_enabler_mass_disable`, since a sudden mass change
  of facts is more likely a reading defect than a fact. A rank past the
  margin of §5.2 is not guarded but paced: each run disables at most the
  same number, lowest rank first, and raises nothing, since a large rank
  change comes from the operator lowering `max_rank` and a small one from
  usage moving, and the job cannot tell them apart. A lower `max_rank`
  therefore drains over a few runs; a rollback that must be fast uses the
  exclude list, `pause`, `disable` or `disable-all` (§8.3). A new exclude
  entry is applied whole. No freeze holds back a disable.
- Writes nothing for a workspace whose state already matches, so the table
  sees writes only on change.

### 5.4 The report

Every run, dry-run or not, logs one JSON line with: eligible, enabled,
would enable and would disable by reason, excluded by each reason of (c),
held, frozen, capped, `bypass`, enabled rows outside the include list, and
in apply mode the commit times. The lists of workspaces to enable and
disable go in the job's own log, which only operators read.

The report also counts `unheld_disabled`: every `enabled = false` row of
`tr_fastpath_workspace` with no hold of its own, fleet-wide, whatever its
rank or eligibility (both tables are read whole). Only a per-workspace
hold counts as a hold here; a row under the fleet hold `*` is counted,
split as `under_kill` when its `changed_at` is a kill's commit timestamp
in `tr_fastpath_kill` (§5.6) and `other` when it is not, so a stop that a
kill only covers is still seen. `other` is not expected to be zero once
the enabler has run: its own disables (balance, debt, latch, rank) are
unheld and unstamped and land there. Such a row is either the
enabler's own earlier disable or a stop that wrote no hold. Before the
enabler is armed it has written nothing, so every such row is the second
kind; WS1 and WS2 gate on the count being zero (§8.2), and `enabler arm`
refuses otherwise (§5.6). Each would-enable among them is listed apart,
with its row's `changed_at`.

### 5.5 How it writes

The switch is written as `SetWorkspace` writes it: the row's `enabled` and
its commit timestamp (one mutation), and on disable the workspace's open
leases revoked in the same transaction (one DML), so each takes no
renewal, expires, drains and closes, and what it admitted still settles.
The enabler's enable adds the hold and credit-row reads of §5.3 before
that write. The statements move to one fixture,
`fastpath/testdata/switch_statements_v1.json`, which Go embeds and Python
reads, so the columns, values and revocation cannot drift. The fixture has
two disables. `disable` is the store's: the switch, the revocation and a
hold with the caller's reason, in one transaction. `disable_unheld` is the
same without the hold, and only the enabler's own disable reads it, through
a function of its own in Python (`disable_unheld`), never a flag on the
common one. Go has no unheld disable at all, so a new Go caller gets the
hold whether or not it knows about holds. The enabler does not wait for a
disabled workspace to drain: W2's `fastpathctl status` and the fleet audit
(§7) see that.

### 5.6 Holds and pausing

The kill path is robust by construction, not by inference: every stop
but the enabler's writes a hold, and the enabler never writes one, so the
enabler never has to guess whether a disabled row was its own change or
someone else's stop. It does not try to
detect a kill from the switch table, whose rows cannot say who wrote them
(`DisableAll` stamps every row it turns off with one `changed_at`,
`store/workspace.go:75-76`).

- **The table.** `tr_fastpath_workspace_hold` is keyed by workspace id
  alone, with `reason` (`operator`, `watch`, `backfill` or `paused` for a
  workspace; `kill`, `pause` and `armed` for `*`, `#pause` and `#armed`,
  which a constraint ties to those rows), `detail` (what the stop says of
  itself: a watch's tripped ceilings) and `held_at`, the holding write's
  commit timestamp. `fastpathctl holds` reads it, the kills and
  `unheld_disabled` as §5.4 counts them, read only, exiting 3 while any
  row is off with no hold of its own (PR 4).
- **The hold is in the store.** `Store.SetWorkspace(ctx, workspace,
  false, reason)` writes a hold, with the reason its caller gives, in the
  switch's transaction, for every caller. `fastpathctl disable` passes
  `operator`; `fastpathwatch` passes `watch` and the ceiling that tripped,
  so a watch's stop (`cmd/fastpathwatch/main.go:287`) is held and the
  enabler does not undo it once the watch has exited. There is no
  `-no-hold`, and Go has no unheld disable. Under a kill, a watch's later
  trip rewrites W's `changed_at` away from the kill's stamp, which is
  harmless now: W is held with the watch's reason and stays off after
  `release-all`, as a stop should.
- `fastpathctl enable WORKSPACE` removes the workspace's hold in the
  switch's transaction, so a hand enable after a holding disable is not
  reverted. The enabler then manages it like any workspace: if it is not
  eligible, the next run disables it, for that cause.
- `fastpathctl release WORKSPACE` removes a hold without enabling. It
  refuses while `#pause` exists ("resume first"), reading `#pause` in its
  transaction, as `release-all` does: released during a pause, the row
  would be held again by resume's backfill.
  `release` and `enable` take only a workspace id (beginning with a letter
  or digit, as `fastpathctl` checks today), so they refuse `*`, `#pause`
  and `#armed`. Only `decide delete` takes any id (`main.go:56-58`), and
  that is the decide table, not this one.
- `fastpathctl disable-all` writes the fleet hold, the row `*` (inserted
  if absent), and inserts one row into `tr_fastpath_kill`, all in the same
  transaction as the switch and the revocations:

  ```sql
  CREATE TABLE tr_fastpath_kill (
    kill_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (kill_at);
  ```

  `kill_at` is written as `PENDING_COMMIT_TIMESTAMP()`, a whole scalar
  column value, the only form Spanner allows: it cannot be an array
  element, so a list on `*` cannot hold it. `DisableAll` stamps every row
  it turns off with that same commit timestamp
  (`store/workspace.go:75-76`), and touches only rows still enabled, so a
  row stopped earlier keeps its own `changed_at`: the rows the kills
  stopped are exactly those whose `changed_at` is a `kill_at` row. Two
  kills cannot share a timestamp, since `DisableAll`'s update conflicts
  with every other switch write. While `*` exists the enabler writes
  nothing and reports `held: fleet`. `fastpathctl release-all` deletes
  `*` and every `tr_fastpath_kill` row in one transaction. It refuses,
  writing nothing, while `#pause` exists ("resume first"), reading
  `#pause` in that transaction: released first, the kill rows would be
  gone before resume could skip the rows they stamped, and resume would
  hold the whole allow-list one row at a time. So under a pause the only
  order is `enabler resume`, then `release-all`.
- Why a table of its own and not hold rows keyed (`*`, `kill_at`): the
  hold table is keyed by workspace id alone, and every enabler write reads
  `*`, `#pause`, `#armed` and the workspace's hold by that key. A second
  key column would make the fleet hold a range instead of one row in every
  one of those transactions. The kill table is append-only, small and
  read whole, by resume and the report only.
- `fastpathctl enabler pause` inserts the row `#pause`, its `held_at` the
  pause transaction's commit timestamp (`PENDING_COMMIT_TIMESTAMP()`), the
  same clock as the switch's `changed_at`. It is insert-only: pausing while
  paused writes nothing and keeps the first `held_at`, so a resume's
  backfill always reaches back to the first pause. While `#pause` exists
  every enabler write stops (§5.3) and the allow-list stays as it is: the way
  to stop the enabler without a kill or a deploy. `fastpathctl enabler
  resume` removes it, only together with the backfill described below.
- `fastpathctl enabler arm` inserts the row `#armed`, and refuses unless
  every `enabled = false` row already has a hold of its own: the WS2
  gate, enforced by the command. The check and the insert are one
  read-write transaction that reads the switch and hold tables whole, so
  a stop committed during it is either seen by the check or ordered after
  the arm and held by PR 4's `disable`. The enabler in `apply` writes
  nothing until `#armed` exists. Nothing removes `#armed`.
- After the arm, `fastpathctl release WORKSPACE` is how an operator hands
  a stopped row back to the enabler: the hold goes, and the next run
  enables the workspace if it is eligible, or leaves it off if not.
- The enabler's own disables write no hold, so a workspace that refills or
  regains its tier joins again.
- `*`, `#pause` and `#armed` can never be workspace ids (`fastpathctl`
  takes ids beginning with a letter or digit).
- **Rows disabled before PR 4 get a hold.** Today's `disable` writes only
  `enabled = false` (the P1 runbook's "Way back" is one,
  `docs/runbooks/fastpath-p1.md:181`), and the include list is exempt from
  the rank, so the enabler would enable such a workspace again.
  `fastpathctl hold-backfill`, from PR 4, writes a hold, reason
  `backfill`, for every `enabled = false` row that has none, in one
  transaction, and is idempotent. It is data, not schema, so it runs as an
  operator command after PR 4's migration and before WS1, not in the
  migration job (W4). The table has a row per workspace ever enabled, so
  it is small. It refuses once `#armed` exists: after that, unheld
  disabled rows include the enabler's own disables, which a backfill
  would wrongly hold. The resume path has its own backfill, bounded by
  the pause (below), and needs no flag.
- **No `fastpathctl` older than PR 4 is used once the enabler applies.**
  An older `disable`, `disable-all` or `fastpathwatch` trip writes no
  hold, and the enabler would enable those workspaces again at its next
  run. WS2's entry checks the release on every node, since `fastpathctl`
  and `fastpathwatch` reach operators only through the release bucket
  (`infra/fastpath_node_startup.sh.tftpl:180-184`).
- **Rolling back below PR 4.** Before any node or `fastpathctl` goes back
  to a release without PR 4, the enabler is paused with `fastpathctl
  enabler pause`, while PR 4's binary is still in hand, and stays paused
  until every node is on PR 4 again. Setting the job's mode to `off`
  instead is not enough here: it leaves no time to backfill from. The
  hold table is schema and stays; an older binary just does not read or
  write it. The pause stops only the enabler: operators keep the older
  binary's `enable` and `disable` meanwhile, and the way back from a kill
  during the rollback is an older `enable` per workspace (§8.3).
- **Resume refuses enabled and held rows.** An older `enable` leaves the
  workspace enabled with its PR 4 hold still there, which the enabler
  would read as held and disable, uncapped. So `fastpathctl enabler
  resume` refuses while any row is enabled and held, and prints those
  rows. The operator resolves each with PR 4's `enable`, which clears the
  hold, or `disable`, which keeps it, and resumes. An enabled and held row
  from a rollback therefore never reaches the enabler. The check counts
  per-workspace holds only, never `*`. A row brought back with an older
  `enable` after a kill is listed only if it carries a hold of its own
  (a kill writes no per-workspace hold; a row it stops keeps whatever
  hold it had, which is none unless an older `enable` left a PR 4 hold on
  it). A PR 4 kill covers its rows with `*` and a kill row instead. So
  resume lists only rows that had a PR 4 hold (a `disable`, a watch's
  trip, a backfill) and were enabled with the older binary.
- **Resuming backfills first, by construction.** A stop made during the
  rollback by an older `disable` or `disable-all` wrote no hold. So
  `fastpathctl enabler resume` removes `#pause` only in the same
  transaction as a backfill since the pause: it writes a hold, reason
  `paused`, for every `enabled = false` row that has no hold and whose
  `changed_at` is at or after the pause row's `held_at`. A row already
  held, by a PR 4 `disable` for instance, keeps its hold and its reason.
  While paused no enabler write could commit (§5.3), so every such row is
  an operator's stop. There is no resume without that backfill.
- **A kill under a pause stays one step to undo, and only the kill.**
  When the fleet hold `*` exists at resume time, resume's backfill skips
  exactly the rows whose `changed_at` is a `tr_fastpath_kill` row, read in
  its transaction, and holds every other unheld row disabled since the
  pause. A row an operator stopped before a kill, inside the pause, keeps
  its own timestamp, so it is held one by one and stays off after
  `release-all`; two kills during one pause each have their row, so
  `release-all` undoes both, and only them. Resume takes no timestamp from
  the operator: it reads the kills' timestamps from the rows the kills
  wrote. The order is fixed: `release-all` refuses while `#pause` exists,
  so the kill rows are still there when resume reads them.
- **A kill by an older `disable-all`** recorded no timestamp, so its rows
  are held one by one at resume like any other stop since the pause. That
  fails safe: they stay off after `release-all`. Bringing them back is an
  operator's own decision, made explicitly: `fastpathctl release-stamped
  TS` releases only rows that are held with reason `paused` and whose
  `changed_at` is exactly TS. It prints how many holds and workspaces it
  would release before writing, and writes only with `-yes`, which, as
  `release`, refuses while `#pause` exists. The enabler
  infers nothing from a timestamp; the operator names one, sees what it
  covers, and confirms.
- **What resume proves, and where to read it.** The invariant is narrow:
  after a resume, every unheld disabled row whose `changed_at` falls in
  the pause's window, from `held_at` to the resume's commit, is under a
  kill. Rows outside the window are not covered by it, and the enabler's
  own disables before the pause are unheld by design. Since resume
  removes `#pause`, the window is gone afterwards, so resume prints it,
  from inside its transaction: the window `[held_at, resume commit)`, the
  count of rows it held, the count it skipped as under a kill, and the
  count already held. The runbook reads that output, not the report's
  fleet-wide `other`.

### 5.7 Settings and deploy

The trust jobs carry their own environment (`trust_tier_job.sh:25-38`) and
deploy only with `TR_TRUST_JOBS_DEPLOY=1` (`trust_jobs.sh:14-17`); nothing
in `rollout.sh` reaches them. So:

- `scripts/deploy/fastpath_enabler_job.sh` deploys the job and its
  schedule, and `deploy.yml` runs it on every control-plane deploy, not
  behind the trust jobs' opt-in, so the job's settings move with the
  control plane's.
- The lists both read live in one file,
  `scripts/deploy/fastpath_lists.sh`: `TR_FASTPATH_PILOT_EXCLUDE` and
  `TR_FASTPATH_ENABLER_INCLUDE`. `rollout.sh` and the job's script source
  it, and a test holds each environment array to the variables.
- The job's own literals: `TR_FASTPATH_ENABLER_MODE` (`off`, `dry-run` or
  `apply`; `off` by default), `TR_FASTPATH_ENABLER_MAX_RANK` (0 by
  default), the thresholds of (b), and the async pilot, overlap and shadow
  lists and probe keys it reads from the same secrets and literals the
  control plane does.
- During a deploy the job and the control plane can briefly disagree on
  the exclude list. A row written for a workspace the control plane
  excludes is inert, since neither door covers it, and the next run
  removes it; a row missing for one it includes only means today's path.

## 6. The wildcard decide row

### 6.1 Semantics

For a request of workspace W at cloud C and region R:

1. W's own row for (C, R), if there is one: its `decide`, on or off.
2. Otherwise the wildcard row `('*', C, R)`, if there is one: its `decide`.
3. Otherwise off.

The node's copy (`DecideView`) keeps off rows and the wildcard, not only the
rows on (`edge/decide.go:937-966`). A copy past its age is
`cache.decide` for every workspace, wildcard or not, as today. The
decide path's count line attributes each answer to `own` or `wildcard`
(`decided.via`), so a step can count what the wildcard decided.

The edge also gains a decision gate beside the decide row,
`workspace.not_enabled`: the node's switch copy says W is not enabled. It
sits with the decide row, after the compose, so the shadow still composes
and compares every covered workspace's request, enabled or not
(`fast-admission-e2-decide.md` §3, "The decide row is checked after
everything else"). Without it the edge would reach the front door and pass
on `owner.off`; with it the pass is named and cheaper.

### 6.2 Commands, old and new

- **`decide status WORKSPACE`** reports the effective decision: it exits 3
  while the workspace decides in that cloud and region, by its own row or
  the wildcard, and 0 while it does not, with `via` (`own`, `wildcard` or
  `none`) beside its rows. Today it reads the workspace's own row alone
  (`fastpathctl/main.go:220-238`), which would exit 0 for a workspace
  deciding under the wildcard; the runbook's checks rely on the exit code
  (`fastpath-edge-decide.md:156`, `:574`), whose meaning, "does not
  decide", this keeps.
- **`decide delete WORKSPACE`** keeps its contract, "it only ever stops one
  deciding" (`main.go:45-47`, `store/decide.go:116-121`), by refusing (exit
  2, nothing written) to delete an off row while the wildcard for that
  cloud and region is on, since the workspace would start deciding. With
  `-follow-wildcard` it deletes anyway and says so. Deleting a
  workspace's own on row is allowed always: under an on wildcard it
  changes nothing, under none it stops the workspace deciding.
- **`decide delete '*'`** deletes the wildcard row itself
  (`fastpathctl/main.go:56-58`, `:159`; `store/decide.go:38-41` accepts the
  key), so every workspace without an own row stops deciding in that cloud
  and region. That is the stop direction, so it stays allowed, with no
  flag; its answer says the row was the wildcard. `decide wildcard off` is the
  same stop that keeps the row for `decide-restore`; the runbook uses that
  one.
- **`decide off WORKSPACE`** writes an override that holds under the
  wildcard; `decide on WORKSPACE` writes an own row that decides with the
  wildcard off, as today.
- **`decide wildcard on|off|status [-cloud gcp] [-region us-central1]`**
  sets and reads the wildcard.
- **`decide-off-all`** turns the wildcard off with every other row
  (`WHERE decide`), so it is still "nobody decides". Every row it turns off
  shares its commit timestamp, which it prints.
- **`decide-restore -at TS`** turns back on exactly the rows whose
  `changed_at` is that timestamp and are off. Off overrides set before the
  kill keep their own timestamps and stay off. So the way back from a kill
  is exact, and nobody reconstructs who was on.
- `docs/runbooks/fastpath-edge-decide.md` and `fastpath-turn-off.md` are
  updated in the same PR as the code (PR 3).

### 6.3 Deploy order

- **Go before the wildcard row.** A node from before this change reads `*`
  as a workspace's id, which no request carries, so it decides nobody new.
  Writing the row early is safe and useless; it is written only once
  `fastpathwatch edge` shows every node on the release.
- **Go before Python's policy.** Go's parser refuses an unknown member
  (`routingsnapshot/edgepolicy.go`, `EdgePolicy`'s comment), and a node that
  refuses a policy keeps its last copy until `not_after` and then passes
  every request. So Go learns the new members first, as optional members
  that may be absent, and Python publishes them only after every node is
  pinned with no rise in `cache.policy` (as G15's P5 was gated).
- **Python's members are absent when off.** With `fastpath_pilot_all`
  false the body is byte-identical to today's, as the key-windows and
  streams members are. `fastpath_pilot_all` is written only as `true`;
  `false` and `null` are refused by both parsers. `fastpath_pilot_exclude`
  is present exactly when `fastpath_pilot_all` is, sorted and unique, 0 to
  64 entries.
- **Rolling Go back below PR 3** needs Python's all mode off first and its
  deploy done: an older node refuses a policy carrying the members and,
  within 60 s, passes every request. That is safe but takes the edge out
  of service. The wildcard row needs no change for a rollback: an older
  node ignores it.
- **During a Python rollout** old and new instances sign different bodies,
  and the edge keeps the newest `issued_at` (`fast-admission-e2-decide.md`
  §4). The edge may go between "the list" and "all" for a minute. Each
  difference is only a pass to Python.

## 7. The fleet audit

- **Where.** A role of the fast-path service on whichever node owns the
  ring key `fleet-audit`, so it moves with node replacement and runs
  exactly once in the fleet. Each run logs a line as it begins, at least
  every 30 minutes as it goes and a summary as it ends; an absence of those
  lines for 90 minutes raises `fleet_audit_stale`. A run may take its whole
  hour, so two summaries can be two hours apart; the lines at its start and
  as it goes keep them within an hour and a minute of each other, so only
  an audit that stopped is absent.
- **Whom.** Every workspace whose `tr_fastpath_workspace` row is enabled or
  changed within 26 hours, read from that table, never by scanning leases.
  Workspaces with an open or draining lease first, then those whose switch
  changed, then the rest in rotation, each group by when it was last tried,
  so a workspace whose checks keep failing does not go first in every run.
- **What.** The checks `fastpathctl audit` makes today, per workspace, each
  in one read-only transaction by the workspace's keys:
  `store.CheckIdentity`, `CheckWinners`, `CheckBookedUsage` and
  `AuditSums`, the synchronous holds counted beside the leases' (#1821).
  `-funded` is dropped: the counter reconciler's combined identity
  (`storage_gcp_counter_reconcile.py`) covers the credit rows across the
  fleet, and nobody can type every workspace's funding.
- **How fast.** At most 5 workspaces a second, low priority, a 10 s deadline
  each, no retry within a run. A pass that cannot cover every workspace
  with a lease within the hour says so in its summary
  (`audited`/`due`), and a workspace with a lease unaudited for three hours
  raises `fleet_audit_stale`.
- **What it raises.** Each fault is a `fleet_audit_fault` line naming the
  workspace and the check, at error severity, with its alert policy in
  `infra/fastpath_alerts.tf` and a row in `docs/runbooks/fastpath-alerts.md`.
- **What retires, and when.** The hourly script retires with PR 2. The
  per-workspace `fastpathwatch -workspace` watches stay, one per include
  list workspace, until WS6's gate is met; WS6's activation PR (PR 8)
  stops them and updates `fast-admission-pilot.md` and the watch runbook.
  No watch is started for a workspace the enabler adds. Their spend
  ceilings guarded a pilot of named workspaces; at fleet scale a
  workspace's lease spend is bounded by its allowance and its balance,
  and W3's fleet ceilings (Spanner CPU, backlogs, pending work overdue)
  stay with W7's alerts. Until they stop, each watch's trip writes a hold
  through the store (§5.6).

## 8. The widening sequence

### 8.1 The bars every step reads

Read with `fastpathwatch edge -since` the step's start, the fleet audit's
summaries since then (its `ended=true` lines alone: a start or progress
line is not a pass), and the enabler's reports. A step's own traffic only:
counts from before it do not count.

1. The fleet audit: at least two complete passes over every workspace of
   the step that held a lease, zero `fleet_audit_fault`.
2. Zero `fastpath_alert` of kind `audit_fault`, `gap`, drain overdue or
   pending work overdue.
3. The I2 bars (`fast-admission-e2-decide.md`, "I2 gates"), with its
   24-hour span replaced by counts, on Joseph's preference for count gates
   over calendar soaks: at least 3,000 comparable requests spanning at
   least three distinct policy digests and two distinct snapshot digests,
   as I2 has them, and from at least 200 distinct API keys (the shadow's
   new `distinct_keys`, a count kept per node over a bounded set of lookup
   hashes and printed as a number only; the gate reads the largest node's
   figure, so a key seen by two nodes is not counted twice; a node's set
   starts with its process and its line carries `distinct_keys_since`, so
   only a node restarted after the step's start counts, and the bar is
   unread until a node cycle or deploy follows that start). The 24 hours
   stood in for the variety of keys, request shapes and policy and catalog
   changes a day brings; the distinct keys and digests count that variety
   directly. Then `agree_full` at least 99.5% with every difference
   explained, zero `differ.estimate*`, zero `row.idempotency_fingerprint`,
   no field counted `other`, and zero `would_admit_python_refused` beyond
   key-revocation lag. Under all mode the shadow keeps comparing every
   covered workspace that is not enabled (§6.1), so this sample keeps
   growing at every step.
4. Front door answers: `owner.busy`, `owner.unreachable` and
   `owner.failed` together under 1% of the front door's calls, from Python
   and the edge each.
5. Grants: zero `RefusedAllowance`; `RefusedFloor` and `RefusedTier`
   counted and each explained by a fact the enabler had not yet seen.
6. W3's ceilings: Spanner CPU, every subscription's backlog, pending work
   overdue, none crossed.
7. The enabler: no `fastpath_enabler_mass_disable` or
   `fastpath_enabler_trust_stale`, and its reports' counts matching the
   step.

Any alert, or any bar missed, is the way back (§8.3), then the cause.

### 8.2 The steps

| Step | What changes | Its own gate, beyond §8.1 |
|---|---|---|
| WS0 | Nodes re-pinned with `-required-tier 3`, `-allowance` and `-floor` (PR 1), after the P1 synthetic workspace reaches effective tier 3 (below) and every pilot workspace passes `fastpathctl ready` at those values | 2,000 leased settles on the current set since the re-pin; zero `RefusedTier` |
| WS1 | `fastpathctl hold-backfill` run (PR 4); the enabler in `dry-run` | four consecutive runs whose reports were read: every current pilot workspace eligible, every exclusion of (c) counted where it should be, and `unheld_disabled` (§5.4) zero: no `enabled = false` row anywhere without a hold |
| WS2 | Entry, in this order: every node's release, and so every `fastpathctl` in use, carries PR 4; `hold-backfill` run again, which holds any stop an older node made since WS1's run (it is idempotent, and the enabler has written no disable yet); a dry-run report taken after it with `unheld_disabled` zero; `fastpathctl enabler arm`, which checks the same itself. Then the enabler in `apply` with `max_rank` 0 (it keeps the include list and enables nobody new); then, once a run's report shows the enabled set within the include list and nothing to disable, `TR_FASTPATH_PILOT_ALL=true` with the explicit list emptied | 2,000 leased settles under all mode; no `workspace` pass from Python for an enabled workspace (its switch copy works) |
| WS3 | The wildcard row on for `gcp`, `us-central1`, then the include list's own on rows deleted, so they decide under the wildcard | 2,000 authorizes with `decided.via` `wildcard`; no `edge_decide_unavailable_sustained` |
| WS4 | `max_rank` 10: the ten largest tier-3 workspaces | 10,000 leased settles from the step's workspaces, and at least five of them granted a lease |
| WS5 | `max_rank` 100 | 20,000 leased settles from the step's workspaces, and at least 50 granted |
| WS6 | PR 7 (the 503) deployed; `max_rank` unbounded: every tier-3 workspace | 20,000 leased settles from the step's workspaces, and at least 80% of those that sent an eligible request granted |

**The P1 synthetic workspace at tier 3.** An override does not reach tier
3 by itself: `effective_trust_tier` caps it at 1 unless the owner's
identity is approved or the override sets `identity_bypass`
(`trust_tiers.py:277`, `:322`), and
`scripts/provision_fastpath_p1_workspace.py:152-153` writes
`identity_bypass=False`. WS0's runbook step reruns the script with `--tier
3 --identity-bypass`, a flag PR 1 adds, which the script accepts only for
a workspace with no API key that it funded by its own admin grant: the
operator's own synthetic workspace, from which nobody else can spend, so
identity verification proves nothing about it. The script is not the
limit: the admin route accepts a bypass for any workspace. The enabler
is: it never enables a workspace whose tier 3 rests on a bypass unless it
is on the include list, where the P1 synthetic workspace is. The pilot's
customer workspace is expected to reach tier 3 with an approved owner.
`fastpathctl ready -required-tier 3` reads the effective tier the grant
reads, so it proves either way.

Why WS2's report gate: an enabled row outside the include list, from a hand
`enable` or an old test, is inert under the explicit list and live under
all mode (§3). The gate proves there is none before coverage widens.

All mode with the enabler off, paused, held or frozen is the allow-list as
it stands: whatever rows are enabled, less the exclude list, are covered,
and nothing joins. Rows written by hand are live. That is the state WS2's
gate starts from, and the state the way back (§8.3) leaves.

Why large first: the largest workspaces carry most of the traffic, so each
step gathers its evidence fastest; their balances dwarf a lease, so the
floor and the missing 503 cannot reach them; and their payment history is
the longest. WS6 waits for the 503 because it brings in balances near the
floor, where a lease can hold the money a synchronous request needs.

### 8.3 The way back

Fastest first, each narrower than the next:

1. `fastpathctl decide off WORKSPACE`: the edge stops answering for it;
   Python's front door call still leases it.
2. `fastpathctl disable WORKSPACE`: off the allow-list, held, its leases
   revoked and drained (W2).
3. `fastpathctl enabler pause`: nothing joins or leaves until resumed.
4. Add to `fastpath_pilot_exclude`, a deploy: the enabler disables those
   workspaces whole at its next run. Lowering `max_rank` also works, at
   the capped rate of §5.3.
5. `fastpathctl decide wildcard off`, or `decide-off-all`.
6. `fastpathctl disable-all`: every workspace off, every lease revoked, the
   enabler held. Undone by `fastpathctl release-all`; if the enabler is
   also paused, `enabler resume` comes first, since `release-all` refuses
   under a pause (§5.6). During a rollback below PR 4 the enabler stays
   paused until every node is on PR 4 again (§5.6), so `resume` and
   `release-all` are not available yet: the way back from a kill then is
   the older binary's `enable` for each workspace to bring back. After
   the roll-forward, `enabler resume` counts per-workspace holds only,
   never `*`: a row brought back that carries no hold of its own is not
   listed, and one that still carries a PR 4 hold (an older `enable` left
   it there before the kill stopped it) is listed, for the operator to
   resolve. A kill-stopped row that still carries a PR 4 hold and was not
   enabled again stays off through `release-all` or `release-stamped`
   until PR 4's `release` or `enable` for it. Then:
   - **After a PR 4 kill** (`*` and a kill row exist): `release-all`
     removes them, and the enabler's next run enables every kill-stopped
     row still off that is eligible and unheld. So before `release-all`,
     `disable` with PR 4, which holds, any of those that should stay off.
   - **After an older-release kill** there is no `*` and no kill row:
     resume holds the rows it left off, reason `paused`, and they come
     back only by an operator's act once resume has run: all at once with
     `fastpathctl release-stamped TS -yes`, TS being the older
     `disable-all`'s printed `at`, or one at a time with `fastpathctl
     release` or `enable`.
7. `TR_FASTPATH_PILOT_ENABLED=false`: every new authorize takes today's
   path at once.

## 9. Async admission

Today `async_settle_pilot_allows(w)` is false for `w` in
`async_settle_fastpath_excluded_workspace_ids`, the explicit fast-path set
less the overlap list (`config.py:1671-1688`), and otherwise the async
pilot set's answer. The edge policy publishes the excluded ones the async
pilot set would otherwise cover (`edge_policy_publisher.py:179-206`),
bounded by the fast-path set's cap of 32 (G15 §4.5). The leased
authorization of a workspace in both pilots is ticketed on purpose, and the
drain settles it through the lease
(`test_a_leased_authorization_in_both_pilots_settles_through_the_async_path`,
#2309).

**What breaks under "all".** With the explicit set emptied, the excluded
set computes empty, so async admission would cover fast-path workspaces
whenever the async pilot set names them, against §4.13. And with an empty
async pilot set (every workspace), the excluded set the policy must publish
is every covered workspace, unbounded.

**The rule under "all".**

- One predicate, `fastpath_covers(w)`: in all mode, `w` not on
  `fastpath_pilot_exclude`; otherwise `w` in the explicit set.
  `async_settle_fastpath_excludes(w)` is `fastpath_covers(w)` and `w` not in
  the overlap list. `async_settle_pilot_allows` reads it in place of the
  set. The rule follows coverage, not the switch, as G15 §4.5 requires: the
  enabler turning a workspace on or off never moves async admission.
- The publisher's `admission_excluded_workspaces` is the async pilot set's
  members that `async_settle_fastpath_excludes` names, so it stays bounded by
  the async pilot set's 32.
- Two validators refuse to start a process with all mode on and:
  - async admission on with an empty async pilot set, since "every
    workspace" for async and "every workspace" for the fast path cannot
    both hold and the published exclusion would be unbounded;
  - an async pilot workspace on neither `fastpath_pilot_exclude` nor the
    overlap list. The operator says which path each async pilot workspace
    takes; turning all mode on never moves one silently.
- With both validators met, `admission_excluded_workspaces` is empty and
  absent, so the async object of the policy is byte-identical to today's
  and Go's `AsyncSettle.Admits` needs no change. A workspace the enabler
  turns on is covered and not in the async pilot set, so async admission
  leaves it out, and the edge decides its async-v1 requests with Python's
  admission-off answer (G15 §4.1).
- The overlap list keeps its meaning and its cap, holds the operator's
  workspace alone, and gains no entry from this work.
- `TestTheAsyncAdmissionVectors` gains all-mode rows, generated by
  `scripts/fastpath/edge_policy_vectors.py`, so Go's answer per workspace is
  held to Python's.

## 10. What the code says that the documents do not

- **The nodes enforce tier 1, not 3.** `service.Defaults` sets
  `RequiredTier: 1` (`fastpath/internal/service/service.go:150-151`), and
  the node's unit passes no `-required-tier`
  (`infra/fastpath_node_startup.sh.tftpl:203-212`). §8 step 8 says the grant
  enforces the tier before any workspace but the operator's, and since
  2026-10-16 the pilot set has held a customer's workspace (`rollout.sh`'s
  comment on `TR_FASTPATH_PILOT_WORKSPACES`). The P1 synthetic workspace is
  at tier 1 by override (`docs/runbooks/fastpath-p1.md`), so raising the
  tier needs its override raised first.
- **An override alone does not make tier 3.** `effective_trust_tier` caps
  an override at 1 unless the owner is approved or `identity_bypass` is set
  (`trust_tiers.py:277`, `:322`), and the P1 provisioning script writes
  `identity_bypass=False` (`provision_fastpath_p1_workspace.py:152-153`).
  §4.11's "an operator's override to tier 3 counts" holds only with one of
  those.
- **No allowance or floor flag.** `Allowance` is `1 << 50` and `Floor` is 0
  by default, and `cmd/fastpath/main.go` defines neither flag, though
  `fastpath-p1.md` speaks of "the fleet's `-required-tier` and `-floor`".
  Only `loadgen` has `-floor`, for its readiness check.
- **No 503 for headroom in leases, until PR 7 is on.** §8 step 4's 503 in
  place of 402 was not in Python's authorize (no lease read there);
  `fast-admission-pilot.md` lists it as not in the pilot. PR 7 adds it to
  the typed authorize behind `fastpath_lease_headroom_503`, off in
  `rollout.sh`, so production still answers 402 until activation turns it
  on. Widening to small balances needs it on (WS6).
- **No fast-path fact in the speculation issuer.** §8 step 4's issuer fact
  (§4.13) is not in `storage_gcp_speculation_shadow.py`. Speculation
  dispatches nothing, so widening does not wait on it, but it must land
  before speculation's real permits.
- **No x402 key type.** §4.11 keeps "x402 and federated (deferred-
  settlement) keys" synchronous, but x402 is a way to buy credits
  (`services/x402_billing.py`), not a key class; only federated keys defer
  settlement (`_deferred_settlement_applies` in `gateway.py`). (c) decides
  x402-funded credits are credits.
- **Python's pilot had no federated, async-shadow or monitor-key gate.**
  The edge has `key.federated`, `workspace.federated`,
  `policy.async_settle_shadow` and `policy.monitor_key`; `pilot_reason`
  had none of them, so under the explicit list only the list itself kept
  those workspaces out. PR 5 adds them before all mode.
- **The async exclusion's bound assumes a list.** G15 §4.5 bounds
  `admission_excluded_workspaces` by "the fast-path set's own cap of 32",
  which all mode removes; §9 restores the bound through the async pilot set.
- **The stream tripwire meets #2337.** `fast-admission-streams.md` §5.1
  item 5 keeps a stream on the lease only while `reservation == estimate`,
  so that a lower stream hold drops streams out rather than cutting them.
  #2337 (open) gives streams the bounded hold, which trips it: once it
  merges, leased streams fall back to Python until the streams design is
  amended. Money is safe either way; the share the fast path carries drops.
- **The switch read grows with the allow-list.** Each node reads every
  enabled row strongly each second (`Switch: time.Second`,
  `store.EnabledWorkspaces`). At one small row per workspace that stays
  cheap, but W3's baseline was measured with the pilot's few rows; PR 6
  measures it on the emulator at ten times the dry-run's eligible count.
- Line references in older notes have moved: the pilot gate is
  `routes/internal/gateway.py:5100-5175`, the set's cap
  `config.py:1624-1635`, and the grant's refusals are in
  `fastpath/internal/store/grant.go` (`:28-37`, `rowsRefuse` at `:345-365`),
  not an `owner/grant.go`.

## 11. Pull requests

Each merges dark: off by default, a literal in `rollout.sh`, the enabler
job's script or a node attribute, changing nothing until its activation
step.

1. **Node money settings.** `-allowance` and `-floor` flags in
   `cmd/fastpath/main.go`; the startup template passes `-required-tier`,
   `-allowance` and `-floor` from instance attributes, absent attributes
   meaning today's values; `fastpathctl ready WORKSPACE -required-tier N
   -floor F` (read-only, `store.GrantReadiness`);
   `provision_fastpath_p1_workspace.py --tier 3 --identity-bypass`, refused
   for a workspace with an API key or funding other than its own grant.
   Runbook: rerun the script so the P1 synthetic workspace reaches
   effective tier 3 (§8.2), run `ready` for each pilot workspace, read
   `LeaseReserved` at the busiest hour ((b)), then re-pin (W8). Order:
   first; independent of the rest. Tests: flag parsing and bounds; the
   template's render with and without the attributes; a grant at tier 2
   refused with `RequiredTier` 3 and an override to 3 granted; the
   script's bypass refused for a workspace with a key, and the shard tier
   reaching 3 with it; the floor test of (b), over the runtime catalog's
   credit endpoints with customer prices, every tier and retirements
   applied, failing when 16,384 output tokens at the highest rate pass a
   quarter of the floor, and a case holding that a retired endpoint is not
   counted; `ready`'s exit codes.
2. **The fleet audit.** `fastpath/internal/auditor/fleet.go`, the ring key,
   the summary line, `fleet_audit_fault` and `fleet_audit_stale` with their
   alert policies and runbook rows; `fast-admission-pilot.md` retires the
   hourly script. Order: before WS0's gate is read. Tests: on the emulator
   a seeded identity violation, a duplicate winner and a booked-usage
   shortfall each raise their fault; the order of whom it audits; the rate
   limit; ownership moving with a ring change and running once; the stale
   alert's absence condition.
3. **Go reads "all" and the wildcard.** `EdgePolicy` gains optional
   `fastpath_pilot_all` (true only) and `fastpath_pilot_exclude` (with it
   only, sorted, unique, 0 to 64); `covers(w)` replaces the list in the
   keyless gate (`decide.go:438-443`) and `policyPass` (`:891-897`);
   `DecideView` keeps off rows and the wildcard with §6.1's precedence;
   `workspace.not_enabled` from the switch copy as a decision gate; the
   decide path's `decided.via`; the shadow's `distinct_keys`;
   `fastpathctl decide wildcard`, `decide-restore -at`, `decide status`'s
   effective answer and `via`, `decide delete`'s refusal and
   `-follow-wildcard`, and `decide delete '*'` defined as deleting the
   wildcard (§6.2), with the usage text and both runbooks.
   Order: before PR 5 publishes and before WS3 writes the row; every node
   pinned, with no `cache.policy` rise, before PR 5's setting moves. Tests:
   parser vectors (absent, true, `false` and `null` refused, an exclude
   list without all refused, unsorted or duplicated refused, 65 entries
   refused); the decide gate's table (own row on or off against wildcard
   on, off or absent, a stale copy); `decide status` exiting 3 under the
   wildcard and 0 under an off override; `decide delete` refusing an off
   row under an on wildcard and allowing it with the flag; `decide delete
   '*'` removing the wildcard and every rowless workspace then not
   deciding; a `*` row read
   by the old rule decides nobody; `decide-off-all` turns the wildcard off
   and `decide-restore` restores exactly the rows of its timestamp;
   `distinct_keys` not counting a key twice and bounded;
   `edge_policy_all_v1.{body,manifest}.json` goldens.
4. **Holds.** `tr_fastpath_workspace_hold` in `migrate_fastpath.sh` and the
   Go schema, guarded by `INFORMATION_SCHEMA` (W4);
   `Store.SetWorkspace(false, reason)` writing the hold for every caller,
   `fastpathctl disable` and `fastpathwatch`'s trip passing their
   reasons, and no unheld disable in Go; the fixture's `disable_unheld`
   for the enabler alone; `enable` removes the hold in its transaction;
   `release`, refused under `#pause`; `enabler resume` refused while any
   row is enabled and held, printing them; `disable-all` writes the fleet
   hold in its transaction; `release-all`; `enabler pause` (insert-only, `held_at`
   the commit timestamp); the `tr_fastpath_kill` table (schema, in
   `migrate_fastpath.sh`, the Go schema and the conformance DDL), a row
   per `disable-all` with its commit timestamp, all deleted by
   `release-all`; `enabler resume` with its backfill since the pause in
   the same transaction, holding only rows with no hold, skipping exactly
   the rows stamped with a `tr_fastpath_kill` row, and printing its window
   and counts;
   `release-stamped TS`, printing its counts and writing only with
   `-yes`; `enabler arm`, its check and insert one transaction;
   `hold-backfill`, refused once `#armed` exists (§5.6);
   `switch_statements_v1.json`, embedded by
   `SetWorkspace`; the rollback and resume rules of §5.6 in
   `docs/runbooks/fastpath-deploy.md`. Order: before PR 6; its migration
   seen in a successful control-plane deploy (`tr_fastpath_workspace_hold`
   and `tr_fastpath_kill` each `created` or `already present`) before any
   release carrying it is pinned or its `fastpathctl` or `fastpathwatch`
   used, since every stop of that release writes the hold tables in the
   switch's transaction and fails outright without them;
   `hold-backfill` run before WS1 and again at WS2's entry; every node's
   release carries it before WS2; the enabler paused, not merely set off,
   before any rollback below it. Tests: the migration test holding the
   statement to the Go schema; `disable-all`'s one transaction writing the
   fleet hold, the switch and the revocations; `enable` clearing a hold;
   `hold-backfill` holding every disabled row without one, leaving enabled
   and already-held rows alone, and a second run writing nothing; `resume`
   holding every row disabled at or after the pause, none disabled before
   it, and removing `#pause` in the same commit, with no way to remove
   `#pause` otherwise; `held_at` equal to the pause's commit timestamp,
   compared on the same clock as a `changed_at` committed just before and
   just after it; a second `pause` leaving the first `held_at`; each
   `disable-all`'s `tr_fastpath_kill` row equal to the `changed_at` of
   every row it turned off, and a row stopped before it keeping its own;
   `release-all` deleting `*` and every kill row in one commit;
   `release-all` while `#pause` exists refused, writing nothing, `*`, the
   kill rows and every switch row as they were; the
   sequence the review described: paused, an older-style unheld `disable
   W` at T1, then `disable-all`, `resume`, `release-all`, and W still held
   while the kill's rows are enabled again by the next run; the same with
   two `disable-all`s during the pause, both kills' rows back after
   `release-all` and W still held; with enabler disables seeded before the
   pause (unheld, unstamped), `unheld_disabled` counting rows under `*` as
   `under_kill` and W and the enabler's rows as `other` before the resume,
   and after it the enabler's rows still in `other` while resume's printed
   window holds zero unheld rows not under a kill, so the check is scoped
   to the window; a `disable` hold made inside the pause keeping its
   reason through the resume; rows turned off by an older-style
   `disable-all` held at resume; `release-stamped` releasing only rows
   with that exact `changed_at` and the reason `paused`, leaving a
   `backfill` or operator hold with the same stamp and a `paused` hold
   with another stamp alone, and writing nothing without `-yes`; `arm`
   refused while any disabled
   row lacks a hold of its own, a `disable` committed during `arm` either
   seen or held, and
   `hold-backfill` refused once `#armed` exists; `release` and `enable`
   refusing `*`, `#pause` and `#armed`; a `fastpathwatch` trip writing a
   hold with reason `watch`, and the workspace staying off through an
   enabler run after the watch has exited; `release` under `#pause`
   refused, writing nothing; `resume` refused while a row is enabled and
   held, listing it, and accepted once PR 4's `enable` or `disable` has
   resolved it; the rollback sequence of §8.3: pause, rollback, a PR 4
   `disable-all` stopping A, B and C, an older-style `enable A`, roll
   forward, `resume` listing nothing and holding nothing (A has no hold, B
   and C are under the kill), PR 4 `disable C`, `release-all`, and after
   the enabler's next run A and B enabled, C off and held with reason
   `operator`; `SetWorkspace` executing the fixture's statements.
5. **Python's "all".** Settings `fastpath_pilot_all` (false) and
   `fastpath_pilot_exclude` (at most 64), read from
   `scripts/deploy/fastpath_lists.sh`; validators: all mode needs the typed
   Spanner store and an empty explicit list, and §9's two; `fastpath_covers`
   in `pilot_reason`, which also gains `federated`, `async_shadow` and
   `monitor_key` to match the edge; a per-process copy of the enabled rows,
   read every 5 s, only in all mode and only in a process whose serving
   region is in `fastpath_pilot_regions` (stale past 15 s it covers nobody,
   so no front door call is made); the pass-over log only for enabled
   workspaces; `async_settle_fastpath_excludes` in
   `async_settle_pilot_allows` and the publisher; the optional policy
   members. Order: after PR 3 is pinned; `TR_FASTPATH_PILOT_ALL=false`.
   Tests: each validator; `pilot_reason` in all mode with the copy fresh,
   stale and missing the workspace, and the three new reasons; the async
   admission table in all mode, the overlap workspace in both pilots still
   ticketed and an enabled workspace refused; the publisher's body
   byte-identical with all mode off; a dormancy test that no Spanner read
   of the copy happens with all mode off or outside the pilot regions;
   regenerated `edge_policy_admission_v1.json` with all-mode rows that PR
   3's `TestTheAsyncAdmissionVectors` passes; the test holding `rollout.sh`
   and the job's script to `fastpath_lists.sh`.
   **Built** (dark): `Settings.fastpath_pilot_all`, `fastpath_pilot_exclude`
   and `validate_fastpath_pilot_all`; `fastpath_covers`,
   `async_settle_fastpath_excludes` and `EnabledWorkspaceCopy`
   (`services/fastpath_frontdoor.py`, built and started by
   `gateway._fastpath_switch_copy`); `scripts/deploy/fastpath_lists.sh`
   with `TR_FASTPATH_PILOT_EXCLUDE` empty, held by
   `tests/test_fastpath_lists.py`, which PR 6 extends to the job's script;
   tests in `tests/test_fastpath_pilot_all.py`. Python's reasons are
   `federated`, `async_shadow` and `monitor_key`. In all mode a workspace
   the copy does not enable, or a stale copy, is `workspace`, the
   explicit list's reason, so WS2's gate reads one reason; its pass-over
   line is written only for a covered workspace the copy's last rows
   enable, however old. The second validator of §9 applies whether
   async admission is on or off, so turning admission on later moves no
   workspace either. The authorize vectors record Python's answer
   unleased for `key.federated`, `workspace.federated`,
   `policy.async_settle_shadow` and `policy.monitor_key`.
6. **The enabler.** `src/trusted_router/fastpath_enabler.py`, its CLI,
   `scripts/deploy/fastpath_enabler_job.sh` (job, schedule, its own
   environment, sourcing `fastpath_lists.sh`) and its step in `deploy.yml`;
   `TR_FASTPATH_ENABLER_MODE=off`, `TR_FASTPATH_ENABLER_MAX_RANK=0`,
   `TR_FASTPATH_ENABLER_INCLUDE` set to today's pilot set, the thresholds of
   (b); `fastpath_enabler_mass_disable` and `fastpath_enabler_trust_stale`
   alerts. Order: after PRs 4 and 5. Tests:
   an eligibility table with one fact flipped per row of (c); the rank and
   the include list; the rank's margin (a workspace at `max_rank + 1`
   kept and counted toward nothing, one past the margin disabled and
   counted in the rank pace, not the fact-change guard); cause precedence
   (a mass latch disabled uncapped, counted as latched, the guard not
   tripped and no alert; a workspace both latched and below the balance
   counted once, as latched); a
   bypassed tier 3 refused off the include list and enabled on it, and
   counted; the bypass read from `tr_trust_override` both in the snapshot
   and in the enable's own transaction (an override row changed to a
   bypass between them refuses the enable); no margin at `max_rank` 0; the
   run aborting on a failed chunk with no write; the guard (more than the
   cap of fact-change disables: none written, the alert raised), the rank
   pacing (at most the cap a run, lowest rank first, no alert), and an
   exclude entry applied whole; on the emulator, a `disable` and a
   `disable-all` committed between the run's read and its enable leave the
   workspace off, and a `pause` committed between the run's read and a
   disable stops that disable; nothing written without `#armed`; no code
   path of the enabler writing the hold table, its disable going through
   `disable_unheld` and leaving no hold row; `unheld_disabled` counting
   every unheld disabled row whatever its rank; a stale or absent watermark
   freezing that workspace's enable and no disable, an override still
   enabled; dry-run writing nothing; the credit recheck refusing an enable
   whose rows changed; the writes executing `switch_statements_v1.json`;
   the switch read's latency on the emulator at ten times the eligible
   count.
   **Built** (dark): `src/trusted_router/fastpath_enabler.py` (`python -m
   trusted_router.fastpath_enabler`), `scripts/deploy/fastpath_enabler_job.sh`
   and its step in `.github/workflows/deploy-production.yml` (the workflow
   that runs the control plane's release steps; `deploy.yml` builds and
   calls it), after the trust jobs' step and behind no opt-in;
   `infra/fastpath_enabler_alerts.tf` and the runbook rows in
   `docs/runbooks/fastpath-alerts.md`. Literals: `TR_FASTPATH_ENABLER_MODE=off`,
   `TR_FASTPATH_ENABLER_MAX_RANK=0`, the balances of (b), and
   `TR_FASTPATH_ENABLER_INCLUDE="${TR_FASTPATH_PILOT_WORKSPACES}"` in
   `fastpath_lists.sh`, where the explicit pilot set moved from `rollout.sh`
   so the include list names it by reference. Tests:
   `tests/test_fastpath_enabler.py` (the fake), `tests/test_fastpath_enabler_deploy.py`
   (the job's script under the deploy harness, the workflow step, the alerts'
   filters), `tests/conformance/test_fastpath_enabler_native.py` (the
   emulator: the arm, the interleaved `disable`, `disable-all` and `pause`,
   the in-transaction bypass, and the switch read: 16,000 enabled rows, ten
   times the tier job's fleet of 1,596, read in about 40 ms on the emulator,
   held under the copy's 2 s read timeout).
   Where it reads the design narrowly, decided here:
   - The fixture travels in the package as
     `src/trusted_router/data/fastpath_switch_statements_v1.json`, since the
     image holds `src` alone; a test holds it to the Go copy byte for byte.
   - The enabler's enable is the fixture's `enable` less its hold removal
     (`holds_removed`), which deletes nothing for a workspace the same
     transaction found unheld, so no enabler code path names the hold table
     in a write; loading refuses a fixture under which either of its writes
     would.
   - The synthetic monitor's and the Stage D probe's keys reach the job as
     their control-plane secrets under names outside the `TR_` prefix
     (`FASTPATH_ENABLER_*_KEY`), since the control surface's settings may
     not hold them; the job keeps only their lookup hashes and resolves each
     workspace by complete key. The async settle shadow's probe key stays
     confined to its own job, as the repository's tripwire requires; its
     workspace is left out only by the exclude list, which (c) has carry
     every probe's workspace. Both that list and
     `async_settle_shadow_workspaces` were empty at this PR, so activation
     adds the probes' workspaces to `TR_FASTPATH_PILOT_EXCLUDE` (done at
     WS2, with all mode); until then only the tier requirement and the
     enabler's own resolution of the two probe keys kept a probe out.
   - The switch read's latency was measured with Python's copy of it
     (`spanner_enabled_workspaces`), the node's `EnabledWorkspaces` SQL less
     its comment, since the dry-run's eligible count is not yet known: at
     16,000 rows it bounds ten times every workspace the tier job judges.
   - `async_shadow` is applied whole, as the exclude list is: both are
     operator lists, not facts. The guard covers tier, balance, incomplete
     credit rows (shards not `0..n-1`, which Go's grant refuses), bypass,
     deletion (or no workspace entity), federated home and the probes.
   - The rank counts include-list workspaces in its order, so `max_rank` N
     is the N largest; the include list is then exempt from leaving.
   - `fastpath_enabler_trust_stale` reads the run's eligible set, those on
     and staying and those joining, so a stalled reconciler shows when
     nothing new would join.
   - An apply run stops writing at the first transaction that finds the
     fleet hold, the pause or no arm; the report says which (`stopped`).
   - "Unbounded" `max_rank` (WS6) is a number above the fleet's size.
7. **The 503 for headroom in leases** (§4.4, §8 step 4): the reserve and the
   insufficient-credit precheck answer 503 when the shortfall sits in
   leases. Money code: a differential test against today's authorize and
   the conformance suite. Order: before WS6.
   **Built** (dark): `fastpath_lease_headroom_503`, the literal
   `TR_FASTPATH_LEASE_HEADROOM_503=false` in `rollout.sh`. The route passes
   it to `SpannerStore.authorize_gateway_typed`; off, nothing new is read
   and every answer is today's. On, the rule is the design's: a hold the
   credit rows cannot cover (no shard, and not their sum, which a
   rebalance would gather), for a workspace not marked in debt, whose live
   leases hold a positive amount that with the rows' signed headroom
   covers it, is outcome `lease_headroom`, answered 503 with `Retry-After:
   1` and type `service_unavailable`; anything else keeps today's answer.
   What the leases hold is section 4.7's term, each open or draining
   lease's donors' `allocation - consumed`, read by
   `credit_exhaustion_precheck` (`lease_aware`) in the snapshot that reads
   the credit rows, through `tr_lease_by_state`, declared on `(state)`
   with the primary key `(workspace_id, lease_id)` appended to its key, so
   two seeks bounded to the workspace's live leases, and their donors by
   key; it is registered in the Spanner SQL manifest. Where it reads:
   - The reserve: after the authorize transaction's final 402, one more
     snapshot on that path alone, under a strict key's budget deadline
     when the transaction ran under one. A lease-held verdict answers the
     503; any other verdict, a failed read included, keeps the 402. The
     workspace is remembered either way.
   - The precheck (#1461), for a workspace the process remembers: every
     hit re-reads the rows and the leases on the lock-free snapshot, with
     no transaction, and answers the 503 while the leases hold the
     shortfall, today's 402 once the balance is exhausted (a lease that
     spent it), and passes the request on, forgetting the workspace, once
     headroom is back (a lease that returned its allocation). So a
     lease-held workspace costs a snapshot per request, as an exhausted one
     does today, and never a locking transaction. A lease read that fails
     answers today's verdict for the rows (the 402), still lock-free.
   The 503 is never the enclave's cached `insufficient_credits` 402
   (`billing_backoff.go` remembers only an authorize 402 of that type), and
   the enclave's authorize retry already retries a 503 with `Retry-After`
   as its least wait, so neither the enclave nor the Go edge changes: the
   edge proxies Python's bytes and headers, evicts its caches on a 402 but
   not on this 503, and its decide shadow counts it `python_error.503`, not
   a refusal. No free-credit exhaustion is recorded for it.
   Tests: `tests/test_fastpath_lease_headroom_503.py` (the differential
   over a matrix of balances, synchronous holds and leases on one and two
   shards, debt-marked rows included: off, the answers, rows and memory are
   those of the same rows with no lease rows and the lease statement never
   runs, and the off answers are pinned to today's rules written down
   independently of the code; on, they differ only where live leases hold
   the shortfall, 402 to 503, never the reverse and never to an admission;
   the precheck's memory, lock-free on every hit, through a lease's return
   and a lease's spending; the route's 503, `Retry-After` and type against
   today's 402) and
   `tests/conformance/test_fastpath_lease_headroom_native.py` (the
   statement on the emulator: live states, closed leases, neighbouring
   workspaces, the debt mark).
8. **Activation, WS0 to WS6.** One `rollout.sh`, `fastpath_lists.sh`, job
   setting or node-attribute change per step of §8.2, each opened with the
   previous step's gate read into its description.
