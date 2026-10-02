# Fast admission and batched settlement

Status: **proposed, v4, 2026-10-02. Nothing built.** Codex and Fable have
reviewed three versions (§11). Joseph's decisions are in §2. **One decision is
still open: the per-request store (§2).**

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
  or 256 output tokens.
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
purpose, for latency and unit cost.

- Spanner stays the system of record for balances, payments, debt and pauses.
- The ledger holds escrowed money, overdraft not yet rolled up, and scope
  ownership.
- Reconciliation is fenced and audited (§5).
- Its loss is bounded and gated, and recovery rebuilds from an archive (§4.8).

## 3. Targets

1. **Latency at the gateway,** from sending authorize to holding a routing
   decision and a hold: p50 under 5 ms, p90 under 10 ms. Excursions (p99, ledger
   view changes, upgrades) fall back where §4.3 allows, and are otherwise
   refused with 503 and `Retry-After`. Keyed requests admitted outside their
   home region also pay a cross-region hop.
2. **Billing-database commits grow with active workspaces, not requests.**
3. **No charge lost and none booked twice** (`durable-settle-outbox.md`):
   - a request that ran is never released free;
   - a request that never ran is never charged;
   - a refunded request costs nothing.
4. **Spending is bounded by money already reserved,** with the exceptions in §5.
5. **Every step can be switched off** per workspace, region and cloud.

## 4. Design

### 4.1 Shape

Per GCP region:

- **Admission service.** Stateless Go, and the front door for every request of a
  fast-path workspace. Gateways on AWS and Azure use the nearest GCP region.
- **Ledger.** A TigerBeetle cluster, version 0.16 or later, which the balancing
  semantics below need.
- **Sweeper.** One leader-elected worker for grants, returns, rollups, repayment
  passes and gates.
- **Settle log.** A Pub/Sub topic holding every settle, refund and heartbeat
  intent before its ledger change (§4.9).
- **CDC archive.** The ledger's change stream, written to GCS. Recovery depends
  on it (§4.8).

Python keeps routing policy and everything off the hot path.

### 4.2 The ledger

Three ledgers in one cluster. A linked chain may span them, as in TigerBeetle's
currency-exchange recipe.

| Ledger | Accounts | Purpose |
|---|---|---|
| Money | per workspace-region: Budget (`debits_must_not_exceed_credits`), Sink, Overdraft; per region: Pool | escrow, charges, overdraft |
| Keys | per capped key and region: Budget (`debits_must_not_exceed_credits`), Sink, Overdraft; per region: Pool | lifetime caps |
| Control | per workspace: Gate; per region: Claims and Sink | scope claims, terminal claims, admission gate |

All of a workspace's accounts are created by one linked `create_accounts` at the
first grant. A missing account is a transient error that would permanently
burn a transfer ID.

**Transfers by kind.** Each kind has its own `code`. IDs avoid 0 and 2^128−1.

**Rollups read counters.** The fence is the counter tuple of the money and key
accounts, read by one `lookup_accounts` batch, which is a consistent snapshot.
Rollups never walk transfers.

**Operations:**

- **Topology:** six replicas across three zones, on local SSD; cluster size is
  fixed at creation.
- **Replacing a replica:** `tigerbeetle recover`, never `format`. It needs a
  healthy quorum, and replacements are serialized; no managed instance group
  auto-heals a replica by formatting it.
- **Upgrades:** one version at a time, clients never newer than replicas. Each
  makes the cluster unavailable for seconds.
- **Sessions:** at most 64, oldest idle evicted, one request in flight each. The
  client never times out, so the service sets deadlines and re-registers after
  `session evicted`.
- **CDC:** one instance per region with an AMQP broker, at least once. Its lag
  bounds expiry dispositions and the archive, and both are alerted.

**Gate before production:** benchmark the whole path, including chains, heartbeats
and key legs, on the real VM and disk shape. One published cloud benchmark of an
unoptimised deployment measured p50 32 ms and p99 over 500 ms.

### 4.3 Ownership of a request

Exactly-once execution today rests on a unique index on the idempotency scope
(workspace, key, idempotency key). Here, ownership is the transfer ID
`H(scope, k)`, written once per scope, whichever path executes it.

- **The first write decides.** A fast admission writes its hold `h0` under
  `H(scope, k)`. A keyed request the front door sends to Python first writes a
  one-unit claim with the same ID on the control ledger, with `code`
  "synchronous". A later request with that scope gets `exists` or an
  `exists_with_different_*` result, and goes to the same owner.
