# Fast admission and batched settlement

Status: **approved by Joseph on 2026-10-03 (v25). Nothing built.** v8 changed
direction to regional leases, Joseph's choice (§2). Codex and Fable reviewed
v1-v25 (§11) and both accepted v25. v26 adds §4.13, how this fits with the
work in flight on the same path, and what that check changed.

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
  production. The gateway sends one when the provider's first byte arrives,
  before relaying it, then one every 10 s or 256 output tokens. Each records
  the delivered usage and renews the reservation. Nothing is charged until
  settle, or until the reaper books the last snapshot of a request whose
  gateway never settled.
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
  - Its records carry money fields only: about 250 bytes each, about 1 KB per
    generation with its heartbeats. Each key is limited to 1 MBps, about
    1,000 generations a second, which sets the shard count of the hottest
    workspaces (§4.3, §6).
  - Each request's full record goes to a second, unordered topic, keyed by
    authorization. Its settle record carries the full record's digest.
  - Every settle, refund, heartbeat and reap record is published before it is
    acknowledged.
  - Subscriptions keep unacknowledged messages for up to 31 days, Pub/Sub's
    maximum, and an archive subscription copies every record to Cloud
    Storage. The archive's lag bounds what a lost subscription can lose
    (§4.8).
- **Lease auditor.** A consumer group on each region's settle log, and the
  only writer of lease consumption to Spanner (§4.8). It applies each lease's
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
- what the auditor has booked against it, and how far it has applied the
  lease's log.

**Consumption and allocation.**

- A lease's allocation starts at L, spread over its donor shards. Returns
  lower it, donor by donor.
- Its consumption is everything booked against it. Overruns can take it above
  the allocation.
- Its remaining allocation is what it still holds in `reserved`: per donor,
  the allocation minus the consumption booked there, never below zero.
  Consumption is booked to the first donor first, and returns come from the
  last donor first.
- Releases, the identity and the allowance use the remaining allocation.
  Consumption beyond the allocation is usage, booked as §4.7 says.
- `release_credit`'s guard checks a shard's whole `reserved`, which includes
  other leases and holds, so it cannot catch one lease releasing too much.
  The auditor's stored per-donor allocation is that check.

**Granting** is one Spanner transaction, never on the request path:

- It reads the workspace's leases under the workspace's range lock, as the
  pilot did, so two regions' grants cannot both pass the check.
- It refuses a grant that would take the workspace's exposure above the trust
  tier's allowance. The exposure is the remaining allocation of its open
  leases and of its draining ones. A draining lease counts only its open
  holds' estimates once an applied final checkpoint or a complete hand-off
  (§4.8) has listed them all.
- The allowance is per workspace, across regions and shards. The carding
  incident of 2026-08 is why.
- It checks the workspace's headroom as the signed sum over its credit
  shards, so debt counts.
- It adds L to `reserved` on donor shards with enough headroom, and records
  the allocation (matched allocation, §4.7).
- It writes the lease row, with the workspace's key-status version (§4.6).

**The owner keeps, in memory:**

| Value | Meaning |
|---|---|
| `held` | the sum of the estimates of open holds |
| `consumed` | the sum of charges booked since the lease began, by settles and reaps |
| `remaining` | the allocation (L, less returns) `− consumed − held` |

- Admission needs `remaining ≥ e`.
- A settle moves an estimate e out of `held` and the actual a into `consumed`.
  A refund moves e out of `held` and nothing into `consumed`. A reap moves e
  out of `held` and the last accepted heartbeat's running charge into
  `consumed` (§4.5), and a release moves e out of `held` and nothing into
  `consumed`. So `consumed` is defined for every terminal the checkpoint audit
  sums.
- An overrun can make `remaining` negative. The owner then admits nothing more
  under the lease.

**Renewals** are the only Spanner writes an owner makes.

- Every few seconds, idle or not, the owner extends each lease's expiry.
- The write is conditional on the lease being open and not revoked, and on
  the owner's epoch.
- In a batch, each lease is its own conditional statement. One that changes
  no row means the lease was revoked or is draining. The owner re-reads it
  and stops using it.
- Each record the owner publishes carries the lease's next **owner sequence
  number**, assigned when the publish is issued. A retry republishes the same
  record with the same number, so a duplicate is recognizable.
- With each renewal, the owner publishes a **checkpoint record** under the
  lease:
  - its cumulative `consumed`, over the terminals with lower sequence
    numbers;
  - its open holds' count, sum and latest end of life;
  - the key-status version it applies, and its open holds of any key being
    moved (§4.6).

**The cutoff.** The owner admits under a lease, decides its holds' outcomes
(§4.5), and issues publishes for it, only while its own clock is before the
lease's expiry minus a skew allowance.

- It reads the clock after recording a hold, and undoes the hold if the
  cutoff has passed.
- After the cutoff it issues no new publish for the lease. Its heartbeats get
  `retry`. It answers a terminal `past_cutoff`, and the front door appends
  that terminal to the drain log at once (§4.5).
- If a renewal then succeeds before the lease drains, the owner adopts its
  drain log before it admits or decides anything again.
- A renewal that keeps failing lets the lease reach its cutoff. The owner then
  stops admitting under it (§4.4).
- The expiry is Spanner's time and the cutoff the owner's, so clock steps
  must stay within the skew allowance (§5).

**Top-ups and closing:**

- When `remaining` falls below a low-water mark, the owner asks for another
  lease.
  - At most one request is outstanding per workspace-region-shard, with a
    cooldown.
  - The amount is sized by the recent rate times a horizon, within the
    allowance.
- An owner stops admitting under a lease when the lease goes idle or reaches
  its maximum life, or when the workspace is paused.
  - It returns the unused remainder, its allocation minus `consumed` and
    `held`, in its next checkpoint record. When the auditor applies that
    record, it releases the remainder from the donors, last donor first, and
    lowers the allocation by it there.
  - It keeps serving the lease's holds. Once none is open, it publishes a
    final checkpoint record and marks the lease draining.
  - The auditor then finishes the lease (§4.8).
- **Retiring.** An owner leaving in a deploy stops admitting, keeps serving
  and renewing its leases until their holds end (at most 2 h 20 min), then
  finishes them as above.
  - The hash ring marks it leaving, so new admissions route to the new owner
    while requests bound by an envelope still reach the old one.
  - This is a hard requirement on the admission service's deploys, like the
    gateways' retirement in §4.12. Old and new owners overlap for up to
    2 h 20 min, so capacity doubles during a deploy.
  - Meanwhile each shard carries the old lease's open holds and the new
    lease's L against the allowance. Small tiers' allowances must admit two
    leases per shard, or successor grants answer 503 until the old lease's
    return is applied.
  - A deploy that stops processes sooner turns every deploy into forced exits,
    which cut streams.
- **A forced exit.** An owner that must exit sooner publishes its open holds in
  hand-off records (authorization, estimate, deadline and last snapshot,
  chunked under the message limit), then a manifest: the number of chunks, a
  digest of their holds, and the sequence numbers they used. It then stops
  deciding and marks its leases draining.
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
  - K is also set by the 1 MBps limit on a lease's ordering key: about 25
    to 30 for the hottest workspace at 100T (§6).
- **Correctness does not depend on exclusive ownership.** Each lease is
  separately reserved money.
  - If a membership change briefly lets two nodes act as owner, each holds its
    own lease. Neither can spend the other's.
  - The cost is a little extra reservation for a while.
- **Requests carry their lease.** Heartbeats, settles and refunds go to the
  owner and lease named in the authorization's signed envelope.
  - While the lease is open, only its owner publishes its records to the log.
  - If the owner cannot be reached, or answers `past_cutoff`, a terminal goes
    to the lease's drain log at once (§4.5), and the front door answers
    `recorded`. A heartbeat it cannot deliver gets `retry`, which stops the
    stream; the stream then settles, into the drain log if need be.
  - For a terminal, a front door that cannot reach an owner first tries a peer
    front door. A heartbeat tries a peer only within a sub-second cap, since
    its own budget is 5 seconds. A front door that cannot reach several owners
    leaves the ring, so a partitioned front door does not route a healthy
    owner's terminals into Spanner.
  - A front door that keeps failing to reach an owner can revoke the lease's
    renewal in Spanner. The lease then expires, and the log takes over
    (§4.8). Revocations are rate-limited per lease and per front door, so one
    flapping link cannot drain the fleet's leases.
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
   - the hold, its deadline rule and its end of life;
   - the `stage_d` payload Python's answer carries today (eligibility, the
     candidate prices and the cap), decided by today's rule (§4.11). The
     enclave heartbeats a stream only when it is there.

   The end of life is absolute, from the owner's clock at admission. The
   gateway ends the stream at the earlier of it and its own 2 h 15 min.

