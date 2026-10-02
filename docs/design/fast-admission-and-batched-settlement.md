# Fast admission and batched settlement

Status: **proposed, v8, 2026-10-02. Nothing built.** v8 changes direction:
Joseph chose regional leases (§2). Codex and Fable reviewed v1-v7 (§11), and
v8 keeps the parts of their findings that still apply.

This is the plan for reaching 100T tokens a month without spending the routing
margin on the billing database, and for taking the control plane out of request
latency.

## 1. Why

**The volume.** September was about 20B tokens. At 3 to 10 times a month,
100T arrives between January and May 2027.

| At 100T tokens a month | Value |
|---|---|
| Generations (about 1,150 tokens each) | about 87B a month, 33,000 a second on average |
| Revenue at about $1 per million tokens | about $100M a month |
| Routing margin at 5% | about $5M a month, about $0.0000575 per generation |
| Spanner at the 2026-10-01 cost per generation ($0.00009) | about $7.8M a month |

**The per-request path today.** The attested gateway (`quill-cloud-proxy`, Go)
calls the Python control plane synchronously. Every cloud's gateway bills
through `https://trustedrouter.com` alone. Each call is one or more multi-region
Spanner transactions:

- `POST /internal/gateway/authorize`: one atomic transaction
  (`authorize_atomic`). It claims the idempotency scope through a unique index,
  holds credit and the key limit, and writes the reservation and authorization
  rows.
- Stage D heartbeats while a stream runs. Every workspace is in the cohort in
  production. The gateway sends one before the first byte, then one every 10 s
  or 256 output tokens. Each records the delivered usage and renews the
  reservation. Nothing is charged until settle, or until the reaper books the
  last snapshot of a request whose gateway never settled.
- `POST /internal/gateway/settle`: one commit on the success path since #1465.
  It books the actual cost, releases the holds, pays app owners, and writes the
  generation record and analytics intent. When that commit is declined, a
  durable intent is written first.

