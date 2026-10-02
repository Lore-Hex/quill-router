# Fast admission and batched settlement

Status: **proposed, v9, 2026-10-02. Nothing built.** v8 changed direction to
regional leases, Joseph's choice (§2). Codex and Fable reviewed v1-v8 (§11),
and v9 answers their reviews of v8.

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
  granted, renewed and booked off the request path.
- **Settle log.** A Pub/Sub topic with one ordering key per lease, written
  through the lease's region's locational endpoint.
  - Pub/Sub delivers one key's messages in the order it receives them, from
    any publisher in that region.
  - Each key is limited to 1 MBps, which sets the shard count of the hottest
    workspaces (§4.3).
  - Every settle, refund, heartbeat and reap record is published before it is
    acknowledged.
- **Lease auditor.** A consumer group on each region's settle log, and the
  only writer of consumption to Spanner (§4.8). It applies each lease's
  records in log order, books them, writes the request records (§4.9), audits
  the owners, and drains the leases of owners that stopped.

Python keeps routing policy and everything off the hot path.

### 4.2 Leases

A lease is an amount L of a workspace's balance, reserved in Spanner for one
owner. Its row records:

- the workspace, region and shard;
- the owner node and its ownership epoch;
- L, and its allocation over donor credit shards;
- its state: open, draining or closed;
- an expiry, which each renewal extends, and whether renewal is revoked;
- what the auditor has booked against it, and the last record it applied.

**Granting** is one Spanner transaction, never on the request path:

- It reads the workspace's open and draining leases. It refuses a grant that
  would take their unbooked total, the sum of `L − booked`, above the trust
  tier's allowance. The allowance is per workspace, across regions and
  shards. The carding incident of 2026-08 is why.
- It checks the workspace's headroom as the signed sum over its credit
  shards, so debt counts.
- It adds L to `reserved` on donor shards with enough headroom, and records
  the allocation (matched allocation, §4.7).
- It writes the lease row, with the workspace's key-status version (§4.6).

**The owner keeps, in memory:**

| Value | Meaning |
|---|---|
| `held` | the sum of the estimates of open holds |
| `consumed` | the sum of settled charges since the lease began |
| `remaining` | `L − consumed − held` |

- Admission needs `remaining ≥ e`.
- A settle moves an estimate e out of `held` and the actual a into `consumed`.
- An overrun can make `remaining` negative. The owner then admits nothing more
  under the lease.

**Renewals** are the only Spanner writes an owner makes.

- Every few seconds, idle or not, the owner extends each lease's expiry.
- The write is conditional on the lease being open and not revoked, and on
  the owner's epoch.
- In a batch, each lease is its own conditional statement. One that changes
  no row means the lease was revoked or is draining. The owner re-reads it
  and stops using it.
- With each renewal, the owner publishes a **checkpoint record** under the
  lease:
  - its cumulative `consumed`;
  - its open holds' count, sum and latest end of life;
  - the key-status version it applies, and its open holds of any key being
    moved (§4.6).

**The cutoff.** The owner admits under a lease, and decides its holds'
outcomes (§4.5), only while its own clock is before the lease's expiry minus
a skew allowance.

- It reads the clock after recording a hold, and undoes the hold if the
  cutoff has passed.
- A renewal that keeps failing lets the lease reach its cutoff. The owner then
  stops admitting under it (§4.4).

**Top-ups and closing:**

- When `remaining` falls below a low-water mark, the owner asks for another
  lease.
  - At most one request is outstanding per workspace-region-shard, with a
    cooldown.
  - The amount is sized by the recent rate times a horizon, within the
    allowance.
- An owner stops admitting under a lease when the lease goes idle or reaches
  its maximum life, when the workspace is paused, or when the owner is
  leaving.
  - Once no holds are open, it publishes a final checkpoint record and marks
    the lease draining.
  - If it is leaving with holds still open, it publishes them in a hand-off
    record (authorization, estimate, deadline and last snapshot), then marks
    the lease draining.
  - The auditor then finishes the lease (§4.8).
