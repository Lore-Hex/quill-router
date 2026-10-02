# Fast admission and batched settlement

Status: **proposed, v2, 2026-10-02. Nothing built.** v1 was reviewed by Codex
and by Fable; together they found 21 problems (§10), all folded into this
version. Joseph's decisions are in §2. **One decision is still open: adding a
TigerBeetle cluster per region as the per-request ledger (§4.2).**

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
calls the Python control plane on Cloud Run synchronously. Each call is one or
more multi-region Spanner transactions:

- `POST /internal/gateway/authorize`: one atomic transaction (`authorize_atomic`
  in `storage_gcp_authorize.py`). It claims the idempotency scope, holds credit
  and the key limit, and writes the reservation and authorization rows.
- Stage D heartbeats while a stream runs. In production every workspace is in
  the Stage D cohort (`rollout.sh` renders an empty pilot list).
- `POST /internal/gateway/settle`: one commit on the success path since #1465.
  It books the actual cost, releases the holds, and writes the generation record
  and the analytics intent.

| Measured | Value |
|---|---|
| Spanner commits per generation, 2026-10-01 | 4.3, before #1464 and #1465 removed about two |
| Authorize p50, us-central1 | 0.155 s |
| Authorize p50, europe-west4 | about 1.6 s (cross-Atlantic Spanner, structural) |
| Settle p90 | 3.1 s (four serial commits; #1465 made it one on the success path) |
| Settles that overrun their estimate | 12.2%; $45.57 a day; 96% of overrun dollars in 0.7% of settles |

Per-request work in the billing database is what costs. The fix is to stop
doing it, without giving up any guarantee the per-request transactions give
today.

## 2. Decisions

**Made by Joseph, 2026-10-02:**

- Batched settlement and regional admission: approved, with a target of under
  about 10 ms of request overhead. This supersedes the earlier rule against
  regional leases for general users, for this design.
- A compiled authorize and settle service, not Python. The gateway is Go
  (`enclave-go`), so the service is Go and shares the gateway's contract types.
- An L4 load balancer in front of the gateways (§4.11).
- No Spanner committed-use discounts.
- Shard ClickHouse later.

**Open, for Joseph: the per-request ledger.** Recommended: one TigerBeetle
cluster per region (§4.2). The alternative is a regional Spanner instance per
region. It closes the same correctness gaps with tools we already run, but needs
two single-region commits per request (p50 about 5-10 ms each, to be measured).
So it misses the 10 ms target and keeps a commit per request.

**A reversal to be explicit about.** `billing-typed-counters.md` §2 rejected
leases and ledgers because they demote Spanner from system of record and add a
second store with asynchronous reconciliation. This design accepts that cost on
purpose, for latency and unit cost. It limits the cost in four ways:

- Spanner stays the system of record for balances, payments, debt and pauses.
- The ledger holds only escrowed money.
- Reconciliation is fenced and audited continuously (§5).
- Losing a region's ledger has a stated, bounded cost (§4.2).

## 3. Targets

1. **Latency, measured at the gateway** from sending authorize to holding a
   routing decision and a hold: p50 under 5 ms, p90 under 10 ms. Tail
   excursions fall back to the synchronous path, which is slow but correct. They
   include p99, ledger view changes and upgrades.
2. **Billing-database commits grow with active workspaces, not requests.**
3. **No charge lost and none booked twice,** the spine of
   `durable-settle-outbox.md`. A request that ran is never released free, and a
   request that never ran is never charged.
4. **Spending is bounded by money already reserved,** with the overrun and
   home-settlement exceptions stated in §5.
5. **Every step can be switched off** per workspace, region and cloud, with an
   epoch-fenced drain (§4.7).

## 4. Design

### 4.1 Shape

Per region:

- **Admission service:** stateless Go, behind the gateway. Any node serves any
  workspace. A consistent-hash ring by workspace is only a cache-locality hint,
  never a correctness dependency.
- **Regional ledger:** a TigerBeetle cluster holding only money (§4.2).
- **Sweeper:** one leader-elected worker. It moves budget between Spanner and
  the ledger and rolls charges back into Spanner (§4.5).
- **Analytics publisher:** settles publish generation records after the ledger
  commit (§4.9).

Python remains the owner of routing policy and of everything off the hot path.

### 4.2 The regional ledger (recommended: TigerBeetle)

TigerBeetle is an open-source (Apache-2.0) financial ledger with a Go client.
Its primitives match this problem:

- accounts with posted and pending debits and credits;
- pending transfers with a timeout;
- `post_pending_transfer` for at most the pending amount, which releases the
  rest;
- `void_pending_transfer`;
- atomic linked chains and idempotent transfer IDs;
- `debits_must_not_exceed_credits`, which rejects a transfer when
  `debits_pending + debits_posted + amount > credits_posted`.

**Accounts, per region:**

| Account | Flags | Role |
|---|---|---|
| Pool | none | Counterparty for budget grants and returns |
| Workspace budget | `debits_must_not_exceed_credits`, `history` | Funded by grants; every admission is a pending debit on it |
| Key budget, for lifetime-capped keys | `debits_must_not_exceed_credits` | A regional sub-budget of the key's remaining cap (§4.6) |
| Workspace overdraft | none | Takes overruns and settles that arrive after a hold expired; rolled up as usage |

**Why it closes the v1 gaps:**

- Every admission is a durable pending transfer, so the admission bound holds by
  construction across node loss and ring changes.
- An authorization's terminal transfer has one ID, shared by settle, Stage D
  booking and refund, so exactly one of them wins.
- Idempotent IDs give exactly-once posting without sequence numbers.
- Hot workspace rows stop contending, because the ledger applies transfers
  serially.

**Operational shape and risks:**

- **Topology:** six replicas across three zones of the region, on local SSD.
  Ephemeral Local SSD on GCP is acceptable only because of six-way replication.
  Cluster size cannot change after creation, so it is sized at creation.
- **Durability:** there is no backup or export tool. Durability is replication,
  and recovery needs a healthy cluster, so losing a majority of a region's
  replicas loses that region's ledger.
- **Loss bound:** at most one rollup interval of charges plus the in-flight holds
  per workspace in that region, because everything older is already in Spanner
  (§4.5). The rollup interval is chosen for that bound: seconds for large
  spenders.
- **Upgrades** go one version at a time, and clients are never newer than
  replicas (the Go client is pinned to the cluster version in CI). Each upgrade
  makes the cluster unavailable for seconds, during which admission falls back
  to synchronous.
- **Sessions:**
  - The limit is 64 by default, with the oldest idle session evicted, and each
    session has one request in flight.
  - The client never times out and retries forever. The admission service sets
    its own deadline and treats a timed-out call as an unknown outcome: the hold
    may still land, and it will expire.
  - Requests are batched per session. All clients (admission, sweeper, rollup,
    reconciliation) stay well under the session limit.
- **Unavailability:** when a region's cluster is down, admission fails over to
  the synchronous path. Settles for holds in that cluster are retried until it
  returns. The sweeper returns no budget from that region while it cannot read
  the ledger.

**Gate:** before any production traffic, benchmark the full path on the real VM
and disk shape. That includes the linked hold, Stage D heartbeats and settle. A
published cloud benchmark of an unoptimised deployment measured p50 32 ms and
p99 over 500 ms, so the target is not assumed (§6).

**Alternatives.**
- **Regional Spanner instance per region:** the same closures with tools we run
  already, at about 5-10 ms per commit and two commits per request.
- **Raft log embedded in the Go service:** TigerBeetle's latency class, but we
  would own the ledger semantics, snapshots, membership, repair and their
  testing. For money code on this timeline, that is the wrong trade.

### 4.3 Authorize

1. **Verify the boot.** Check the request's boot signature (the per-boot Ed25519
   key in the boot registry, `gateway_boot.py`) and that the boot's image digest
   is accepted. Boot, workspace and key state come from caches with a maximum
   age (§4.7).