- **Changes do not move scopes.** That holds when a key gains a window limit, a
  route class changes, or a workspace switches mode, because ownership is
  resolved by scope before eligibility.
- **The fingerprint check** compares the fingerprint hash stored on `h0` or on
  the claim. A mismatch answers 409, as today.
- **Attempts.** `k` advances only on `id_already_failed`, which is permanent and
  deterministic, so every caller walks the same sequence.
- **Scope of the front door.** Keyed requests of a fast-path workspace go
  through the front door in the workspace's home region, for as long as any of
  its scopes may be replayed (the 30-day retention). The home region is recorded
  in Spanner. When the ledger cannot answer, keyed requests get 503; they never
  get a guess.
- **Unkeyed requests** carry a scope the gateway minted for that invocation.
  They may use the nearest region, and fall back to the synchronous path behind
  the breaker in §7.
- **Python stays bounded without knowing about the ledger.** It spends only
  balance not held in regional escrow, so a stale synchronous worker cannot
  double-spend escrow.

### 4.4 Authorize

1. Verify the boot signature and the accepted image digest, against caches with
   a maximum age.
2. Evaluate the compiled routing snapshot (§4.10) and compute the estimate e.
   Without `max_tokens`, it assumes 512 output tokens.
3. One linked chain:

   | Transfer | ID | Notes |
   |---|---|---|
   | pending `Budget→Sink` for e (`h0`) | `H(scope,k)` | `user_data_128` = 64-bit fingerprint hash + 64-bit nonce hash; `user_data_32` = snapshot version |
   | one unit `Gate→Sink` | `H(scope,k,gate)` | fails if the gate is closed |
   | pending key `Budget→Sink` for e, if the key is capped | `H(scope,k,key)` | |

   The authorization ID `A` is `h0`'s ID. The generation ID is derived from `A`,
   as today.
4. Answer with a **signed envelope** that the gateway echoes on heartbeat,
   settle and refund:
   - `A` and the generation ID;
   - the frozen candidates, prices, fees and app terms;
   - the snapshot version and the boot binding;
   - the hold amounts and the expiry.

   Snapshots stay immutable while any hold references them.

**Responses on contention:**

- **`exceeds_credits` on `h0`** means a regional shortfall: the sweeper may just
  be behind. A keyed request gets 503 with `Retry-After`; an unkeyed one takes
  the synchronous path. The answer is 402 only when Spanner's balance cannot
  cover the estimate.
- **A lost authorize response.** The gateway retries only on 502, 503 and 504,
  with the same bytes, nonce and key. A retry finds `h0`:
  - If the nonce hash matches, this is the same invocation, which never acted on
    the lost answer. The service re-signs the envelope against `h0`'s snapshot
    version, so it is the same answer.
  - If the nonce hash differs, another invocation owns the scope, and the
    gateway answers 409 `idempotency_replay`, as today.

### 4.5 One authorization's state machine

**Every settle, refund and heartbeat writes its intent to the settle log first**
(§4.9). Only then does it touch the ledger.

**The terminal claim.** Every terminal chain begins with a one-unit
`Claims→Sink` transfer on the control ledger, ID `H(A,terminal)`, with the kind
(settle or refund) in `user_data_64`.

- The first chain to commit owns the authorization.
- A retry of the same kind gets `exists`. Its chain is atomic, so the service
  reads the committed outcome. The assumption that a retried chain answers
  `exists` then `linked_event_failed` is tested in the benchmark gate.
- A different kind gets `exists_with_different_user_data_64`, and the service
  answers with the winner's outcome, read from the ledger. It never reports its
  own request.

Notation: e is the estimate, c the amount posted by heartbeats so far, a the
actual cost, and "open" the latest open hold.

| Event | Chain after the claim (money and key legs alike) |
|---|---|
| Heartbeat n, delivered `c_n` | post open for `c_n − c_(n−1)`, ID `H(A,post,n)`; pending `Budget→Sink` for `e − c_n`, ID `H(A,hold,n)` (`user_data_128` = A, `user_data_64` = n, `user_data_32` = endpoint hash) |
| Settle, open, `c ≤ a ≤ e` | post open for `a − c` |
| Settle, open, `a > e` | post open for `e − c`; overrun of `E = a − e` (below) |
| Settle, open, `a < c` | void open; reversal `Sink→Budget` for `c − a`; repay |
| Refund, open | void open; reversal `Sink→Budget` for c (if c > 0); repay |
| Settle, expired, `a ≥ c` | overrun of `E = a − c` |
| Settle, expired, `a < c` | reversal `Sink→Budget` for `c − a`; repay |
| Refund, expired | reversal `Sink→Budget` for c (if c > 0); repay |

