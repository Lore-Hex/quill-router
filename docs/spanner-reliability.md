# Spanner reliability

TrustedRouter's production ledger uses the Enterprise Plus `nam6`
multi-region configuration. The application must still monitor transaction
contention and client behavior because the managed-service SLA does not cover
application bugs, quota exhaustion, or hot-row retries.

## Production baseline

- Source project: `quill-cloud-proxy`
- Source instance/database: `trusted-router-nam6` / `trusted-router`
- Configuration: `nam6`, Enterprise Plus, capacity autoscaled by Terraform between 1,000 and 10,000 processing units (`infra/spanner_trusted_router.tf`; 400 until 2026-09-25 and 600 after, see the High CPU section). `spanner_reliability.sh` does not set capacity.
- Replica topology: two read-write locations (`us-central1`, `us-east1`), two
  read-only locations (`us-west1`, `us-west2`), and one witness
  (`us-central2`). This is five replica locations, not four application
  regions. `nam6` cannot be edited down to three locations; a three-location
  topology would require a separate migration to `nam7`.
- Database deletion protection: enabled
- Point-in-time recovery: 7 days
- Backups: daily full plus incremental every 4 hours, retained for 7 days
- Isolated copy: latest daily full backup copied every day to
  `trustedrouter-dr/trusted-router-backups`, retained for 30 days
- Alert destination: `security@trustedrouter.com`
- Alert conditions reduce replica and method time series before evaluation, so
  one condition opens one incident instead of one incident per series.

Apply or reconcile the baseline:

```bash
bash scripts/deploy/spanner_reliability.sh --apply
```

Set `TR_DR_BILLING_ACCOUNT` on the first run if the DR project does not exist.
Without `--apply`, the script prints the intended changes.

Customer-facing billing-path alerts are managed separately so they can be
reconciled without touching Spanner capacity or backup resources:

```bash
bash scripts/deploy/gateway_reliability.sh --apply
```

These policies deliberately trigger before the broad Spanner contention
policy:

- Any `5xx` from `/internal/gateway/authorize`, `settle`, or `refund` opens an
  incident immediately, except an authorize `503` or a settle `503`, which
  the counted policies below take over. A refund `503` still pages here. A single `500` with zero latency, no instance id and
  no container output, logged against the revision being retired at the
  second its instances print "Shutting down", is the rollout race: stock
  uvicorn closed its listener the instant SIGTERM arrived and refused a
  request the front end had already dispatched (five such incidents in the
  week to 2026-09-25, all at rollouts). The image now runs
  `python -m trusted_router.serve`, which keeps accepting for
  `TR_SHUTDOWN_DRAIN_SECONDS` (default and maximum 3 seconds) after the first
  SIGTERM before uvicorn's graceful shutdown, which waits for in-flight requests
  without a request timeout. Cloud Run's 10-second SIGKILL is the only hard
  deadline; request completion and process exit before it are not guaranteed.
  A second SIGTERM or any SIGINT starts ordinary graceful shutdown without
  waiting for the drain window. If one recurs, check the serving revision's
  entry point before anything else.
- An authorize `503` is counted rather than paged at once. "TR Gateway:
  authorize 503 not lease headroom" compares the authorize 503s in Cloud
  Run's request log (`trustedrouter_gateway_authorize_503`) with the lines
  the app writes for the four 503s it answers by design, and opens an
  incident when the two counts differ for three minutes. All four are
  retryable, carry `Retry-After: 1` and are retried by the enclave:
  - lease headroom, `billing.lease_headroom_unavailable`
    (`trustedrouter_gateway_lease_headroom_503`): the workspace's shortfall
    is held in fast-path leases (`TR_FASTPATH_LEASE_HEADROOM_503`), and its
    headroom with them is at least the nodes' floor
    (`TR_FASTPATH_LEASE_HEADROOM_503_FLOOR_MICRO`; under it the answer is
    the insufficient-credits 402);
  - rebalance busy, `billing.credit_rebalance_busy`
    (`trustedrouter_gateway_rebalance_busy_503`): the workspace's credit
    covers the hold but is spread across credit shards, and the per-process
    rebalance cooldown is held by a rebalance another of its requests just
    ran. Until this line existed the 503 had no mark and paged as storage
    impact (one false page on 2026-11-16);
  - strict budget busy, `billing.strict_budget_busy` written by
    `trusted_router.routes.internal.gateway`
    (`trustedrouter_gateway_strict_budget_busy_503`): a key with a strict
    budget could not get its per-process admission slot (one strict
    authorize per key, 16 strict keys per instance). Until this line
    existed the 503 had no mark and paged
    as storage impact (one false page on 2026-11-17). The handler's own
    `WARNING trusted_router.main billing.strict_budget_busy` line for the
    same 503 has no trace and is not counted;
  - headroom raced, `billing.credit_headroom_raced`
    (`trustedrouter_gateway_headroom_raced_503`): the workspace's credit is
    spread across credit shards, and its own concurrent requests took the
    headroom from every bounded repair of the credit recovery (`credit
    headroom changed concurrently`). Until this line existed the 503 had no
    mark and paged as storage impact (one false page on 2026-11-18).

  Every other authorize 503 writes none of these lines and pages: a Spanner
  outage or deadline, no instance, a crash, a paused workspace, and `credit
  headroom changed concurrently` from a recovery that stopped early (its
  forced shard-count reload failed) or followed a failed debt-mark heal. More lines than 503s pages too,
  because a line written without its 503 could hide a real one. The deploy
  gate (`scripts/deploy/assert_no_billing_5xx.sh`) draws the same line per
  request, by trace.
- "TR Gateway: credit rebalance busy" opens an incident when more than two
  rebalance-busy 503s land in ten minutes, or more than five in an hour. The
  policy above no longer pages on them, so this one bounds them. See "Credit
  rebalance busy" below.
- "TR Gateway: strict budget busy" opens an incident when more than two
  strict-budget-busy 503s land in ten minutes, or more than five in an hour,
  for the same reason. See "Strict budget busy" below.
