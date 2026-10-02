# Fast admission and batched settlement

Status: **proposed, v3, 2026-10-02. Nothing built.** Codex and Fable reviewed
v1 and v2 (§11). Joseph's decisions are in §2. **One decision is still open:
the per-request store (§2).**

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
through `https://trustedrouter.com` alone (`enclave-go/.../endpoints.go`). Each
call is one or more multi-region Spanner transactions:

- `POST /internal/gateway/authorize`: one atomic transaction
  (`authorize_atomic`). It claims the idempotency scope through a unique index,
  holds credit and the key limit, and writes the reservation and authorization
  rows.
- Stage D heartbeats while a stream runs. In production every workspace is in
  the cohort (`rollout.sh` renders an empty pilot list). The gateway sends one
  before the first byte, then one every 10 s or every 256 output tokens.
- `POST /internal/gateway/settle`: one commit on the success path since #1465.
  It books the actual cost, releases the holds, pays app owners, and writes the
  generation record and analytics intent.

| Measured | Value |
|---|---|
| Spanner commits per generation, 2026-10-01 | 4.3, before #1464 and #1465 removed about two |
| Authorize p50, us-central1 | 0.155 s |
| Authorize p50, europe-west4 | about 1.6 s (cross-Atlantic Spanner, structural) |
| Settle p90 | 3.1 s (four serial commits; #1465 made it one on the success path) |
| Settles that overrun their estimate | 12.2%; $45.57 a day; 96% of overrun dollars in 0.7% of settles |

## 2. Decisions

**Made by Joseph, 2026-10-02:**

- Batched settlement and regional admission: approved, with a target of under
  about 10 ms of request overhead. This supersedes the earlier rule against
  regional leases for general users, for this design.
- A compiled authorize and settle service, not Python, in Go like the gateway.
- An L4 load balancer in front of the gateways (§4.12).
- No Spanner committed-use discounts.
- Shard ClickHouse later.

**Open, for Joseph: the per-request store.**

| | TigerBeetle cluster per region (recommended) | Regional Spanner instance per region |
|---|---|---|
| What it is | A purpose-built ledger: pending, post, void, timeouts, idempotent IDs | Today's billing code (`authorize_atomic`, typed settle, Stage D, reaper) against a per-region database holding escrowed balances |
| Authorize latency | about 3-6 ms p50 (§6) | about 10-20 ms p50: two single-region commits |
| Cost at 100T | a few VMs per region, plus rollup commits | still a commit per request, at regional prices; to be measured |
| New work | the state machine in §4.5 and the ledger's operations | moving balances into regional escrow; the code mostly exists |
| Fit to the 10 ms target | yes | no |

**A reversal to be explicit about.** `billing-typed-counters.md` §2 rejected
leases and ledgers, because they demote Spanner from system of record and add a
second store with asynchronous reconciliation. This design accepts that cost on
purpose, for latency and unit cost. Spanner stays the system of record for
balances, payments, debt and pauses. The per-request store holds escrowed money,
overdraft charges not yet rolled up, and scope ownership. Reconciliation is
fenced and audited (§5), and its loss is bounded and gated (§4.8).

## 3. Targets

1. **Latency at the gateway** from sending authorize to holding a routing
   decision and a hold: p50 under 5 ms, p90 under 10 ms. Tail excursions are
   p99, ledger view changes and upgrades. They fall back where §4.3 allows,
   and are otherwise refused with 503 and `Retry-After`.
2. **Billing-database commits grow with active workspaces, not requests.**
3. **No charge lost and none booked twice** (`durable-settle-outbox.md`). A
   request that ran is never released free, a request that never ran is never
   charged, and a refunded request costs nothing.
4. **Spending is bounded by money already reserved,** with the overrun and
   home-settlement exceptions in §5.
5. **Every step can be switched off** per workspace, region and cloud.

## 4. Design

### 4.1 Shape

Per GCP region:

- **Admission service:** stateless Go, behind the gateways. Gateways on AWS and
  Azure use the nearest GCP region, and the cross-cloud hop counts against
  their latency budget.
- **Ledger:** a TigerBeetle cluster holding money and scope ownership (§4.2).
- **Sweeper:** one leader-elected worker for grants, returns, rollups and gates
  (§4.7).
- **Settle log:** a Pub/Sub topic that receives every settle payload before the
  gateway gets its acknowledgement (§4.9).

Python keeps routing policy and everything off the hot path.

### 4.2 The ledger

**Accounts, per workspace and region,** all created by one linked
`create_accounts` at the first grant. A missing account is a transient error
that would permanently burn a transfer ID.

| Account | Flags | Holds |
|---|---|---|
| Budget | `debits_must_not_exceed_credits` | Granted money; every hold debits it |
| Usage sink | none | Credit side of every charge; its credits minus its debits are the charges to roll up |
| Overdraft | none | Debit side of charges that exceeded both the hold and the free budget |
| Gate | none; closed while paused | One unit moved to the pool by every admission, so closing it stops admission without touching open holds |
| Pool (one per region) | none | Counterparty for grants and returns |

Capped keys get the same pair of budget and overdraft accounts per key (§4.6).
Rollups read balances with `lookup_accounts` (counter differences), not by
walking transfers.

**Operations:**

- **Topology:** six replicas across three zones, on local SSD. Cluster size is
  fixed at creation.
- **Replacing a replica:** use `tigerbeetle recover`, never `format`, which can
  lose committed operations. It needs a healthy quorum, and replacements are
  serialized. A managed instance group must never auto-heal a replica by
  formatting it.
- **Upgrades** go one version at a time, and clients are never newer than
  replicas (the Go client is pinned to the cluster version in CI). Each upgrade
  makes the cluster unavailable for seconds.
- **Sessions:** at most 64 by default, oldest idle evicted, one request in
  flight each. The client never times out, so the service sets its own
  deadline. Every client handles `session evicted` by re-registering. All
  clients together stay well under the limit.
- **Change data capture:** the cluster streams every transfer and every expiry
  over AMQP, at least once. That feed drives the expiry handling in §4.5, the
  reconciliation in §4.9 and an off-cluster archive.

**Gate:** before any production traffic, benchmark the whole path on the real VM
and disk shape. That means holds, heartbeats, settles and their key legs. A
published cloud benchmark of an unoptimised deployment measured p50 32 ms and
p99 over 500 ms, so the target is not assumed (§6).

### 4.3 Who owns a request

Exactly-once execution today rests on a unique index on the idempotency scope
(`tr_reservation.idempotency_scope`): concurrent first calls collide and the
loser replays. The ledger has no unique index on user data, so ownership comes
from the transfer ID itself.

- **Deterministic hold IDs.** Attempt *k* of a scope uses the ID
  `H(scope, k)`. The scope is workspace, key and idempotency key, as today.
  Concurrent first calls with the same scope converge on `exists`, and the
  loser replays the winner. Attempt *k* advances only on `id_already_failed`,
  a permanent and deterministic result, so every caller walks the same
  sequence. Hashed IDs cost the ledger some write locality; the benchmark
  measures it.
- **One home region per workspace for keyed requests.** A request carrying the
  caller's `Idempotency-Key` is admitted only in its workspace's home region,
  which is recorded in Spanner and cached. A retry that arrives through another
  region's gateway reaches the same ledger. Requests without a caller key carry
  a scope the gateway minted for that invocation alone, so they may use the
  nearest region.
- **No guessing on ambiguity.** If the ledger's answer for a keyed request is
  unknown, or the ledger is unavailable, the service answers 503 with
  `Retry-After`. It never falls back to the synchronous path, which could
  execute the scope a second time. Unkeyed requests may fall back, behind the
  breaker in §7.
- **The route decides the owner.** Whether a request takes the fast or the
  synchronous path is a pure function of the workspace's mode, its route class
  and its key class (§4.11). Retries therefore reach the same owner.
- **Mode switches are epoch-fenced.** Changing a workspace's mode stops new
  holds, drains open ones, and keeps the old owner answering replays for the
  idempotency retention window before the new owner accepts keyed scopes.

### 4.4 Authorize

1. Verify the boot signature (the per-boot Ed25519 key in the boot registry)
   and that the image digest is accepted. Use cached state with a maximum age
   (§4.8).
2. Evaluate the compiled routing snapshot (§4.10) and compute the estimate.
   Without `max_tokens` the estimate assumes 512 output tokens, which is why
   overruns are common.
3. Commit one linked `create_transfers`:
   - a one-unit `Gate→Pool` transfer;
   - a pending debit on the budget, with ID `H(scope, k)`: the first hold `h0`,
     whose ID is the authorization ID `A`;
   - for a capped key, a pending debit on the key budget.

   `h0` carries 64-bit hashes of the request fingerprint and of the invocation
   nonce in `user_data_128`. Holds reopened by heartbeats carry `A` there
   instead, so a heartbeat can find the latest one. `user_data_64` carries the
   attempt or heartbeat sequence, and `user_data_32` the snapshot version.
4. Answer with a **signed envelope** that the gateway echoes on heartbeat,
   settle and refund. It carries:
   - the authorization ID and the prospective generation ID;
   - the frozen candidates, prices, fees and app terms;
   - a hash of the invocation nonce and the boot binding;
   - the hold amounts and the expiry.

   Pricing at settle is then a pure function of the envelope.

**A lost authorize response.** The gateway retries only on 502, 503 and 504,
with the same bytes, nonce and key. A retry finds the hold by ID:

- If the nonce hash matches, this is the same invocation retrying, which never
  received the first answer. The service re-evaluates, re-signs, and binds the
  new envelope to the existing hold. That is safe because nothing acted on the
  lost one.
- If the nonce hash differs, another invocation owns the scope, and the gateway
  answers 409 `idempotency_replay`, as today.

Both checks read `h0`. A different fingerprint hash answers 409, as today,
and the nonce hash decides between the two cases above.

### 4.5 One authorization's state machine

Transfer IDs are pure functions of the authorization ID `A` and a step:

| Event | Transfers, linked | Effect |
|---|---|---|
| Authorize | gate debit; pending `Budget→Sink`, `H(scope,k)` | hold `h0` = estimate |
| Heartbeat *n*, delivered so far `c_n` | post `h_(n-1)` for `c_n − c_(n-1)`, ID `H(A,post,n)`; pending `Budget→Sink` for `estimate − c_n`, ID `H(A,hold,n)` | charges as the stream runs, and keeps one open hold for the rest of the cap |
| Settle, actual `a` | post the open hold for `min(a, estimate) − c_n`, ID `H(A,terminal)`; if `a` exceeds the estimate, a `balancing_debit` `Budget→Sink` for the excess against free budget, then `Overdraft→Sink` for whatever the free budget did not cover | one terminal claim |
| Settle below what heartbeats posted (`a < c_n`) | void the open hold, ID `H(A,terminal)`; reversal `Sink→Budget` for `c_n − a`, ID `H(A,reverse)` | the customer pays `a`, not `c_n` |
| Refund | void the open hold, ID `H(A,terminal)`; reversal `Sink→Budget` for `c_n`, ID `H(A,reverse)` | a refunded request costs nothing, as today |
| Settle after the open hold expired | `Overdraft→Sink` (after the `balancing_debit` against free budget) for `a − c_n`, ID `H(A,terminal-late)` | charged, net of what heartbeats posted |

**Rules:**

- **The terminal claim is `H(A,terminal)`.** Settle posts with it and refund
  voids with it, so exactly one of them commits. The loser gets `exists` or
  `exists_with_different_flags` and reads the winner. A heartbeat that arrives
  after the claim fails with `pending_transfer_already_posted` or `_voided`,
  and answers `already_terminal`.
- **A retried linked chain** returns `exists` for its first event and
  `linked_event_failed` for the rest. Chains are atomic, so the service treats
  that as "already committed" and reads the result.
- **Heartbeats keep today's checks:**
  - the sequence only increases, and gaps are allowed;
  - the endpoint is pinned by the first heartbeat;
  - usage never regresses;
  - tokens stay within the envelope's limits;
  - the running cost stays at or under the hold.

  A heartbeat always references the latest open hold, found by
  `query_transfers` on `user_data_128 = A`. It never references a hold it has
  not read, because `pending_transfer_not_found` would burn its ID.
- **Expiry.** A reopened hold keeps the authorization's remaining lifetime, or
  the 300 s grace, whichever is longer, as today. So a dead stream strands the
  undelivered remainder for minutes, not hours. Expiry is reported by the
  change-data feed, which records the disposition (`expired` with `c_n`
  posted, today's `reaped_snapshot`).
- **A behavior change:** a settle that arrives after expiry is charged.
  Today it is logged as lost.

### 4.6 Keys

A lifetime-capped key gets a budget account funded from its remaining cap, like
today's key escrow shards (`key-usage-row-sharding.md`). It also gets an
overdraft account.

- **Every transition in §4.5 has a key leg in the same linked chain:** the hold,
  each heartbeat's post and reopen, the terminal post or void, the reversal,
  and the overrun via `balancing_debit` then overdraft.
- **Overruns consume the cap.** A key with a $10 cap whose $5 hold settles at $8
  has $2 left, not $5.
- **Bound:** all regional key budgets plus synchronous key holds never exceed a
  key's remaining cap. Rollup applies `usage += Δcharged` to `tr_key_limit`.
- **Window-limited and `budget_strict` keys stay synchronous.**

### 4.7 Budget: grants, returns, rollups and gates

The sweeper does all Spanner work, off the request path.

- **Grant g:**
  1. One Spanner transaction checks the donor shard's headroom, adds `g` to
     `reserved`, and writes a grant row in state pending (ID G).
  2. Then a `Pool→Budget` transfer with ID G.
  3. A replay returns `exists`, and the row becomes applied.

  Grants come from one shard's headroom (`credit-row-sharding-handoff.md`),
  sized by headroom rather than trailing spend, and the rows record the donor
  shard.
- **Return r:**
  1. A Spanner intent row R records the maximum.
  2. A `balancing_debit` `Budget→Pool`, ID R, moves only free funds. Its actual
     amount `r'` is read back from the ledger.
  3. A Spanner transaction claims R exactly once, releases `r'` from the donor
     shard's `reserved`, and absorbs recovery debt from it. A crash at any
     point resumes from R's state.
- **Rollup,** from account counters:
  - budget-leg charges `U` = the sink's credits minus the overdraft's debits;
  - overdraft charges `O` = the overdraft's debits;
  - reversals `X` = the sink's debits.

  Returns go `Budget→Pool` and never pass through the sink, so they are not
  usage; they leave `reserved` through their own protocol above. One Spanner
  transaction moves the fences and applies `total_usage += ΔU + ΔO − ΔX` and
  `reserved −= ΔU − ΔX`. **Overdraft never touches `reserved`.**
- **Interval:** adaptive. Seconds for large spenders, minutes for small ones.
  A workspace-region costs about three commits per interval: grant, return and
  rollup.
- **Pause, revoke, trust downgrade:** close the gate account, so new admissions
  fail at once. Then sweep free funds back, and keep sweeping funds that
  settling holds release until the pause lifts. Reopen by voiding the closing
  transfer.
- **An overdraft balance blocks new grants** until the rollup has booked it.
- **Home settlement:** deferred usage from a peer plane is booked to Spanner
  unconditionally, as today. When `available` falls below the outstanding
  regional budgets, the sweeper grants nothing more and sweeps budgets back
  until it is non-negative. The extra exposure is at most the open holds, and
  it is alerted.

### 4.8 Freshness, loss and fences

- **Maximum state age.** Cached boot, workspace and key state has one. Past it,
  admission stops for that workspace. Pauses also close the gate (§4.7), so a
  pause does not depend on cache age.
- **Fence-age gate.** If a workspace-region's last successful rollup is older
  than its bound, admission stops there: 503 for keyed requests, the
  synchronous path for unkeyed ones. A stuck sweeper therefore cannot let
  unbooked charges grow.
- **Losing a region's ledger** loses at most the charges since the last
  successful fence, plus open holds. The runbook:
  1. Declare the loss and stop that region's admission.
  2. Release the region's escrow in Spanner from the last fence.
  3. Book late settles through the synchronous path, with the authorization ID
     as the Spanner claim key.
  4. Accept that keyed scopes admitted since the last fence can execute again.

  The lost charges are revenue; the escrow is the customer's.

### 4.9 Settle durability and side effects

- **Durable before the acknowledgement.** Settle commits the ledger chain, then
  publishes the full settle payload with the envelope to the regional settle
  log. Only then does it acknowledge the gateway. If the publish fails after
  the commit, the gateway's retry gets `exists` and republishes. The gateway
  keeps settles in memory only (about 15 s of retries, then dropped), and
  settle runs after the stream ends, so the publish wait is off the client's
  path.
- **Consumers of the settle log,** each idempotent on the authorization ID:
  - generation records and activity analytics to ClickHouse;
  - auto-refill scheduling, budget alerts, metadata webhooks, routing feedback
    and route-fallback reports;
  - for heartbeat-only authorizations that expired, the change-data feed
    triggers the record and disposition.
- **Reconciliation** compares the ledger's terminal and expiry events with the
  log and ClickHouse, and alerts on any gap. A gap it cannot fill from the log
  becomes a stub record with the amount.
- **Disposition and evidence lookups** read the ledger by authorization ID, and
  ClickHouse within T for `gateway_request_id`. Synthetic-probe workspaces stay
  synchronous, so the release gates keep reading Spanner.
- **App-owner payouts** stay on the synchronous path at first (§4.11). Moving
  them needs a durable payout obligation tied to each charge and reversal,
  applied from the settle log.

### 4.10 Routing

- **A compiled snapshot.** Python compiles routing into a versioned, signed
  snapshot on every catalog or health change: candidates, prices, health,
  fallback order and service tiers. Snapshots stay immutable while any hold
  references them.
- **Failure handling.** A snapshot that fails verification keeps the previous
  one. One past its freshness limit stops fast admission.
- **Per-workspace inputs** that need a read today, such as broadcast
  destinations, are cached under the maximum age.

### 4.11 What stays synchronous at first

Credit-funded keys on standard catalog routes go first, streaming or not.
These stay on today's Python path:

- BYOK routes, custom and user-provided models, Polyphemus selection, native
  batch, video and image jobs, and hosted tools with
  `additional_cost_reservation_microdollars`.
- x402 and federated (deferred-settlement) keys.
- Keys with window limits, and `budget_strict` keys.
- OAuth-app keys with a markup (owner payouts, §4.9) and synthetic-probe
  workspaces.
- Workspaces that are paused, in debt, below the minimum balance, or below the
  trust tier that allows grants. The carding incident of 2026-08 is why new and
  low-trust workspaces stay synchronous.

Token volume is concentrated in a few large workspaces. As of 2026-07-19, one
workspace accounted for 74% of all tokens to date, so a narrow fast path still
carries most of the traffic.

### 4.12 The gateway load balancer and receipt keys

Each region's gateway instance group goes behind a regional external passthrough
Network Load Balancer:

- TLS still terminates inside the enclave, so attestation is unchanged.
- Connection draining, at least as long as the longest stream, makes scale-in
  safe. The gateway autoscaler (quill-cloud-proxy #432) can then drop its
  scale-out-only mode.

Receipt verification today discovers instances from DNS A records, with an
instance-termination gap. Registration in the boot registry becomes the
publication path:

- Registration includes the attestation history.
- It is required before an instance takes traffic.
- The collector and the public instructions read the registry, not DNS.

## 5. Invariants

Each has a production check.

1. **Admission bound.** Every fast admission is a pending debit on the budget
   (and key budget), so the ledger rejects any that would exceed the grants.
2. **Conservation.** `reserved` for a workspace-region equals the grants minus
   rolled-up budget charges, plus rolled-up reversals, minus applied returns.
   It allows for pending grant rows, landed-but-unreleased returns, and the
   unrolled delta. An auditor diffs it at every fence.
3. **One terminal claim per authorization** (`H(A,terminal)`). The loser reads
   the winner.
4. **No charge lost.** Settle acknowledges only after the ledger chain and the
   settle-log publish. Late settles are charged net of heartbeat posts.
5. **No charge invented.** Only boot-signed heartbeats and settles post.
   Refunds and below-posted settles reverse.
6. **Overdraft never moves `reserved`.** Excess first consumes free budget, so
   an overrun reduces later admissions.
7. **Ownership.** Hold IDs are deterministic in the scope. Keyed scopes have
   one home region and one owner. Ambiguity answers 503, never a second owner.
8. **Key caps.** Key budgets plus synchronous key holds never exceed the
   remaining cap, and overruns consume it.
9. **Pauses** close the gate at once. No admission runs on state older than the
   maximum age.
10. **Returns** take only free funds, and are applied to Spanner exactly once
    through their intent row.
11. **Rollups** are monotone, fenced per account, and replay as no-ops.
    Recovery debt is absorbed and blocks grants.
12. **Mode switches** are epoch-fenced, with replay ownership kept for the
    retention window.
13. **Loss bound.** Admission stops when the last successful fence is older
    than its bound. Loss is at most that window plus open holds.
14. **Records.** Every terminal or expiry event has one generation record within
    T, from the settle log or as a reconciled stub.
15. **Latency** is stated as percentiles. Excursions take the documented
    fallback or a 503.

## 6. Latency and load

| Step | Estimate |
|---|---|
| Gateway to admission service, same region | 0.5-1 ms (more from AWS and Azure) |
| Boot signature, caches, routing evaluation | 0.2-1 ms |
| Linked hold (gate, budget, key) | 1.5-4 ms p50; 10-30 ms p99 under load |
| Sign the envelope, reply | under 0.3 ms |
| **Total** | **about 3-6 ms p50; over 10 ms at p99** |

Ledger load at 100T is set mostly by heartbeats: about two transfers each (four
with a key leg). That is at least one before the first byte, plus one per 10 s
or per 256 output tokens. The benchmark gate measures the whole path at that
rate, including a hot workspace and a replica failover.

## 7. Lessons from the retired regional-quota leases

The September pilot (`git show 44924155^:docs/design/regional-quota-leases.md`;
`docs/incidents/2026-09-26-regional-ledger-grant-storm.md`) had the same outline
and was retired on 2026-09-27.

| Pilot failure | This design |
|---|---|
| The lease ledger was Bigtable with app profiles pinned to us-central1, so europe-west4 settles read it across the Atlantic and timed out | Each region's ledger is local. Keyed requests go to the workspace's home region by design, and the latency is stated |
| A workspace went from about 6 to 430 authorizations a minute; grant and quarantine transactions aborted 94-96%; authorize p50 reached 3.7 s | Grants are never on the request path. One sweeper per region issues them serially with a per-workspace cooldown, sized by headroom |
| The ledger's p99 amplified into a fleet-wide Spanner abort storm | The request path never touches Spanner. Unkeyed fallback to the synchronous path goes through a per-workspace-region breaker (cooldown, as in the incident fix) and sheds with 503 when that path is saturated |
| Ambiguous leases were quarantined, not guessed back into service | Ambiguous keyed requests get 503 (§4.3) |

## 8. Rollout

1. **Measure** commits per generation after #1464 and #1465, and authorize's
   timing fields per region.
2. **Gateway load balancer and receipt-key publication** (§4.12), independent
   of the rest.
3. **Ledger and admission service in shadow:** gateways mirror authorize,
   heartbeat and settle. A comparator reports any difference from Python in
   decisions, holds, charges per authorization and records. Requests are
   unaffected.
4. **Benchmark gate** (§6).
5. **Pilot:** Joseph's own workspace, then a few large ones, with kill switches
   per workspace, region and cloud.
6. **Widen;** move app payouts and the remaining route types (§4.11) one at a
   time; then retire the Python hot path.

## 9. Not decided here

- **The per-request store** (§2).
- **Tuning values:** grant sizing and cooldown, rollup intervals and fence-age
  bounds, the maximum state age, and the idempotency retention window. They
  come from the benchmark and the pilot.
- **Home-region assignment** for workspaces whose traffic moves between
  continents.

## 10. Not in this design

Sharding ClickHouse (decided later), and Spanner topology for the system of
record.

## 11. Review history

- **v1 (2026-10-02)** kept per-request state in admission-node memory. Codex
  found 14 problems; Fable confirmed them and found 7 more.
- **v2** moved money into a per-region TigerBeetle ledger. Codex (7 P1, 5 P2)
  and Fable (4 P1, 9 P2, 6 P3) then found these gaps:
  - scope ownership without uniqueness;
  - fallback that could execute a scope twice;
  - a terminal ID that Stage D's incremental posts broke;
  - late settles double-charging posted heartbeats;
  - refunds unable to undo posted deltas;
  - a rollup that released escrow for overdraft and counted returns as usage;
  - overruns that did not reduce headroom;
  - incomplete key legs;
  - returns without a recovery protocol;
  - unrecoverable lost authorize responses and settle payloads;
  - app payouts and settle side effects without a home;
  - a sweep that was not a lasting revocation;
  - a loss bound with no gate;
  - two operations errors: replicas are rebuilt with `recover`, and
    change-data capture does exist.
- **v3** answers each one in §§4-8. Both reviewers checked the TigerBeetle
  semantics used here against its documentation and source.