- The maximum life bounds the winners the auditor stores for a lease.

### 4.3 Ownership

- **Routing.** The front door hashes the workspace (and shard) to an owner and
  forwards the request to it, a hop of about 0.5 ms in the same region.
- **Shards.** A workspace that is hot enough is split into K shards.
  - K is recorded with the workspace.
  - Each shard has its own lease and owner.
  - A request picks a shard by its own hash.
  - One workspace carried 74% of all tokens to date, so this is needed from
    the start.
  - K is also set by the 1 MBps limit on a lease's ordering key.
- **Correctness does not depend on exclusive ownership.** Each lease is
  separately reserved money.
  - If a membership change briefly lets two nodes act as owner, each holds its
    own lease. Neither can spend the other's.
  - The cost is a little extra reservation for a while.
- **Requests carry their lease.** Heartbeats, settles and refunds go to the
  owner and lease named in the authorization's signed envelope.
  - While the lease is open, only its owner publishes its records.
  - If the owner cannot be reached, the front door answers retry. The gateway
    retries as today, and its settles come from its durable outbox.
  - A front door that keeps failing to reach an owner can revoke the lease's
    renewal in Spanner. The lease then expires, and the log takes over
    (§4.8).
- **Keyed requests** (an `Idempotency-Key`) stay on today's synchronous path
  at first. Python's unique index already owns their exactly-once rule.
- **Unkeyed requests** get a scope for one gateway invocation, keyed by the
  enclave's `invocation_nonce`.
  - This is new. Today Python mints a fresh UUID for each authorize that
    presents no key (`routes/internal/gateway.py`).
  - The owner remembers each invocation's answer until the hold closes, so a
    retry of a lost authorize response gets the same answer.
  - If the owner died between the two, the retry is admitted afresh under
    another lease. The earlier hold is never settled, because the gateway
    acts on one answer. It ends uncharged when its lease drains.
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

   The end of life is absolute, from the owner's clock at admission. The
   gateway ends the stream at the earlier of it and its own 2 h 15 min.

**A fast authorization reads and writes no store,** when its workspace is
eligible, its caches are fresh and its lease has room. A streaming request's
first heartbeat then waits for its publish, as it waits for a Spanner commit
today.

**When the lease is short:**

- The owner asks for a top-up.
- **A sharded workspace** tries one other shard's owner, then gets 503 with
  `Retry-After`. It never falls back to the synchronous path. One shard's
  share of the hottest workspace, on one credit row, is the pilot's grant
  storm (§7).
- **An unsharded workspace's** unkeyed request takes the synchronous path,
  behind a per-workspace concurrency cap and the breaker in §7.
- 402 comes only when the workspace's balance cannot cover e. When the balance
  covers e but the headroom outside leases does not, Python answers 503 with
  `Retry-After`. This needs a Python change: today an insufficient-credit
  reservation is answered 402.

### 4.5 One authorization

**Who decides.** For each authorization `A`, the first terminal (settle,
refund or reap) in its lease's log order wins. The owner, the auditor and the
request records all follow that rule.

- While the lease is open, its owner is the only publisher of its records.
  - It handles `A`'s heartbeats and terminals under a per-`A` lock.
  - It publishes only the winner, and answers after the publish is
    acknowledged.
  - So its memory follows log order.
- The owner answers a later terminal for `A` with the winner's outcome, and
  does not publish it. It charges nothing.
- An answer that depends on the owner's decision is given only if the publish
  was acknowledged before the owner's cutoff.
  - Otherwise the answer is `recorded`, and the log decides.
  - The gateway needs only to know that the record is durable.
- Once the lease is draining, any node publishes its records, through the
  lease's region's endpoint, and the log alone decides (§4.8).

**Terminals.** A settle moves e out of `held` and a into `consumed`. A refund
moves e out of `held`.

