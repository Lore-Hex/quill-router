# Fewer billing-path 503s, safely (2026-11)

Status: option A implemented in shadow mode (`-starved-leases log`, the
fast path release default); enforcement (`close`) is an operator switch, not
turned on. Everything else here is a proposal.

Scope: the control plane's gateway billing path, `/internal/gateway/authorize`,
`/settle`, `/refund` and `/heartbeat` on the `trusted-router` Cloud Run
service, as called by the attested gateway, and the fast path owner that
admits under leases.

## 1. What the logs say

Fourteen days of Cloud Run request logs and the app's own marker lines
(2026-11-10 to 2026-11-24), joined with the gateway's `enclave.request_end`
lines for what the customer received. Logs only: no Spanner, no ClickHouse,
no gateway calls. Figures are rounded; no workspace is named.

### 1.1 Control-plane 5xx by endpoint and cause

About 800 gateway billing-path 5xx, all 503, nearly all in us-central1:

| Endpoint | Share | Causes (from the marker line each 503 writes) |
|---|---:|---|
| authorize | ~47% | **lease headroom ~97%** (`billing.lease_headroom_unavailable`); Spanner deadline ~2%; store unavailable, rebalance busy, strict budget busy: one or two each |
| settle | ~46% | **async `transient_store_error` ~95%**; `finish_unconfirmed` ~3.5%; deadline <1% |
| heartbeat | ~6% | fast path owner unreachable ~75%; deadline ~25% |
| other | <1% | key lookup, video claim: deadline |

Time shape:

- **Settle**: every `transient_store_error` but one falls in one twelve-hour
  window (2026-11-21 20:00 to 11-22 08:00 UTC), the hourly hot-workspace
  burst before the lock-hint fix (#2855) and the drain worker running its
  SQL. In the forty hours since, one settle 503 of that kind.
- **Authorize**: four episodes in three workspaces, each minutes long, each
  one workspace. The largest (2026-11-23, about fifteen minutes) is two
  thirds of the fortnight's authorize 503s. None before 2026-11-21: the fast
  path floor went from $20 to $2 on 2026-11-20, which made small balances
  leaseable.

### 1.2 What customers saw

The gateway retries an authorize 503 three times within 28 s, the control
plane's `Retry-After: 1` as the least wait
(`enclave-go/internal/trustedrouter/authorization_retry.go`), then relays
the status. A settle failure never changes the response: the gateway logs
`settle_outcome="deferred"` or `"failed"` and its retry queue, the durable
settle outbox and the reaper finish the money.

- **Authorize, customer-visible: about 120 requests** in the fortnight, every
  one with `authorize_attempts=3`. They match the lease-headroom 503s three to
  one: **no lease-headroom 503 was ever rescued by a retry.** The marker's
  premise, "retryable", does not hold in practice.
- **Settle, customer-visible: none.** Every response with a settle 503 was a
  200. The cost is latency to the ledger, the retry queue's load and pages,
  not the customer's answer.
- **Heartbeat**: retried within the stream's budget; a stream stops only
  when that is exhausted. Small.

### 1.3 Why lease headroom does not clear

A lease takes an allocation from the workspace's credit. It gives the unused
part back only when it stops admitting: idle for ten minutes, or an hour old
(`fastpath/internal/owner/shard.go`, `TopUps.over`). In the 2026-11-23
episode:

1. the fast path answered `busy` for nearly every request from about 16:30:
   the lease had less free room than one request's hold (about $1.13, a large
   prompt with the 16,384-token output reserve), and the store refused a
   top-up because the headroom outside leases was under the floor plus the
   amount asked (`store.RefusedFloor`);
2. Python's synchronous path took each request, rebalancing credit shards,
   until about 17:19, when the headroom outside leases fell under one hold;
   from then each hold was answered the lease-headroom 503;
3. an admission still fitted every few minutes (a hold under the lease ended
   and freed room), which restarted the lease's idle clock, so it never went
   idle;
4. at about 17:35, an hour after the lease was taken, it closed, its room
   came back, and the workspace's requests succeeded again.

The money was there the whole time, split between a lease too small for the
request and a headroom too small for it.