| Measured | Value |
|---|---|
| Spanner commits per generation, 2026-10-01 | 4.3, before #1464 and #1465 removed about two |
| Authorize p50, us-central1 | 0.155 s |
| Authorize p50, europe-west4 | about 1.6 s (cross-Atlantic Spanner, structural) |
| Settle p90 | 3.1 s (four serial commits; #1465 made it one on the success path) |
| Settles that overrun their estimate | 12.2%; $45.57 a day; 96% of overrun dollars in 0.7% of settles |

## 2. Decisions

**Made by Joseph, 2026-10-02:**

- **Regional leases.** Each admission node holds a reserved slice of a
  workspace's budget, admits in memory, and settles in batches. This supersedes
  the earlier rule against regional leases for general users, for this design.
  The target is under about 10 ms of request overhead.
- A compiled admission and settle service, not Python, in Go like the gateway.
- An L4 load balancer in front of the gateways (§4.12).
- No Spanner committed-use discounts.
- Shard ClickHouse later.

**Considered and rejected (versions 1-7, §11):**

| Store per request | Why not |
|---|---|
| A TigerBeetle cluster per region | **Storage:** a transfer is never deleted (about 16 TiB per 40B transfers), so a busy region would rotate clusters every one to three weeks. **Maturity:** the largest disclosed production volume is about 39 transactions a second; we need about 33,000 requests a second. **Review:** seven rounds kept finding subtle state-machine issues. |
| A regional Spanner database per region | **Latency:** about 10-20 ms p50, which misses the target. **Cost:** still a commit per request. |

**What the industry does.** We found no published system that writes to a
globally consistent ledger on every request at this rate to enforce spend.
The common patterns are:

- pre-authorized quota held at the edge;
- local counters or token buckets, refilled centrally;
- reconciliation in batches;
- overspend bounded by what is outstanding (§7).

**A reversal to be explicit about.** `billing-typed-counters.md` §2 rejected
leases, because they demote Spanner from system of record for in-flight spend.
This design accepts that, bounded:

- Spanner stays the system of record for balances, payments, debt and pauses.
- A lease is money Spanner has already reserved.
- Per-request truth is the durable settle log (§4.9), and every lease
  reconciles against it (§4.8).

## 3. Targets

1. **Latency at the gateway,** from sending authorize to holding a routing
   decision and a hold: p50 under 5 ms, p90 under 10 ms. Admission is in
   memory; what remains is the hop to the owner (§4.3) and the boot-signature
   check.
2. **Billing-database commits grow with active leases, not with requests**
   (§6).
3. **No charge lost and none booked twice** (`durable-settle-outbox.md`):
   - a request that ran is never released free;
   - a request that never ran is never charged;
   - a refunded request costs nothing.
4. **Spending is bounded by money reserved in leases,** plus the windows §5
   names.
5. **Every step can be switched off** per workspace, region and cloud.

## 4. Design

### 4.1 Shape

Per GCP region:

- **Admission service.** Go, and the front door for every request of a
  fast-path workspace.
  - Each workspace (or workspace shard, §4.3) has one owner node, chosen by
    consistent hashing.
  - The owner holds the workspace's lease and every open hold in memory.
  - Gateways on AWS and Azure use the nearest GCP region.
- **Spanner,** unchanged as the system of record. Leases are rows there,
  granted and checkpointed off the request path.
- **Settle log.** A Pub/Sub topic, ordered by authorization, written through
  the region's locational endpoint. Every settle, refund, heartbeat and reap
  record is published before the owner acknowledges it.
- **Lease auditor.** One consumer of the settle log per region. It keeps its
  own per-lease totals, checks the owners' checkpoints against them, and
  closes the leases of owners that died (§4.8).

Python keeps routing policy and everything off the hot path.

### 4.2 Leases

A lease is an amount L of a workspace's balance, reserved in Spanner for one
owner. It records:

- the workspace, region and shard;
- the owner node and its ownership epoch;
- the amount L, and the cumulative consumption last checkpointed;
- an expiry, which each checkpoint extends.

**Granting** is one Spanner transaction, never on the request path:

- It checks the workspace's headroom as the signed sum over its credit shards,
  so debt on shard 0 counts.
- It adds L to `reserved` on donor shards with enough headroom, recording which
  ones (the matched allocation of v6, §4.7).
- It writes the lease row.
- The trust tier caps L, so a new or untrusted workspace's exposure is small.
  The carding incident of 2026-08 is why.

**The owner keeps, in memory:**

| Value | Meaning |
|---|---|
| `held` | the sum of the estimates of open holds |
| `consumed` | the sum of settled charges since the lease began |
| `remaining` | `L − consumed − held` |

- Admission needs `remaining ≥ e`.
- A settle moves an estimate e out of `held` and the actual a into `consumed`.
- An overrun can make `remaining` negative. The owner then admits nothing more
  under the lease until a top-up lands.

**Checkpoints** are the only writes the owner makes:

- Every few seconds, or after a large change, the owner writes each lease's
  cumulative `consumed` to Spanner.
- The write is conditional on the stored epoch and the previous checkpoint, so
  a replay or a stale owner changes nothing.
- Each record the owner publishes carries its lease and a per-lease sequence
  number. A checkpoint records the sequence it covers.
- The same transaction books the delta: `total_usage` grows by it, and
  `reserved` shrinks by it on the lease's donor shards until L is used up.
- It also extends the lease's expiry.
- One transaction can carry many leases.

**Top-ups and returns:**

- When `remaining` falls below a low-water mark, the owner asks for a new lease
  of its own size.
  - At most one request is outstanding per workspace-region-shard, with a
    cooldown.
  - The amount is sized by the recent rate times a horizon, within the trust
    cap.
- An idle lease is closed by its owner: a final checkpoint, then
  `L − consumed` is released.

### 4.3 Ownership

- **Routing.** The front door hashes the workspace (and shard) to an owner and
  forwards the request to it, a hop of about 0.5 ms in the same region.
- **Shards.** A workspace that is hot enough is split into K shards.
  - K is recorded with the workspace.
  - Each shard has its own lease and owner.
  - A request picks a shard by its own hash.
  - One workspace carried 74% of all tokens to date, so this is needed from
    the start.
- **Correctness does not depend on exclusive ownership.** Each lease is
  separately reserved money.
  - If a membership change briefly lets two nodes act as owner, each holds its
    own lease. Neither can spend the other's.
  - The cost is a little extra reservation for a while.
- **Requests carry their owner.** Heartbeats, settles and refunds go to the
  node and lease named in the authorization's signed envelope, not to whoever
  owns the workspace now.
  - If that node is gone, any node publishes the record to the settle log.
  - The auditor applies it to the lease (§4.8).
- **Keyed requests** (an `Idempotency-Key`) stay on today's synchronous path
  at first. Python's unique index already owns their exactly-once rule.
- **Unkeyed requests** have a scope minted for one gateway invocation.
  - The owner remembers each invocation's answer until the hold closes, so a
    retry of a lost authorize response gets the same answer.
  - If the owner died between the two, the retry is admitted afresh. The
    earlier hold is never settled, because the gateway acts on one answer. It
    is released at its deadline, uncharged.
- **Python stays bounded without knowing about leases.** Leases are in
  `reserved`, which synchronous reservations cannot use.

### 4.4 Authorize

1. Verify the boot signature and the accepted image digest, against caches with
   a maximum age.
2. Evaluate the compiled routing snapshot (§4.10) and compute the estimate e.
   Without `max_tokens`, it assumes 512 output tokens.
3. At the owner, check the workspace's state from a cache with a short maximum
   age (pause, trust, revocation). Then hold e against the lease.
4. Answer with a **signed envelope** that the gateway echoes on heartbeat,
   settle and refund:
   - the authorization ID `A` and the generation ID;
   - the owner node and the lease;
   - the frozen candidates, prices, fees and app terms;
   - the snapshot version and the boot binding;
   - the hold, its deadline rule and its end of life.

**Nothing is written on the request path.**

**When the lease is short:**

- The owner asks for a top-up.
- An unkeyed request takes the synchronous path, behind the per-workspace
  breaker in §7.
- 402 comes only when the workspace's balance cannot cover e. Python answers
  503 with `Retry-After`, not 402, when the balance covers e but the headroom
  outside leases does not.

### 4.5 One authorization

- **Terminals.** A settle or refund is published to the settle log first, then
  applied by the owner, then acknowledged.
  - A settle moves e out of `held` and a into `consumed`. A refund moves e out
    of `held`.
  - The owner arbitrates terminals in memory: the first for `A` wins. A later
    one is answered with the winner's outcome and charges nothing.
- **Heartbeats** are published under `A` and answered after the publish is
  acknowledged.
  - A signed deadline comes back with each answer.
  - A heartbeat acknowledged after the deadline it echoes is answered
    `deadline_passed`.
  - The owner keeps each hold's latest valid snapshot in memory. The log keeps
    them all.
- **The reaper** runs at the owner. A hold still open at its deadline plus a
  grace is reaped:
  - a reap record with the last valid snapshot s is published;
  - s is charged, as today's reaper does.
- **Decision 70 stays.** When the snapshot wins, a later settle or refund
  charges nothing more.
- **Times** are today's, capped:
  - the gateway ends a stream at 2 h 15 min;
  - the reaper acts at a deadline capped at 2 h 20 min, plus its grace.

### 4.6 Keys

- **Capped keys stay synchronous at first,** like window-limited and
  `budget_strict` keys (§4.11). On the fast path, a lowered cap could be
  overspent by the leases already out.
- **Uncapped keys:**
  - Usage is aggregated per key in the owner's checkpoint and booked to
    `tr_key_limit` with it.
  - Revocation reaches admission through the key-status cache, whose maximum
    age is short and stated as the exposure.
- **Adding a cap** moves the key to the synchronous path. Python serves it once
  every lease that admitted the key has been checkpointed past the change.
  Until then it answers 503 with `Retry-After`.

### 4.7 Budget, pauses and debt

- **The auditor's identity per workspace:** `reserved` equals the sum, over
  open leases, of `L − booked consumption`, floored at zero per lease. Every
  term is a Spanner value.
- **Rollups are the checkpoints** (§4.2), using matched shard allocation:
  - a lease's consumed delta books usage and releases `reserved` on the same
    donor shard, so that shard's headroom is unchanged;
  - an overrun beyond L books usage only, on shard 0, as an overrun does today.
- **Pause, revoke, trust downgrade, or a switch out of fast mode:**
  - Spanner marks the workspace's leases closing and refuses new grants.
  - Owners learn of it from the state cache or a pushed control message,
    within the cache's maximum age. They then stop admitting and close the
    leases.
  - **Exposure:** at most the leases' `remaining` during that window, which
    the trust caps keep small.
- **Home settlement.** Deferred usage from a peer plane is booked to Spanner
  unconditionally, as today. When `available` falls below the outstanding
  leases, no lease is granted or topped up, and the owners close their leases.

### 4.8 Owners that die, and audit

The **lease auditor** consumes the whole settle log for its region.

- **Its per-lease totals** come from settle, refund and reap records.
  - They are deduplicated by authorization: the first terminal for `A` wins.
  - They are independent of the owners' memory.
- **Audit.** Every owner checkpoint must equal the auditor's total through the
  checkpoint's sequence number. A mismatch alerts.
- **Owner death.** A lease whose checkpoints stop past its expiry is closed by
  the auditor:
  - it books the auditor's total, net of what was already checkpointed;
  - it reaps the holds that reached their deadline with no terminal, at their
    last logged snapshot;
  - it releases the rest.

  A new owner never reuses a dead owner's lease. It is granted a new one.
- **A slow owner, not a dead one,** finds its next checkpoint refused once the
  auditor has closed the lease. It stops admitting under that lease at once.
  Records it still publishes for the lease's holds are booked by the auditor as
  late terminals: usage only, since nothing is left to release.
- **Loss of a region.** Spanner and Pub/Sub are multi-zone and outlive a
  region's admission service. The auditor, run from another region, closes the
  region's leases from the log. Nothing financial lives only in the lost
  region.
- **What can be lost:** a request whose gateway died before any settle or
  heartbeat reached the log. It is uncharged, as today.

### 4.9 Settle durability, records and side effects

- **Records before acknowledgement.** Settle, refund, heartbeat and reap
  records go to the settle log under `A` before the owner acknowledges.
  - If the publish fails, the owner answers with an error, and the gateway
    retries as today.
  - Nothing is applied to a lease without its record.
- **Records.** One consumer writes each authorization's generation and activity
  record from the log, once, from the winning terminal. Duplicates are
  idempotent on `A`.
  - Amount-sensitive consumers act once per terminal: budget alerts,
    auto-refill, metadata webhooks, routing feedback and route-fallback reports.
- **Lookups.** Disposition and evidence lookups read the records, with
  ClickHouse within the records bound for `gateway_request_id`.
- **What stays synchronous for now.**
  - Synthetic-probe workspaces, so release gates keep reading Spanner.
  - OAuth-app keys with a markup, until payouts have a durable obligation tied
    to each charge.

### 4.10 Routing

- Python compiles routing into a versioned, signed snapshot on every catalog or
  health change.
- A snapshot that fails verification keeps the previous one, and one past its
  freshness limit stops fast admission.
- Per-workspace inputs are cached under the maximum age.

### 4.11 What stays synchronous at first

Credit-funded keys on standard catalog routes go first, streaming or not. These
stay on today's Python path:

- requests with an `Idempotency-Key` (§4.3);
- BYOK routes, custom and user-provided models, Polyphemus selection, native
  batch, video and image jobs, and hosted tools with
  `additional_cost_reservation_microdollars`;
- x402 and federated (deferred-settlement) keys;
- keys with lifetime caps or window limits, and `budget_strict` keys (§4.6);
- OAuth-app keys with a markup, and synthetic-probe workspaces;
- workspaces that are paused, in debt, below the minimum balance, or below the
  trust tier that allows leases.

Token volume is concentrated. As of 2026-07-19, one workspace accounted for 74%
of all tokens to date, so a narrow fast path still carries most of the traffic.

### 4.12 The gateway load balancer and receipt keys

Each region's gateway group goes behind a regional external passthrough Network
Load Balancer:

- TLS still terminates inside the enclave, so attestation is unchanged.
- Connection draining, as long as the longest stream, makes scale-in safe for
  the gateway autoscaler (quill-cloud-proxy #432).
- Receipt keys move off DNS discovery, which has an instance-termination gap.
  Boot-registry registration, with attestation history, becomes the publication
  path, required before an instance takes traffic.

## 5. Invariants

Each has a production check.

1. **Admission bound.** Every fast admission is a hold against a lease whose
   amount Spanner has reserved. The sum of open holds and settled charges never
   exceeds L, except for overruns.
2. **Conservation.** The identity in §4.7 holds after every checkpoint. The
   auditor's totals equal the owners' checkpoints at matching log positions.
3. **One terminal per authorization.** At the owner, and in the auditor, by
   first record in log order. The loser answers with the winner's outcome.
4. **No charge lost.** Every terminal is in the settle log before it is
   acknowledged. Dead owners' leases are closed from the log.
5. **No charge invented.** Only boot-signed settles and the reaper's snapshots
   of validated heartbeats charge.
6. **No lease is reused after its owner dies.** A new owner gets a new lease.
7. **Ownership is routing, not safety.** Two owners can never spend the same
   reserved money.
8. **Key caps.** Capped keys are served synchronously.
9. **Pauses** stop admission within the state cache's maximum age. Exposure is
   at most the open leases' `remaining`, capped by trust tier.
10. **Checkpoints** are cumulative, conditional on epoch and on the previous
    value, and so are idempotent.
11. **Latency** is stated as percentiles. Excursions take the documented
    fallback or a 503.

## 6. Latency and load

| Step | Estimate |
|---|---|
| Gateway to front door, same region | 0.5-1 ms (more from AWS, Azure) |
| Front door to owner | about 0.5 ms |
| Boot signature, caches, routing evaluation | 0.2-1 ms |
| The hold, in memory | microseconds |
| Sign the envelope, reply | under 0.3 ms |
| **Total** | **about 2-3 ms p50** |

**Spanner load** is grants and checkpoints:

- about one transaction per active lease every few seconds, batched across
  leases;
- a workspace hot enough for K shards adds K leases;
- none of it grows with requests.

**Pub/Sub load:**

- one settle record per generation, plus heartbeats;
- the pre-first-byte heartbeat waits for its publish, as it waits for a Spanner
  commit today.

The benchmark measures the owner's throughput on the hottest workspace, the
publish latency, and the auditor at the target rate.

## 7. Lessons from the retired regional-quota leases

The September pilot (`git show 44924155^:docs/design/regional-quota-leases.md`;
`docs/incidents/2026-09-26-regional-ledger-grant-storm.md`) was also built on
leases, and was retired on 2026-09-27.

| Pilot failure | This design |
|---|---|
| Every authorization point-read its lease in a Bigtable pinned to us-central1, so europe-west4 read across the Atlantic and timed out | The lease lives in the owner's memory. The request path reads and writes no store |
| A workspace went from about 6 to 430 authorizations a minute; grants aborted 94-96%; authorize p50 reached 3.7 s | Grants and top-ups are never on the request path. At most one is outstanding per workspace-region-shard, with a cooldown, sized by rate within the trust cap |
| The ledger's p99 amplified into a fleet-wide Spanner abort storm | The request path never touches Spanner. Fallback to the synchronous path goes through a per-workspace-region breaker and sheds with 503 when that path is saturated |
| Ambiguous leases were quarantined, not guessed back into service | A dead owner's lease is never reused. The auditor closes it from the log |

## 8. Rollout

1. **Measure** commits per generation after #1464 and #1465, and authorize's
   timing fields per region.
2. **Gateway load balancer and receipt-key publication** (§4.12), independent
   of the rest.
3. **A spike** of the owner, checkpoints and the auditor on one region:
   ownership hand-off, an owner killed mid-stream, the hottest workspace's rate
   on one owner, and Pub/Sub ordering and redelivery.
4. **Shadow.** Gateways mirror authorize, heartbeat and settle. A comparator
   reports any difference from Python in decisions, per-authorization charges,
   reaper outcomes and records.
5. **Benchmark gate** (§6).
6. **Pilot:** Joseph's own workspace, then a few large ones, with kill switches
   per workspace, region and cloud.
7. **Widen;** move keyed requests, capped keys, payouts and the remaining route
   types (§4.11) one at a time; then retire the Python hot path.

## 9. Not decided here

- **Tuning values:** lease sizes per trust tier, the low-water mark, the top-up
  horizon and cooldown, the checkpoint interval, the state cache's maximum age,
  the reaper's grace, and the shard count rule. They come from the spike, the
  benchmark and the pilot.
- **Keyed requests on the fast path** need a durable per-scope claim. It is a
  later step.
- **Home-region assignment** for workspaces whose traffic moves between
  continents.

## 10. Not in this design

Sharding ClickHouse (decided later), and Spanner topology for the system of
record.

## 11. Review history

- **v1 (2026-10-02)** kept per-request state in admission-node memory. Codex
  found 14 problems; Fable confirmed them and found 7 more.
- **v2** moved money into a per-region TigerBeetle ledger. Codex (7 P1, 5 P2)
  and Fable (4 P1, 9 P2, 6 P3) found:
  - unowned scopes and unsafe fallback;
  - a terminal ID broken by Stage D;
  - late settles and refunds against posted heartbeats;
  - rollup conflation and missing headroom accounting;
  - incomplete keys, returns and payouts;
  - unrecoverable records and sweep-only revocation;
  - an ungated loss bound, plus operations errors.
- **v3** answered those. Codex (5 P1, 6 P2) and Fable (3 P1, 7 P2, 5 P3) found:
  - a second terminal ID for late settles;
  - indistinguishable voids, so a loser could report the wrong outcome;
  - an overrun leg that was not one chain, with headroom released before debt
    was repaid;
  - no expired-state transitions;
  - a settle payload published after the ledger;
  - recovery that double-booked rolled-up charges;
  - ownership that moved with key or route changes;
  - and, among the P2s, shard targets, the key accounting domain, rollup and
    return ordering, durable heartbeat state, epoch enforcement and record
    revisions.
- **v4** answers each in §§4-9:
  - the claim transfer with the kind in its user data;
  - the known-amount overrun and repay transfers;
  - the expired-state rows;
  - intents before the ledger;
  - archive completeness tied to fences;
  - scope claims for both paths;
  - a key ledger with its own sink;
  - FIFO donor rows and sweeper ordering;
  - versioned records.

  TigerBeetle semantics were re-checked against its documentation for this
  version: balancing to zero on 0.16 and later, non-zero single-phase amounts,
  combined balancing flags, and closed accounts accepting only voids.
- **v5.** Codex (5 P1, 6 P2) and Fable (1 P1, 5 P2, 5 P3) found these problems
  in v4:
  - expiry and underrun settles released headroom while debt was outstanding;
  - after a failed child leg, the other path could still claim an attempt ID;
  - an expired-state settle could race a heartbeat's new hold;
  - scopes that existed only in Spanner were unprotected when a workspace
    entered fast mode;
  - a cap could be added while uncapped usage was still unbooked;
  - returns raced refunds, and negative deltas had no allocation;
  - fences had no exact archived cut;
  - post and void codes, leg IDs, version floors and heartbeat replay were
    unspecified;
  - and the ledger's growth was unbounded.

  v5 answers them:
  - Heartbeats leave the ledger, and the reaper books snapshots at the
    deadline, as today; Decision 70 stays. Nothing is charged before a
    terminal, so nothing is reversed.
  - Every admission and return repays debt first.
  - Each scope gets one claim, linked into its admission, with
    `A = H(scope, nonce)` and no attempt counter.
  - Workspace modes have an atomic check in Python and a backfill of retained
    scopes.
  - Cap activation waits behind a marker and a fence.
  - Per-workspace pools let rollups release returns from archived counters.
  - Fences come from the archived change stream.
  - Every leg has a deterministic ID, versions are pinned, and clusters rotate.

  One correction to v4: from 0.16.0, single-phase amounts may be zero.
- **v6.** Codex (8 P1, 1 P2) and Fable (1 P1, 6 P2, 7 P3) found these problems
  in v5:
  - the reaper could act on a stale view of the log, reaping live streams,
    acknowledged renewals, durable settles and the final settle at the end of
    life;
  - one shard took all usage while reservations drained elsewhere, so Python
    could spend headroom twice;
  - an archive cut could split a linked chain;
  - lowering a cap left the old allowance spendable;
  - a terminal winner that was never archived could not be rebuilt;
  - a cluster could retire with expired, unsettled holds;
  - entering fast mode needed an unindexed backfill and a keyed outage;
  - replays were answered 409 where today returns the original;
  - and the rotation's copy, grants, sinks and reader were underspecified.

  v6 answers them:
  - The reaper consumes the whole log, acts only at the deadline plus 60 s and
    while current, and completes durable intents instead of reaping them.
    Heartbeats carry signed deadlines.
  - Matched shard allocation, with debt booked on shard 0.
  - Fences only at chain ends.
  - Lowering a cap returns the old allowance first.
  - Recovery prefers the gateway's intent when no terminal was archived.
  - Retirement waits for a terminal per authorization.
  - Entry is a front-door phase with no backfill: unkeyed requests are fast at
    once, and keyed requests after the retention period.
  - Replays answer as today.
  - Rotation copies claims with their original time, and returns the old
    cluster's funds before granting. The gate closes against its own sink.
  - Rollups read the archive writer's balance view.

  TigerBeetle's state machine confirms that `exists` ends a linked chain;
  §4.2 now relies on that rather than leaving it open.
- **v7.** Codex (6 P1, 2 P2) and Fable (1 P1, 5 P2, 7 P3) found these problems
  in v6:
  - refunded holds could refill a lowered key cap;
  - a heartbeat's renewal was checked at arrival but ordered at publication,
    and the reaper relied on a monitoring metric sampled once a minute;
  - the node and the reaper applied different acceptance rules;
  - a fixed entry wait missed reservations still settling;
  - rotation dropped synchronous ownership too early, admitted in two clusters
    at once, and left a funding gap;
  - recovery could charge a refunded request;
  - late terminals after retirement could book twice;
  - replays needed the raw nonce;
  - expiry events and old claims had no rule;
  - funded workspaces got 402 while their funds sat in escrow.

  v7 answers them:
  - Capped keys stay synchronous, and the key ledger is gone.
  - The reaper acts only after a probe published under the authorization's
    ordering key comes back, and takes deadlines from every signed heartbeat.
    A heartbeat is `accepted` only if its publish was acknowledged before its
    deadline.
  - Entry waits on a scan of Python's idempotency index.
  - Rotation publishes the new cluster only after the old one is closed,
    carries synchronous claims while Python holds their scopes, and pre-funds
    the new cluster.
  - Recovery books the cheapest logged terminal.
  - Retired clusters answer `already_terminal`.
  - Replays rebuild the envelope only for the same nonce.
  - `two_phase_expired` always ends a chain, and claims expire with today's
    retention.
  - Python answers 503, not 402, while funds sit in escrow.
- **v8 changes direction.** Joseph chose regional leases on 2026-10-02, after
  seven rounds on a per-request ledger and a survey of how high-volume billers
  enforce spend.
  - **What carries over:**
    - the settle log written before acknowledgement;
    - the reaper and Decision 70;
    - matched shard allocation;
    - keyed and capped-key requests on the synchronous path;
    - the front door and the routing snapshot.
  - **What goes:** the TigerBeetle ledger, cluster rotation, scope and terminal
    claims, and fences on the change stream.
  - **What is new:**
    - owner-held leases, with cumulative conditional checkpoints;
    - workspace shards for the hottest workspaces;
    - the lease auditor, which checks live leases and closes dead owners'
      leases from the settle log.