**Heartbeats** are published under the lease and answered after the publish
is acknowledged.

- Today's validity rules carry over (`heartbeat_gateway_atomic`):
  - a lower sequence, or the same sequence with another hash, is stale;
  - the same sequence and hash is a replay, answered without a second
    publish;
  - a changed endpoint, regressed usage, usage beyond the authorized tokens,
    or a running charge above the hold's cap is rejected.
- A signed deadline comes back with each answer. A heartbeat whose publish is
  acknowledged after the deadline it echoes is answered `deadline_passed`.
- The owner keeps each hold's latest valid snapshot in memory. The log keeps
  them all.

**Reaping is ordered in the log.** A reaped hold is charged its last valid
snapshot, as today's reaper does in production. (`scripts/deploy/rollout.sh`
sets `TR_REAP_SNAPSHOT_BOOKING_ENABLED=true`; the code default is false.)

- **While the lease is open,** the owner reaps a hold still open at its
  deadline plus a grace. It publishes a reap record naming that hold and its
  snapshot, before its cutoff. The record reaps only that hold.
- **Once the lease is draining,** the auditor publishes ticks, each carrying
  its time (§4.8). A hold with no terminal is reaped at the first tick past
  its deadline, as of that point in the log, plus the grace.
  - Ticks are published only after a deadline plus the grace, and the grace
    is longer than twice the skew allowance.
  - So a heartbeat acknowledged before its deadline is received before any
    tick that could reap it, and a renewed stream is never reaped.

**Decision 70 stays.** When the snapshot wins, a later settle or refund
charges nothing more.

**Times** are today's, capped:

- the gateway ends a stream at 2 h 15 min;
- the reaper acts at a deadline capped at 2 h 20 min, plus its grace.

### 4.6 Keys

- **Capped keys stay synchronous at first,** like window-limited and
  `budget_strict` keys (§4.11). On the fast path, a lowered cap could be
  overspent by the leases already out.
- **Uncapped keys:**
  - The auditor books usage per key to `tr_key_limit`, with the lease's other
    bookings.
  - Revocation reaches admission through the key-status cache, whose maximum
    age is short and stated as the exposure.
- **Adding a cap** moves the key to the synchronous path.
  - The change bumps the workspace's key-status version.
  - A grant carries the version current when it was made. An owner admits
    under a lease only with a key-status cache at least that new.
  - Owners stop admitting the key once they see the change.
  - Python enables the cap when every lease that could hold one of the key's
    earlier holds meets one of two conditions:
    - it has a booked checkpoint record that applies the change and shows
      none of the key's holds open;
    - it has finished draining.
  - The key's booked usage then includes every fast hold. Until then, the key
    answers 503 with `Retry-After`.

### 4.7 Budget, pauses and debt

**The identity, per credit shard.**

- `reserved` equals:
  - the unbooked allocation on that shard of every open or draining lease;
  - plus the unsettled synchronous holds on that shard.
- Every term is a Spanner value.
- Both the lease audit and the existing counter reconciler
  (`storage_gcp_counter_reconcile.py`, which counts request holds but not
  leases) check this one identity.

**Booking** uses matched shard allocation. A lease's booked consumption raises
`total_usage` and lowers `reserved` on the same donor shard, so that shard's
headroom is unchanged.

**Shards never mix signs.**

- Consumption beyond L is booked as usage on shards that still have headroom,
  the lease's donors first.
- Only what no shard can cover leaves a shard negative, and then none is
  positive.
- Headroom that appears while a shard is negative repays that shard first:
  from a hold's release, a lease's return, or a payment.
- The overrun booking sets a debt marker on the workspace. A release reads
  one more row only while the workspace is in debt.
- So Python's per-shard reservation check (`reserve_credit`) means the same as
  the signed sum.
- Today an overrun stays on the hold's shard. That shard can go negative while
  another stays positive, and Python can then spend the positive shard though
  the workspace has nothing left. The synchronous path adopts the same rules.