2. **Route and estimate.** Evaluate the compiled routing snapshot (§4.8) and
   compute the estimate as today. Without `max_tokens`, the estimate assumes 512
   output tokens, which is why overruns are common.
3. **Hold.** One linked `create_transfers`:
   - a pending debit on the workspace budget;
   - for a capped key, a pending debit on the key budget.

   Both have a timeout of hours. That matches today's reservation TTL: 2 hours,
   and 26 hours for native batch.
4. **Answer with a signed envelope.** It carries the authorization ID, the
   prospective generation ID, the frozen candidates, prices, fees and app terms,
   the snapshot version, the hold amounts and the expiry. The gateway echoes it
   on heartbeat, settle and refund. So no per-request envelope store exists, and
   pricing at settle is a pure function of the envelope.

**IDs and idempotency:**

- **Time-based transfer IDs, not hashes.** A rejected transfer (for example
  `exceeds_credits`) makes its ID fail forever (`id_already_failed`), so an ID
  derived from the idempotency scope could never be admitted after one refusal.
- **Where the scope lives:** the scope hash goes in `user_data_128`, a
  fingerprint hash in `user_data_64`, and the snapshot version in
  `user_data_32`.
- **Replay:** a retry with the same scope finds the hold by `query_transfers`
  and replays it. A different fingerprint answers 409, as today. On
  `id_already_failed`, the service mints a new attempt ID.