**Overrun of a known amount E,** three linked transfers:

1. `Overdraft→Budget` for E;
2. `Budget→Sink` for E;
3. `Budget→Overdraft` for E, flagged `balancing_debit` and `balancing_credit`.

The customer is charged the full E. Free budget pays what it can, and the rest
stays as overdraft debt. On 0.16 and later, a balancing transfer moves at most
its amount, possibly zero, without error.

**Repay** is `Budget→Overdraft`, flagged `balancing_debit` and
`balancing_credit`, with an amount at the per-request ceiling. It moves the
smaller of the free budget and the overdraft debt. Every chain that releases
funds ends with it, and the sweeper also runs it on a timer. Released headroom
therefore pays debt before any later admission can spend it.

**Heartbeat rules:**

- **Finding the open hold.** The first heartbeat references `h0` by
  `lookup_transfers [A]`. Later ones use `query_transfers` with
  `user_data_128 = A`, filtered by the reopen `code`, newest first.
- **Contention.** A heartbeat never references a hold it has not read, because
  `pending_transfer_not_found` would burn its ID. On
  `pending_transfer_already_posted` it checks the terminal claim: if one exists
  the answer is `already_terminal`, otherwise this was contention, so it
  re-reads and retries. `pending_transfer_expired` answers `already_terminal`.
- **Validation.** Sequence, endpoint pin, token non-regression and the token cap
  are checked as today, against the admission node's memory. The endpoint hash
  and the cumulative cost are durable in the hold chain; token components are
  durable in the settle log. After a node restart, the next heartbeat re-checks
  the endpoint and cost from the ledger and takes its token components as the
  new baseline. That is a deliberate, narrow weakening.
- **Expiry.** A reopened hold keeps the authorization's remaining lifetime or
  the 300 s grace, whichever is longer, as today. The CDC `two_phase_expired`
  event records the disposition: expired with c posted, today's
  `reaped_snapshot`.
- **A behavior change:** a settle that arrives after expiry is now charged, net
  of what heartbeats posted. Today it is logged as lost.

### 4.6 Keys

Capped keys run the same chain on the key ledger, under their own IDs. Their
Sink is separate, so a charge is never counted twice.

- **Grant.** A regional key budget is granted from the key's remaining cap. One
  Spanner transaction adds it to `tr_key_limit.reserved` on a donor key shard,
  which removes it from synchronous headroom, and writes a pending grant row.
  Then comes a `Pool→Budget` transfer.
- **Return and rollup** mirror §4.7 against `tr_key_limit`.
- **Overruns consume the cap.** A $10-capped key whose $5 hold settles at $8 has
  $2 left.
- **Uncapped keys** have no key accounts:
  - Revocation reaches admission through the key-status cache, whose maximum age
    is short and stated as the exposure.
  - Their usage is booked to `tr_key_limit` by a settle-log consumer, idempotent
    on `A`, so a cap or window added later starts from correct usage.
- **Window-limited and `budget_strict` keys stay synchronous.**

### 4.7 Budget: grants, returns, rollups and gates

The sweeper does all Spanner work, off the request path, in this order per
workspace-region: rollup, then repay, then return, then grant.

- **Grants.**
  - One Spanner transaction checks a donor shard's headroom, adds g to
    `reserved`, and writes a pending grant row G with the shard.
  - Then a `Pool→Budget` transfer with ID G. A replay returns `exists` and the
    row becomes applied.
  - If the transfer fails permanently, a cancel path releases the row.
  - Grant rows are drained FIFO. Rollups and returns consume the oldest open row
    first, so every delta has a donor shard.
  - Sizing goes by headroom, not trailing spend, with a per-workspace cooldown.
- **Returns.**
  1. A Spanner intent row R records the maximum.
  2. A `balancing_debit` `Budget→Pool`, ID R, moves only free funds. Its actual
     amount `r'` is read back.
  3. A Spanner transaction claims R exactly once, releases `r'` from the donor
     rows, and absorbs recovery debt from it.

  Because the rollup runs first, a return never meets a `reserved` that lags the
  ledger.
