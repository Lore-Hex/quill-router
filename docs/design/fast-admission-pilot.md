# The fast path's pilot: Joseph's workspace

The design's rollout step 8 (`fast-admission-and-batched-settlement.md`
§8), as `fast-admission-production-rollout.md` asks: what the pilot enables,
what it checks, and how it turns off. It starts on Joseph's own workspace,
on his word (2026-10-14), after P1's runs 1 to 6 (`fast-admission-p1-results.md`),
and adds one customer's workspace on his word (2026-10-16).

## Where the client lives

In production the enclaves call the control plane's internal routes,
`/internal/gateway/authorize`, `heartbeat`, `settle` and `refund`
(`routes/internal/gateway.py`), and the control plane reserves credit in one
Spanner transaction per request. The design has the enclave call the fast
path itself in the end; for the pilot the control plane calls it, for three
reasons found on 2026-10-14:

- the enclave's measured image accepts one billing authority,
  `https://trustedrouter.com` (`validateControlPlaneEndpoint` in
  quill-cloud-proxy), and a new address or variable is a new image;
- an AWS enclave reaches the control plane over vsock alone and cannot reach
  a GCP VPC address at all;
- the fast path's nodes are in GCP `us-central1`, where the control plane
  that serves that region runs on the same network.

So the pilot is the control plane in `us-central1` calling the front doors
(`fastpath/internal/frontdoor`, internal addresses, port 8080) for a
request in the cohort, and today's path everywhere else. The enclave is
unchanged.

## What it enables

For an authorize in the cohort, the control plane asks a front door
(`POST /v1/authorize` with the workspace, the authorization id as the
request's hash, the estimate, the stream flag and the enclave boot's binding)
and, if admitted, writes the authorization row with the sealed envelope in
its payload and reserves nothing: no credit hold, no key hold; the
`tr_reservation` row it writes holds nothing and carries only the
idempotency scope. The hold is the lease's. At settle or refund the control
plane prices the actual charge as today, forwards the terminal to a front
door (`/v1/settle` with the charge, the full record and the money fields;
`/v1/refund`), and then writes the terminal's records alone: the
authorization marked settled with its finalization facts, the generation
record, the activity row and the benchmark row. It claims no reservation,
releases no hold, books no usage and pays no markup; the lease booked the
usage, and the audit (`fastpathctl audit`) holds it. The records say what
the fast path decided, never what the route asked: the owner's winner (its
kind and charge), or the closed lease's disposition, and the generation
and activity rows carry that charge too. The terminal is durable before
the front door is asked, as today's two-commit settle makes it: the
settle-outbox intent is pending first, with the charge frozen. A terminal
the fast path has not decided (`recorded`, durable in the drain log with
the lease's order still to decide; `failed`; `invalid`; a winner of
another kind; a booking answered to a refund, which has no settle body to
record; no front door in reach) is answered as today's `intent_durable`,
and the drain asks the front door again later (`_apply_leased` in
`services/settle_outbox_apply.py`), since the enclave does not retry a
non-streaming terminal itself. Decided, the records commit in one commit
with the intent resolved, so the refill attachment and the row's
retention clock are as today's (the outbox's own rule: armed only once no
sibling intent is outstanding); a record commit that fails after the
front door decided is answered `intent_durable` too, and the drain writes
the records. The drain marks a row done without asking the front door
once the authorization is settled, and parks a row, spending no attempt,
when the store is away. A BYOK terminal (the key's own provider
paid) charges the lease nothing, as today's path books nothing. A hold
admitted whose authorization row then could not be written is refunded
at once, best effort, so it does not wait for the lease to drain.

The cohort, `pilot_reason` in `services/fastpath_frontdoor.py`, is the
design's §4.11 at its narrowest: a credit-funded key on a standard route,
not streaming, with a verified boot, no service tier,
no key cap or window (alert-only windows included), no strict budget, no
app markup, every candidate priced in credits (a BYOK fallback books no
credit usage, and a lease would charge it); a workspace in the pilot set;
a control plane in a pilot region, with the settle outbox and the durable
activity outbox on (the leased settle writes both in its own commit).
An idempotency key does not exclude a request: the enclave sends one on
every authorize, and a leased authorization replays by its scope exactly
as today's does: the leased authorize writes a `tr_reservation` row that
holds nothing (credit 0, key 0) and carries the scope, the one unique
index both paths claim, so a key used on today's path and then admitted,
or the reverse, or admitted and retried with the pilot turned off, gets
one authorization, two attempts at once conflict on that index, a retry's
hold is given back, and a differing body is 409. The row expires as
today's does: a leased authorization abandoned with no terminal gets
today's lifecycle (the reaper finalizes it released, holding nothing,
while the lease ends the hold at its end of life; a pending outbox intent
keeps the reaper off it), and the leased finalize claims it with the
records, so it retires with them. Everything else stays on
today's path, as does any request the front door does not admit: `off` (the
workspace is not on the fast path's own switch), `busy` (no lease took it),
`invalid`, a front door that failed, or none serving. A request is never
refused for a fast path that is not there.