- "TR Gateway: credit headroom raced" opens an incident when more than two
  headroom-raced 503s land in ten minutes, or more than five in an hour,
  for the same reason. See "Credit headroom raced" below.
- A settle `503` is counted rather than paged at once, as an authorize `503`
  is. The snapshot settle (`services/async_settle_handler.py`, `unavailable`)
  answers its designed 503s with `Retry-After: 1` (`finish_unconfirmed`,
  `transient_store_error` and its other reasons) and writes one
  `WARNING ... async_settle.unavailable reason=...` line for each, for
  settle and refund alike (`trustedrouter_gateway_settle_unavailable_503`).
  The gateway retries the same settlement identity and the charge is
  recovered. "TR Gateway: settle 503 not marked retryable" compares the
  settle and refund 503s in Cloud Run's request log
  (`trustedrouter_gateway_settle_503`) with those lines and opens an
  incident when the two counts differ for three minutes: a settle 503 with
  no line (Spanner outage, no instance, a crash, a legacy settle path's own
  503) pages, and so do lines without 503s. A refund 503 is counted there
  too, because its line is the same, and pages at once on the billing 5xx
  policy as well. "TR Gateway: settle unavailable surge" opens an incident
  when more than two marked settle 503s land in ten minutes, or more than
  five in an hour (the baseline is about one a week); read the lines'
  `reason=` first.
- A run of `503`s from one instance id, each with tens of seconds of latency
  and "connection to the instance had an error", while that instance's own
  output stops and its CPU stays flat, is a process whose event loop stopped
  (two in europe-west4 on 2026-10-28, silent for one and two minutes). With
  `TR_LOOP_STALL_EXIT_SECONDS=30`, which `scripts/deploy/rollout.sh` sets
  for this service because it keeps CPU allocated between requests, the same
  runner ends such a process once its loop has gone about 30 seconds (29
  at the least) without a tick. `faulthandler` first writes every thread's
  stack to stderr under a
  `Timeout (0:00:30)!` line and exits with status 1. It writes them through a
  non-blocking descriptor of its own, so a backed-up stderr loses the stacks
  rather than holding the exit. Read the stacks from the instance's stderr:
  the frame each thread is waiting in is the cause, and it belongs in the
  follow-up. An exit with no stacks points at the log pipe, and a
  `serve.stall_dump_blocking` warning at startup means the descriptor fell
  back to stderr itself.
- Successful billing calls taking at least 10 seconds are counted. More than
  two per minute for three consecutive minutes opens one incident.
- The general Spanner contention policy has three independent gates: more than
  100 aborted commits per minute for 10 minutes, lock wait above 2 seconds per
  second for 10 minutes, or lock wait above 30 seconds per second for 5
  minutes. It is intentionally separate because metadata, rate-limit, and
  maintenance transactions must not be reported as customer-facing billing
  degradation.

Every condition reduces all regions and revisions into one time series. The
log-based `5xx` policy rate-limits notifications to one every 30 minutes and
auto-closes after 30 quiet minutes. Metric policies do not renotify while an
incident remains open. Unauthenticated safe reads use a process-local
rate-limit counter so a crawler cannot create a transactional Spanner hot row.
The alerts use status, latency, path, and aggregate transaction metadata only;
they never inspect or export prompts or outputs.

### Admission read failures

The read-only async admission and drain-health observers use request tags
`tr_async_admission` and `tr_async_drain_health`. Their 500 ms budget is shared
across Spanner result-stream restarts, not renewed for each RPC. The SDK can
restart streams on UNAVAILABLE or transport resets even with `retry=None`.
An exhausted read remains ineligible; it never authorizes from partial results.

Failed reads emit `async admission read failed` with a fixed operation, error
category, elapsed milliseconds, and failure count. Identical operation/category
pairs log at most once per minute per process, counting suppressed repetitions
in the next failure log. Different categories and operations are independent.
Successful reads remain quiet. No SQL parameters, workspace IDs, exception
text, or stack traces are attached. This local log limit does not sample the
Spanner API failure metric or change any alert threshold.

An API-failure alert counts RPCs, not customers or failed inference requests.
Correlate its window with these tags, billing request logs, and per-method/status
Monitoring data before assigning impact. Lack of permission to read that data
is an evidence gap, not proof of no failures. The 2026-10-11 evening alert exposed
untagged failed admission reads, but its full RPC breakdown was unavailable;
the stream-budget defect is independently reproduced with the real SDK and is
not established as the cause of every error in that incident.

### Shadow transaction cleanup

The SDK skips rollback when a transaction callback raises a Google API error.
Shadow evidence callbacks can fail after acquiring read locks, for example
when the counter read succeeds but the retention-fence read times out. Without
cleanup, those locks remain until server-side expiration and can block later
flushes to the same counter. The Oct 11 evening samples showed shadow counter
read deadlines alongside `tr_shadow_evidence` read/write lock contention.

The shadow writer now uses the existing bounded callback cleanup wrapper.
It attempts rollback on non-ABORTED callback API failures, preserving the
original error even if cleanup fails. Cleanup shares the transaction budget,
with only the existing 50 ms strict-deadline allowance after exhaustion. It
does not retry a callback, touch customer balances, or roll back an uncertain
commit. A failed first read without a known transaction ID cannot be rolled
back; the wrapper does not invent an ID or claim successful cleanup in that
case. The stream-reset and read-lock defects are separate: fixing one does
not establish that every Spanner API failure has been eliminated.

New workspaces and their new API keys start with 16 exact billing shards.
Existing workspaces retain their current shard count until the pause, drain,
verify, reshard workflow completes; changing the default never rewrites a live
ledger in place.