- **The authorization ID is the workspace hold's transfer ID,** so replay does
  not depend on which node answered first. The generation ID stays
  `generation_id_for_authorization`, a deterministic function of the
  authorization ID.
- **Native batch stays synchronous,** because its scopes are claimed across
  regions today.

### 4.4 Settle, refund and Stage D

**The terminal transfer.** Each authorization has one terminal transfer ID. It
is a pure function of the authorization ID, shared by settle, Stage D snapshot
booking and refund. The first to commit wins. A later one with the same ID gets
`exists` or `exists_with_different_*` and reads the winner with
`lookup_transfers`. That is the same "recorded winner" contract the gateway
relies on today.

- **Settle:** verify the envelope and the boot signature, and price the usage
  from the envelope. Then one linked chain posts `min(actual, hold)` on the hold
  under the terminal ID. If the actual exceeds the hold, the chain also books
  the excess as a single-phase debit on the workspace overdraft account, under a
  deterministic ID. That is today's bounded-overdraft rule
  (`billing-typed-counters.md`). Settle answers `finalized` only after this
  commits.
- **Settle after the hold expired:** the post fails, so the whole charge is
  booked as a deterministic-ID debit on the overdraft account. Expiry never
  turns into a free request.
- **Refund:** void the hold with the terminal ID.
- **Stage D, in the fast path from the start,** because every workspace is in
  the cohort. A pending transfer cannot be partly posted and stay open. So each
  heartbeat snapshot is a linked pair: post the delivered delta on the current
  hold, and open a new hold for the remaining cap.
  - Delivered usage is charged as it streams, and a dead gateway leaves only the
    undelivered remainder to expire. That replaces today's `reaped_snapshot`
    booking.
  - The cap check (running cost at most the hold) needs no extra state.
  - Each heartbeat costs two ledger transfers; the benchmark includes them.

**Responses and lookups.** The settle response reports the ledger outcome,
including the winner's cost when it loses. Disposition lookups read the ledger
by authorization ID. `GET /generation` reads ClickHouse, which is eventually
consistent within a stated delay T (§4.9). A receipt carrying `gen` is
unaffected.

### 4.5 Budget: grants, returns and rollup

The sweeper does all Spanner work, per workspace and region, off the request
path.

**Grant:**
- One Spanner transaction moves headroom into `reserved` and writes a grant row.
- Then a pool-to-workspace transfer, whose ID is the grant ID, makes the money
  spendable. A crash replays to `exists`.
- **Source:** grants come from one credit shard's headroom under the per-shard
  predicate (`credit-row-sharding-handoff.md`), with consolidation for large
  grants. Returns go back to the donor shard.