The front doors are the fleet's serving members, `tr_fastpath_member`, read
from Spanner and cached thirty seconds, so a node a deploy replaces is found
by its new address; each has a breaker (five failures, thirty seconds).
One call tries at most two of them within one budget, the whole call's.

## The switches

Three, all in the control plane's settings (`config.py`), none on by
default, in the order they are turned on:

1. `TR_FASTPATH_PILOT_WORKSPACES`: the pilot set, Joseph's workspace and,
   from 2026-10-16, one customer's, by ID.
2. The fast path's own switch for that workspace:
   `fastpathctl -database $DB enable WORKSPACE` (W1). The front door
   answers `off` until it is on.
3. `TR_FASTPATH_PILOT_ENABLED=true`: the client.

`TR_FASTPATH_PILOT_REGIONS` is `us-central1` and stays so until the fast
path has nodes elsewhere. `TR_FASTPATH_FRONT_DOOR_TIMEOUT_SECONDS` is the
whole budget of one call, 1.5 s, spent inline on a customer request; past
it the request takes today's path.

## How it turns off

Any one of: `TR_FASTPATH_PILOT_ENABLED=false` (every new authorize takes
today's path at once; terminals of admitted requests still go to the front
door, which keeps them); `fastpathctl disable WORKSPACE` (the front door
answers `off`, the workspace's leases are revoked and drained, W2); the
workspace removed from the set. A control-plane setting changes with a
deploy; the fast path's switch changes at once.

## What it checks

- The audit after every hour of the pilot, and before and after a switch:
  `fastpathctl -database $DB -funded CREDITS audit WORKSPACE` exits 0,
  CREDITS the sum of the workspace's `total_credits` at the time. It counts
  the workspace's synchronous holds in flight beside its leases' (#1821,
  running from 2026-10-17 05:10Z). On `tr-fastpath-1`,
  `/var/tmp/pilot-audit-hourly.sh` runs it for each workspace in the set
  every hour, logs to `/var/tmp/pilot-audit.log`, and writes an alert of
  kind `other` on any problem, which **TR Fast path: other** notifies; a
  funding it cannot read is logged and waits for the next hour. Like the
  watches, it does not survive the node's reset: it is restarted after.
- The counter reconciler's combined identity on the workspace
  (`storage_gcp_counter_reconcile.py`): reservations and bookings of the
  leases with the synchronous holds.
- `fastpathwatch -workspace WORKSPACE` for each workspace in the set, with
  P1's ceilings and `-fresh 15m -window 20m`, since one Spanner CPU series a
  few minutes late stopped the first watch (2026-10-16 02:00Z). The spend
  ceiling counts the workspace's whole usage, today's path included, so it
  is set for each workspace above its own expected spend over the watch's
  life.
- The control plane's log line per authorize the pilot took or passed over
  (`fastpath authorize <status>`), and the settle's.
- Cloud Logging: no `fastpath_alert` entries for the workspace.

## Two exposures the pilot carried

Both were fixed (#1784 and #1783), and the fixes run on the nodes from
2026-10-17 05:10Z, the first release the nodes ran after their pins
(`fast-admission-p1-results.md`, "The release the nodes ran"). Before
that, the pilot's first fifteen minutes ran with them:

- A non-streaming hold with no terminal is never released by the owner:
  it has no heartbeats, so adoption skips it and a healthy owner keeps
  renewing its lease past the hold's end of life; it ends when the lease
  drains. The pilot reaches this when a hold is admitted and its
  authorization row is not written and the best-effort refund is not
  taken (logged at error), or when a durable intent is never decided. The
  audit shows such holds; W2's turn-off drains them.
- A refund that reaches the drain log (the owner out of reach) becomes a
  winner with no boot binding, so the auditor's pending work alerts
  `no_boot_binding` on every sweep and that pack is never marked done;
  the lease does not retire. Money is unaffected.

## What is not in the pilot

Streams (a small share of requests; left out on Joseph's word,
2026-10-16); any workspace but the two in the set; the 503 in place of 402
when a balance's headroom sits in leases (the fallback reserves today, so a
large lease allocation on a small balance can refuse a request today's
path would have taken; each workspace's room is checked before it joins
the set); the enclave
calling the fast path itself; W7's alert policies.

## The state on 2026-10-17

The pilot is live. The control plane in `us-central1` runs it from
2026-10-17 04:55Z (#1793, merged as `d0e4c7169`), for Joseph's workspace
and one customer's; the first request it admitted was the customer's, and
by 05:11Z it had admitted and settled 42 with no warning, four taking
today's path when no lease had room. The nodes run
`93e69666a`, checked on their processes, with W7's alert policies applied
and exercised (`docs/runbooks/fastpath-alerts.md`), a watch for each
workspace and the hourly audit on `tr-fastpath-1`.
