# Fast admission and batched settlement

Status: **proposed, v7, 2026-10-02. Nothing built.** Codex and Fable have
reviewed six versions (§11). Joseph's decisions are in §2. **One decision is
still open: the per-request store (§2).** Since v5 the design has known that a
TigerBeetle cluster fills in days at this volume, so clusters must rotate
(§4.13). That bears on the decision.

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
| Storage at 100T | nothing is deleted: about 125 TiB a month by TigerBeetle's own figure. A region carrying half the traffic fills a 16 TiB data file in about 8 days, so clusters rotate (§4.13) | rows expire by TTL, so storage stays bounded |
| Cost at 100T | six replicas per cluster on local SSD, two clusters during a rotation, a broker for the change stream, and rollup commits | still a commit per request, at regional prices; to be measured |
| New work | the state machine in §4.5, the reaper, rotation, and the ledger's operations | moving balances into regional escrow; the code mostly exists |
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
- **Ledger.** A TigerBeetle cluster. Clients and replicas run one release, at
  least 0.16.43 (§4.2).
- **Sweeper.** One leader-elected worker for rollups, grants, returns, gates,
  cap activations and cluster rotation.
- **Reaper.** One leader-elected worker. It keeps each open authorization's
  latest heartbeat snapshot and resolves the holds whose gateway went silent
  (§4.5).
- **Settle log.** A Pub/Sub topic, ordered by authorization. It receives every
  heartbeat, and every settle, refund and reaper intent before its ledger
  change (§4.9).
- **Change stream and archive.** TigerBeetle's CDC job publishes every ledger
  event to an AMQP broker, and a writer archives the events to GCS. Rollups and
  recovery read only archived events (§4.7, §4.8).

Python keeps routing policy and everything off the hot path.

### 4.2 The ledger

Two ledgers in one cluster. A linked chain may span them, as in TigerBeetle's
currency-exchange recipe.

| Ledger | Accounts | Purpose |
|---|---|---|
| Money | per workspace-region: Budget (`debits_must_not_exceed_credits`), Sink, Overdraft, Pool | escrow, charges, debt, grants and returns |
| Control | per workspace: Gate. Per region: ScopeClaims and AdmitSink, GateSink, TerminalClaims and TerminalSink | scope claims, the admission gate, terminal claims |

Capped keys stay synchronous at first (§4.6), so there is no key ledger.

- **Only Budget accounts carry a balance flag.** On the others, `exceeds_*`
  cannot happen, so it can never permanently burn a deterministic ID.