- **Size:** about a minute of the workspace's recent spend in the region, capped
  at a share of its available balance.
- **No grant** while the workspace is paused, has unrecovered debt, or falls
  below a minimum balance. Those workspaces stay synchronous.

**Return:**
- A `balancing_debit` from the workspace budget to the pool takes only free
  funds: credits minus posted and pending debits.
- The same Spanner transaction releases that amount from `reserved` and absorbs
  recovery debt, as `release_credit` does today
  (`absorb_unrecovered_recovery_tx`).

**Rollup:**
- The ledger assigns monotone timestamps. The sweeper stores the last
  rolled-up timestamp per workspace and region in Spanner.
- Each rollup applies `total_usage += Δposted` (workspace budget plus overdraft)
  and moves the matching amount out of `reserved`, but only when the fence
  advances. A replay is a no-op.

**Interval:** adaptive. A large spender rolls up every few seconds, which sets
the loss bound in §4.2; a small one, every few minutes. Spanner then sees about
one commit per active spender per interval. At 100T that is still thousands of
commits a second at peak, and the node count is sized in the benchmark (§6).

**Pause, revoke, trust downgrade:**
- The sweeper sweeps the workspace (or key) budget to zero with
  `balancing_debit`.
- New admissions then fail at once with `exceeds_credits`, while open holds can
  still settle.
- Accounts are never closed while holds are open. A closed account refuses
  posts, and voids are its only exception.

**Home settlement:**
- Deferred usage from a peer plane is booked to Spanner unconditionally, as
  today (`apply_federated_usage`, daily-capped). So `available` can fall below
  the outstanding regional budgets.
- When it does, the sweeper grants nothing more and sweeps budgets back until
  `available` is non-negative.
- The extra exposure is at most the outstanding budget, and it is alerted.

### 4.6 Keys

Lifetime-capped keys get a regional sub-budget account, funded from the key's
remaining cap like today's key escrow shards (`key-usage-row-sharding.md`). The
sub-budgets plus the synchronous path's holds never exceed the remaining cap.
Two kinds of key stay synchronous: keys with window limits (UTC day, week and
month floors), and `budget_strict` keys (exact windows inside the transaction).

### 4.7 Freshness, modes and switching

- **Maximum age.** Cached boot, workspace and key state has a maximum age. Past
  it, admission falls back to synchronous and never uses stale state, so a
  missed change event cannot extend exposure beyond that age.
- **One mode at a time.** Each workspace-region is in exactly one admission mode,
  recorded in Spanner with an epoch.
- **Switching from fast to synchronous:**
  1. Stop grants and new holds.
  2. Serve the existing holds' heartbeats, settles, refunds and replays from the
     fast path until they are terminal or expired.
  3. Return the budget and flip the epoch.

  Python never reads the regional ledger.

### 4.8 Routing

Python compiles routing into a versioned, signed snapshot on every catalog or
health change: candidates per model, prices, health, fallback order and service
tiers. The admission service evaluates it.

- A snapshot that fails verification keeps the previous one.
- A snapshot past its freshness limit sends requests to the synchronous path.
- Inputs that need per-workspace reads today (the OAuth app owner, broadcast
  destinations) are cached under the same maximum age, or the request stays
  synchronous.

### 4.9 Generation records and analytics

- **Publish after commit.** Settle publishes the generation record to the
  analytics topic after the ledger commit, carrying the ledger timestamp.
- **Delivery** to ClickHouse is at-least-once and idempotent on the generation
  ID, which ReplacingMergeTree already provides.
- **Reconciliation.** A job compares the ledger's posted terminal transfers since
  its watermark with ClickHouse and republishes gaps. So every settled generation
  has exactly one record within T.
- **No ordering keys.** Pub/Sub is not on the money path, so its 1 MB/s
  per-ordering-key limit does not cap a large workspace.
- **No per-request Spanner rows** for fast-path traffic. Disputes and refunds
  read the ledger and ClickHouse.

### 4.10 What stays synchronous at first