This PR ships only aggregate-first credit checks and transfer telemetry.
Credit-shard rebalance uses signed headroom, `SUM(total_credits - total_usage -
reserved)`. For positive estimates, the read-only precheck checks aggregate
affordability before its candidate loop; the locked rebalance checks it before
its target check. Insufficient aggregate headroom returns `INSUFFICIENT`.
Rebalance returns `NOT_NEEDED` only if the original target is already funded.
Authorize reruns its own candidates after repair; guarded debit repairs and
retries shard zero. The precheck's existing funded-candidate retry is unchanged.
Nonpositive estimates are an exception: rebalance returns `NOT_NEEDED` without
reading; precheck still checks signed affordability (`[-1, 0, 0]`, estimate `0`,
returns `INSUFFICIENT`). Initial bounded reserve/debit fast paths are unchanged.

Repairs retain the original `estimate - target_available` transfer, filling any
negative target first and taking from the largest positive donors. Usage and
reservations never move; total credits remain unchanged. Successful transfers
return `mode=topped_up`; authorize logs that mode or `mode=none` for no transfer.
Cooldown, attempt cap, and 402/503 mapping are unchanged.

Every consolidation and non-target reuse variant was withdrawn. Astra's round-3
sequence was: “Start with four shards holding **$0.50 each**, without reservations
or debt.” Authorize $0.60, grant $2, authorize $0.50 on a donor, settle it for $2,
then authorize $1.50: consolidation accepted at net −$0.10; needed-only rejected
with $1.40 left. Round 4: “Start with three shards holding $1.50 each”; authorize
$1, settle $3.50, debit $1, then authorize $1.50: non-target reuse accepted at net
−$1.50; target repair rejected at net $0. Her debt-free counterexample starts with
“credits `[40,150]`”: authorize 50 on shard 1, debit 60, settle 150, authorize 40;
reuse accepted, needed-only rejected. Eligibility guards cannot prevent later
settlements from exposing debt-backed capacity. Returned-shard debit also falsely
rejected an affordable 80-unit debit after a peer reserved on the returned shard.

This change does **not** remove the convoy class. Needed-only repairs can require
another all-shard transaction for each request, and the cooldown can still cause
HTTP 503 despite sufficient net headroom. `DEFAULT_NEW_BILLING_SHARDS=16`
fragments a $25 starter workspace too finely. Starting on one shard and splitting
on growth needs a separate design. See the review sequences and strict xfail in
[the convoy incident](incidents/2026-09-25-credit-rebalance-convoy.md).

## Alert response

### High CPU

Keep high-priority CPU below 45% in every multi-region replica. Confirm whether
the load is user traffic or system work before scaling. Scaling adds headroom
but does not repair transaction contention. The alert intentionally uses the
maximum observed CPU so short high-priority spikes remain actionable.

Replay the policy with its own aligner before blaming traffic: the metric has
ten-second samples, so a one-minute mean can read 3% while the policy's
five-minute maximum reads 50%. Sizing note: on 2026-09-25 a single customer
burst of about 15 requests per second sustained 25-34% high-priority CPU as a
five-minute mean (60-second peaks to 47%) at 400 processing units and opened
this policy; the instance was raised to 600. Periodic jobs that read Spanner must go through
a covering index. The hourly reservation-overrun rollup reads two hours of
`tr_reservation` by `terminal_at` through `tr_reservation_by_terminal`; before
that index existed its full-table scan was the only source of this alert on
2026-09-24 (docs/incidents/2026-09-24-spanner-overrun-rollup-scan.md).

Operational reports must read request, generation, token, provider, model,
latency, and error analytics from ClickHouse. Do not aggregate or full-scan raw
Spanner generation, entity, or analytics-outbox rows for reporting. Spanner is
reserved for bounded ledger and control-plane reads on this path.

For payment ownership, use the bounded operator helper rather than searching
JSON bodies for a Stripe ID:

```bash
uv run python -m scripts.inspect_payment_owner \
  --project quill-cloud-proxy --instance trusted-router-nam6 \
  --database trusted-router --payment-intent pi_EXAMPLE --include-email
```

Supply the existing read-only credential through
`CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` and the Stripe key through
`STRIPE_SECRET_KEY`; never print key values. The helper performs one exact
Stripe GET followed by at most two Spanner primary-key reads. Reads are tagged
`tr_ops_payment_owner`, LOW priority, have five-second RPC timeouts, and do not
retry. It reports missing attribution without scanning for a replacement.
An owner found this way is the **current workspace owner**, not proof of who
made a historical payment or caused a database incident.

`LIMIT` is not a query-work budget. A leading-wildcard body search still scans
the table when it returns zero rows. Operators with direct SQL access can bypass
the helper, so it is an approved safe path, not an IAM-enforced SQL firewall.

### Transaction contention

Inspect Spanner transaction and lock insights. A high abort ratio or lock-wait
rate generally means transactions repeatedly update the same keys. Decode the
reported row key before assuming the billing ledger is involved. Check the
billing shard distribution only when the hot row belongs to a billing table;
otherwise repair the specific metadata or worker access pattern before raising
capacity.

### One workspace on one credit row

A workspace with one credit shard (every workspace starts with one) writes the
same `tr_credit_balance` row on every synchronous authorize, settle and
refund, and a capped key's `tr_key_limit` row beside it. The reserves scan
under the exclusive lock and queue for the row, and since 2026-11-21 so do
the settle, refund and reap releases
([reserves on one row aborting each other](incidents/2026-11-10-reserve-lock-upgrade-aborts.md),
"The releases, November 21"). Until then the releases scanned under a shared
lock and upgraded, so a burst from one such workspace made its releases
abort each other and be aborted by older reserves, and every abort redid its
statements on the same row. The signature of that upgrade, should an
unhinted writer of these rows bring it back: in
`SPANNER_SYS.TXN_STATS_TOP_MINUTE`, `tr_settle_one_commit` and `tr_finalize`
show several aborts for each commit while `tr_authorize` shows few; authorize
and settle both take seconds; `LOCK_STATS_TOP_MINUTE` puts the wait on one
credit row and one key row; contended settles answer `intent_durable`
(`commit_path=deferred`) and the drain charges them minutes later. A
workspace whose key has a spend cap is on this path even when it is enabled
on the fast path (`fastpath pilot passed over reason=key_cap`).