**Returns repay debt first.**

- Releasing a lease's unbooked amount goes through today's release primitive
  (`release_credit`).
- Freed money repays a negative shard first. It then absorbs unrecovered
  payment claims, and the recovery pause is re-evaluated
  (`absorb_unrecovered_recovery_tx`).
- An overrun lowers the signed headroom that grants read.

**Pause, revoke, trust downgrade, or a switch out of fast mode:**

- Spanner refuses new grants for the workspace.
- Owners learn of it from the state cache or a pushed control message, within
  the cache's maximum age. They then stop admitting and close the
  workspace's leases (§4.2).
- **Exposure:** at most the sum of `L − consumed` over the workspace's open
  leases, within the trust allowance.
  - Not `remaining`: refunds free held capacity, which new admissions can use
    until the owner stops.

**Home settlement.** Deferred usage from a peer plane is booked to Spanner
unconditionally, as today. When `available` falls below the outstanding
leases, no lease is granted or topped up, and the owners close their leases.

### 4.8 The auditor, draining and owners that stop

The **lease auditor** is a consumer group on each region's settle log.
Pub/Sub gives each lease's records to one member at a time, in log order.

**It is the only writer of consumption.**

- Per lease, every few seconds, one Spanner transaction:
  - books the consumption applied since the last one (§4.7), with per-key
    usage;
  - stores the winners decided since then, keyed by authorization, and the
    ID of the last record applied;
  - is conditional on the previously stored record ID.
- Only then does it acknowledge the records.
- A redelivered record at or before the stored ID is skipped. A duplicate
  publish is recognized by its record ID.
- A terminal for an authorization with a stored winner charges nothing.
  Winners are kept until the lease closes.
- One transaction can carry many leases, each its own conditional statement.

**It audits the owners.**

- At each checkpoint record, the owner's cumulative `consumed` must equal the
  sum of the terminals the owner published before it.
- A difference is a fault. The auditor alerts and revokes the lease.

**Expiry.**

- A lease not renewed by its expiry plus the skew allowance is marked
  draining by the auditor.
- That transaction is conditional on the lease still being open, with the
  renewal the auditor read.
- By then the owner's cutoff has passed, so the owner neither admits nor
  decides under the lease.

**Draining.** A draining lease keeps its unbooked amount reserved until every
hold it could have admitted has ended, at a terminal or a reap:

- **If the owner published a final checkpoint or a hand-off record,** the
  holds it lists are all there are.
- **Otherwise,** the auditor knows the holds the log shows through heartbeats.
  Holds it cannot see end by the lease's expiry plus the 2 h 20 min maximum
  life, plus the grace.

Records that arrive meanwhile are booked against that reservation, terminals
by the first-terminal rule.

- A front door checks a heartbeat for a draining lease only for its signature
  and its deadline.
- The auditor applies today's validity rules in log order, and an invalid
  heartbeat changes nothing.

**Reaping in a draining lease.**

- The auditor publishes ticks under the lease, each carrying its time.
- Holds are reaped at the ticks by the rule in §4.5.
- The ticks are in the log, so every consumer reaches the same outcome.

**Close.**

- When the drain ends, the auditor books what remains and releases
  `L − booked` through the release primitive (§4.7).
- It marks the lease closed.
- A record for a closed lease charges nothing, as a settle after today's
  reaper does.

A new owner never reuses a dead owner's lease. It is granted a new one.

**When an owner's region is lost.**

- **If Pub/Sub in that region still works,** an auditor run from another
  region drains the region's leases from the log.
- **If Pub/Sub in that region is down,** records acknowledged there cannot be
  read until it recovers. Its leases stay reserved, and draining waits.
  Nothing is released from them meanwhile. The region's owners cannot publish,
  so they stop admitting.

**What can be lost:** a request whose gateway died before any settle or
heartbeat reached the log. It is uncharged, as today.

### 4.9 Settle durability, records and side effects