Credit-funded keys on standard catalog routes, streaming or not, go first.
Everything else stays on today's Python path, and the admission service forwards
it there:

- BYOK routes, custom and user-provided models, Polyphemus selection, native
  batch, video and image jobs, and hosted tools with
  `additional_cost_reservation_microdollars`.
- x402 and federated (deferred-settlement) keys.
- Keys with window limits, and `budget_strict` keys.
- Workspaces that are paused, in debt, below the minimum balance, or below the
  trust tier that allows grants. The carding incident of 2026-08 is why new and
  low-trust workspaces stay synchronous.

Token volume is concentrated in a few large workspaces. As of 2026-07-19, one
workspace accounted for 74% of all tokens to date, so a narrow fast path still
carries most of the traffic.

### 4.11 The gateway load balancer and receipt keys

Each region's gateway instance group goes behind a regional external passthrough
Network Load Balancer:

- TLS still terminates inside the enclave, so attestation is unchanged.
- Connection draining, at least as long as the longest stream, makes scale-in
  safe. That lets the gateway autoscaler (quill-cloud-proxy #432) drop its
  scale-out-only mode.

Receipt verification today discovers gateway instances from DNS A records
(`services/receipt_key_collector.py`, `templates/public/receipts.html`). Behind
one load-balancer address, that no longer reaches every instance. The boot
registry already verifies that an instance's attestation commits to its receipt
key, so registration becomes the publication path:

- Registration includes the attestation history and is required before an
  instance takes traffic.
- The collector and the public instructions read the registry instead of DNS.

## 5. Invariants

Each one has a production check.

1. **Admission bound.** Every fast-path admission is a pending transfer on the
   workspace budget (and key budget). The ledger rejects any that would make
   `debits_pending + debits_posted > credits_posted`. Check: ledger balances
   versus grants, continuously.
2. **Conservation.** Spanner `reserved` for a workspace and region equals the
   ledger's granted minus rolled-up posted amounts, allowing for the in-flight
   rollup delta. Check: an auditor diff that must be zero at every fence.
3. **One terminal transfer per authorization,** shared by settle, snapshot
   booking and refund. The loser returns the winner.
4. **No charge lost.** Settle acknowledges only after the terminal or overdraft
   transfer commits. An expired hold plus a settle becomes a deterministic-ID
   overdraft debit. Overdraft balances are rolled up and alerted.
5. **No charge invented.** Only a boot-signed settle or heartbeat posts. Expiry
   voids, so a request that never ran is never charged.
6. **Overruns never increase `reserved`.** Posted amounts never exceed the hold,
   releases are never negative, and the overrun lands only in the overdraft
   account.
7. **Idempotency.** The same scope gives the same hold. A fingerprint mismatch
   answers 409, and `id_already_failed` gets a new attempt ID. Replay works
   across node loss and mode switches.
8. **Key caps.** The regional sub-budgets plus synchronous holds never exceed a
   key's remaining cap. Window-limited and strict keys stay synchronous.
9. **Freshness.** No admission runs on state older than its maximum age. Pause
   and revocation sweep budgets to zero within one sweeper interval (stated in
   seconds when built). Accounts with open holds are never closed.
10. **Returns take only free funds** (`balancing_debit`), and never while a
    regional cluster is unreadable.
11. **The rollup is monotone,** fenced by ledger timestamps, so replays are
    no-ops. Recovery debt is absorbed in the same Spanner transaction and
    blocks grants.
12. **Mode exclusivity.** A workspace-region is in one mode at a time, switched
    by an epoch-fenced drain.
13. **Loss bound.** Losing a regional ledger loses at most one rollup interval
    of charges plus in-flight holds per workspace. Both are stated and alerted.
14. **Analytics completeness.** Every posted terminal transfer has exactly one
    generation record within T, and the reconciliation job proves it.
15. **Latency** is stated as percentiles, with the synchronous path for
    excursions.

## 6. Latency budget

| Step | Estimate | Note |
|---|---|---|
| Gateway to admission service | 0.5-1 ms | VPC, same region |
| Boot signature, cache lookups, routing evaluation | 0.2-1 ms | to be benchmarked |
| Linked hold in the ledger | 1.5-4 ms p50; 10-30 ms p99 under load | batching, one round trip, quorum fsync |
| Sign the envelope, reply | under 0.3 ms | |
| **Total** | **about 3-6 ms p50; over 10 ms at p99** | the p99 is why the synchronous fallback stays |

The benchmark gate measures this whole path on the production VM and disk shape
at the target concurrency. It includes heartbeats, a hot workspace and a replica
failover.

## 7. Lessons from the retired regional-quota leases

The September pilot (`git show 44924155^:docs/design/regional-quota-leases.md`;
incident `docs/incidents/2026-09-26-regional-ledger-grant-storm.md`) had the
same outline: Spanner grants, a fenced lease, local reservations and a
reconciler. It was retired on 2026-09-27 for three failures, and each one shapes
this design.

| Pilot failure | What this design does instead |
|---|---|
| The lease ledger was Bigtable with app profiles pinned to us-central1, so europe-west4 settles read it across the Atlantic and timed out | Each region's ledger is in that region; no request reads another region's ledger |
| A workspace went from about 6 to 430 authorizations a minute; grant and quarantine transactions aborted 94-96% of the time, and authorize p50 reached 3.7 s | Grants are never on the request path. One sweeper per region issues them serially with a per-workspace cooldown, and admission falls back to synchronous rather than waiting for a grant |
| The ledger's p99 amplified into a fleet-wide Spanner abort storm | The request path never touches Spanner. The sweeper's commits are rate-limited and back off on aborts |

## 8. Rollout

1. **Measure** commits per generation after #1464 and #1465, and authorize's
   timing fields (`routing_ms`, `store_ms`) per region.
2. **Gateway load balancer and receipt-key publication** (§4.11), independent
   of the rest.
3. **Ledger and admission service in shadow.** Gateways mirror authorize,
   heartbeat and settle. The service computes decisions, routing and holds
   against a shadow ledger. A comparator reports any difference from Python,
   including boot checks and cohort membership. No effect on requests.
4. **Benchmark gate** (§6).
5. **Pilot:** Joseph's own workspace first, then a few large workspaces, with kill
   switches per workspace, region and cloud.
6. **Widen the cohort,** and move generation records off Spanner for fast-path
   traffic.
7. **Port the remaining route types** (§4.10), then retire the Python hot path.

AWS and Azure run their own stores (DSQL, Postgres). The sweeper talks to
storage through the same store contract, so each cloud follows after GCP.

## 9. Not decided here

- The ledger choice (§2).
- Grant size and share cap, minimum balance, rollup intervals, grant cooldown and
  the maximum state age. Their values come from the benchmark and pilot data, and
  the doc is updated when they are chosen.
- Whether one TigerBeetle cluster per region, or a smaller number of
  multi-region clusters, serves better once cross-region latency is measured.

## 10. Review history

v1 (2026-10-02) kept per-request state only in admission-node memory and used
per-allowance sequence numbers over Pub/Sub. Codex found 14 problems; Fable
confirmed them and found 7 more. The main ones:

- closing an allowance could release money a running request would spend;
- watermarks are not exactly-once;
- authorize idempotency had no durable owner;
- workspace allowances bypassed key caps;
- Stage D needed one terminal state machine;
- the allowance equations broke on overruns;
- home-settlement debits could overdraw.

The rest covered:

- recovery debt and pause propagation;
- the pricing envelope and the settle response contract;
- the ClickHouse delivery gap and the latency claim;
- receipt keys and Pub/Sub's ordering-key limit;
- split-brain ownership, boot authentication and kill-switch continuity;
- commit counts, per-request Spanner rows and per-shard grants.

v2 answers each in §§4-7. Fable checked every TigerBeetle semantic used here
against its documentation. The critical ones were re-checked for this version:
transient errors burn IDs, closed accounts refuse posts, the admission
inequality, and session limits.