**A warm fast authorization reads and writes no store:** its workspace is
eligible, its caches are fresh and its lease has room. A cold cache reads
Spanner, and heartbeats and terminals write Pub/Sub synchronously. A streaming
request's first heartbeat then waits for its publish, as it waits for a
Spanner commit today.

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
  - The change covers the insufficient-credit precheck too
    (`credit_exhaustion_precheck`, #1461). It reads the same headroom,
    `total_credits − total_usage − reserved`, and remembers an exhausted
    workspace per process, so a balance held in leases would look exhausted
    to it.
  - A wrong 402 is also cached downstream: the enclave suppresses a
    credential's requests for a window after an `insufficient_credits` 402
    from authorize (`billing_backoff.go`). A 503 is not cached.

### 4.5 One authorization

**Who decides.** For each authorization `A`, the first terminal (settle,
refund or reap) in its lease's order wins: the owner's records by their
sequence numbers, then the lease's drain log (below). The owner, the auditor
and the request records all follow that rule.

- While the lease is open, its owner is the only publisher of its records.
  - It handles `A`'s heartbeats and terminals under a per-`A` lock.
  - It publishes only the winner, and answers after the publish is
    acknowledged.
  - So its memory follows log order.
  - If a publish fails, it resumes the paused ordering key and republishes
    the same records, with the same sequence numbers, before anything new.
    Until a publish succeeds again it admits nothing new under the lease:
    every new stream's first heartbeat would fail, and each would hold its
    estimate for 2 hours (below). It keeps renewing and adopting meanwhile;
    a terminal it cannot publish is answered with an error, and the gateway
    retries it as today.
  - Its publishes are bounded by a deadline shorter than the reaper's grace
    minus twice the skew allowance. The deadline covers the request and the
    client library's queued retries, not only the caller's wait. A republish
    after a failure is issued before the cutoff too, so every owner publish
    is received by the cutoff plus the deadline, which is what the fence time
    allows for (§4.8).
  - A publish that timed out can still be stored later. So the auditor does
    not rely on the deadline alone: a draining lease stores an owner boundary
    (§4.8), and an owner record beyond it is ignored.
- The owner answers a later terminal for `A` with the winner's outcome, and
  does not publish it. It charges nothing.
- An answer that depends on the owner's decision is given only if the publish
  was acknowledged before the owner's cutoff.
  - Otherwise the answer is `recorded`, and the lease's order decides.
  - The gateway needs only to know that the record is durable.
- **The drain log.** A terminal the owner does not take goes to the lease's
  drain log, a Spanner table. A front door appends it, with its money fields
  and its full record's digest, when the owner cannot be reached or once the
  lease is draining. It then answers `recorded`.
  - Rows are keyed by lease, authorization and record ID, and ordered by
    their Spanner commit timestamp, then record ID. Appends are independent
    inserts with no counter row, so a crashed owner's streams all settling
    at once do not queue on one row.
  - A lease's terminals have one order: the owner's records by their
    sequence numbers, then the drain log. The first terminal for an
    authorization in that order wins. Reaps of a draining lease are
    appended to the drain log too (§4.8).
  - **Adoption.** At every renewal the owner reads its lease's drain log for
    its open holds. The read is read-only and outside the renewal's
    transaction, with a cursor on commit timestamp, so it stays small and
    takes no locks the appends would wait on. A front door that appends also
    tells the owner, best-effort; the notice only prompts a read. The reaper
    reads a hold's rows again before reaping it, as the backstop.
  - Adoption goes through the same per-`A` lock and winner check as a terminal
    that reaches the owner directly, so a hold already decided is not
    decided again. The owner adopts the first row for a hold, by commit
    timestamp then record ID, and publishes it as its own record, carrying
    that row's record ID. So the auditor recognizes the drain-log copy by
    identity, as well as by authorization.
  - An append is conditional, in its own transaction, on the lease not being
    closed. A refused append is answered as today's
    `_already_settled_gateway_data` answers an authorization that is already
    terminal, field for field (cost, disposition, outcome):
    - from A's stored winner. Winners are kept at least 7 days after the
      lease closes, longer than the enclave's queue can hold a settle (§4.8).
      A kept lease that the auditor closed, with no winner for A, released
      A's hold uncharged at close, and the answer says so, as `released`. A
      lease an operator closed because its records were beyond recovery
      (§4.8) answers `pending` instead, since the request may have run;
    - after that, from the authorization's written records (§4.9);
    - when neither has a winner, as today's helper answers an outcome it
      cannot establish: `pending`, never released. A missing record is never
      read as a refund. A `pending` answer is `already_settled` without
      `settled`, so it ends the enclave's retries, and the enclave logs a lost
      charge even when the charge is booked and only its record awaits
      rebuilding. That is intended; the loss metric is read with it in mind.

    That is a 200, which ends the enclave's retries; `stageDDispositionLost`
    then counts a loss only for a reap or a release. A 409 would not end
    them: the queue retries any error.
  - A row-deletion policy removes a lease's rows after it closes. The drain
    log lives in the multi-region database, with the leases.
  - Appends happen only when an owner is unreachable, has stopped or is past
    its cutoff, so one Spanner transaction per terminal there is affordable.
  - A heartbeat for a draining lease is refused with 409 and a new reason,
    `lease_draining`. Nothing renews a hold whose owner has stopped.
  - The enclave's handling, at quill-cloud-proxy `a06050f`:
    - A heartbeat gets three attempts, the later two on a 502, 503 or 504,
      within its 5-second budget (`authorizeRetry`, from the `Heartbeat` call
      in `internal/trustedrouter/stage_d.go`). A 409 is not retried.
    - If the heartbeat still fails, the stream ends (`sendHeartbeatLocked` and
      `markHeartbeatLostLocked` in `cmd/enclave/stage_d.go`). The enclave then
      sends a settle with the usage it delivered, not a refund (`BeforeTerminal`
      in `main.go`). A settle that fails goes to its in-memory retry queue
      (`settlementRetries`).
    - The first heartbeat is the exception. If it fails, before any byte
      reaches the client, the enclave answers 503 and sends no settle
      (`main.go`). Nothing was delivered, and the hold ends at its deadline.
  - So refusing a draining lease's heartbeats needs no enclave change beyond
    logging the new reason. It is also why a heartbeat that fails through its
    retries stops a stream today, including under the heartbeat kill
    switch's 503.
  - Owners retire through deploys without draining (§4.2), so this cuts
    streams only after a crash, a revocation or a forced exit. That is a cost
    today's design does not have: a Python instance's crash cuts no stream,
    since the state is in Spanner.
- **`retry`** is a 503. For a heartbeat, once its retries are spent, it stops
  the stream, which then settles. A terminal is answered `retry` only when the
  drain log refuses it or Spanner is unavailable. The enclave then retries the
  settle from its queue (`settlement_retry.go` at `a06050f`):
  - six attempts, with delays of 0, 0.5, 1, 2, 4 and 8 s between them,
    15.5 s in all;
  - each attempt has its own 28-second transport budget;
  - one queue worker serves every queued settle, so a job can also wait
    behind others.

  That queue is the whole budget, so a Spanner outage longer than it loses
  those settles (§4.8).
- **A stream's hold before its first heartbeat.** Today such a hold waits out
  the 2-hour reservation time (`GATEWAY_RESERVATION_TTL_SECONDS`). Here it
  would also hold the trust allowance, so a short blip could refuse a small
  workspace's grants for hours. Releasing it sooner needs the enclave:
  - Today the enclave sends the first heartbeat only when the provider's
    first byte arrives (`selectedRoute.Ready()` in `serveStreaming`). An
    allowance would then have to bound the provider's time to first byte:
    minutes for reasoning models, with a tail. That tail, and any provider
    incident beyond it, would release live streams whose first heartbeat is
    then refused after a long, already-paid wait.
  - So the release waits for a heartbeat at stream open, before the provider
    answers, a quill-cloud-proxy change (§9). An enclave that sends it
    declares that in its Stage D boot registration too, and the release
    applies only to holds admitted for such a boot. Then a streaming hold
    with no accepted heartbeat by the first-heartbeat allowance plus the
    heartbeat grace is released uncharged by its owner. A draining lease's
    heartbeats are refused, and its holds end as §4.8 says, so the release
    does not apply there, and a hand-off record need not carry the boot's
    declaration.
    The allowance is the authorize-to-heartbeat latency, with the provider's
    latency out of it. The fast path admits only streams the enclave
    heartbeats (§4.11), so this never releases a stream that runs without
    heartbeats.
  - "Accepted" means durable: a first heartbeat that was stored but whose
    answer was lost leaves a hold with a heartbeat, which is reaped at its
    snapshot, as today. Without a durable heartbeat the enclave delivered
    nothing and sends no settle, so nothing can still be owed.
  - Until then every streaming hold keeps today's 2 hours. A burst of
    first-heartbeat failures holds those estimates against the trust
    allowance for up to 2 hours, so a small workspace can get 503s with
    `Retry-After` for that long. That is the cost of not cutting live
    streams.
- Non-streaming holds never heartbeat, and keep today's 2-hour reservation
  time.
- **A Spanner stall longer than the expiry window cuts streams fleet-wide.**
  Renewals fail, leases reach their cutoff, and heartbeats get `retry`. Today
  a stall longer than a heartbeat's 5-second budget already does this, since
  every heartbeat is a Spanner commit. The expiry window is sized as a
  multiple of Spanner's observed commit-stall tail (§9).

**Terminals.** A settle moves e out of `held` and a into `consumed`. A refund
moves e out of `held`.

**Heartbeats** are validated, and only accepted ones are published under the
lease. A heartbeat is answered after its publish is acknowledged.

- Today's validity rules carry over (`heartbeat_gateway_atomic`):
  - a lower sequence, or the same sequence with another hash, is stale;
  - the same sequence and hash is a replay, answered with the deadline
    already granted, without a second publish;
  - a changed endpoint, regressed usage, usage beyond the authorized tokens,
    or a running charge above the hold's cap is rejected.
- A signed deadline comes back with each answer. A heartbeat whose publish is
  acknowledged after the deadline it echoes is answered `deadline_passed`.
- The owner keeps each hold's latest valid snapshot in memory. The log keeps
  them all.

**Reaping is ordered in the log.** A reaped hold is charged its last valid
snapshot, as today's reaper does in production. (`scripts/deploy/rollout.sh`
sets `TR_REAP_SNAPSHOT_BOOKING_ENABLED=true`; the code default is false.)