- **Records before acknowledgement.** Settle, refund, heartbeat and reap
  records go to the lease's log before they are acknowledged.
  - If the publish fails, the answer is an error, and the gateway retries as
    today.
  - Nothing is applied to a lease without its record.
- **Request records.** The auditor writes each authorization's generation and
  activity record from its stored winner, after the commit that stored it.
  - Writes are idempotent on `A`, so a crash between the two loses nothing.
  - Amount-sensitive consumers act once per winner: budget alerts,
    auto-refill, metadata webhooks, routing feedback and route-fallback
    reports.
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
2. **Conservation.** The per-shard identity in §4.7 holds after every booking.
   Each checkpoint record equals the terminals its owner published before it.
3. **One terminal per authorization:** the first in its lease's log order.
   While the lease is open, the owner publishes only winners. After that, the
   log alone decides.
4. **No charge lost.** Every terminal is in the log before it is acknowledged.
   A draining lease keeps its reservation until every hold it could have
   admitted has ended.
5. **No charge invented.** Only boot-signed settles, and reaps at the last
   validated heartbeat's snapshot, charge.
6. **No lease is reused after its owner stops.** A new owner gets a new lease.
7. **Ownership is routing, not safety.** Two owners can never spend the same
   reserved money.
8. **Key caps.** Capped keys are served synchronously. A new cap takes effect
   only after every fast hold of the key is booked.
9. **Pauses** stop admission within the state cache's maximum age. Exposure is
   at most the sum of `L − consumed` over open leases, within the workspace's
   trust allowance.
10. **Renewals and bookings are conditional:** on lease state and epoch, and
    on the stored record ID. Replays change nothing.
11. **Shards never mix signs.** No credit shard is positive while another is
    negative.
12. **Latency** is stated as percentiles. Excursions take the documented
    fallback or a 503.

**The windows Target 4 allows:**

- the state cache's maximum age, for pauses and revocation;
- a clock wrong by more than the skew allowance, which could let an owner
  admit or decide after its lease drains;
- overruns, which are booked as debt (§4.7).

## 6. Latency and load

| Step | Estimate |
|---|---|
| Gateway to front door, same region | 0.5-1 ms (more from AWS, Azure) |
| Front door to owner | about 0.5 ms |
| Boot signature, caches, routing evaluation | 0.2-1 ms |
| The hold, in memory | microseconds |
| Sign the envelope, reply | under 0.3 ms |
| **Total** | **about 2-3 ms p50** |

**Spanner load** is grants, renewals and the auditor's bookings:

- renewals and bookings are each about one transaction per active lease every
  few seconds, batched across leases;
- a workspace hot enough for K shards adds K leases;
- none of it grows with requests.

**Pub/Sub load:**

- one settle record per generation, plus heartbeats, checkpoint records and
  ticks;
- a lease's records share one ordering key, limited to 1 MBps, so a lease's
  record rate bounds its request rate and sets K;
- the pre-first-byte heartbeat waits for its publish, as it waits for a
  Spanner commit today.

The benchmark measures the owner's throughput on the hottest workspace, the
publish latency, and the auditor at the target rate.

## 7. Lessons from the retired regional-quota leases

The September pilot (`git show 44924155^:docs/design/regional-quota-leases.md`;
`docs/incidents/2026-09-26-regional-ledger-grant-storm.md`) was also built on
leases, and was retired on 2026-09-27.

| Pilot failure | This design |
|---|---|
| Every authorization point-read its lease in a Bigtable pinned to us-central1, so europe-west4 read across the Atlantic and timed out | The lease lives in the owner's memory. Fast admission reads and writes no store |
| A workspace went from about 6 to 430 authorizations a minute; grants aborted 94-96%; authorize p50 reached 3.7 s | Grants and top-ups are never on the request path. At most one is outstanding per workspace-region-shard, with a cooldown, sized by rate within the trust allowance |
| The ledger's p99 amplified into a fleet-wide Spanner abort storm | Fast admission never touches Spanner. A sharded workspace never falls back to the synchronous path. An unsharded one falls back behind a per-workspace concurrency cap and a per-workspace-region breaker, and sheds with 503 when that path is saturated |
| Ambiguous leases were quarantined, not guessed back into service | A dead owner's lease is never reused. The auditor drains it from the log, keeping its reservation until its holds have ended |