`TR_WORKSPACE_BILLING_GATE_WIDTH` (`trusted_router/workspace_billing_gate.py`)
lets that many of a workspace's credit-row transactions from one process be
inside Spanner at a time: the reserve, the one-commit settle or refund, the
two-commit finalize and the snapshot settle's frozen apply. The rest wait in
the process.

- It changes when a transaction starts, and so how much of its deadline is
  left when it does; nothing else about it. A caller without a slot runs the
  same transaction at once. The money cannot depend on it.
- A waiting settle, refund or finalize goes before a waiting reserve, and
  within each group the order is arrival order. One row cannot serve a
  burst's reserves and its settles at once. A settle completes a request
  that already holds credit and has the short budget (the snapshot settle's
  `TR_ASYNC_SETTLE_SYNC_BUDGET_SECONDS`, five seconds deployed and two by
  default, past which it answers 503); a reserve is new work with twenty. So the reserves are the ones that wait, and a client that sends
  requests as fast as they finish is slowed at its reserves.
- One wait is at most a quarter of the Spanner budget the caller has left,
  inside that budget: five seconds of a reserve's twenty, a second and a
  quarter of a one-commit settle's five or of a five-second snapshot
  settle, half a second of a two-second one. All of one request's waits
  together are at most
  `TR_WORKSPACE_BILLING_GATE_WAIT_MS` (5000), however many of its
  transactions meet the gate. Then the caller starts ungated.
- At most `TR_WORKSPACE_BILLING_GATE_MAX_WAITERS` (8) callers of one
  workspace wait in a process and at most 12 across workspaces. One more
  starts ungated at once.
- It applies only to a transaction that writes the credit row, of a workspace
  the process knows to have exactly one credit shard. Not to a BYOK-only
  reserve or its settle (no credit is held); not to a sharded workspace; not
  to a workspace whose shard count the process has not loaded, since a guess
  would make a sharded workspace's settles on different rows take turns (the
  next reserve of that workspace on the instance loads it); not to a caller
  with no Spanner deadline to wait inside (the settle-outbox drain, whose
  applies already take turns per workspace); and not to a strict-budget
  key's authorize, which already takes turns on its key's own slot ("Strict
  budget busy" below). That key's settles are gated like any other.

What it costs. The wait comes out of the transaction's own deadline, so that
no request takes longer than it could before. A request behind a slow or
stalled transaction of its own workspace on the same instance therefore
starts with up to a quarter less time (at most five seconds less, over the
whole request), and a transaction that needed more than what is left fails
on its deadline where, ungated, it would have finished: a reserve answers
the storage 503 the gateway retries, a one-commit settle defers to the
drain, the snapshot settle answers its 503. During a burst the reserves of
that workspace are slower by design, and by how much depends on how long
each transaction holds its turn. While the releases still upgraded their
lock a turn took a second or more: in the first bursts behind the gate many
waiting reserves reached the five-second bound and went around it, most
waiting settles reached theirs, and the snapshot settle's 503s fell but did
not stop. With the releases taking the row exclusively a turn should be
tens of milliseconds. The gateway waits 25 seconds for the response and
retries a reserve only on a 5xx, so a reserve that waited is a slow 200, not
a retry. That exchange is the point of the gate: the settle's 503 pages and
rolls deploys back; a few seconds on the bursting workspace's own reserves
does neither.

**November 27: backpressure on new authorizations.** In the 17:14-17:15 UTC
minute, 12 GCP instances served authorization traffic. 2,411 calls got a 429;
273 of 275 successful calls still took at least a second. The local credit-row
queues reported 614 reserve timeouts and 164 full-queue reserve bypasses.
Across 17:13-17:29 UTC one workspace's single credit row accounted for
150,385 cumulative lock-wait seconds. This is concurrent wait, not downtime.
The lock, API failure and slow billing call alerts resolved before any fix.

With `TR_GATEWAY_AUTHORIZE_RESERVE_BACKPRESSURE=true`, when a reserve on
this instance waits at least a second and times out, or finds its *own*
workspace queue full, it still runs the original transaction. Until the last
such transaction for its key exits, new authorizations for that key have a
per-process concurrency limit of two (never higher than the configured limit).
Excess new calls get 429 with `Retry-After: 1` before creating a hold.
Settles, refunds and existing requests keep their slots. A full global queue
alone does not trigger the change. This does not serialize different keys or
instances and is not a promise to eliminate contention.

On a comparable burst, compare successful throughput and latency, HTTP 429
and 5xx counts, lock waits, settle aborts, and outbox drain age. A fall in
lock waits caused only by rejecting more useful work is not success.
`billing.authorize_reserve_backpressure` distinguishes these refusals from
the static per-key limit. Roll back by setting the flag false in the GCP
rollout. No alert or transaction deadline changes.

The model that ships with the gate
(`tests/test_workspace_billing_gate_storm_model.py`) compares these
mechanisms. It is a model, not Spanner: the lock queue's order (wound-wait,
a freed lock to the oldest waiter), the statement, commit and retry timings
and the clients' behaviour are assumptions, and its default timings were too
kind to the gate alone. Read its numbers as a comparison of mechanisms on
the same assumptions, not as a forecast. Fifty clients over eight processes,
gate on:

- Set so that the earlier shape is as bad as it was (`MODEL_UPSTREAM=5`, a
  five-second provider call; `MODEL_ABORT_COST=0.4`; `MODEL_UPGRADE_GAP=0.02`,
  a 20 ms shared-lock window before the upgrade) and with the earlier
  releases (`MODEL_HINTED_SETTLE=0`), it shows that shape: about six settle
  aborts for each settle commit and more than three hundred snapshot settle
  503s in forty seconds, with the bound at two seconds or at five.
- The same settings with the hinted releases, two runs: a gate wait of about
  a third of a second at the median and 1.35 seconds at the 90th percentile,
  at most two waits in a run reaching their bound, at most about one settle
  abort in a hundred settle commits and no snapshot settle past its two
  seconds.

