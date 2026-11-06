# Widening the fast path to every eligible workspace

Status: decided, nothing built. This is the short note the production
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
  changes what it stops. A kill is never undone by the enabler: every
  enable is a transaction conditional on no hold (§5.6).
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
| `tr_fastpath_workspace` (W1) | **the enabler (new)**; `fastpathctl enable`, `disable`, `disable-all` | who may be granted | the owner, the front door, `Store.Grant`; **the edge and Python, as a copy (new)** |
| `tr_fastpath_workspace_hold` **(new)** | `fastpathctl disable` (by default), `disable-all`, `enabler pause` | whom the enabler must not enable, and whether it may write at all | the enabler, in each enable's transaction |
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
tier by operator override counts as fresh. The freeze stops enables only:
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
  bill to hold a tier-3 workspace can route to. That ratio is about sixty
  for a request with no output limit (§4.13: 32,000 tokens sent against a
  512-token hold), so $1,000 permits about $60,000 of charges against
  $1,000 of holds in the worst case, per workspace, each overrun reserved by
  its shortfall write and stopped by the debt mark (§4.7). That is the
  bound accepted here, for a workspace with a 30-day payment history, $50
  paid, an approved owner and no dispute. Separately, it must not throttle
  honest traffic: a lease's allocation is a top-up of at most `TopUps.Max`,
  $10 (`service.go:156`), sized to about a minute of spend (`Horizon`), and
  a shard keeps several while holds are open, so honest exposure is about a
  minute of a workspace's spend. PR 1's precondition reads each pilot
  workspace's `LeaseReserved` with `fastpathctl status` (by its keys) at
  its busiest hour: if any is above a quarter of $1,000, the allowance is
  set to four times the highest instead, and this note is amended with the
  new exposure bound, not the figure read. `1 << 50`, today's value,
  bounds nothing.
- **$20 floor, and what it does not do.** The floor is the headroom a grant
  leaves for the same workspace's synchronous requests (streams the enclave
  does not heartbeat, service tiers, capped keys). No floor covers every
  hold: a hold prices the prompt at its estimate as well as the output
  (`authorize_candidates.py:483-485`), and the prompt is bounded only by
  the context window. Today a stream's hold is its full estimate, output
  budget unbounded by `HOLD_OUTPUT_TOKENS`; #2337 (open) gives streams the
  same bounded hold as any catalog request, which makes stream holds
  boundable by output but changes nothing about the prompt. $20 is twice
  the output part of a 16,384-token hold at the live catalog's highest
  output rate (600 $/M in `src/trusted_router/data/provider_models/`, about
  $9.83), so an ordinary request's hold fits. PR 1's test reads the live
  catalog, not the routing snapshot fixture, and fails when that output
  part passes half the floor, so a price rise is reviewed rather than
  absorbed. A hold larger than the floor that meets a lease holding the
  rest gets 402 until PR 7's 503, and 503 after it; WS6 waits for PR 7 for
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

Until PR 5 lands, Python relies on the enabler and W1 alone for the
federated, async-shadow and monitor-key rows. All mode waits for PR 5, so
it never does so under all mode.

### (d) The enabler job

**Decision.** A Python Cloud Run job, `fastpath_enabler`, every 15 minutes
at minutes 12, 27, 42 and 57, five minutes after the tier job
(`scripts/deploy/trust_tier_job.sh`, `7,22,37,52`). It is deployed by its
own script with its own environment, and on every control-plane deploy
rather than behind `TR_TRUST_JOBS_DEPLOY=1`. Details in §5.

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

**Why.** The script lives in `/var/tmp`, is lost with its node, runs one
`fastpathctl` process per workspace and needs `-funded` typed by hand. None
of that survives a few thousand workspaces.

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
over) are exempt from the rank and from nothing else.

The rank uses lifetime usage because the snapshot already holds it and it
needs no analytics read; the largest spenders carry most of the traffic
(§4.11) and have the longest payment history.

### 5.3 What a run does