- **Rollup, from the fenced counters:**

  | Delta | Definition |
  |---|---|
  | charges `ΔC` | the Sink's credits minus its debits |
  | overdraft `ΔO` | the Overdraft's debits minus its credits |
  | escrow consumed | `ΔC − ΔO` |

  One Spanner transaction applies `total_usage += ΔC` and `reserved −= (ΔC −
  ΔO)` to the FIFO donor rows. It moves the fences only if they still match the
  read, so a replay is a no-op. Returns and grants never pass through the Sink.
- **The auditor's identity at every fence:** `reserved`, summed over open grant
  rows, equals the Budget's credits minus its debits, corrected for pending
  grant rows, returns landed but not released, and unfenced deltas.
- **Pause, revoke, trust downgrade, and a switch out of fast mode:**
  - Close the gate with a pending `closing_debit` transfer: zero amount, timeout
    0, ID per pause epoch. New admissions fail at once.
  - Sweep free funds back, and keep sweeping funds that settling holds release.
  - Reopen by voiding the closing transfer.
- **An overdraft balance blocks new grants** until repaid or rolled up.
- **Home settlement.** Deferred usage from a peer plane is booked to Spanner
  unconditionally, as today. When `available` falls below the outstanding
  regional budgets, the sweeper grants nothing more and returns budget until it
  is non-negative. The extra exposure is at most the open holds, and it is
  alerted.

### 4.8 Freshness, loss and recovery

- **Maximum state age.** Cached boot, workspace and key state has one; past it,
  admission stops. Pauses close the gate, so they do not depend on cache age.
- **Fence-age gate.** If a workspace-region's last successful fence is older than
  its bound, admission stops there: 503 for keyed requests, the synchronous path
  for unkeyed ones.
- **The archive is complete up to each fence.** A fence advances only after the
  CDC archive has acknowledged every transfer up to that fence's ledger
  timestamp. Per-authorization evidence (posts, claims, expiries, scope claims)
  is therefore durable for everything already rolled up.
- **Losing a region's ledger:**
  1. Declare the loss and stop that region's admission.
  2. Rebuild from the archive up to the last fence plus the settle log:
     per-authorization posted amounts, terminal claims and scope claims.
  3. Release escrow not consumed as of the fence.
  4. Book each late settle or refund through the synchronous path, net of its
     archived posts, with `A` as the Spanner claim key.
  5. Answer replays of archived scopes from the archive.

  Only scopes and charges after the last fence are unknown. That window is the
  stated loss bound: the charges are revenue lost, and keyed scopes in it may
  execute again.

### 4.9 Settle durability, records and side effects

- **Intent before ledger.** Settle, refund and heartbeat events go to the settle
  log, identified by `(A, kind, sequence)`, before any ledger change.
  - If the ledger chain then fails or is unavailable, settle answers
    `intent_durable`, an existing disposition the gateway treats as success.
  - A recovery worker completes the chain from the log, and the terminal claim
    makes completion exactly-once.
  - A crash after the commit loses nothing: the payload is already durable.
- **Records are versioned.** One consumer joins log intents with ledger outcomes
  and CDC events. It writes the generation and activity record for `A` with
  revision precedence: a terminal settle or refund supersedes an expiry
  snapshot, and a higher revision wins. Duplicate CDC events cannot restore an
  older revision. Amount-sensitive consumers act on revision deltas: budget
  alerts, auto-refill, metadata webhooks, routing feedback and route-fallback
  reports.
- **Reconciliation** compares ledger terminal and expiry events with the records
  and alerts on any gap.
- **Lookups.** Disposition and evidence lookups read the ledger by `A`, and
  ClickHouse within T for `gateway_request_id`.
- **What stays synchronous for now.** Synthetic-probe workspaces, so release
  gates keep reading Spanner, and OAuth-app keys with a markup, until payouts
  have a durable obligation tied to each charge and reversal (§4.11).

### 4.10 Routing

- Python compiles routing into a versioned, signed snapshot on every catalog or
  health change.
- A snapshot that fails verification keeps the previous one, and one past its
  freshness limit stops fast admission.
- Per-workspace inputs are cached under the maximum age.

### 4.11 What stays synchronous at first

Credit-funded keys on standard catalog routes go first, streaming or not. These
stay on today's Python path, with keyed scopes still claimed at the front door
(§4.3):

- BYOK routes, custom and user-provided models, Polyphemus selection, native
  batch, video and image jobs, and hosted tools with
  `additional_cost_reservation_microdollars`;
- x402 and federated (deferred-settlement) keys;
- keys with window limits, and `budget_strict` keys;
- OAuth-app keys with a markup, and synthetic-probe workspaces;
- workspaces that are paused, in debt, below the minimum balance, or below the
  trust tier that allows grants. The carding incident of 2026-08 is why.

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