## 8. Rollout

1. **Measure** commits per generation after #1464 and #1465, and authorize's
   timing fields per region.
2. **Gateway load balancer and receipt-key publication** (§4.12), independent
   of the rest.
3. **Python changes that stand alone:**
   - shards that never mix signs (§4.7), which closes a gap on today's path;
   - 503 instead of 402 when a balance's headroom sits in leases;
   - the combined identity in the counter reconciler.
4. **A spike** of the owner, renewals and the auditor on one region:
   - ownership hand-off, and an owner killed mid-stream;
   - the hottest workspace's rate on one owner;
   - Pub/Sub ordering across publishers in one region, the per-key limit, and
     redelivery;
   - the auditor's conditional commits while its members change.
5. **Shadow.** Gateways mirror authorize, heartbeat and settle. A comparator
   reports any difference from Python in decisions, per-authorization charges,
   reaper outcomes and records.
6. **Benchmark gate** (§6).
7. **Pilot:** Joseph's own workspace, then a few large ones, with kill switches
   per workspace, region and cloud.
8. **Widen;** move keyed requests, capped keys, payouts and the remaining route
   types (§4.11) one at a time; then retire the Python hot path.

## 9. Not decided here

- **Tuning values:**
  - lease sizes and allowances per trust tier;
  - the low-water mark, the top-up horizon and the cooldown;
  - the renewal, checkpoint and tick intervals;
  - the skew allowance, and the reaper's grace;
  - the state cache's maximum age, and the shard count rule.

  They come from the spike, the benchmark and the pilot.
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
- **v9.** Codex (7 P1, 4 P2, 1 P3) and Fable (2 P1, 5 P2, 7 P3) found these
  problems in v8:
  - closing an expired lease released money that live holds still needed, and
    a slow owner could keep admitting after the close;
  - Pub/Sub orders messages only within a key, so the auditor could book from
    an incomplete log and subtract its way to negative usage;
  - the owner, a node publishing for an unreachable owner, and the auditor
    could each decide a different terminal, and the auditor's reaps were not
    in the log;
  - the auditor could reap a renewed stream whose heartbeat sat in its
    backlog;
  - adding a key cap ignored the key's open fast holds;
  - pause exposure was stated as `remaining`, which refunds refill;
  - a negative shard beside a positive one let Python spend money the
    workspace no longer had;
  - the auditor had no durable commit protocol;
  - trust caps were per lease;
  - the identity omitted synchronous holds;
  - returns no longer repaid debt;
  - the hottest workspace's fallback repeated the pilot's grant storm;
  - a regional Pub/Sub outage could strand leases that the design said
    another region would close.

  v9 answers them:
  - One ordering key per lease. While a lease is open, its owner is the only
    publisher of its records, so the owner's decisions and the log's order
    agree.
  - An owner admits and decides only before its cutoff. The auditor drains a
    lease only after its expiry plus the skew, and keeps its reservation
    until every hold it could have admitted has ended.
  - The auditor is the only writer of consumption. Its commits are
    conditional on a stored log position, and they store the winners. It
    reaps draining leases at ticks in the log.
  - Keys move to the synchronous path behind a version fence, after their
    fast holds are booked.
  - Pause exposure is the sum of `L − consumed`, and the trust allowance is
    per workspace.
  - Shards never mix signs. Returns repay debt first, through the release
    primitive.
  - One identity covers leases and synchronous holds.
  - Sharded workspaces never fall back to the synchronous path.
  - A region's leases stay reserved while its Pub/Sub is down.