- **Enables** each eligible workspace not enabled, at most 500 a run, so a
  group lands over a few runs. Each enable is its own read-write
  transaction that reads the fleet hold `*`, the pause row and the
  workspace's own hold, and the workspace's credit rows, and writes the
  switch only if no hold exists and the rows still make it eligible; else
  it writes nothing and counts why. Spanner orders that transaction
  against any `disable` or `disable-all` that writes a hold, so an enable
  is either before the kill, and the kill turns it off, or after, and sees
  the hold. A per-workspace freeze ((a)) skips the enable.
- **Disables** each enabled workspace that is latched, paused, in debt or
  held, with no cap: these are the safe direction and already refused by
  the grant. For any other cause that came from a change of facts (tier
  below 3, balance under $40, a federated home, a deletion, a probe), it
  disables at most 10% of the enabled set or 20 workspaces a run, whichever
  is larger; past that it disables none for those causes and raises
  `fastpath_enabler_mass_disable`, since a sudden mass change of facts is
  more likely a reading defect than a fact. A cause that came from the
  operator, a lower `max_rank` or a new exclude entry, is applied whole.
  No freeze holds back a disable.
- **Treats an emptied allow-list as a kill.** If it finds no enabled row
  while some row changed within the last 24 hours, and there is no fleet
  hold, it writes the fleet hold itself, writes nothing else, and raises
  `fastpath_enabler_found_empty`. Its own disables are capped and never
  empty the list, so an empty list is someone else's kill, including one
  from a `fastpathctl` older than PR 4 that writes no hold.
- Writes nothing for a workspace whose state already matches, so the table
  sees writes only on change.

### 5.4 The report

Every run, dry-run or not, logs one JSON line with: eligible, enabled,
would enable and would disable by reason, excluded by each reason of (c),
held, frozen, capped, enabled rows outside the include list, and in apply
mode the commit times. The lists of workspaces to enable and disable go in
the job's own log, which only operators read.

### 5.5 How it writes

The switch is written as `SetWorkspace` writes it: the row's `enabled` and
its commit timestamp (one mutation), and on disable the workspace's open
leases revoked in the same transaction (one DML), so each takes no
renewal, expires, drains and closes, and what it admitted still settles.
The enabler's enable adds the hold and credit-row reads of §5.3 before
that write. The statements move to one fixture,
`fastpath/testdata/switch_statements_v1.json`, which Go embeds and Python
reads, so the columns, values and revocation cannot drift. The enabler does
not wait for a disabled workspace to drain: W2's `fastpathctl status` and
the fleet audit (§7) see that.

### 5.6 Holds and pausing

- `fastpathctl disable WORKSPACE` writes a hold row in the switch's
  transaction, unless given `-no-hold`. An operator who turns a workspace
  off means it to stay off.
- `fastpathctl enable WORKSPACE` removes the workspace's hold in the
  switch's transaction, so a hand enable after a holding disable is not
  reverted. The enabler then manages it like any workspace: if it is not
  eligible, the next run disables it, for that cause.
- `fastpathctl release WORKSPACE` removes a hold without enabling.
- `fastpathctl disable-all` writes the fleet hold, the row `*`, in the same
  transaction as the switch and the revocations. While it exists the
  enabler writes nothing and reports `held: fleet`. `fastpathctl
  release-all` removes it.
- `fastpathctl enabler pause` and `resume` write and remove the row
  `#pause`. While it exists the enabler reads and reports and writes
  nothing, and the allow-list stays as it is: the way to stop the enabler
  without a kill or a deploy.
- The enabler's own disables write no hold, so a workspace that refills or
  regains its tier joins again.
- `*` and `#pause` can never be workspace ids (`fastpathctl` takes ids
  beginning with a letter or digit).
- **No `fastpathctl` older than PR 4 is used once the enabler applies.**
  An older `disable` writes no hold, and the enabler would enable that
  workspace again at its next run. WS2's entry checks the release on every
  node, since `fastpathctl` reaches operators only through the release
  bucket (`infra/fastpath_node_startup.sh.tftpl:180-184`). An older
  `disable-all` is caught by §5.3's empty-list rule.

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
  `-follow-wildcard` it deletes anyway and says so. Deleting an on row is
  allowed always: under an on wildcard it changes nothing, under none it
  stops the workspace deciding.
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
  exactly once in the fleet. Each run logs a summary line; an absence of
  that line for 90 minutes raises `fleet_audit_stale`.