1. **Admission bound.** Every fast admission is a pending debit on a budget that
   cannot be overdrawn, behind an open gate.
2. **Conservation.** The auditor's identity in §4.7 holds at every fence.
3. **One terminal claim per authorization.** The loser answers with the
   winner's outcome.
4. **No charge lost.** Intents are durable before any ledger change. Expired
   holds still settle, net of posted heartbeats.
5. **No charge invented.** Only boot-signed heartbeats and settles post.
   Refunds and below-posted settles reverse.
6. **Overdraft.** It never moves `reserved`, and released headroom repays it
   before any admission can spend it.
7. **Ownership.** Exactly one deterministic ID per scope attempt, whichever path
   executes it. Ambiguity answers 503.
8. **Key caps.** Key budgets plus synchronous key holds never exceed the cap,
   and overruns consume it.
9. **Pauses** close the gate at once. No admission runs on state older than its
   maximum age.
10. **Returns** take only free funds, after the rollup, applied exactly once.
11. **Rollups** are monotone, fenced on a consistent counter snapshot, and
    drained FIFO by donor shard.
12. **Archive completeness.** A fence advances only after the archive holds
    everything before it.
13. **Loss bound.** Admission stops when the last fence is older than its bound.
    Loss is at most that window plus open holds.
14. **Records.** Every terminal or expiry event has one current record within T,
    with revision precedence.
15. **Latency** is stated as percentiles. Excursions take the documented
    fallback or a 503.

## 6. Latency and load

| Step | Estimate |
|---|---|
| Gateway to admission service, same region | 0.5-1 ms (more from AWS, Azure and non-home regions) |
| Boot signature, caches, routing evaluation | 0.2-1 ms |
| Linked hold (money, gate, key) | 1.5-4 ms p50; 10-30 ms p99 under load |
| Sign the envelope, reply | under 0.3 ms |
| **Total** | **about 3-6 ms p50; over 10 ms at p99** |

Most of the ledger load at 100T comes from heartbeats: one before the first byte,
then one per 10 s or 256 output tokens. Each heartbeat is two transfers, or four
with a key leg, plus a log publish. Settles and refunds add a claim, and
sometimes an overrun or a repay. The benchmark gate measures the whole path at
that rate, including a hot workspace and a replica failover.

## 7. Lessons from the retired regional-quota leases

The September pilot (`git show 44924155^:docs/design/regional-quota-leases.md`;
`docs/incidents/2026-09-26-regional-ledger-grant-storm.md`) had the same outline
and was retired on 2026-09-27.

| Pilot failure | This design |
|---|---|
| The lease ledger was Bigtable pinned to us-central1, so europe-west4 settles read it across the Atlantic and timed out | Ledgers are regional. Keyed requests go to the workspace's home region by design, and that latency is stated |
| A workspace went from about 6 to 430 authorizations a minute; grants aborted 94-96%; authorize p50 reached 3.7 s | Grants are never on the request path. One sweeper per region issues them serially, with a cooldown, sized by headroom |
| The ledger's p99 amplified into a fleet-wide Spanner abort storm | The request path never touches Spanner. Unkeyed fallback to the synchronous path goes through a per-workspace-region breaker and sheds with 503 when that path is saturated |
| Ambiguous leases were quarantined, not guessed back into service | Ambiguous keyed requests get 503 |

## 8. Rollout

1. **Measure** commits per generation after #1464 and #1465, and authorize's
   timing fields per region.
2. **Gateway load balancer and receipt-key publication** (§4.12), independent of
   the rest.
3. **A ledger spike.** Build the §4.5 chains against a real cluster and test the
   assumptions marked in this doc: retried-chain results, zero-amount balancing,
   cross-ledger chains and session limits.
4. **Shadow.** Gateways mirror authorize, heartbeat and settle. A comparator
   reports any difference from Python in decisions, holds, per-authorization
   charges and records.
5. **Benchmark gate** (§6).
6. **Pilot:** Joseph's own workspace, then a few large ones, with kill switches
   per workspace, region and cloud.
7. **Widen;** move payouts and the remaining route types (§4.11) one at a time;
   then retire the Python hot path.

## 9. Not decided here

- **The per-request store** (§2).
- **Tuning values:** grant sizing and cooldown, rollup intervals and fence-age
  bounds, the maximum state age, and the uncapped-key revocation window. They
  come from the spike, the benchmark and the pilot.
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
