# The fast path's pilot: Joseph's workspace

The design's rollout step 8 (`fast-admission-and-batched-settlement.md`
§8), as `fast-admission-production-rollout.md` asks: what the pilot enables,
what it checks, and how it turns off. It starts on Joseph's own workspace,
on his word (2026-10-14), after P1's runs 1 to 6 (`fast-admission-p1-results.md`).

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
its payload and reserves nothing: no credit hold, no key hold, no
`tr_reservation` row. The hold is the lease's. At settle or refund the control
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
every authorize, and a leased authorization replays by its scope as
today's does (the index row of the scope is written with the
authorization row; a retry's hold is given back). Everything else stays on
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

1. `TR_FASTPATH_PILOT_WORKSPACES`: the pilot set, Joseph's workspace id.
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
  `fastpathctl -database $DB -funded CREDITS audit WORKSPACE` exits 0.
- The counter reconciler's combined identity on the workspace
  (`storage_gcp_counter_reconcile.py`): reservations and bookings of the
  leases with the synchronous holds.
- `fastpathwatch -workspace WORKSPACE` with P1's ceilings, the spend
  ceiling at a tenth of the workspace's credits.
- The control plane's log line per authorize the pilot took or passed over
  (`fastpath authorize <status>`), and the settle's.
- Cloud Logging: no `fastpath_alert` entries for the workspace.

## Two exposures the pilot carries

Both are the fast path's to fix, filed as follow-ups, and stated here
because the pilot runs with them:

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

Streams (they join once registrations carry the heartbeat declaration,
§4.11); any workspace but Joseph's; the 503 in place of 402 when a
balance's headroom sits in leases (the fallback reserves today, so a large
lease allocation on a small balance can refuse a request today's path
would have taken; the pilot workspace's balance is large); the enclave
calling the fast path itself; W7's alert policies.

## The state on 2026-10-14

Landed on one branch, each piece tested against the fake Spanner store and
the route itself: the settings, the client, the cohort predicate, the typed
store's leased authorize (`authorize_gateway_typed(..., fastpath_envelope=...)`)
and leased finalize (`typed_finalize_leased_result`), and the route wiring
(`_fastpath_admission` before the typed store call in
`_authorize_gateway_sync_impl`; `_settle_leased_authorization` before the
settle-outbox intent in `_settle_gateway_authorization`). The pilot set and
the client switch are off until the deploy that follows is verified.