- **Whom.** Every workspace whose `tr_fastpath_workspace` row is enabled or
  changed within 26 hours, read from that table, never by scanning leases.
  Workspaces with an open or draining lease first, then those whose switch
  changed, then the rest in rotation.
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
- **What retires.** The hourly script, and the per-workspace
  `fastpathwatch -workspace` watches. Their spend ceilings guarded a pilot
  of named workspaces; at fleet scale a workspace's lease spend is bounded
  by its allowance and its balance, and W3's fleet ceilings (Spanner CPU,
  backlogs, pending work overdue) stay with W7's alerts.

## 8. The widening sequence

### 8.1 The bars every step reads

Read with `fastpathwatch edge -since` the step's start, the fleet audit's
summaries since then, and the enabler's reports. A step's own traffic only:
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
   figure, so a key seen by two nodes is not counted twice). The 24 hours
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
7. The enabler: no `fastpath_enabler_mass_disable`,
   `fastpath_enabler_found_empty` or `fastpath_enabler_trust_stale`, and its
   reports' counts matching the step.

Any alert, or any bar missed, is the way back (§8.3), then the cause.

### 8.2 The steps

| Step | What changes | Its own gate, beyond §8.1 |
|---|---|---|
| WS0 | Nodes re-pinned with `-required-tier 3`, `-allowance` and `-floor` (PR 1), after the P1 synthetic workspace is put at tier 3 by override and every pilot workspace passes `fastpathctl ready` at those values | 2,000 leased settles on the current set since the re-pin; zero `RefusedTier` |
| WS1 | The enabler in `dry-run` | four consecutive runs whose reports were read: every current pilot workspace eligible, every exclusion of (c) counted where it should be |
| WS2 | Entry: every node's release, and so every `fastpathctl` in use, carries PR 4. The enabler in `apply` with `max_rank` 0 (it keeps the include list and enables nobody new); then, once a run's report shows the enabled set within the include list and nothing to disable, `TR_FASTPATH_PILOT_ALL=true` with the explicit list emptied | 2,000 leased settles under all mode; no `workspace` pass from Python for an enabled workspace (its switch copy works) |
| WS3 | The wildcard row on for `gcp`, `us-central1`, then the include list's own on rows deleted, so they decide under the wildcard | 2,000 authorizes with `decided.via` `wildcard`; no `edge_decide_unavailable_sustained` |
| WS4 | `max_rank` 10: the ten largest tier-3 workspaces | 10,000 leased settles from the step's workspaces, and at least five of them granted a lease |
| WS5 | `max_rank` 100 | 20,000 leased settles from the step's workspaces, and at least 50 granted |
| WS6 | PR 7 (the 503) deployed; `max_rank` unbounded: every tier-3 workspace | 20,000 leased settles from the step's workspaces, and at least 80% of those that sent an eligible request granted |

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
4. Lower `max_rank` or add to `fastpath_pilot_exclude`, a deploy: the
   enabler disables the step's workspaces at its next run.
5. `fastpathctl decide wildcard off`, or `decide-off-all`.
6. `fastpathctl disable-all`: every workspace off, every lease revoked, the
   enabler held.
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
- **No allowance or floor flag.** `Allowance` is `1 << 50` and `Floor` is 0
  by default, and `cmd/fastpath/main.go` defines neither flag, though
  `fastpath-p1.md` speaks of "the fleet's `-required-tier` and `-floor`".
  Only `loadgen` has `-floor`, for its readiness check.
- **No 503 for headroom in leases.** §8 step 4's 503 in place of 402 is not
  in Python's authorize (no lease read there); `fast-admission-pilot.md`
  lists it as not in the pilot. Widening to small balances needs it (WS6).
- **No fast-path fact in the speculation issuer.** §8 step 4's issuer fact
  (§4.13) is not in `storage_gcp_speculation_shadow.py`. Speculation
  dispatches nothing, so widening does not wait on it, but it must land
  before speculation's real permits.
- **No x402 key type.** §4.11 keeps "x402 and federated (deferred-
  settlement) keys" synchronous, but x402 is a way to buy credits
  (`services/x402_billing.py`), not a key class; only federated keys defer
  settlement (`_deferred_settlement_applies` in `gateway.py`). (c) decides
  x402-funded credits are credits.