Why the reserve's bound stays at five seconds. After the first bursts with
the gate on, lowering `TR_WORKSPACE_BILLING_GATE_WAIT_MS` to two seconds was
proposed: many waiting reserves reached the five-second bound, and
successful billing calls of ten seconds or more began to page with the gate.
With the hinted releases the model says not to. The bound decides where a
saturated row's queue waits. A reserve that reaches it goes around the gate
into Spanner, where it competes with the settles for the row and wound-wait
favours the older transaction, so a lower bound moves the wait from the
reserves (a slow 200) back onto the settles (a 503). Gate on, hinted
releases, two runs each (four at two and five seconds in the first setting):

| Setting | Bound | Snapshot settle 503s | Settle attempt p50, p99 | Reserve p99 | Gate waits that reached their bound |
|---|---|---|---|---|---|
| Saturated: default timings, 0.3 s provider call | 2 s | 0 | 1.2 to 1.4 s, 1.6 to 1.7 s | 3.1 to 3.2 s | about 240 |
| | 3 s | 0 | 0.7 s, 1.1 to 1.2 s | 3.7 to 3.8 s | about 120 |
| | 4 s | 0 | 0.36 s, 0.7 s | 4.3 to 4.4 s | 2 to 4 |
| | 5 s | 0 | 0.36 s, 0.4 to 0.7 s | 4.4 s | 0 |
| Slower: the same with `MODEL_SLOWDOWN=2` | gate off | 205 to 209 | 2.0 s, 2.07 s | 3.9 s | |
| | 2 s | 182 to 192 | 2.0 s, 2.07 s | 5.0 to 5.1 s | about 280 |
| | 3 s | 101 to 163 | 1.9 to 2.0 s, 2.05 to 2.07 s | 4.9 to 5.0 s | about 225 |
| | 4 s | 68 to 72 | 1.8 s, 2.07 s | 5.9 to 6.0 s | about 180 |
| | 5 s | 19 to 25 | 1.55 s, 2.03 to 2.04 s | 6.6 s | about 125 |
| Calibrated, as above | 2 s | 0 | 0.1 s, 0.7 to 0.8 s | 2.1 s | 2 to 4 |
| | 5 s | 0 | 0.1 s, 0.7 s | 2.1 s | 0 to 2 |

In the calibrated setting the bound makes no difference. Wherever the row is
saturated, each step down from five seconds costs settles: in the first
setting the settles slow toward their budget and the model's own acceptance
check (no more transactions on the rows at once than twice the number of
processes) fails at two and three seconds; in the slower one the snapshot
settle's 503s rise at every step, and at two seconds they are about what no
gate gives. No modelled reserve took ten seconds at any bound; the longest
took 6.7 seconds, at five seconds in the slower setting. The live
ten-second calls came from bursts before the hinted releases, in which each
turn took a second or more, so the first thing to read after they ship is
whether those calls remain. If they do, a lower bound would trade them for
settle 503s, by this model's account.

What it is not. It is per process: the row still sees up to that many
transactions from each instance, callers that reached a bound or are not
gated run beside them, and transactions from different instances still
meet on the row. So it bounds the pile-up; it is not a hard limit on the
row's concurrency, and it is not what removed the lock upgrade: before the
releases took the row exclusively, the first bursts behind the gate still
aborted about as often and most waiters timed out. The waiter caps
bound the queue, not the instance: a waiting request holds the request slot
it would otherwise have held while waiting inside Spanner, and one
workspace's holder, waiters and ungated requests can together fill an
instance's sixteen slots, as they can without the gate. The per-key
in-flight limits in front of the routes are what keep room for others. A
slot held for more than 60 seconds stops counting, so a leaked slot heals; a
transaction that really runs that long shares its turn. `0` turns the gate
off, and is the way back.

Each caller that waited or went around writes one INFO line, with no
workspace, key or request identifier; a caller that found the slot free
writes nothing (about two lines for each request during a burst, none
otherwise; a failure to write one never stops the transaction):

```
metric=workspace_billing_gate outcome=waited kind=settle_one_commit wait_ms=41.3 queued=2
```

`outcome` is `waited` (got the slot after waiting), `timeout` (reached its
bound and started ungated), `waiters_full` (the workspace's or the process's
waiters were full), `allowance_spent` (the request had already used its
waits), `no_budget` (no Spanner budget left to wait in) or `subjects_full`.
`kind` is `authorize`, `settle_one_commit`, `finalize` or `frozen_finalize`.
`queued` is how many callers of the workspace were ahead of it. During a
burst from one workspace, read these beside the statistics above: mostly
`waited`, with fewer aborts than commits for `tr_settle_one_commit`, few
`commit_path=deferred` settles and no settle 503, means the gate is
absorbing the burst; mostly `timeout` or `waiters_full` means the burst is
larger than one process's queue, and the excess is running as it did before
the gate.

### Latency and API failures