## 2. Options, ranked by customer impact per unit of risk

Each option: expected reduction (from section 1), money safety, failure
modes, rollout.

### A. Close a starved lease (implemented, shadow)

When a request finds no room under any lease of its shard that still admits,
and the store's last answer to that shard's top-up, within a minute, was a
refusal for the floor or the allowance, the owner closes those leases
(`fastpath/internal/owner/starved.go`). Their next checkpoint (every
renewal round, 5 s in production) returns their free room, the auditor
credits it, and the next request is admitted, by a new lease sized for it
or by the synchronous path.

- **Reduction**: the lease-headroom class, ~97% of authorize 503s and
  essentially all customer-visible billing 503s, from minutes per episode to
  seconds (one renewal round plus the auditor's commit).
- **Money safety**: `Lease.Close` is the transition idleness, age, a pause
  and a retirement already take; only its moment changes. A closing lease
  admits nothing, serves every hold it has (heartbeats, settles, refunds,
  reaps unchanged), and returns only `Free()`: allocation less consumed,
  held and buffered. Nothing a hold can still charge is returned, so there is
  no lost charge; no record kind or field is new, so the auditor, the drain
  log and the terminal order are untouched, and there is no double charge.
  In `proofs/LeaseLifecycle.tla` the owner's stop (`OwnerStop`) is already
  unguarded, enabled at any time, so this trigger adds no behaviour the
  model lacks; `CreditDebt` models returns. No spec changes.
- **Failure modes**: (1) churn: a closed lease is followed by a new grant
  sized for the request, which may later starve and close in turn; bounded by
  the top-up cooldown (one ask per shard per second) since a close needs a
  fresh refusal, and a closed lease answers `ErrClosing`, not `ErrNoRoom`, so
  it is never counted twice. (2) traffic moves to the synchronous path while
  the returned room settles: that is today's path for every unleased
  workspace. (3) a refusal that no longer stands: bounded to a minute, and
  any other answer, a grant included, forgets it.
- **Rollout**: the release default is `log`: the owner writes `fastpath:
  owner starved shard ... mode=log ... admissions=K` at most once a minute
  per shard and closes nothing. Read it beside
  `billing.lease_headroom_unavailable` for a week (section 4), then set
  `fastpath_starved_leases = "close"` (runbook "Starved leases"). Running
  nodes keep the startup script they were created with, so a node takes the
  attribute only once replaced by one created after this change; until then
  it runs the release default, `log`.

### B. Per-workspace async admission

Today one workspace's drain backlog ages the fleet-wide p95 past 5 s
(`drain_unhealthy`) and closes async settle admission for everyone, sending
herds of snapshot-sync settles onto hot rows. Admission keyed by workspace
(the hot one goes synchronous or waits; others stay async) would have kept
the 11-21/22 window's 503s to the hot workspace.

- **Reduction**: of the settle class (~46% of 503s, none customer-visible),
  most of the burst's spillover; on the current evidence (one settle 503 in
  forty hours after #2855) small, but it removes the failure's blast radius.
- **Money safety**: admission chooses only async-or-sync, both proven paths;
  the async ticket, its intent row and the drain are unchanged. The proof
  oracle covers both outcomes.
- **Failure modes**: a per-workspace health signal needs per-workspace age,
  which the drain's health read does not keep today; a wrong signal sends a
  workspace synchronous, i.e. today's fallback.
- **Rollout**: compute the per-workspace verdict beside the fleet one and log
  disagreements (`async_settle_projection_admission` already has a
  `workspace=` field), then gate.

### C. Fast-path admission for capped keys (E2 key windows)

`docs/design/fast-admission-e2-key-windows.md`: serve keys with spend caps
on the fast path, so the hottest workspace's authorize and settle stop
writing its single credit row and key row per request.

- **Reduction**: the hot-row class at its root (the 11-21/22 settle storm,
  rebalance busy, headroom raced); today a small and non-customer-visible
  share. It is the scale fix, not the 503 fix.
- **Money safety**: the key-cap fence (`proofs/KeyCapFence.tla`) is the
  hard part and is specified; large change.
- **Rollout**: as that design's plan. Note it would make A matter more: more
  workspaces' money in leases.

### D. Automatic credit and key row sharding for hot workspaces

Spread a hot workspace's credit row and key row over shards.

- **Reduction**: hot-row aborts; but shards fragment credit, the same split
  that produces rebalance busy and headroom raced 503s, and lease headroom is
  a split of the same kind. Not a 503 reduction on this evidence.
- **Operator action**: consolidating pre-October 16-shard workspaces stays
  `scripts/shard_workspace.py` run by the operator; automatic resharding
  would pause billing per workspace and needs its own design.

### E. 429 with Retry-After for self-contention

Answer authorize contention a workspace causes itself (strict budget busy,
rebalance busy, conflict after escrow) 429 instead of 503.

- **Reduction**: zero 503s in practice (two such 503s in fourteen days); and
  for lease headroom, a 429 fails the customer request just the same: the
  status changes, the outcome does not. Relabelling, not reduction. Worth it
  only if paging on self-inflicted load becomes noisy.

### F. Batch the drain's hot-row intents per workspace

Apply one workspace's pending intents in one commit.

- **Reduction**: drain commits and lock wait on hot rows during bursts; the
  burst already drains with zero aborts since #2855. Margin.
- **Money safety**: each intent's frozen inputs and idempotent apply must be
  kept per intent inside the batch; a batch that aborts must not mark any
  intent done. Needs a proof-oracle extension.

### G. The gateway retry hold, so #2856 (5 s sync budget) is safe

#2856 relies on a gateway hold that does not apply: the gateway reports its
own deadline as `network`, not as a retryable control-plane answer. The fix
is in quill-cloud-proxy (classify the enclave's own deadline on a settle as
retryable under the hold, and prove the hold covers the 5 s budget). Settle
503s are not customer-visible and are now rare, so this is low priority;
#2856 stays held.

### Ranking

1. **A**: nearly all customer-visible billing 503s, existing transition,
   shadow first.
2. **B**: blast radius of the next hot workspace; moderate change.
3. **C**: the scale fix; large.
4. **G, F**: margin on a class that is no longer customer-visible.
5. **D, E**: not reductions on this evidence.

## 3. What A changes, exactly

- `fastpath/internal/owner/starved.go`: `Starved` (`off`, `log`, `close`),
  `Owner.starved`, `Owner.noteGrant`.
- `shard.go`: `Admit` collects the leases that answered `ErrNoRoom` and calls
  `starved` before its own top-up ask; `grant` records the store's answer.
- `owner.Config.StarvedLeases` and `Logf`; the zero value is off, so every
  existing test and caller is unchanged.
- `service.Defaults()`: `StarvedLog`. Node flag `-starved-leases`; startup
  attribute `fastpath-starved-leases`; Terraform local
  `fastpath_starved_leases`, empty (the release default holds).

Python is not changed: the lease-headroom 503 stays as it is, and goes away
because the money comes back.

## 4. Evidence to read before `close`

After the release is pinned and each node runs it:

1. Starved lines: `fastpath: owner starved shard` in the nodes' logs.
2. For each `billing.lease_headroom_unavailable` episode (control plane), a
   starved line for the same workspace within the minute before. An episode
   with none means a cause A does not address (for example a hold larger than
   the owner's largest top-up, $10, which no lease can take).
3. Starved lines outside any episode: how often, and how large `free` is.
   Frequent lines with no 503s mean `close` would churn leases for workspaces
   the synchronous path is serving anyway; acceptable, but read it.
4. After `close`: lease-headroom 503 episodes should last seconds, the gateway
   lines `attribution="authorization" status=503 authorize_attempts=3` should
   fall to near zero, and the auditor should show checkpoints with returns
   for the closed leases.

## 5. Not done here

- Holds above the owner's largest top-up never fit a lease; such a workspace
  is always on the synchronous path and its leases' room is parked. A could
  be extended to close when the need exceeds `TopUps.Max`, after its shadow
  data is read.
- The lease-headroom 503's `Retry-After: 1` is optimistic; with A on it is
  about right.
- B, C, F and G as above.