- **Python's pilot has no federated, async-shadow or monitor-key gate.**
  The edge has `key.federated`, `workspace.federated`,
  `policy.async_settle_shadow` and `policy.monitor_key`; `pilot_reason`
  has none of them, so under the explicit list only the list itself keeps
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
   -floor F` (read-only, `store.GrantReadiness`). Runbook: raise the P1
   synthetic workspace's override to 3, run `ready` for each pilot
   workspace, read `LeaseReserved` at the busiest hour ((b)), then re-pin
   (W8). Order: first; independent of the rest. Tests: flag parsing and
   bounds; the template's render with and without the attributes; a grant
   at tier 2 refused with `RequiredTier` 3 and an override to 3 granted;
   the floor against the live catalog (`data/provider_models`): failing
   when the highest output rate times 16,384 passes half of it; `ready`'s
   exit codes.
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
   `-follow-wildcard` (§6.2), with the usage text and both runbooks.
   Order: before PR 5 publishes and before WS3 writes the row; every node
   pinned, with no `cache.policy` rise, before PR 5's setting moves. Tests:
   parser vectors (absent, true, `false` and `null` refused, an exclude
   list without all refused, unsorted or duplicated refused, 65 entries
   refused); the decide gate's table (own row on or off against wildcard
   on, off or absent, a stale copy); `decide status` exiting 3 under the
   wildcard and 0 under an off override; `decide delete` refusing an off
   row under an on wildcard and allowing it with the flag; a `*` row read
   by the old rule decides nobody; `decide-off-all` turns the wildcard off
   and `decide-restore` restores exactly the rows of its timestamp;
   `distinct_keys` not counting a key twice and bounded;
   `edge_policy_all_v1.{body,manifest}.json` goldens.
4. **Holds.** `tr_fastpath_workspace_hold` in `migrate_fastpath.sh` and the
   Go schema, guarded by `INFORMATION_SCHEMA` (W4); `fastpathctl disable`
   writes a hold unless `-no-hold`; `enable` removes the hold in its
   transaction; `release`; `disable-all` writes the fleet hold in its
   transaction; `release-all`; `enabler pause` and `resume`;
   `switch_statements_v1.json`, embedded by `SetWorkspace`. Order: before
   PR 6, and every node's release carries it before WS2. Tests: the
   migration test holding the statement to the Go schema; `disable-all`'s
   one transaction writing the fleet hold, the switch and the revocations;
   `enable` clearing a hold; `SetWorkspace` executing the fixture's
   statements.
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
6. **The enabler.** `src/trusted_router/fastpath_enabler.py`, its CLI,
   `scripts/deploy/fastpath_enabler_job.sh` (job, schedule, its own
   environment, sourcing `fastpath_lists.sh`) and its step in `deploy.yml`;
   `TR_FASTPATH_ENABLER_MODE=off`, `TR_FASTPATH_ENABLER_MAX_RANK=0`,
   `TR_FASTPATH_ENABLER_INCLUDE` set to today's pilot set, the thresholds of
   (b); `fastpath_enabler_mass_disable`, `fastpath_enabler_found_empty` and
   `fastpath_enabler_trust_stale` alerts. Order: after PRs 4 and 5. Tests:
   an eligibility table with one fact flipped per row of (c); the rank and
   the include list; the run aborting on a failed chunk with no write; the
   caps, and the operator's causes applied whole; on the emulator, a
   `disable` and a `disable-all` committed between the run's read and its
   enable leave the workspace off; the pause row stopping every write; an
   emptied allow-list writing the fleet hold; a stale or absent watermark
   freezing that workspace's enable and no disable, an override still
   enabled; dry-run writing nothing; the credit recheck refusing an enable
   whose rows changed; the writes executing `switch_statements_v1.json`;
   the switch read's latency on the emulator at ten times the eligible
   count.
7. **The 503 for headroom in leases** (§4.4, §8 step 4): the reserve and the
   insufficient-credit precheck answer 503 when the shortfall sits in
   leases. Money code: a differential test against today's authorize and
   the conformance suite. Order: before WS6.
8. **Activation, WS0 to WS6.** One `rollout.sh`, `fastpath_lists.sh`, job
   setting or node-attribute change per step of §8.2, each opened with the
   previous step's gate read into its description.