A one-minute commit-latency event (2026-10-11 09:13Z: every transaction shape
about 7x slower, pure inserts included, throughput down) reaches the
API-failure policy through the budgeted callers' deadlines. The request-latency
policy's "Commit p99 exceeds 1 second in a minute" condition pages beside it
and names the cause; the ten-minute conditions do not see a single slow
minute. Check `SPANNER_SYS.TXN_STATS_TOP_MINUTE` per transaction shape, which
groups by fingerprint and reports the columns touched, not row keys: when
shapes that touch no contended row slowed by the same factor, treat the
minute's lock waits as a likely consequence. `LOCK_STATS_TOP_MINUTE` names the
row ranges and samples the transactions that conflicted there, holders and
waiters alike without saying which was which, and can omit either side; it
corroborates where contention was, not who caused it. Settle holder
responsibility, or rule contention out, only with independent evidence (the
per-shape latencies above, request logs, the tags' own code paths).

Separate Spanner server latency from network and application latency. Inspect
the `method`, `status`, and serving-region labels. `ABORTED` is tracked by the
contention policy; unexpected statuses are tracked by the API-failure policy.

For a gateway billing-path incident:

1. Query Cloud Run request logs for the affected internal gateway path and
   note status, latency, service region, and request ID.
2. Check `SPANNER_SYS.TXN_STATS_TOP_MINUTE` and
   `SPANNER_SYS.LOCK_STATS_TOP_MINUTE`. Decode hot row keys and determine
   whether one workspace or API key dominates.
3. Run the typed billing invariant audit before changing counters.
4. Verify shard distribution. Use the guarded online split for an eligible
   hot workspace; never delete or rewrite live reservations.
5. Confirm post-fix requests settle across multiple shards, no new `5xx`
   appears, and the invariant audit remains clean.

For a single legacy hot account, prefer `scripts/expand_billing_shards.py`
when its exact-key preflight permits the repair. It atomically adds capacity
without pausing traffic or modifying existing holds, usage, or spend limits.
Non-strict daily/weekly/monthly-only caps are supported with the explicit
`--allow-window-caps` option. The cap values must agree in metadata and every
typed row; new rows retain those same values and start with zero usage. Window
enforcement still sums all configured shards, so this does not multiply the
customer's allowance. Strict and lifetime-capped keys remain ineligible.
Use the read-only ops credential for the default dry run; `--apply` requires
a separate named deployment identity and repeats every check transactionally.
Do not remove a customer cap or change strict mode to qualify for this repair.

Authorize, idempotency-replay, settle, refund, and typed-finalize transactions
carry stable `tr_*` Spanner transaction tags. Tags identify the operation in
transaction and lock insights without containing a workspace, key, request,
authorization, prompt, or output. Hot row keys still identify the affected
ledger row and must be decoded through the read-only operator path.

The guarded online split briefly pauses new authorizations while existing holds
drain and invariants are checked. A `503 Workspace billing is paused` during
that interval is real customer impact, not an alert false positive. The gateway
logs `billing.authorize_workspace_paused` with only workspace ID and request ID
so the incident is attributable. Schedule proactive legacy-account splits
serially in low-traffic windows; GitHub concurrency keeps at most one pending
run and cancels older pending runs even when `cancel-in-progress` is false.

### Credit rebalance busy

"TR Gateway: credit rebalance busy" means authorize answered more than two
rebalance-busy 503s in ten minutes (its fast bound) or more than five in an
hour (its slow bound). The two are the sides of one condition, because a
policy may hold only one PromQL condition; the window's lines show which
was crossed. Storage is healthy and no credit is lost. For each of those requests the workspace's aggregate credit covered
the hold, no single credit shard did, and the per-process rebalance cooldown
(`REBALANCE_COOLDOWN_SECONDS`, half a second) was held by a rebalance that
another request of the workspace had just been allowed to run.

The typed authorize's credit recovery (`storage_gcp.py`) gives a refused
request at most three passes, and three more if a key-limit repair sends it
round again. A pass whose snapshot finds a funded shard retries on it. A
pass that needs a rebalance and finds the cooldown held sleeps 0.25 seconds
and goes round again when it is the first or second pass, and answers the
503 at once when it is the third. So one entry into the recovery waits
twice at most, half a second in all (a request sent round again after a
key-limit repair can enter it twice), and less when its earlier passes went to funded-shard
retries or to the one forced reload of the shard count: it can meet the
cooldown for the first time on its last pass and be answered 503 without
waiting. The request of 2026-11-16 took 467 ms in all with `attempts=3`,
which leaves room for one wait.

The answer is the storage 503 with `Retry-After: 1`, and the enclave retries
it, three attempts in all, seconds apart, so one request writes at most
three lines. Three lines in ten minutes is therefore the least at which a
customer's request can have failed on this 503 alone, and the fast bound
pages there. Fewer are requests made to wait and answered on a later
attempt; the slow bound limits how long that can go on. One
line now and then is expected: three in the thirty days to 2026-11-16, one
line each, on three different days. More is the convoy of
[September 25](incidents/2026-09-25-credit-rebalance-convoy.md): requests of
one or more workspaces cannot consolidate their credit, and their customers
wait on retries or fail.

1. Read the window's `billing.credit_rebalance_busy` lines. Each carries the
   workspace id, the request id and the trace of its 503, and nothing else.
2. For each workspace, read what its requests logged on
   `trusted_router.storage_gcp` around them: `credit rebalance workspace=...
   outcome=... moved_micro=...` for every rebalance that ran, and `credit
   rebalance cooldown exhausted` for each 503. `outcome=insufficient
   moved_micro=0` beside a 503 is a rebalance that moved nothing and still
   held the cooldown.
3. Read the workspace's credit shard count and its `tr_credit_balance` rows
   by primary key (the workspace id and each shard), never by a scan, and
   compare each shard's share with the workspace's estimates. A balance cut
   into shares smaller than its estimates needs a rebalance for every
   request.
4. One workspace points at its shard spread. Its repair is a shard count
   that follows the balance, which is the operator's to run. Many workspaces
   at once point at a recent deploy or at the cooldown itself.

Do not lengthen the wait, shorten the cooldown or answer these requests 402
to quiet the alert: the cooldown keeps a fragmented workspace from running
one all-shard transaction per request, and the funds exist.

Known and left for later:

- A rebalance that moves nothing still arms the cooldown. The cooldown is
  stamped when a request is allowed to rebalance, before its transaction
  runs, and stays stamped when the locked read finds the aggregate short
  (`outcome=insufficient moved_micro=0`, an honest 402 for that request). On
  2026-11-16 that held the cooldown against a smaller request the balance
  could cover. It was answered 503 after one wait at most and served on the
  enclave's retry about a second later: an avoidable 503. The preferred
  repair gives the waits a budget of their own, so a pass spent on a
  funded-shard retry does not take a wait away and a request outlasts one
  stamp. That is money code and needs its own change, with a regression for
  that schedule.
- A line can be written without its 503. The route writes the line and the
  app answers afterwards, so a caller that disconnects mid-authorize leaves
  the line and no 503 in the request log. That is more lines than 503s and
  pages in the other direction. The lease-headroom, strict-budget-busy and
  headroom-raced lines have the same exposure.

### Strict budget busy

"TR Gateway: strict budget busy" means authorize answered more than two
strict-budget-busy 503s in ten minutes (its fast bound) or more than five in
an hour (its slow bound), the same two bounds as "Credit rebalance busy" and
for the same reason: the enclave makes three attempts, seconds apart, so one
request writes at most three lines, and three in ten minutes is the least at
which a customer's request can have failed on this 503 alone. Storage is
healthy and nothing was held. Each of those requests was for a key created
with `budget_strict`, and could not get the key's per-process admission slot
(`trusted_router/strict_budget.py`, `docs/routing-controls-20260927.md`):

- one strict authorize per key and at most 16 strict keys per instance at
  once;
- a caller that finds the key's slot taken waits up to
  `STRICT_ADMISSION_WAIT_SECONDS` (one second), with at most two waiting per
  key and 16 per instance;
- a caller still without a slot at the end of the wait, or that finds the
  waiters full, is answered the 503 ("Strict budget authorization is busy;
  retry with backoff").

The slot is held for the strict authorize's transaction, so anything that
slows that transaction makes the key's other authorizes wait longer. Before
2026-11-17 each of these 503s paged as an unmarked authorize 503 (one false
page that day: two overlapping authorizes on one strict key, the second
answered after its one-second wait).

1. Read the window's `billing.strict_budget_busy` lines from
   `trusted_router.routes.internal.gateway`. Each carries the workspace id,
   the request id and the trace of its 503, and nothing else. The route's
   `billing.authorize_strict_budget_busy` and the handler's
   `billing.strict_budget_busy` warnings (Spanner store; on the Postgres
   store only the handler's) carry the same request id.
2. One workspace points at its own traffic: its client sends more
   concurrent authorizes on one strict key than one in flight and two
   waiting per instance, or each of its strict authorizes holds the slot
   long. Compare the latency of its successful authorizes with Spanner's
   for the window.
3. Many workspaces at once point at Spanner latency, which holds every slot
   longer, at more than 16 strict keys authorizing on one instance, or at a
   recent deploy.

Do not lengthen the wait, raise the limits or change a key's strict mode to
quiet the alert: the slot bounds the harm one strict key can do to an
instance, and the database's conditional updates, not the slot, keep the
budget correct across the fleet.

### Credit headroom raced

"TR Gateway: credit headroom raced" means authorize answered more than two
headroom-raced 503s in ten minutes (its fast bound) or more than five in an
hour (its slow bound), the same two bounds as "Credit rebalance busy" and
for the same reason. Storage is healthy and nothing was held. For each of
those requests the workspace's credit was spread across credit shards, no
shard in the request's write set covered the hold, and the typed
authorize's credit recovery (`storage_gcp.py`, see "Credit rebalance busy"
above for its passes) used all three of its passes, each one ended by its
repair:

- a lock-free snapshot found funded shards, and the retry on them lost them;
- a rebalance consolidated credit onto one shard (or found it already
  there), and the re-authorize lost it;
- the one forced reload of the shard count after an incomplete snapshot;
- a wait for the rebalance cooldown on the first or second pass.

The workspace's own concurrent requests took the headroom each time, and no
pass proved the aggregate balance exhausted, so a 402 would be an
accounting lie: the answer is the storage 503, `credit headroom changed
concurrently; retry`, and the route writes `billing.credit_headroom_raced`
for it. The same message from a recovery that stopped early, because its
forced reload of the shard count failed (`credit shard-count forced reload
failed after incomplete precheck`), gets no line: it is not this race, and
it pages as an unmarked authorize 503. Nor does one that followed a failed
debt-mark heal (`credit debt-mark heal failed; keeping the refusal`): a
stale mark refuses every shard while the snapshot, which does not read the
mark, still sees them funded, so its passes can look like the race. Before 2026-11-18 every one of these
503s counted that way: three of the four 503s in the burst of 2026-11-04
16:08Z, one on 2026-11-16 02:38Z, and the one that paged on 2026-11-18
07:41Z (a workspace with 32 credit shards, nearly out of credit, bursting
concurrent requests: three `credit rebalance ... outcome=moved` lines, then
the 503 after 39 Spanner RPCs in 0.83 seconds).

1. Read the window's `billing.credit_headroom_raced` lines. Each carries
   the workspace id, the request id and the trace of its 503, and nothing
   else. The route's `billing.authorize_storage_unavailable` and the
   handler's `storage.unavailable` warnings carry the same request id and
   `error_class=CreditHeadroomRaced`.
2. For each workspace, read what its requests logged on
   `trusted_router.storage_gcp` around them: `credit rebalance workspace=...
   outcome=... attempt=...` for every rebalance that ran. Then read its
   credit shard count and its `tr_credit_balance` rows by primary key (the
   workspace id and each shard), never by a scan, against its estimates and
   how many requests it sends at once.
3. One workspace points at its own burst: many concurrent requests over a
   balance cut into many small shares, often near the end of its credit,
   where each request takes the headroom another has just consolidated.
   Once the balance is really exhausted the snapshot proves it and the
   answer is an honest 402. A shard count that follows the balance is the
   operator's repair. Many workspaces at once point at Spanner latency,
   which widens every race, or at a recent deploy.

Do not answer these requests 402, or add passes, to quiet the alert: the
funds may exist, and the three passes bound how much Spanner work one
refused request can cost.

### One instance loses Spanner

When one control-plane instance returns `503` on every Spanner-backed route
for a minute or so, its siblings are healthy, and Spanner's request metric
shows almost none of the failed calls, the fault is between that instance and
the Google front end. The transaction and lock statistics above will show
nothing. Read the instance's own log lines from
`trusted_router.storage_gcp_transport_watch` for the window:

- `spanner channel state=… previous=… previous_ms=…` on every connectivity
  transition of the main store's channel that gRPC reports.
- `spanner credential refresh ok|failed … elapsed_ms=… inline=…` for
  access-token refreshes: every slow or failed one, quick ones at most once
  every five seconds. `inline=True` means an RPC waited on it.

No channel line during the stall and a reconnect as it ends means the
connection stopped answering until it was replaced. No channel transition
logged, with a failed or slow refresh, means the metadata server did not
answer. Neither means the stall is somewhere these lines do not see.

google-auth and gRPC write to their own loggers, which reach neither Cloud
Run logs nor the log shipper. Their WARNING records do reach Sentry Logs:
search the window there by logger name (`google.auth*`, `grpc*`). The
[November 10 report](incidents/2026-11-10-one-instance-spanner-stall.md) has
the full table and what the watch does not cover.

The main store also moves its own traffic to a second connection when the
first stops answering and the second answers
(`trusted_router.storage_gcp_channel_failover`). It suspects a stall after
three `DEADLINE_EXCEEDED` in a row, or a call waiting three seconds with no
answer since it started (checked as calls start and end, and every second
while any are in flight), then sends the same cheap call on both connections
under a two second deadline:

- `spanner channel failover client=… reason=deadline_streak|waiting
  stalled_ms=… waiting=… deadline_streak=… probe_ms=… failovers=…`
  (WARNING): the second connection answered, the first did not, traffic moved.
  At most one a minute per instance. `stalled_ms` runs from the start of the
  first call that got no answer. The old connection is closed 65 seconds
  later.
- `spanner channel probe kept client=… reason=… standby=answered|silent
  active=answered|silent stalled_ms=… probe_ms=…`: a suspected stall that did
  not move traffic. `standby=silent active=silent` means every connection was
  failing, so the fault was not this instance's connection.
- `spanner channel failover dropped client=… error_class=…` (WARNING) at
  startup: creating the session pool failed on the failover client, so the
  store retried it on the SDK's own client and runs without failover.

`TR_SPANNER_CHANNEL_FAILOVER=0` in the rollout's `ENV_VARS` turns it off.

Do not restart the instance on a database-dependent check, and do not reset
the channel across the fleet.

### Backup workflow

Check the latest workflow execution:

```bash
gcloud workflows executions list tr-spanner-cross-project-backup \
  --location=us-central1 \
  --project=quill-cloud-proxy \
  --limit=5
```

Then verify a recent READY backup exists:

```bash
gcloud spanner backups list \
  --instance=trusted-router-backups \
  --project=trustedrouter-dr \
  --sort-by='~createTime' \
  --limit=5
```

The workflow is idempotent. Re-running it for the same source backup returns
`already_copied`.

## Metadata retention

Backups expose storage growth; they do not cause it. Inspect the latest
per-table footprint with:

```sql
SELECT interval_end, table_name, used_bytes
FROM spanner_sys.table_sizes_stats_1hour
WHERE interval_end = (
  SELECT MAX(interval_end)
  FROM spanner_sys.table_sizes_stats_1hour
)
ORDER BY used_bytes DESC;
```

Per-request state is bounded without deleting active or unresolved billing
records:

- `tr_gateway_authorization`, `tr_reservation`, and `tr_settle_outbox` use a
  nullable `terminal_at` and a 30-day row deletion policy.
- Active, pending, dead, and repairable rows keep `terminal_at=NULL`. Spanner
  TTL does not select a row while that value is NULL.
- A successful settlement makes the reservation immutable but leaves all three
  retention clocks stopped while Bigtable repair is pending. After the
  deterministic activity row is confirmed, one transaction sets
  `terminal_at` on the reservation, authorization, and outbox. It also clears
  the terminal outbox body. The authorization keeps its content-free replay
  record for the bounded idempotency window.
- New terminal generations are not written to `tr_entities`. Existing generic
  rows remain untouched by the migration and can be archived or removed in a
  separate, reviewed operation.
- Bigtable writes new activity, benchmark, synthetic, and rollup cells to
  distinct families with 30-day, 30-day, 14-day, and 730-day GC policies.
  Readers prefer those families and fall back to legacy `m` cells. The
  migration never adds a GC policy to `m`.

Apply the additive migration:

```bash
scripts/deploy/migrate_request_retention.sh
scripts/deploy/migrate_request_retention.sh --apply
```

The first command is a dry run. The apply path refuses to add a Spanner policy
if any row would be immediately eligible. It performs no `DELETE`, `DROP`, or
`terminal_at` backfill.

Roll out application code with `TR_REQUEST_RECORD_WRITE_MODE=legacy` in every
region first. After all revisions can read both layouts and production smoke
passes, switch regions to `typed`. Do not roll back to a revision that predates
typed-table reads after the first typed authorization is created.

Spanner table-size metrics include retained historical versions. TTL deletion
is asynchronous, and physical storage can remain visible until the database
version-retention window and compaction have elapsed.

## CPU for background work

The combined control plane runs settlement, observation, and heartbeat loops
outside HTTP requests. Its Cloud Run deployment therefore explicitly sets
`--no-cpu-throttling`. A warm minimum instance count keeps instances resident;
it does not guarantee CPU between requests under request-based billing.
Suspended background work can resume after its database deadline has elapsed.
See Google's [background activity guidance](https://docs.cloud.google.com/run/docs/tips/general)
and [billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings).

Instance-based billing increases idle compute charges. This setting does not
increase instance floors, ceilings, concurrency, or database deadlines. Keep
the guarded no-traffic warmup and regional ramps. Do not silently restore
request-based CPU allocation unless these loops have been moved to a supported
execution lifetime. Public-only services that do not own these loops are not
changed by this control-plane setting.

After rollout, verify `run.googleapis.com/cpu-throttling` is `false` on each
serving revision and compare background progress and database errors across
idle and active periods. Fixing the configuration mismatch does not by itself
prove every database alert was caused by CPU suspension.