- **In money, that is the last accepted heartbeat's running charge.** The
  owner prices each heartbeat once, through the same frozen fee layers as a
  settle: the receipt fee and the app markup on top of the usage
  (`delivered_usage_charge_microdollars` in `storage_gcp_stage_d.py`). Every
  accepted heartbeat record carries that running charge, as today's heartbeat
  answer does (`running_micro`).
- Every terminal record, reaps included, carries its money fields as a settle
  does. The auditor books a record's amount and never prices a record itself,
  so the owner's reap and the auditor's reap of one hold charge the same
  amount, and the checkpoint audit compares like with like.

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
    bookings. It advances the key's window counters too, as `release_key`
    does.
  - Revocation reaches admission through the key-status cache, whose maximum
    age is short and stated as the exposure. A bulk deletion (#1496) is many
    revocations at once and reaches admission the same way.
  - Deleting a key removes its entities and keeps its `tr_key_limit` rows
    (`SpannerApiKeys.delete`), so the auditor can still book a deleted key's
    usage. A key row that is missing is handled as
    `_release_key_or_skip_deleted` handles it today.
- **Adding a cap, a window limit or `budget_strict`** moves the key to the
  synchronous path.
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
  - the remaining allocation on that shard of every open or draining lease;
  - plus the unsettled synchronous holds on that shard.
- Every term is a Spanner value.
- Both the lease audit and the existing counter reconciler
  (`storage_gcp_counter_reconcile.py`, which counts request holds but not
  leases) check this one identity.

**Booking** uses matched shard allocation. A lease's booked consumption raises
`total_usage` and lowers `reserved` on the same donor shard, so that shard's
headroom is unchanged.

**A negative shard is covered at once, or marks every shard.**

- Consumption beyond the allocation is booked as usage on the lease's first
  donor shard.
- Any write that leaves a credit shard negative covers it in the same
  transaction, moving credit from the workspace's other shards' headroom.
- Only if the workspace's signed sum is negative does the write instead mark
  every shard row of the workspace in debt.
- Every writer that can leave a shard negative does this:
  - a settle or reap that overruns, and a lease booking beyond its
    allocation;
  - federated settlement, which books on shard 0
    (`storage_gcp_federated_settlement.py`);
  - debits and chargebacks.
- **How a settle covers its shard.** Its update of its own row returns the
  new headroom (`THEN RETURN`). Only if that is negative does the transaction
  then take donor rows, in ascending shard order.
  - Two settles going negative on different shards can wait on each other.
    Spanner's wound-wait aborts one, which retries.
  - Covering stays rare when a workspace's shard count follows its balance,
    as the credit-shard convoy incident recommended
    (`docs/incidents/2026-09-25-credit-rebalance-convoy.md`). A workspace
    whose overruns are routine has too many shards for its balance.
- A reservation or a grant refuses a marked row. `reserve_credit` gains one
  condition on the row it already updates, so the hot path still touches one
  shard.
- **Money coming in repays debt first.** One shared primitive does it for
  every way money comes in: a release on a marked row, and the writers that
  spread credit today through `distribute_credit_amount`:
  `_credit_workspace_balance_tx` (payments, grants, auto-refill),
  `_credit_across_shards` (credit-transfer returns), the earnings-to-credit
  transfer, and the shard-admin credits. Into a marked workspace, it repays the
  negative shards first, in ascending order, in the same transaction. When
  the signed sum is no longer negative, the mark is cleared on every row, and
  only then is the rest spread over the shards as today
  (`distribute_credit_amount`). A customer who pays is never left behind a
  mark.
- **Lock order.** New multi-row writers (covering, repayment, marking) take
  credit rows in ascending order after their own row, then key rows.
  - Existing single-row attempts keep their order, such as authorize's scan
    of shards in random order.
  - Where two transactions lock rows in opposite orders, Spanner's wound-wait
    aborts one, which retries.
  - The cold paths that take the largest donor first
    (`rebalance_credit_for_estimate`, `_debit_escrow`) change to ascending
    order.
- So no shard is negative unless every shard is marked, and Python's
  per-shard check means the same as the signed sum.
- Today an overrun stays on the hold's shard. That shard can go negative
  while another stays positive, and Python can then spend the positive shard
  though the workspace has nothing left. A one-time pass covers or marks the
  workspaces already in that state (§8).

**Returns repay debt first.**

- Releasing a lease's remaining allocation goes through today's release
  primitive (`release_credit`).
- On a marked row, freed money repays the negative shards first. It then
  absorbs unrecovered payment claims, and the recovery pause is re-evaluated
  (`absorb_unrecovered_recovery_tx`).
- An overrun lowers the signed headroom that grants read.

**Pause, revoke, trust downgrade, or a switch out of fast mode:**

- Spanner refuses new grants for the workspace.
- Owners learn of it from the state cache or a pushed control message, within
  the cache's maximum age. They then stop admitting and close the
  workspace's leases (§4.2).
- **Exposure:** at most the sum of `allocation − consumed` over the
  workspace's open leases, within the trust allowance.
  - Not `remaining`: refunds free held capacity, which new admissions can use
    until the owner stops.

**Home settlement.** Deferred usage from a peer plane is booked to Spanner
unconditionally, as today. When `available` falls below the outstanding
leases, no lease is granted or topped up, and the owners close their leases.

### 4.8 The auditor, draining and owners that stop

The **lease auditor** is a consumer group on each region's settle log.
Pub/Sub gives each lease's records to one member at a time, in log order.

**It is the only writer of lease consumption.** Python still books
synchronous holds, through `settle_atomic`.

- Per lease, every few seconds, one Spanner transaction stores what any
  member needs in order to carry on:
  - the consumption booked since the last commit (§4.7), with per-key usage,
    and the remaining allocation;
  - each open hold the log has shown, with its estimate, its latest valid
    snapshot (sequence, hash, usage and running charge) and its deadline;
  - the winners decided since the last commit, keyed by authorization, each
    with its pending work: its request records (for a refund or a release,
    its disposition record) and amount-sensitive side effects (§4.9);
  - its progress: the highest owner sequence number applied, and the last
    tick applied;
  - a commit version, which every commit advances.
- The transaction is conditional on the commit version it read, and it
  stores the winners in the same transaction as their bookings. The commit
  version is the guard: a member that stalled cannot commit over another's
  work, since its commit fails and it re-reads. Within that, a member
  recognizes a duplicate terminal from the winners it has loaded.
- Only then does it acknowledge the records.
- **Taking over a lease.** A member loads the progress and the open holds
  first, extending the records' acknowledgement deadlines while it loads.
  - It skips a redelivered owner record at or below the stored sequence
    number.
  - It recognizes another publisher's record by what it is for: a terminal by
    its authorization (a terminal for an authorization with a winner charges
    nothing), a tick by its number.
  - While a lease is open, only its owner publishes terminals, and never two
    for one authorization. So the stored winners are needed only once the
    lease is draining, and are loaded then.
- Only the member holding the lease publishes its ticks, numbered on from the
  stored last tick. It skips a tick at or below that number.
- A gap in an owner's sequence numbers stops the lease's processing and
  alerts.
- One transaction can carry many leases, each its own conditional statement.
- Winners are stored packed, one row per lease per commit. A row-deletion
  policy removes them once the lease is closed, their pending work is done,
  and 7 days have passed. Spanner's policies delete on a timestamp column, so
  each pack's is set when both have happened, and a pack whose work never
  completes keeps it unset and stays.
  - That pending work leaves every winner a written record: a charged winner
    its generation and activity records, a refund or a release a compact
    disposition record with its boot binding (§4.9).
  - The 7 days outlast the enclave's queue, which holds a settle for at most
    about 52 hours: 1,024 jobs on one worker, each with at most six attempts
    of up to 28 seconds (§4.5).

**It audits the owners.**

- At each checkpoint record, the owner's cumulative `consumed` must equal the
  sum of the terminals it published with lower sequence numbers. A
  republished duplicate counts once. Adopted terminals are owner records, so
  they are audited the same way; their drain-log copies are recognized by
  record ID when the drain log is applied.
- A difference is a fault. The auditor alerts and revokes the lease.
- The audit proves the owner's accounting consistent, not its prices right: an
  owner that priced every record wrongly, but consistently, would pass it.
  A heartbeat's running charge, and so a reap's, is capped at its hold; a
  settle can overrun its hold and is booked in full, as today (§4.2). Two
  checks cover pricing: the
  shadow comparator compares every per-authorization charge with Python's
  before cutover (§8), and afterwards a sampled job reprices records from
  their archived inputs (usage, frozen prices and fee terms) and alerts on any
  difference.

**Expiry.**

- A lease not renewed by its expiry plus the skew allowance is marked
  draining by the auditor.
- That transaction is conditional on the lease still being open, with the
  renewal the auditor read.
- By then the owner's cutoff has passed, so the owner neither admits nor
  decides under the lease.

**Draining.** A draining lease keeps its remaining allocation reserved until
every hold it could have admitted has ended, at a terminal or a reap:

- **If the auditor has applied the owner's final checkpoint record, or a
  hand-off manifest and every chunk it names,** the holds they list are all
  there are. A partial hand-off counts as none.
- **Otherwise,** the auditor knows the holds the log shows through heartbeats.
  Holds it cannot see end by the lease's expiry plus the 2 h 20 min maximum
  life, plus the grace.

Records that arrive meanwhile are booked against that reservation, terminals
by the first-terminal rule.

- A draining lease's heartbeats are refused (§4.5), so nothing extends a
  deadline the auditor has stored.

**The drain log and reaping.**

- The auditor publishes ticks under the lease, each carrying its time.
- While a lease is open, the auditor leaves its drain log to the owner.
- It reads a draining lease's drain log only after applying a tick it
  published once its own clock passed F plus the skew allowance, and after
  storing S. Owner records up to S come first.
- When a lease drains, its **fence time** F is stored with it: the expiry
  plus the skew allowance plus the owner's publish deadline. Every draining
  transaction stores it, the auditor's and the owner's alike (a final
  checkpoint or a forced exit), from the lease's last renewed expiry. So no
  draining lease lacks F. F has one use: a fence tick, the auditor's or a
  rebuild's, is published only once its publisher's clock passes F plus the
  skew allowance.
- **The owner boundary S.** When the auditor applies its fence tick, it first
  stores S, the highest owner sequence number it has applied, in the lease
  row, with T, the publish time Pub/Sub gave the tick as delivered. It does
  that before it appends any reap or decides any drain-log terminal.
  - Whoever stores S, the auditor or a rebuild, stores it with T in the
    per-lease commit that advances progress to S, carrying the bookings and
    winners of every record up to S. So whenever S is set, every owner
    record up to it is booked, and one that arrives later is a duplicate of
    a record applied. Records after the auditor's fence tick are therefore
    either above S, and ignored, or already booked.
  - S is written once, conditional on being unset. A member that takes over,
    or a rebuild, finds it set and uses the stored value. Its own commit of
    anything above S fails the commit-version guard, and it re-reads.
- An owner record above S is ignored, whenever it arrives, in ordinary
  processing and in a rebuild alike. A late terminal loses, and a late
  heartbeat or checkpoint changes nothing; a remainder a late checkpoint
  returned is released at close anyway.
- S follows the order Pub/Sub received the records in. Their publish times do
  not, since servers' clocks differ, so a stored boundary is the only rule
  live processing and a rebuild can share. Owner sequence numbers are issued
  in order, and a republish reuses its number, so a record received after the
  tick is above S.
- It reads drain-log rows up to its read timestamp, in commit-timestamp
  order. A row committed later has a later timestamp, so none is missed.
- At a tick past a hold's deadline plus the grace, it reaps the hold by
  inserting a reap row, in a read-write transaction that first reads the
  hold's drain-log rows. A concurrent append for the same hold conflicts with
  that read, and one of the two retries. A terminal already there wins.
- It books a draining lease's winners in that order: owner records, then the
  drain log.

**Close.**

- The auditor closes a draining lease only after applying a tick that it
  published once the drain's end condition held. Everything published before
  that tick has then been applied, including terminals for holds the auditor
  never saw.
- The close transaction also reads the lease's drain log beyond what the
  auditor has applied. If it finds a row, it aborts, and the auditor applies
  the row first. An append checks, in its own transaction, that the lease is
  not closed. The two conflict, so an acknowledged append is never left
  behind by a close.
- It books what remains, releases the remaining allocation through the
  release primitive (§4.7), and marks the lease closed.
- A record for a closed lease charges nothing, as a settle after today's
  reaper does.

A new owner never reuses a dead owner's lease. It is granted a new one.

**Retention.**

- A lease whose records cannot be read stays reserved, and nothing is
  released from it. That covers a region whose Pub/Sub is down and a stopped
  auditor.
- Unacknowledged records wait for up to 31 days. The archive subscription
  copies every record to Cloud Storage on its own, so a stopped auditor
  loses nothing.
- A lease whose records expired unread by the auditor, or whose owner
  sequence numbers show a gap, does not close by the ordinary path. It stays
  reserved, and an operator rebuilds it from the archive.
  - The archive keeps each record's publish time and message ID. It does not
    keep the order between publishers, since Cloud Storage export does not
    preserve it across files.
  - A rebuild starts from the auditor's stored winners and progress, and never
    decides an authorization that already has a winner.
  - It needs no order between publishers. Owner records are ordered by their
    sequence numbers, and those above the stored boundary S are ignored. A
    draining lease's later terminals and reaps are in its drain log in
    Spanner, in order, which does not expire.
  - **Completeness first.** A rebuild decides nothing until the archive holds
    every record received before a fence tick: the one stored with S, or,
    when S was never stored, one the rebuild publishes under the lease
    itself. The archive is complete once the archive subscription's oldest
    unacknowledged message was published later than that tick's publish time
    T plus a margin, or there is none in a sample taken after then: that
    subscription acknowledges a message only after the object holding it is
    finalized. Until then the lease stays pending.
    - The margin assumes Pub/Sub's servers' clocks agree within seconds,
      which is a different assumption from the nodes' skew allowance.
    - The export writes Avro with message metadata, since the publish time
      and the message ID are what the fence and the gap check read.
    - The oldest-unacknowledged-message age is a sampled metric, minutes
      behind, so the check allows that margin. One stuck message holds every
      rebuild's check; rebuilds are rare enough for that wait.
  - If S was stored, every owner sequence number up to S must be present. A
    missing number is a true gap, and the lease stays reserved for an
    operator.
  - If S was never stored, nothing depends on it yet: no reap was appended and
    no drain-log terminal decided. The rebuild publishes its tick as the
    auditor would, once its clock passes F plus the skew allowance. Once the
    archive is complete through that tick, it holds every owner record
    received before it, which includes every publish the owner had
    acknowledged. The rebuild stores S as the highest owner sequence number
    with every number below it present, and T as its tick's publish time. It
    then proceeds as the auditor would.
  - The rebuild stores what it books as winners, so a later resumption of
    the ordinary path agrees with it.
- Records unreadable for longer than 31 days, in a regional outage that long,
  are beyond recovery. Such a lease stays reserved until an operator closes
  it.
- **A lease the auditor never drained,** because it stopped while the lease
  was open, is first marked draining by the rebuild, with the same
  conditional transaction, once the expiry plus the skew has passed. That
  stores F, and the rebuild then proceeds as above. Stored winners and the
  commit-version guard apply throughout.
  - If its owner is still renewing, the operator first revokes its renewal,
    so the lease can expire. When the auditor was down longer than the
    subscription's retention, the archive, not the subscription, is the
    source.

**When an owner's region is lost.**

- **If Pub/Sub in that region still works,** an auditor run from another
  region drains the region's leases from the log.
- **If Pub/Sub in that region is down,** records acknowledged there cannot be
  read until it recovers. Its leases stay reserved, and draining waits.
  Nothing is released from them meanwhile. The region's owners cannot publish,
  so they stop admitting.

**What can be lost:** a request whose gateway could not get a settle or
heartbeat into the log within its retry budget. That is what happens today
when Python is unreachable: the first durable point is Python's
`tr_settle_outbox`, written when the settle reaches Python.

- Such a request is charged as a reap, at its last snapshot, or not at all.
- That includes a settle refused while Spanner is unavailable for longer than
  the enclave's retry queue lasts (§4.5). The queue holds 1,024 settles per
  enclave and drops the oldest when full (`settlement_retry.go` at
  `a06050f`), so an outage that fills it loses settles before their attempts
  run out.
- A durable settle outbox in the enclave would close this. It is a
  quill-cloud-proxy change (§9).

### 4.9 Settle durability, records and side effects

- **Records before acknowledgement.** Settle, refund, heartbeat and reap
  records go to the lease's log before they are acknowledged, and a draining
  lease's terminals to its drain log.
  - If the publish fails, the answer is an error, and the gateway retries as
    today.
  - Nothing is applied to a lease without its record.
- **Request records.** Whoever records a settle, the owner or a front door,
  first publishes the full request record to the unordered record topic,
  keyed by authorization, and waits for its acknowledgement. Only then does it
  record the settle, on the lease's log or in its drain log, with the full
  record's digest.
  - A stream's first heartbeat also publishes its full authorization record
    (model, endpoint and frozen prices). The two publishes go out together,
    and the heartbeat is answered after both are acknowledged, so the first
    byte waits for one round trip, not two.
  - The first heartbeat record itself carries what a reap's request record
    needs: the model, endpoint, frozen prices, fee and markup terms,
    workspace and key. Later heartbeats carry only their snapshot and running
    charge. So a reap never depends on the authorization record having
    arrived, which only adds detail.
  - The record topic's subscription is acknowledged only after staging, so a
    ClickHouse outage shorter than the subscription's 31-day retention loses
    nothing. The record topic's Cloud Storage export is the backstop: a
    staged record lost in ClickHouse is rebuilt from it. While a winner's
    records wait, disposition lookups answer from the stored winner.
  - A consumer stages the full records in ClickHouse, keyed by
    authorization.
  - The auditor writes each authorization's generation and activity record
    from its stored winner and the staged record with the winner's
    authorization and digest, after the commit that stored the winner. An
    authorization can have two staged records, an original and an enclave
    retry's compacted copy, so the join is never by authorization alone.
  - A reap has no full record from a gateway. The auditor builds one, from the
    first heartbeat record (model, endpoint, frozen prices, fee and markup
    terms, workspace and key) and the reaped snapshot's usage and running
    charge, names the heartbeat record it came from (its owner sequence), and
    publishes it to the record topic before the reap's outcome, which names
    its digest. So a reap is rebuilt from the record topic's export alone,
    like a settle.
  - It first publishes the winner's outcome to the record topic: the
    authorization, the outcome, the cost, the boot binding, and the digest of
    the winning terminal's full record. The digest picks the winner among the
    archived full records: an enclave's retry can carry a compacted record
    with a different digest than the original, since its queue keeps only the
    billing identity and measured usage (`compactSettlementRetryJob` in
    `settlement_retry.go` at `a06050f`), and both are archived. So once the
    pack is deleted, every written record can still be rebuilt from the
    topic's Cloud Storage export, as staged records are. A winner's pending
    work is done only once that publish is acknowledged and its records are
    written.
  - The two message kinds on the record topic, full records and outcomes,
    carry a `kind` attribute, which the staging consumer and the export
    reader both read. An outcome republished after a crash is identical to
    the first, so the export may hold duplicates, and readers keep one.
  - A lookup that finds no winner and no record for an authorization whose
    lease the auditor closed reports it, and a periodic check compares
    outcomes with records. Either one rebuilds the missing record from the
    export. Until then lookups answer `pending`.
  - A staged record is removed only once its winner's records are written,
    or once its lease has closed with no winner that needs it, as for losing
    and refunded terminals. A staged record never expires while the auditor
    is behind.
  - The pending work is stored with the winner, so a crash between the two
    leaves it to be done (§4.8). Writes are idempotent on `A`.
  - Amount-sensitive consumers act once per winner: budget alerts,
    auto-refill, metadata webhooks, routing feedback and route-fallback
    reports.
- **Lookups.** Disposition and evidence lookups answer from the stored winner
  while it is kept, then from the records, with ClickHouse within the records
  bound for `gateway_request_id`, and `pending` when neither has one, as
  today's helper does. That covers winners from the drain log and from
  closed leases: the enclave looks a disposition up after a settle times
  out, and counts a loss when it reads `reaped_snapshot` (`main.go` at
  `a06050f`).
  - The owner mints A with its lease in it, so a lookup by A alone reads that
    lease's packs, bounded by the commits in the lease's maximum life, and
    the records, which are keyed by authorization. A is `gwa-`, the lease
    ID and a random suffix, at most 64 bytes, which `tr_reservation`'s and
    `tr_gateway_authorization`'s authorization columns and the ClickHouse keys
    hold; the lease ID's length sets the suffix's. Today A is `gwa-` and a
    UUID (`_new_gateway_authorization_id`), and no consumer parses it.
  - A refund or a release writes no generation record, as today, so the
    auditor writes it a compact disposition record instead: the
    authorization, its outcome and its boot binding, which a boot-signed
    lookup checks. A release's outcome is `released` in its winner, its
    record and every answer. Today's values are `settled`, `reaped_snapshot`
    and `refunded`. The enclave counts a loss from `already_settled` without
    `settled`, and from a lookup only for `reaped_snapshot`, so a new value is
    safe. So after the packs are deleted, every winner still has a
    record, and only an authorization the auditor never saw answers
    `pending`.
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

Credit-funded keys on standard catalog routes go first: requests that do not
stream, and streams the enclave heartbeats. These stay on today's Python path:

- streams the enclave does not heartbeat. The owner decides that with
  today's rule (`_stage_d_eligibility_reason` in `gateway.py`) and its
  inputs:
  - the same kill switches and pilot set (`stage_d_eligibility_enabled`,
    `stage_d_heartbeat_enabled`, `stage_d_pilot_workspaces`);
  - the request's verified Stage D boot signature, from a boot whose
    registration declared that it heartbeats. A verified boot alone does not
    prove it: at `a06050f` a boot in the spend-lease shadow mode registers
    without heartbeats on (`initializeSpendLeaseShadow`), and the Stage D
    signer falls back to that key (`stageDBootDigestSigner` in
    `spend_lease.go`).
    - The enclave sends the declaration when it registers (§9), computed from
      `stageDConfig.usageHeartbeat` on the one registration path its boot
      runs, so a boot declares nothing it does not do. At `a06050f` there are
      two paths, Stage D and spend-lease shadow (`main.go` skips the Stage D
      path when a spend-lease flag is set). At `29be0fdd` there is a third:
      in speculation shadow mode (§4.13) a boot registers through the Stage D
      path even with heartbeats off. Its periodic re-registration runs from
      that same path, so a boot's declaration changes only with a new boot.
      Python stores it on the boot record, `GatewayBoot`, from the
      registration route (§8). There are two declarations, heartbeats and the
      heartbeat at stream open, and a missing one means undeclared.
    - A re-registration replaces the declarations with what it sends. Today's
      `observe_gateway_boot` keeps fields the new registration omits, so it is
      not reused for them.
    - Until registrations carry it, every stream stays on Python. Python's own
      rule reads the same field behind a setting, turned on only when every
      digest in the accepted image list belongs to a build that declares. That
      closes the same gap on today's path; reading it sooner would end today's
      Stage D for every enclave;
  - streaming `chat.completions` or `responses`, priced in credits on
    standard endpoints, outside the priority and auto service tiers. The
    enclave applies the same route test (`stageDStreamEligible` in
    `stage_d.go`).

  A stream the rule refuses goes to Python, so a streaming `/v1/messages`
  request stays: `serveMessages` relays it without heartbeats. Releasing a
  hold before its first heartbeat (§4.5) assumes the stream heartbeats.
  The owner applies the switches as Python does: with heartbeats disabled it
  answers heartbeats `retry`, which ends running streams as today's 503 does
  (§4.5); with eligibility disabled it admits no new streams on the fast
  path and keeps serving those it admitted;
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
- **Scale-in needs an application-aware retirement.**
  - An instance chosen for removal fails the load balancer's health check, so
    it gets no new connections.
  - It also closes the connections it already has, since keep-alive
    connections outlive the health check: `Connection: close` or GOAWAY when
    a connection is idle, and 503 only for a request that arrives after that.
  - It keeps its in-flight requests and their settlements running, and exits
    only after they end, up to 2 h 15 min. Today's enclave shutdown cancels
    requests after 90 seconds, so this is a quill-cloud-proxy change (§9).
  - The load balancer's own connection draining stops at one hour, so it is
    not relied on.
  - GCE's autoscaler does not wait that long, so a small controller retires
    instances this way, or the autoscaler stays scale-out only
    (quill-cloud-proxy #432).
- Receipt keys move off DNS discovery, which has an instance-termination gap.
  Boot-registry registration, with attestation history, becomes the publication
  path, required before an instance takes traffic.

### 4.13 Work in flight on the same path

Checked on 2026-10-03 against quill-router `26780a22` and quill-cloud-proxy
`29be0fdd`. The enclave facts this design cites at `a06050f` still hold there
(`stageDStreamEligible`, `serveMessages` without heartbeats, the settle queue
and the heartbeat attempts), except the registration paths, which §4.11 now
lists.

- **Speculative invocation** (`docs/speculation-protocol-v1.md`). The enclave
  starts the provider request before authorize answers, under a signed grant
  that lasts at most 30 seconds and carries a bounded number of permits;
  output waits for the ordinary authorize. It is in shadow only: the router's
  observer (#1457) and the enclave's coordinator (quill-cloud-proxy #439) are
  both merged with their modes off. It hides authorize's latency and leaves
  its Spanner commits; fast admission removes both. So:
  - **A request takes one or the other.** A workspace on the fast path gets
    no speculation grants. Its authorize already answers in milliseconds, and
    its unsettled exposure then has one bound, the lease allowance, not two
    to be added up. (A grant's own bound is
    `min(tier ceiling / 100, paid headroom / 10, $1)`, and the protocol
    leaves "all other issued rights" to its caller.)
  - Speculation keeps its value for what stays synchronous (§4.11), and
    until fast admission ships.
  - A grant's paid headroom never counts leased money: leases sit in
    `reserved`, and the issuer reads `total_credits`, `total_usage` and
    `reserved` (`storage_gcp_speculation_shadow.resolve`).
  - Both rest on Stage D heartbeats: a grant's route must be Stage D, and the
    fast path admits only heartbeated streams.
  - The enclave decodes authorize and settle answers, errors included. Their
    bytes are frozen in `tests/fixtures/speculation_v1/` (#1429, #1485), and
    the compiled service must serve the same bytes (§8).
- **The Python path's own diet.** Authorize folded its pause read into the
  credit reserve (5 round trips, `c74e79b9`), settle became one commit
  (#1465), and its counter releases joined that commit's batch (#1456, 6
  operations). On 2026-10-03 the path cost about 2.3 commits per generation,
  down from 4.3 two days earlier. This is the path the synchronous cohort
  keeps (§4.11) and the reference the shadow comparator checks against, so
  its statements are the ones the compiled service must match.
- **The trust-tier job** (#1484, #1491) selects its candidates from one
  snapshot, in shadow. The trust allowance (§4.7) reads the tier it
  maintains, and nothing in that job depends on leases.
- **Key management at scale** (#1496, open) pages the key list and deletes
  keys in bulk. §4.6 says how deletions reach admission.

## 5. Invariants

Each has a production check.

1. **Admission bound.** Every fast admission is a hold against a lease whose
   amount Spanner has reserved. The sum of open holds and settled charges never
   exceeds the lease's allocation, except for overruns. Returns lower the
   allocation, per donor, before anything is released.
2. **Conservation.** The per-shard identity in §4.7 holds after every booking.
   Each checkpoint record equals the terminals its owner published with lower
   sequence numbers.
3. **One terminal per authorization.** The owner decides what it takes, and
   publishes only winners, so log order agrees with it. A terminal it does
   not take, because it is unreachable, past its cutoff or gone, goes to the
   drain log in Spanner, as do a draining lease's reaps. The first terminal
   in the lease's order wins: owner records up to the stored boundary S,
   then the drain log.
4. **No charge lost.** Every terminal is in the log or the drain log before it
   is acknowledged.
   A draining lease keeps its reservation until the auditor has applied a
   tick published after every hold it could have admitted has ended. A lease
   with unread or missing records never closes by the ordinary path.
5. **No charge invented.** Only boot-signed settles, and reaps at the last
   validated heartbeat's snapshot, charge.
6. **No lease is reused after its owner stops.** A new owner gets a new lease.
7. **Ownership is routing, not safety.** Two owners can never spend the same
   reserved money.
8. **Key caps.** Capped keys are served synchronously. A new cap takes effect
   only after every fast hold of the key is booked.
9. **Pauses** stop admission within the state cache's maximum age. Exposure is
   at most the sum of `allocation − consumed` over open leases, within the
   workspace's trust allowance.
10. **Renewals and bookings are conditional:** renewals on lease state and
    epoch, bookings on the auditor's commit version. Replays change nothing.
11. **Debt marks every shard.** No credit shard is negative unless every
    shard row of the workspace is marked in debt, and a marked row admits
    nothing.
12. **Latency** is stated as percentiles. Excursions take the documented
    fallback or a 503.

**The windows Target 4 allows:**

- the state cache's maximum age, for pauses and revocation;
- a clock wrong, or stepped, by more than the skew allowance, which could let
  an owner admit or decide after its lease drains;
- overruns, which are booked as debt (§4.7);
- requests whose gateway could not reach the log within its retry budget
  (§4.8);
- an owner record received after a tick that reaped its hold, which only
  loses, as Decision 70 allows;
- a drain-log settle committed between the owner reaper's read and its
  publish: milliseconds, after which the reap, an owner record, comes first.
  Decision 70 covers it, as it covers today's reaper race.

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
- commits do not grow with requests, but row writes do. Winners are packed
  one row per lease per commit, so what grows is the request records: about
  two rows per generation, about 66,000 a second at 100T;
- at about 2,000 row writes a second per node, a multi-region figure, that is
  about 33 nodes of the `nam6` instance (`infra/spanner_trusted_router.tf`).
  At $3.705 per node-hour, that is roughly $90,000 a month, or about 2% of the
  routing margin. Moving
  request records to ClickHouse (§9) removes it.

**Pub/Sub load:**

- about 1 KB of money records per generation: a settle and about three
  heartbeats, about 250 bytes each, plus checkpoint records and ticks;
- a lease's records share one ordering key, limited to 1 MBps, so a lease
  carries about 1,000 generations a second. The hottest workspace, about
  24,000 a second at 100T, needs K of about 25 to 30;
- the full request records go to the unordered topic, which has no per-key
  limit;
- the pre-first-byte heartbeat waits for its publish, as it waits for a
  Spanner commit today.

**After an owner crash,** its leases' streams stop at their next heartbeat
and settle into the drain log within one heartbeat interval. For 2,000 open
streams on a lease, that is about 200 independent inserts a second. Each
request makes its first settle call itself, so those appends are concurrent.
Only settles that fail go to the enclave's single queue worker, one Spanner
commit each.

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
   timing fields per region. Measured on 2026-10-03, hour ending 10:00 UTC:
   about 81,500 commits for about 35,800 settles, 2.3 per generation, before
   #1456 deployed.
2. **Gateway load balancer and receipt-key publication** (§4.12), independent
   of the rest.
3. **Python changes that stand alone:**
   - the heartbeat declarations on `GatewayBoot` and in the registration
     route, stored as sent and replaced on re-registration, a missing one
     meaning undeclared, and the setting that has Python's own rule read them
     (§4.11);
   - the debt mark on every shard, and covering a negative shard at once
     (§4.7), which closes a gap on today's path, with a one-time pass over
     workspaces that already have a negative shard;
   - **a shard count that follows the balance**, Joseph's decision, which the
     convoy incident deferred: new workspaces on one shard, splitting as they
     grow, with a one-time consolidation. Without it, covering runs on
     routine overruns of small, many-shard workspaces
     (`DEFAULT_NEW_BILLING_SHARDS = 16` today);
   - 503 instead of 402 when a balance's headroom sits in leases, in the
     reserve and in the insufficient-credit precheck (§4.4);
   - the combined identity in the counter reconciler.
4. **A spike** of the owner, renewals and the auditor on one region:
   - ownership hand-off, and an owner killed mid-stream;
   - the hottest workspace's rate on one owner;
   - Pub/Sub ordering across publishers in one region, the per-key limit,
     record sizes, and redelivery;
   - the auditor's conditional commits while its members change, and what it
     writes: open holds, pending work, bytes, and restoring a lease's state
     on takeover and during draining.
5. **Shadow.** Gateways mirror authorize, heartbeat and settle. A comparator
   reports any difference from Python in decisions, per-authorization charges,
   reaper outcomes and records, and in the answer bytes the enclave decodes,
   which include the error envelopes frozen in `tests/fixtures/speculation_v1/`.
6. **Benchmark gate** (§6).
7. **Pilot:** Joseph's own workspace, then a few large ones, with kill switches
   per workspace, region and cloud. The first cohort is requests that do not
   stream. Streams join once the enclave sends the heartbeat declaration and
   registrations carry it (§4.11).
8. **Widen;** move keyed requests, capped keys, payouts and the remaining route
   types (§4.11) one at a time; then retire the Python hot path.

## 9. Not decided here

- **Tuning values:**
  - lease sizes and allowances per trust tier;
  - the low-water mark, the top-up horizon and the cooldown;
  - the renewal, checkpoint and tick intervals;
  - the skew allowance, and the reaper's grace;
  - the expiry window, as a multiple of Spanner's observed commit-stall tail
    (§4.5);
  - the first-heartbeat allowance, against the measured authorize-to-heartbeat
    latency, once the enclave heartbeats at stream open (§4.5);
  - the state cache's maximum age, and the shard count rule.

  They come from the spike, the benchmark and the pilot.
- **Keyed requests on the fast path** need a durable per-scope claim. It is a
  later step.
- **Enclave changes (quill-cloud-proxy), Joseph's call:**
  - a durable settle outbox, which would close the loss in §4.8;
  - a heartbeat at stream open, which the release of a stream's hold before
    its first heartbeat waits for (§4.5);
  - heartbeats on the Messages path (`serveMessages`), which would bring
    streaming `/v1/messages` onto the fast path (§4.11);
  - declaring heartbeats, and later the heartbeat at stream open, in the
    Stage D boot registration, which fast streaming admission requires
    (§4.11);
  - the retirement phase in §4.12, without which gateway scale-in stays
    scale-out only.
- **Where request records live at 100T:** ClickHouse rather than Spanner
  (§6).
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
- **v10.** Codex (4 P1, 4 P2) and Fable (2 P1, 5 P2, 8 P3) found these
  problems in v9:
  - the auditor's knowledge of open streams lived only in a member's memory,
    so a member change could release a stream that ran;
  - heartbeats on a draining lease were renewed without today's validity
    rules;
  - an overrun made a lease's unbooked amount negative, which inflated the
    allowance and asked the release primitive to release a negative amount;
  - a draining lease could close at a wall-clock horizon before the auditor
    had applied a settle published before it;
  - records could expire unread during a long regional outage;
  - a record ID is not a cursor, publishes could fail ambiguously, and
    winners could be deleted before their request records were written;
  - federated settlement and other writers could leave a negative shard
    without the marker;
  - stopped leases held their unused remainder against the allowance;
  - the 1 MBps per-key limit was not computed;
  - the enclave has no durable settle outbox;
  - load-balancer draining stops at one hour.

  v10 answers them:
  - The auditor's per-lease commit stores open holds with their snapshots and
    deadlines, winners with their pending work, and its progress. It is
    conditional on that progress.
  - Owner records carry sequence numbers assigned when the publish is
    issued. Failed publishes are republished with the same numbers.
  - A draining lease's heartbeats are refused. Owners retire through deploys
    without draining.
  - Consumption and remaining allocation are separate. Releases, the identity
    and the allowance use the remaining allocation.
  - A lease closes only after the auditor applies a tick published after its
    drain ended.
  - Leases with unread or missing records stay reserved for an operator, and
    an archive subscription keeps every record.
  - A negative shard is covered at once from the other shards, or marks every
    shard in debt. Every writer is listed.
  - Stopping leases return their unused remainder.
  - Money records are about 250 bytes, the full records go to an unordered
    topic, and K is about 25 to 30 for the hottest workspace.
  - The loss without an enclave outbox is stated.
  - Gateways retire in an application-aware way.
- **v11.** Codex (4 P1, 4 P2) and Fable (1 P1, 5 P2, 6 P3) found these
  problems in v10:
  - the commit condition did not cover batches of front-door records, so two
    members could book the same terminals;
  - returns released allocation without lowering it, so a later release could
    take another lease's reservation;
  - draining leases' unknown holds dropped out of the allowance;
  - a hand-off had no completeness marker;
  - money arriving in a marked workspace never cleared the mark;
  - covering a negative shard in the settle path broke the stated lock order;
  - deploys need old owners kept for up to 2 h 20 min;
  - the enclave contract for a refused heartbeat was not stated;
  - the auditor's state was unsized;
  - a settle could reach the log before its full record, and reaps had no
    record inputs;
  - the archive loses the order between publishers;
  - a failed health check does not stop requests on existing connections.

  v11 answers them:
  - A commit version every commit advances, with winners inserted in the
    same transaction as their bookings.
  - Allocation per donor, lowered by returns.
  - Draining leases count their remaining allocation until an applied final
    checkpoint or a complete hand-off lists their holds. Hand-offs end with a
    manifest.
  - Every way money comes in repays negative shards first and clears the
    mark. Covering follows the own row, then donors in ascending order, and
    stays rare when shard count follows balance.
  - Owner retirement is a hard deploy requirement, with a leaving state in
    the hash ring.
  - The enclave stops and settles on any refused heartbeat (`stage_d.go`), so
    refusing a draining lease's heartbeats needs no enclave change.
  - Takeover loads progress and open holds first. Winners are packed one row
    per lease per commit, and request records' Spanner cost is estimated.
  - Full records are acknowledged before money records, and a stream's first
    heartbeat publishes its authorization record.
  - An archive rebuild uses an order-free rule for other publishers'
    terminals.
  - Gateways get an application retirement phase, a quill-cloud-proxy change.
- **v12.** Codex (1 P1, 1 P2) and Fable (1 P1, 3 P2, 7 P3) found these
  problems in v11:
  - the archive rebuild's order-free rule could change who won: it could
    charge a refunded request, or re-decide a stored winner;
  - staged full records could expire before a late auditor joined them;
  - the enclave contract was asserted without evidence, and `retry` was
    undefined;
  - nothing made shard count follow balance, so covering would run on
    routine overruns;
  - a stream's first heartbeat serialized two publishes;
  - and, among the P3s, the inflow writers, winner deduplication,
    connection close at scale-in, the price basis, donor order, staging
    lookups and the deploy-time allowance.

  v12 answers them:
  - A draining lease's terminals and reaps go to a per-lease drain log in
    Spanner. Its order follows the owner's sequence numbers, so a lease has
    one order, and a rebuild reproduces the live outcome.
  - Staged records are removed only once their winners' records are written,
    or their lease has closed without needing them.
  - The enclave's handling is cited at quill-cloud-proxy `a06050f`, and
    `retry` is defined.
  - A shard count that follows balance is a decision for Joseph before
    covering ships.
  - The authorization record and the first heartbeat publish together.
  - The P3s are answered in place.
- **v13.** Codex (2 P1, 1 P2, 1 P3) and Fable (1 P1, 2 P2, 7 P3) found these
  problems in v12:
  - an owner publish that timed out could still be stored after the drain's
    tick, and a rebuild would then let it beat the reap that won live;
  - a close was not fenced against a drain-log append committed after the
    auditor's last read;
  - the drain log's per-lease counter row would serialize a crashed owner's
    settles, and the enclave's retries of about 30 s would drop the tail;
  - settles between an owner's death and its lease draining depended on
    those retries;
  - a Spanner stall longer than the expiry window cuts streams fleet-wide,
    which was not stated;
  - concurrent first-heartbeat publishes could leave a reap without its
    record;
  - the enclave retries heartbeats three times on 502, 503 and 504, which the
    doc misstated;
  - and, among the P3s, the fence on the auditor's clock, the append
    condition, drain-log deletion, Invariant 4's wording, the record topic's
    backstop and `retry`'s meaning.

  v13 answers them:
  - The auditor stores the owner frontier when the lease drains. Owner
    records above it lose, live and in a rebuild.
  - The close transaction re-reads the drain log, and appends check that the
    lease is not closed.
  - The drain log is ordered by commit timestamp, with no counter row.
  - A front door that cannot reach an owner appends a terminal at once,
    whatever the lease's state. The owner's reaper adopts a terminal it finds
    there.
  - The Spanner-stall case is stated, and the expiry window is sized against
    Spanner's commit-stall tail.
  - The first heartbeat record carries what a reap needs.
  - The enclave's retries are cited exactly, with the first-heartbeat
    exception.
- **v14.** Codex (1 P2, 1 P3) and Fable (2 P2, 7 P3) found no money defect in
  v13. Their findings:
  - a terminal appended while the owner was briefly unreachable was adopted
    only at the hold's reap time;
  - an owner past its cutoff had no defined answer for a terminal, so its
    settles could die in the enclave's retry queue;
  - a rebuild had no branch for a lease with no stored frontier;
  - the enclave's heartbeat attempts and settle retries were misstated;
  - and, among the P3s, partitioned front doors, the reaper-versus-append
    race, Invariant 3's wording, late heartbeats and checkpoints, the first
    heartbeat's hold, and auditing adoptions.

  v14 answers them:
  - The owner reads its drain log at every renewal and adopts what it finds,
    carrying the row's record ID. Front doors tell it best-effort.
  - An owner past its cutoff answers `past_cutoff`, and the front door
    appends at once.
  - A rebuild with no frontier waits for draining, applies the archive's
    owner prefix up to its first gap, and stores the frontier first.
  - The enclave's timings are cited exactly.
  - The P3s are answered in place.
- **v15.** Codex (1 P1) and Fable (1 P1, 1 P2, 6 P3) found these problems in
  v14:
  - the rebuild for a lease with no stored frontier froze a frontier from an
    archive that could lag, or miss a suffix still awaiting export, so an
    acknowledged settle could be ignored forever;
  - a stream whose first heartbeat failed kept its estimate for two hours,
    which here would also hold the trust allowance;
  - and, among the P3s, adoption reads inside the renewal's transaction,
    adoption outside the terminal path, `retry` for a closed lease, peer
    forwarding against the heartbeat's budget, an owner resuming after a late
    renewal, and the enclave's drain time after a crash.

  v15 answers them:
  - The frontier becomes a fence time F stored when the lease drains. Owner
    records Pub/Sub received after F are ignored, live and in a rebuild,
    since the archive keeps the publish time.
  - A rebuild waits until the archive subscription's oldest unacknowledged
    message is newer than F, which proves the archive complete through F.
  - A streaming hold with no accepted heartbeat is released uncharged after
    the first-heartbeat allowance.
  - The P3s are answered in place.
- **v16.** Codex (1 P1, 1 P2, 2 P3) and Fable (1 P2, 7 P3) found these
  problems in v15:
  - publish times follow servers' clocks, not the order Pub/Sub received the
    records in, so live processing (received before the tick) and a rebuild
    (published by F) could disagree about a late owner settle;
  - a 409 for a closed lease is retried by the enclave's queue;
  - the first-heartbeat allowance has to cover the provider's time to first
    byte, since the enclave heartbeats only then;
  - and, among the P3s, the tick's receipt time, the archive's format, the
    sampled completeness metric, F on owner-initiated drains, renewing owners
    during a rebuild, republish bounds, durable heartbeat acceptance, and
    concurrent first settles.

  v16 answers them:
  - The auditor stores an owner boundary S, in receipt order, before anything
    depends on it. Live processing and a rebuild both use S.
  - A rebuild sets S only when none was stored, after proving the archive
    complete through F.
  - A closed lease's terminal gets today's settle-after-reaper answer, which
    ends the enclave's retries.
  - The first-heartbeat allowance is per route, from measured first-byte
    latency, and a heartbeat at stream open is listed as an enclave change.
  - The P3s are answered in place.
- **v17.** Codex (1 P1, 1 P2) and Fable (2 P2, 5 P3) found these problems in
  v16:
  - streaming `/v1/messages` was in the first cohort, but the enclave never
    heartbeats it, so releasing a hold before its first heartbeat would free
    a running stream;
  - a closed lease answered `settled` false even when the authorization's
    settle had won, so the enclave would log a charged settle as lost;
  - a per-route first-byte allowance would turn the provider's slow tail and
    its incidents into refused streams after long, already-paid waits;
  - a rebuild still proved completeness through F, by publish times, rather
    than through a tick in the order Pub/Sub received the records;
  - and, among the P3s, which commit stores S, writing S only once, routes
    with no measured first byte, and F's remaining uses.

  v17 answers them:
  - The fast path admits only streams the enclave heartbeats, by today's
    Stage D rule. Streaming Messages stays on the Python path.
  - A closed lease answers from the stored winner, field for field, as
    `_already_settled_gateway_data` does today.
  - Releasing a stream's hold before its first heartbeat waits for the
    enclave to heartbeat at stream open. Until then streaming holds keep
    today's 2 hours, and no per-route allowance is needed.
  - A rebuild proves the archive complete through a fence tick: the one
    stored with S, or its own.
  - S is written once, in the commit that advances progress to it, and F's
    one use is named where F is defined.
  - The P3s are answered in place.
- **v18.** Codex (1 P2) and Fable (2 P2, 4 P3) found no money defect in v17.
  Their findings:
  - a closed lease answered "released uncharged" for an authorization whose
    winner had already been deleted, so a late retry of a charged settle
    would be logged as lost;
  - "streams the enclave heartbeats" was asserted by route, while the enclave
    heartbeats only when the answer carries the `stage_d` payload and it
    booted with heartbeats on;
  - a rebuild's S could include owner records received after its tick, so it
    was not the receipt-order boundary the live path keeps;
  - and, among the P3s, the settle queue's capacity, lookups for drain-log
    and closed-lease winners, an owner admitting while its publishes fail,
    and what T is.

  v18 answers them:
  - A closed lease answers from the stored winner while it is kept, then from
    the written records, and as released uncharged only when neither has a
    winner. Winners are deleted only after their records are written.
  - The owner applies today's Stage D rule with its inputs: the kill
    switches, the pilot set and the verified Stage D boot, which an enclave
    registers only when it heartbeats. The envelope carries the same
    `stage_d` payload. The release before a first heartbeat applies only to
    boots that declare the stream-open heartbeat.
  - Whoever stores S, the auditor or a rebuild, stores it in the commit that
    books every record up to it. So a record at or below S that arrives later
    is a duplicate, and one above S is ignored, live and in a rebuild alike.
  - The P3s are answered in place.
- **v19.** Codex (2 P2) and Fable (1 P2, 5 P3) found no money defect in v18.
  Their findings:
  - a verified boot does not prove the enclave heartbeats: a boot in the
    spend-lease shadow mode registers without heartbeats on, and the Stage D
    signer falls back to its key;
  - once a lease's winners are deleted, a refund has no record to answer a
    lookup from, and a miss in the records was answered "released";
  - and, among the P3s, refunds after deletion, the lookup key, the kill
    switches' effect on running streams, the auditor's share of the early
    release, and what an owner whose publishes fail still does.

  v19 answers them:
  - Fast streaming admission requires a boot whose registration declares
    that it heartbeats, an enclave change; until it ships, streams stay on
    Python.
  - Winners are kept at least 7 days after close, beyond the enclave's
    queue. Before deletion every winner has a written record: generation
    and activity records when charged, a compact disposition record with its
    boot binding for a refund or a release. A miss answers `pending`, as
    today's helper does, never released.
  - A is minted with its lease in it, so a lookup by A finds the lease.
  - The early release is the owner's alone.
  - The P3s are answered in place.
- **v20.** Fable (1 P2, 5 P3) found no money defect in v19. Codex's round 19
  stalled before its verdict; its notes pointed at the same place as the
  last point below. The findings:
  - the heartbeat declaration had nowhere to live: `GatewayBoot` and the
    registration route carry no capability, so either every stream stayed on
    Python with no way to tell when that ended, or an absent field was read
    as declared;
  - and, among the P3s, leases an operator closed, the outcome value for a
    release, the column the row-deletion policy reads, A's shape, and the
    pilot's first cohort.
  - From Codex's notes: a disposition record written once to ClickHouse
    could not be rebuilt after its pack was deleted.

  v20 answers them:
  - The declarations, heartbeats and the heartbeat at stream open, are
    stored on `GatewayBoot` from the registration route, a standalone Python
    change in §8; a missing one means undeclared. Python's own rule reads the
    same field once every accepted image sends it.
  - The auditor publishes every winner's outcome to the record topic before
    its pending work is done, so the Cloud Storage export can rebuild any
    lost record.
  - The P3s are answered in place, and the enclave's lookup is described as
    it is: after a settle times out, counting a loss only for
    `reaped_snapshot`.
- **v21.** Fable accepted v20 (no money defect, 5 P3). Codex (1 P2) found
  that the archived outcome could not say which of two archived full records
  won: an enclave's retry carries a compacted record with a different digest,
  and both are archived. It also noted that re-registration must replace the
  declarations as sent, which today's `observe_gateway_boot` merge does not.

  v21 answers them:
  - The archived outcome carries the winning terminal's full-record digest.
  - A re-registration replaces the declarations; Python's own rule reads them
    behind a setting turned on once every accepted image declares, and the
    enclave computes the declaration on both registration paths.
  - Fable's P3s: the record topic's `kind` attribute and duplicate outcomes,
    who rebuilds a lost record, the columns A must fit, and the lost-charge
    log a `pending` answer causes.
- **v22.** Fable accepted v21 (no money defect, 3 P3). Codex (1 P2) found
  that a reap had no full record in the record topic, so a reap's lost records
  could not be rebuilt from the export after its pack was deleted.

  v22 answers them:
  - The auditor publishes a full record for each reap, built from the first
    heartbeat record and the reaped snapshot, before the outcome that names
    its digest.
  - The live join from a winner to its staged record is by authorization and
    digest, as the rebuild is.
  - A boot registers, and re-registers, through one path, so its declaration
    changes only with a new boot.
- **v23.** Codex accepted v22 (no findings). Fable (1 P1, 1 P3) found that
  reaps were priced without the fee layer today's reaper applies: a heartbeat
  record carried usage and prices but not the receipt fee or the app markup,
  so the auditor's reap undercharged by them, and an owner's reap, priced with
  the envelope's fees, would disagree with the auditor's sum at the next
  checkpoint and revoke a healthy lease.

  v23 answers them:
  - The owner prices each heartbeat once, through the same fee layers as a
    settle, and every accepted heartbeat record carries that running charge,
    as today's heartbeat answer carries `running_micro`. A reap charges the
    last accepted heartbeat's running charge.
  - Every terminal record carries its money fields; the auditor books a
    record's amount and never prices one itself.
  - A reap's full record carries the fee terms and names the heartbeat record
    it was built from.
- **v24.** Codex and Fable both accepted v23 with no defect. Their closing
  notes: the owner's terminal arithmetic named only settles and refunds
  (Fable), and the checkpoint audit proves accounting, not pricing (Codex).

  v24 answers them:
  - A reap moves its estimate out of `held` and the last accepted running
    charge into `consumed`; a release moves it out and books nothing.
  - The pricing limitation is stated, with its two checks: the shadow
    comparator before cutover, and a sampled repricing job after it.
- **v25.** Codex (1 P2) found that v24's "each charge is still capped at its
  hold" contradicted settle overruns, which are booked in full. v25 scopes the
  cap to heartbeat running charges and the reaps that use them.
- **v26.** Joseph approved v25 on 2026-10-03. v26 checks the design against
  the work in flight on the same path and adds §4.13. What the check changed:
  - a workspace on the fast path gets no speculation grants, so its unsettled
    exposure has one bound;
  - the 503-instead-of-402 change covers the insufficient-credit precheck
    (#1461), whose answer the enclave caches;
  - at quill-cloud-proxy `29be0fdd` a third registration path exists
    (speculation shadow), which the declaration rule covers;
  - deleted keys keep their `tr_key_limit` rows, so the auditor can book their
    usage;
  - the shadow comparator also checks the answer bytes the enclave decodes;
  - §8's first measurement is recorded: 2.3 commits per generation.