- **Accounts exist before they are used.** A workspace's accounts are created
  by one linked `create_accounts` before its first grant. The region's control
  accounts are created with the cluster. A missing account is one of the seven
  transient errors that permanently fail a transfer ID
  ([`id_already_failed`](https://docs.tigerbeetle.com/reference/requests/create_transfers/#id_already_failed)).
- **Pool is per workspace,** so that workspace's counters show its grants and
  returns (§4.7).

**IDs are deterministic.** H is a 128-bit keyed hash that never yields 0 or
2^128−1.

| Transfer | ID |
|---|---|
| hold | `A` (§4.3) |
| every other leg of an authorization | `H(A, leg)`: `gate`, `repay`, `settle`, `settle/1..3`, `refund`, `reap`, `terminal`, `terminal/1..3` |
| scope claim | `H(scope)` |
| grant | the grant row's ID |
| return | `R = H(workspace, region, fence)`, and `H(R, repay)` for its repay leg |

A retry rebuilds the same IDs, so a lost response never applies a leg twice.
TigerBeetle's state machine ends a linked chain at the first result other than
`created`, `exists` included: the chain commits nothing, and only a transiently
failed ID burns. Every multi-leg operation here is therefore one chain whose
head leg answers `exists` exactly when the whole chain committed before:

- the scope claim, or `H(A, gate)` without one;
- `H(A, settle)`, `H(A, refund)` or `H(A, reap)`;
- `H(A, terminal)`;
- `H(R, repay)`;
- a grant's ID.

A retry that meets `exists` there reads the committed outcome. §8 keeps a test
of this for each release.

**Chain ends in the change stream.** An event of type `single_phase`,
`two_phase_pending`, `two_phase_posted` or `two_phase_voided` whose transfer
carries `flags.linked` continues a chain; any other event ends one. A
`two_phase_expired` event always ends one: it reports a hold that may have been
linked when created, long before. A chain's events are consecutive.

**Codes.** Holds have code 1. A post or void must carry its pending transfer's
code, or zero
([two-phase transfers](https://docs.tigerbeetle.com/coding/two-phase-transfers/)),
so the resolver is identified by its ID, not its code. Overrun legs, repays,
grants and returns, gate units, scope claims (fast, synchronous) and terminal
claims each have their own code. Holds carry the nonce and fingerprint hashes,
64 bits each, in `user_data_128`, the key's 64-bit ledger number in
`user_data_64`, and the snapshot version in `user_data_32`. Other legs carry `A`
in `user_data_128`.

**Versions.** Clients and replicas run the same release, 0.16.43 or later:

- **0.16.0:** balancing transfers may move zero, and a post of amount 0 posts
  zero. Single-phase amounts may also be zero.
- **0.16.4:** an ID that failed with a transient error stays failed. Ownership
  (§4.3) depends on it.
- **0.16.43 (cluster):** the CDC job
  ([requirements](https://docs.tigerbeetle.com/operating/cdc/)).

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
- **CDC:** one job per cluster (a second exits). Delivery is at least once, and
  the job resumes from the last acknowledged timestamp. Each event carries both
  accounts with their balances as of that event. Its lag bounds rollups,
  recovery and the reaper, and it is alerted.

**Growth.** TigerBeetle never deletes a transfer. Its
[hardware guide](https://docs.tigerbeetle.com/operating/hardware/) gives about
16 TiB per 40 billion transfers, roughly 440 bytes each, and ext4 limits a data
file to 16 TiB.

| Per fast generation | Transfers |
|---|---|
| Admission, unkeyed or keyed | 3 or 4: gate, repay and hold, plus the scope claim |
| Terminal | 1: a post or a void |
| An overrun (12% of settles today) | 3 more |
| A heartbeat | none (§4.5) |

That averages about 4.5.

- **At 100T tokens a month:** with 80% on the fast path, that is about 315B
  transfers, or about 125 TiB a month across regions.
- **A region carrying half the traffic** fills 16 TiB in about 8 days.

Clusters therefore rotate (§4.13). The data file is also bounded by the
machine's local SSD, about 9 to 36 TB per VM depending on the GCP machine
family. The spike measures bytes per transfer and the largest data file the
chosen machine and file system support.

**Gate before production:** benchmark the whole path, including chains, the
change stream, the reaper's probes and a rotation, on the real VM and disk shape. One
published cloud benchmark of an unoptimised deployment measured p50 32 ms and
p99 over 500 ms.

### 4.3 Ownership of a request

Exactly-once execution today rests on a unique index on the idempotency scope
(workspace, key, idempotency key), retained 30 days. Here, a keyed scope is
owned by one transfer: the scope claim `H(scope)`, written at most once in the
home region's control ledger.

- **Fast path.** The claim is the first leg of the admission chain (§4.4) and
  carries `A`. It commits exactly when the admission commits, so a failed chain
  leaves no claim.
- **Synchronous path.** The front door writes the claim alone, with the
  synchronous code and the caller's `A`, and forwards the request to Python.
  Python's own replay checks then apply, as today.
- **The first write decides.** A later request for the scope gets `exists` or
  an `exists_with_different_*` result, reads the claim, and goes to its owner.
  The claim touches no flagged account. It can fail transiently, and burn, only
  in a cluster that has stopped admitting (§4.13), where nothing will look for
  it.
- **The authorization ID** is `A = H(scope, nonce)`. The nonce identifies one
  gateway invocation, and the gateway retries with the same bytes, nonce and
  key.
- **Replays answer as today** (`routes/internal/gateway.py:1379-1385`), from
  what the claim and the hold store:
  - the caller's own `A`: the same invocation, which never acted on the lost
    answer. The service re-signs the envelope against the hold's snapshot
    version, which is retained because the hold is recent;
  - another `A` with the same fingerprint: a replay marker carrying the original
    nonce's hash. The gateway compares its own nonce's hash and answers 409
    `idempotency_replay`, as it does now for another nonce. Today the answer
    carries the raw nonce, so the gateway changes to compare hashes;
  - a different fingerprint: 409.
- **A claim counts for 30 days after its admission, plus the longest hold:**
  today's retention. A request that finds an older claim treats it as absent
  and goes to Python under a synchronous signature, where the unique index
  arbitrates concurrent duplicates, as it does for any scope today. TigerBeetle
  cannot reuse the claim's ID, so this is how an expired scope is reused.
- **A synchronous claim pins the scope to Python** while Python holds a
  reservation for it; §4.13 carries it that long. Every request for it is
  forwarded, and Python's own idempotency decides. A request Python rejected
  leaves the scope free to retry there, as today, and no fast claim can compete
  for it.
- **Failed attempts burn only their own invocation's IDs** (`A`, `H(A, gate)`
  and so on). That invocation's retries fail the same way. A new invocation has
  a new nonce, a new `A` and a clean chain. There is no attempt counter, so no
  path can take an earlier attempt's place.
- **Unkeyed requests** have a scope the gateway minted for the invocation, so
  they need no claim. They may use the nearest region. They fall back to the
  synchronous path only after a definitive failure, never after a timeout, and
  behind the breaker in §7.
- **Python stays bounded without knowing about the ledger.** Grants move escrow
  into `reserved`, which synchronous reservations cannot use, so a stale
  synchronous worker cannot spend regional escrow.

**Workspace modes.** A Spanner row per workspace holds its mode and
`front_door_until`. Python's authorize transaction checks it with a predicate on
the reservation INSERT, which every authorize runs. The check is then atomic
with the reservation, and the transaction stays DML-only and never reads
`tr_key_limit`. A transaction that read the old mode commits before the change
or aborts. A zero-row insert is a mode refusal, answered 503, never insufficient
credits.

| Mode | Unkeyed requests | Keyed requests |
|---|---|---|
| synchronous | Python | Python, as today |
| front door | fast path | the front door writes a synchronous claim; Python accepts only with the front door's signature over it |
| fast | fast path | fast path, claimed |
| leaving (`front_door_until` ahead) | Python | as in front-door mode |

The gate is open in front-door and fast modes and closed otherwise.

**Transitions:**

- **Into fast mode,** in two steps and with no backfill:
  1. Set `front door`. Unkeyed requests take the fast path at once. From then
     on, Python refuses a keyed request that carries no signature, so a front
     door still on the old mode is refused and refreshes.
  2. Set `fast` once Spanner shows no keyed reservation of the workspace
     created before step 1.
     - The check is a range scan of `tr_reservation_by_idemp` over the
       workspace's scope prefix (`<workspace>#`), so no new index is needed.
     - It also catches reservations whose settlement is pending or dead,
       because they stay until settled and 30 days on.
     - Until it passes, keyed requests keep their synchronous claims.
- **Out of fast mode** (pause, revoke, trust downgrade, or a switch): close the
  gate (§4.7), then set `leaving` with `front_door_until` 30 days plus the
  longest hold ahead. Until then keyed requests still pass the front door, so
  replays of fast scopes find their claims.
- **A home-region move** is the exit, then the entry in the new region once
  `front_door_until` has passed. Claims never move between regions.

### 4.4 Authorize

1. Verify the boot signature and the accepted image digest, against caches with
   a maximum age.
2. Evaluate the compiled routing snapshot (§4.10) and compute the estimate e.
   Without `max_tokens`, it assumes 512 output tokens.
3. One linked chain, in this order:

   | Leg | ID |
   |---|---|
   | scope claim, keyed requests only: one unit `ScopeClaims→AdmitSink`, carrying `A` | `H(scope)` |
   | one unit `Gate→AdmitSink`; it fails while the gate is closed | `H(A, gate)` |
   | repay: `Budget→Overdraft`, `balancing_debit` and `balancing_credit`, amount `AMOUNT_MAX` | `H(A, repay)` |
   | the hold: pending `Budget→Sink` for e, timeout T (§4.5) | `A` |

   The repay moves the smaller of the free budget and the debt, so debt is paid
   before the hold can draw. With no debt it moves zero. The generation ID is
   derived from `A`, as today.
4. Answer with a **signed envelope** that the gateway echoes on heartbeat,
   settle and refund:
   - `A` and the generation ID;
   - the frozen candidates, prices, fees and app terms;
   - the snapshot version and the boot binding;
   - the hold amounts, the deadline rule and the end of life (§4.5).

   Snapshots stay immutable while any hold references them.

**Responses:**

- **`exceeds_credits` on the hold** means a regional shortfall. The sweeper may
  be behind, or debt was repaid first. A keyed request gets 503 with
  `Retry-After`; an unkeyed one takes the synchronous path.
- **402 only when the balance cannot cover the estimate.** Python changes so
  that when the workspace's balance covers it but the headroom outside regional
  escrow does not, it answers 503 with `Retry-After`.
  `_insufficient_credits_error` already tells those cases apart.
- **A closed gate or admission sink** sends the service to refresh the
  workspace's mode and the region's cluster (§4.13), then route again.
- **A lost authorize response** is covered by §4.3: the retry rebuilds the same
  chain.

### 4.5 One authorization: terminals, heartbeats and the reaper

**Every settle, refund and reaper decision writes its intent to the settle log
first** (§4.9). Only then does it touch the ledger.

**Times.**

- **End of life:** the gateway ends a stream still running at 2 h 15 min, and
  settles it. Today renewal is open-ended. The shadow phase checks the cap
  against the longest real streams.
- **Deadline:** today's rule, capped at 2 h 20 min. That is two hours after
  admission, or 300 s after the latest accepted heartbeat if that is later.
- **The reaper acts at the deadline plus 60 s at the earliest** (below). A
  stream ended at its end of life therefore has more than five minutes to
  settle first.
- **The ledger timeout T** is 2 h 30 min. It is only a backstop for a reaper
  that is down: before it fires, every hold has been resolved by a terminal or
  by the reaper.

**While the hold is open, the hold arbitrates.** A pending transfer resolves
once.

| Event | Chain | IDs |
|---|---|---|
| Settle, a ≤ e | post the hold for a | `H(A, settle)` |
| Settle, a > e | post the hold for e; overrun for E = a − e | `H(A, settle)`, `H(A, settle/1..3)` |
| Refund | void the hold | `H(A, refund)` |
| Reaper, snapshot s | post the hold for s, or void it when s is 0 | `H(A, reap)` |

Heartbeats keep s within e, as today, so the reaper never overruns.

**Overrun of a known amount E,** three linked transfers:

1. `Overdraft→Budget` for E;
2. `Budget→Sink` for E;
3. `Budget→Overdraft` for E, flagged `balancing_debit` and `balancing_credit`.

The customer is charged the full E. Free budget pays what it can, and the rest
stays as debt. The next admission's repay leg (§4.4) or return (§4.7) pays it
first.

**Contention.** The loser always reports the winner's outcome, never its own
request.

| Result on the post or void | Meaning | Action |
|---|---|---|
| `exists` | a retry of the same resolution | read the committed outcome |
| `exists_with_different_*` | the same resolution committed with other values, such as an amount | read the committed outcome |
| `pending_transfer_already_posted` or `pending_transfer_already_voided` | another resolution won | look up `H(A, settle)`, `H(A, refund)` and `H(A, reap)`, and report the one that exists |
| `pending_transfer_expired` | the backstop fired | use the rows below |

None of these burns an ID. Only the seven transient errors do.

**After the backstop has fired**, which happens only when the reaper was down
past T, a terminal claim heads each chain: one unit `TerminalClaims→TerminalSink`,
ID `H(A, terminal)`, with the kind in `user_data_64`.

| Event | Chain after the claim |
|---|---|
| Settle | overrun for E = a |
| Reaper, snapshot s | overrun for E = s |
| Refund | nothing |

The first claim wins. `exists` is a retry. Any `exists_with_different_*` result
means another terminal won, whether it differs in kind or only in flags (a
refund's claim stands alone, unlinked): the loser reads the committed claim and
reports it.

**Decision 70 stays.** When the reaper's snapshot wins, a later settle or refund
is a no-op, as today (`settle_outbox_apply.py:159-164`). When a settle or refund
wins first, the reaper does nothing. Nothing is charged before a terminal, so
nothing is ever reversed.

**Heartbeats never write to the ledger.** Each heartbeat:

1. is checked against the envelope by whichever admission node receives it: the
   endpoint pin, and a running charge within the hold;
2. is published to the settle log under `A`;
3. is answered after the publish is acknowledged:
   - `accepted`, with the new deadline, signed, but only if the acknowledgement
     arrived before the deadline the heartbeat echoes;
   - `deadline_passed` otherwise. The gateway stops and settles, and that settle
     may lose to the reaper, as a late heartbeat can lose today;
   - `already_terminal`, when one batched lookup finds a resolution or a
     terminal claim;
   - `end_of_life`, past the end of life. The gateway stops the stream and
     settles.

**The reaper is the authority on heartbeats.** It consumes the log in order per
authorization.

- **Deadlines:** a signed heartbeat's deadline counts whether or not its usage
  is valid, because the gateway was told `accepted`.
- **Snapshots:** only a valid heartbeat moves the snapshot. Validity is today's
  checks (sequence, endpoint, token non-regression and the cap) against durable
  per-authorization state: the last accepted sequence, payload hash, token
  counts and running charge. So an altered payload under an accepted sequence
  number is ignored, whichever admission node took it.
- **Hold events:** the archived change stream tells it when holds open and
  resolve.

**The reaper resolves the holds of silent gateways.**

- **At the deadline plus 60 s, it publishes a probe under `A`** in the hold's
  region, and acts only when it consumes that probe.
  - Messages under one ordering key in one region arrive in the order they were
    published. By then the reaper has seen every heartbeat answered `accepted`,
    all acknowledged before their deadline, and every settle or refund intent
    published before the probe.
  - The spike verifies that ordering across publishers.
  - A probe made stale by a renewal or an intent is dropped.
- **It never reaps an authorization with a durable settle or refund intent.**
  It completes that intent instead, as today's reaper guards pending intents
  (`storage_gcp_authorize.py:1373`).
- **Otherwise it writes its intent** (`A`, snapshot s) to the log, then posts
  the hold for s, or voids it when s is 0. Any settle or refund races it
  through the hold itself.
- **If its probes stop arriving,** because a key or the consumer is stalled,
  holds reach the backstop, and their settles book a in full through the rows
  above. Nothing waits on a monitoring metric, and a slow log never turns a
  live stream's tail into a free one.
- **Heartbeats and terminals go to the region the envelope names,** wherever
  the gateway runs, so one ordering key orders them.
- **Its state is checkpointed** with its log and stream positions. After a
  restart it is rebuilt by replay, which needs retained acknowledged messages
  and seek on the subscription.

### 4.6 Keys

**Capped keys stay synchronous at first,** like window-limited and
`budget_strict` keys (§4.11). On the fast path, a lowered cap could be refilled
by holds released afterwards, unless every key had its own ledger accounts,
gate and epochs. That machinery waits until the shadow phase shows how much
traffic uses capped keys.

**Uncapped keys:**

- Revocation reaches admission through the key-status cache, whose maximum age
  is short and stated as the exposure.
- Their usage is booked to `tr_key_limit` by the sweeper's rollups. Each
  archived charge is attributed through its hold's key number.
- **Adding a cap** moves the key to the synchronous path.
  - Python serves it only after the sweeper's fence has passed a marker
    transfer written after the change, so every earlier fast charge is booked.
  - Until then the key answers 503 with `Retry-After`.
  - Requests already admitted settle in full and may pass the cap, as they can
    today.

### 4.7 Budget: grants, returns, rollups and gates

The sweeper does all Spanner work, off the request path, in this order per
workspace-region: rollup, then return, then grant.

**Rollups come from the archived change stream.** A fence is a timestamp t. The
workspace's balances at t are those carried by the last archived event at or
before t that touched each account. They are exact and consistent with each
other, and everything before t is archived by construction.

The archive writer keeps a checkpointed view of each account's latest archived
balances, and of the timestamp up to which the archive is complete. Balances are
absolute, so a duplicate delivery changes nothing. Rollups read that view and
never scan the stream.

**Fences fall only at chain ends** (§4.2). The archive's completion timestamp
advances only past an event that ends a chain. A fence or a recovery cut
therefore never splits one, so a terminal's post and its overrun legs are
archived together or not at all.

| Delta since the last fence | Definition |
|---|---|
| charges ΔC | the Sink's credits |
| debt ΔO | the Overdraft's debits minus its credits |
| returns ΔR | the Pool's credits |
| grants landed ΔG | the Pool's debits |

One Spanner transaction, conditional on the stored fence, then:

- drains the escrow consumed, ΔC − ΔO, across open grant rows oldest first.
  On each drained row's shard it applies `total_usage += y` and
  `reserved −= y`, so that shard's headroom is unchanged;
- books the signed ΔO to `total_usage` on shard 0. New debt lowers shard 0's
  headroom, as an overrun lowers its shard's headroom today, and a repayment
  restores it. No other shard's bound moves, so Python cannot spend headroom
  that charges already used;
- drains ΔR across open rows oldest first, releasing `reserved` on their
  shards;
- closes any row that reaches zero;
- marks grant rows landed through ΔG;
- applies today's recovery-debt absorption to returned funds;
- books uncapped keys' usage;
- moves the fence to t.

A replay of the same fence is a no-op. Between two chain-end fences, neither
ΔC − ΔO nor ΔR is ever negative, so rows only drain:

- nothing reverses a charge (§4.5);
- a repayment raises consumption;
- new debt is never larger than the overrun that created it.

**Grants:**

1. One Spanner transaction checks the workspace's headroom as the signed sum
   over its shards, so debt on shard 0 counts, as the credit rebalancer already
   does. It then adds g to `reserved` on a donor shard with enough headroom,
   and writes a grant row with that shard.
2. A `Pool→Budget` transfer for g, with the row's ID. Neither account can refuse
   it once both exist, so it does not fail; a replay answers `exists`.

Grants are sized by headroom, not trailing spend, with a per-workspace cooldown.
They leave headroom outside escrow for the workspace's synchronous requests.

**Returns** are one linked chain:

1. a repay leg, so debt is paid before funds leave;
2. `Budget→Pool`, flagged `balancing_debit`, for at most the amount to return.

The next rollup sees the return in the Pool's counters and releases exactly that
amount from `reserved`. A return therefore never moves more than the ledger
holds, and Spanner releases only what archived counters show. No read can race
the write.

**The auditor's identity at every fence t:**

`reserved` = the Budget's credits minus its debits, both posted, at t + grant
rows written but not landed by t.

Every term is exact.

**Pause, revoke, trust downgrade, and a switch out of fast mode:**

- Close the gate with a pending `closing_debit` transfer from Gate to GateSink:
  zero amount, timeout 0, ID per epoch. New admissions fail at once. Its credit
  side is not AdmitSink, so a rotation that closes AdmitSink never touches it.
- Sweep free funds back with returns.
- Reopen by voiding the closing transfer.

The gate is its own account because a closed account accepts nothing but voids
of pending transfers
([`flags.closed`](https://docs.tigerbeetle.com/reference/account/#flagsclosed)).
Closing the Budget would stop settles from posting.

**Home settlement.** Deferred usage from a peer plane is booked to Spanner
unconditionally, as today. When `available` falls below the outstanding regional
budgets, the sweeper grants nothing more and returns budget until it is
non-negative. The extra exposure is at most the open holds, and it is alerted.

### 4.8 Freshness, loss and recovery

- **Maximum state age.** Cached boot, workspace and key state has one; past it,
  admission stops. Pauses close the gate, so they do not depend on cache age.
- **Archive-lag gate.** If a region's archive falls behind by more than its
  bound, admission stops there: 503 for keyed requests, the synchronous path for
  unkeyed ones.
- **Losing a region's ledger:**
  1. Declare the loss and stop that region's admission.
  2. Roll up to the archive's last event, t_a, so everything archived is booked.
     Then release each workspace's remaining `reserved` in the region, except
     what open holds account for. That part stays reserved until their
     terminals are booked or their deadlines pass, as in home settlement
     (§4.7).
  3. Book the rest from the settle log through the synchronous path, claimed in
     Spanner by `A`, so each is booked once. That covers every settle, refund
     and reaper intent whose terminal is not in the archive:
     - a settle books a in full, since nothing is charged before a terminal;
     - a refund books nothing;
     - when an authorization has more than one logged terminal intent,
       recovery books the cheapest: a refund, then the reaper's snapshot, then
       a settle. It never charges more than the lost ledger could have. Where
       the ledger chose a costlier terminal, the difference is revenue lost,
       and stated as part of the loss;
     - the reaper books its snapshot, and finishes open authorizations from its
       state, rebuilt from the log and the archive if the region took it too.
  4. Seed the replacement cluster's scope claims from the archive, so replays
     find them on the request path.

  Only keyed scopes claimed after t_a are unknown. They may execute again if
  replayed, and that window, bounded by the archive-lag gate, is the stated
  loss. No charge is lost once its intent reached the settle log.

### 4.9 Settle durability, records and side effects

- **Intent before ledger.** Settle, refund and reaper intents go to the settle
  log, identified by `(A, kind)`, before any ledger change.
  - If the chain then fails, or the ledger is unavailable, settle answers
    `intent_durable`, an existing disposition the gateway treats as success.
  - A recovery worker completes these intents from the log. The hold's single
    resolution, or the terminal claim, makes completion exactly-once.
  - Heartbeats are consumed only by the reaper, which also reads settle and
    refund intents so that it never reaps over one (§4.5).
- **Records.** One consumer joins the log with the archived ledger events.
  - It writes each authorization's record once, from the winning terminal.
    Duplicate events are idempotent on `A` and cannot change the winner.
  - Amount-sensitive consumers act once per terminal: budget alerts,
    auto-refill, metadata webhooks, routing feedback and route-fallback reports.
- **Reconciliation** compares the ledger's resolutions and terminal claims with
  the records, and alerts on any gap.
- **Lookups.** Disposition and evidence lookups read the ledger by `A`, and
  ClickHouse within the records bound for `gateway_request_id`.
- **What stays synchronous for now.**
  - Synthetic-probe workspaces, so release gates keep reading Spanner.
  - OAuth-app keys with a markup, until payouts have a durable obligation tied
    to each charge (§4.11).

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
- keys with lifetime caps or window limits, and `budget_strict` keys (§4.6);
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

### 4.13 Cluster rotation

Each region retires a cluster before its data file fills (§4.2):

1. **Prepare.** Format the next cluster and create its control accounts. The
   sweeper creates each active workspace's accounts there. While the old
   cluster still admits, it returns a share of each workspace's free funds
   there and grants that share in the new cluster, so admissions find funds
   after the switch.
2. **Switch.** The sweeper closes the old cluster's AdmitSink with a pending
   closing transfer. From that commit, no gate leg or scope claim succeeds
   there.
   - The new configuration is published only after the close commits.
   - A node admits in the new cluster only after reading that closing transfer,
     whose ID is per rotation, in the old one.
   - So the two clusters never admit at once, and the lookup in step 3 always
     sees a frozen old cluster.
   - The sweeper then returns the old cluster's remaining free funds and grants
     the new cluster from them.
   - Terminal claims and gate closings use their own accounts, so late
     terminals and pauses still work in the old cluster.
3. **Carry the claims.** The sweeper copies into the new cluster the old
   cluster's fast claims whose original time is within the retention (30 days
   plus the longest hold), and its synchronous claims whose scope Python still
   holds a reservation for. That holding is checked in Spanner by scope during
   the copy.
   - Each copy keeps its code and `A`, records the old cluster, and carries the
     original time in `user_data_64`, so later rotations count from the first
     claim, not from the copy.
   - The copy's size is the keyed share of traffic over that window. A cluster
     rotates early enough to leave room for it. If the keyed share is too large
     for that, keyed requests stay synchronous in that region. The shadow phase
     measures it.
   - Until the copy finishes, the front door looks a scope up in the new
     cluster, then in the old one.
4. **Drain.** Holds in the old cluster settle there; the envelope names the
   cluster.
   - The reaper resolves the rest by their deadlines.
   - The sweeper keeps rolling up and returning there until no hold is open,
     then runs a final rollup, and Spanner releases what remains.
5. **Retire** the old cluster once every authorization admitted there has a
   terminal there and the archive holds its last event.
   - A terminal means a resolution of the hold, or a terminal claim after the
     backstop. Zero open holds is not enough: a backstop-expired hold may still
     wait for its settle.
   - The recovery worker and the reaper finish those first. The sweeper counts
     open authorizations from the archive view.
   - After retirement, a late settle, refund or reaper intent for one of its
     authorizations answers `already_terminal` and books nothing, since each
     already has a terminal that a rollup booked.
   - A request whose carried claim points at it comes from another invocation,
     and is answered 409 `idempotency_replay`, where the gateway's nonce rule
     would have led.

Two clusters run in a region only during a drain: about T, plus the copy.

## 5. Invariants

Each has a production check.

1. **Admission bound.** Every fast admission is a pending debit on a budget that
   cannot be overdrawn, behind an open gate, after free funds have repaid any
   debt.
2. **Conservation.** The auditor's identity in §4.7 holds at every fence.
3. **One terminal per authorization.** The hold resolves once; after a backstop
   expiry, the terminal claim decides. The loser answers with the winner's
   outcome.
4. **No charge lost.** Intents are durable before any ledger change. The reaper
   books the snapshot of every hold its gateway abandoned. It acts only after
   its probe has come back through the authorization's ordering key, and never
   reaps an authorization with a durable settle or refund intent.
5. **No charge invented.** Only boot-signed settles, and the reaper's snapshots
   of validated heartbeats, post. Nothing reverses a charge.
6. **Debt.** Charges beyond free budget become debt. Admissions and returns
   repay it from free funds first, so released headroom never funds anything
   while debt is outstanding.
7. **Ownership.** A keyed scope has one claim, written once: atomically with its
   admission, or before Python sees the request. Python's acceptance is atomic
   with the workspace's mode. Ambiguity answers 503.
8. **Key caps.** Capped keys are served synchronously. Adding a cap waits for a
   fence, so the synchronous check sees every earlier fast charge. Requests in
   flight when a cap is added may pass it, as today.
9. **Pauses** close the gate at once. No admission runs on state older than its
   maximum age.
10. **Returns** move only free funds, after repaying debt. Spanner releases them
    only from archived counters.
11. **Rollups** are monotone, fenced at archived chain ends, and drain grant rows
    oldest first. Every shard's headroom is unchanged by them, except shard 0's
    by outstanding debt.
12. **Archive completeness.** Rollups and recovery read only archived events,
    and never a partial chain.
13. **Loss bound.** Admission stops when the archive lags past its bound. Loss is
    at most the keyed scopes claimed in that window. Beyond that, an
    authorization whose terminal was not archived is booked at its cheapest
    logged terminal, never above what the lost ledger could have charged.
14. **Records.** Every terminal has one record within the records bound.
15. **Growth.** A cluster rotates before its data file fills, and retires only
    when every authorization admitted there has a terminal.
16. **Latency** is stated as percentiles. Excursions take the documented
    fallback or a 503.

## 6. Latency and load

| Step | Estimate |
|---|---|
| Gateway to admission service, same region | 0.5-1 ms (more from AWS, Azure and non-home regions) |
| Boot signature, caches, routing evaluation | 0.2-1 ms |
| Linked admission (claim, gate, repay, hold) | 1.5-4 ms p50; 10-30 ms p99 under load |
| Sign the envelope, reply | under 0.3 ms |
| **Total** | **about 3-6 ms p50; over 10 ms at p99** |

Ledger load is admissions and terminals: about 4.5 transfers per generation
(§4.2). A heartbeat costs one settle-log publish, one batched ledger lookup for
`already_terminal`, and no ledger write. The reaper adds one probe per
authorization that reaches its deadline. The one
before the first byte waits for that publish, as it waits for a Spanner commit
today; the benchmark measures it. The benchmark gate runs the whole path at the
target rate, including a hot workspace, a replica failover and a rotation.

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
   assumptions marked in this doc:
   - `exists` inside a linked chain;
   - zero-amount balancing and cross-ledger chains;
   - session limits;
   - bytes per transfer and the largest data file;
   - change-stream throughput and a rotation.
4. **Shadow.** Gateways mirror authorize, heartbeat and settle. A comparator
   reports any difference from Python in decisions, holds, per-authorization
   charges, reaper outcomes and records. The phase also measures:
   - the longest streams, against the end of life (§4.5);
   - the keyed share of traffic (§4.13);
   - the share on capped keys (§4.6).
5. **Benchmark gate** (§6).
6. **Pilot:** Joseph's own workspace, then a few large ones, with kill switches
   per workspace, region and cloud.
7. **Widen;** move payouts and the remaining route types (§4.11) one at a time;
   then retire the Python hot path.

## 9. Not decided here

- **The per-request store** (§2).
- **Tuning values:** grant sizing and cooldown, the rollup interval, the
  archive-lag bound, the reaper's log-lag bound, the maximum state age, the
  uncapped-key revocation window, the end of life and the rotation threshold.
  They come from the spike, the benchmark and the pilot.
- **The keyed share of traffic,** which sizes each rotation's claim copy (§4.13).
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
