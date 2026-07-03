# Durable settle outbox

Status: **design, not yet implemented** (the fast kill-switch half of #43/#33
shipped in PRs #109/#110; this is the remaining half). Owner decision needed on
the prod rollout + the enclave-side question in §6.

## 1. Problem

When the attested gateway (quill-cloud-proxy enclave) finishes a request it
calls the control plane's `POST /internal/gateway/settle` with the token
actuals; `_settle_gateway_authorization` books the real cost against the
reservation (`typed_finalize_gateway_authorization` for typed reservations, the
legacy path otherwise). The reservation is one-shot and idempotent — a second
settle on an already-settled authorization returns `already_settled` without
double-charging.

The gap is a **settle that never lands**: the enclave computed actuals, but the
settle call fails and its retries are exhausted (Spanner blip during the finalize
txn, a control-plane crash after computing cost but before commit, a network
partition that outlives the enclave's retry budget). The hold then sits until
`reap_expired_reservations` reclaims it — and the reaper **releases the hold
WITHOUT booking a charge** (`actual_micro=0`, `success=False`). That is the
documented "bounded-loss accept" in `storage_gcp_authorize.py`: a genuinely
completed request whose settle was lost gets served **for free**.

Today this is bounded and rare (generous `expires_at` = execution deadline +
settle-retry window + margin means the reaper only fires on genuinely abandoned
holds). It becomes a real revenue leak "before whales": a single large customer
hammering the gateway multiplies the rare lost-settle into material dollars, and
a correlated Spanner incident can lose a burst of settles at once.

The `reap_expired_reservations` docstring already names the fix:
> A durable settle outbox (gateway persists actuals on response) is the planned
> enhancement to recover the rare completed-but-settle-lost charge instead of
> releasing it free.

## 2. Goal

Recover completed-but-settle-lost charges: no request that actually ran is
released free. Preserve today's guarantees — **exactly-once billing** (never
double-charge), no new hot-path deadlock surface, and **zero behavior change
until deliberately enabled** (default-off, like every billing cutover here).

## 3. Design — mirror the broadcast durable-job pattern

The codebase already has a proven durable-job + drain-worker pattern for
broadcast delivery (`storage_broadcast.py` enqueue → `due`/`claim` with lease →
`mark` with exponential backoff + `dead` after `max_attempts`; drained by
`POST /internal/broadcast/drain`). The settle outbox is the same shape.

### 3.1 Table `tr_settle_outbox`

Idempotent by `authorization_id` (the natural settle key — one settle per
authorization). Columns:

| column              | type      | note                                             |
|---------------------|-----------|--------------------------------------------------|
| `authorization_id`  | STRING PK | one row per authorization; INSERT is the claim   |
| `settle_body`       | JSON      | the full `GatewaySettleRequest` (token actuals)  |
| `success`           | BOOL      | settle vs refund intent                          |
| `status`            | STRING    | `pending` / `done` / `dead`                      |
| `attempts`          | INT64     | drain retry count                                |
| `next_attempt_at`   | TIMESTAMP | backoff gate                                     |
| `lease_owner`       | STRING    | drain worker lease (nullable)                    |
| `leased_until`      | TIMESTAMP | lease expiry (nullable)                          |
| `created_at`        | TIMESTAMP |                                                  |
| `updated_at`        | TIMESTAMP |                                                  |

DDL added to the existing idempotent `apply_ddl` migration script (additive,
NULL/defaulted columns — safe to run before the code that reads them, same
runbook as the typed-counter migration).

### 3.2 Enqueue at settle (behind a flag)

In `_settle_gateway_authorization`, gated on `settings.settle_outbox_enabled`
(default **False**):

1. **Before** attempting the inline settle, `INSERT` the settle intent into
   `tr_settle_outbox` keyed by `authorization_id`. `ALREADY_EXISTS` = a prior
   attempt already recorded the intent → fine, proceed (idempotent).
2. Attempt the inline settle exactly as today.
3. On inline success, `UPDATE … SET status='done'` for that `authorization_id`.
4. If the inline settle raises, the row stays `pending` and the drain recovers it.

The intent is durable the instant step 1 commits, so a crash anywhere after that
is recoverable. When the flag is off, the settle path is byte-identical to today.

### 3.3 Drain worker

`POST /internal/gateway/settle-outbox/drain?limit=N` (internal-token auth, same
as the broadcast drain), invoked by the existing internal-worker cron. It
`claim`s due `pending` rows (lease), and for each **replays
`_settle_gateway_authorization`** with the stored body. Replay is safe because
settle is already idempotent: an authorization that DID settle inline returns
`already_settled` → mark the outbox row `done`; one that didn't settles now.
Failures back off (`mark` with `next_attempt_at`); after `max_attempts` the row
goes `dead` and alerts (a settle we genuinely cannot apply — needs a human).

### 3.4 Interaction with the reaper (the ordering that matters)

The whole point is that the outbox must win the race against the reaper's
free-release. Two mutually-reinforcing guarantees:

- **Drain cadence < reaper `expires_at`.** The drain runs on a tight loop
  (seconds–minutes); `expires_at` is the generous execution deadline. A lost
  settle is recovered by the drain long before the reaper considers the hold
  abandoned.
- **Even if the reaper wins,** it releases via the same claim-gated
  `settle_atomic` (whoever claims the reservation row first wins). A later
  outbox drain then finds the reservation already settled (as a free release)
  and marks the outbox row `done`. To *recover* the charge in that case the
  reaper must **skip reservations that still have a `pending` outbox row** —
  i.e. `reap_expired_reservations` gains a `NOT EXISTS (pending tr_settle_outbox)`
  guard so it never free-releases a hold the outbox still intends to charge.
  (This guard is the one change to the reaper; it is a no-op when the flag is
  off / the table is empty.)

## 4. Exactly-once argument

- Enqueue keyed by `authorization_id` (PK) — at most one intent per settle.
- Apply (inline or drain) goes through the existing one-shot `settled` gate —
  `already_settled` short-circuits, so N replays book at most once.
- Reaper free-release is suppressed while a `pending` outbox row exists, so a
  charge and a free-release cannot both happen.
- Net: **at-least-once delivery of the intent + idempotent at-most-once apply =
  exactly-once billing.**

## 5. Rollout (default-off, Joseph-gated)

1. Run the additive DDL against prod (safe; adds an empty table).
2. Merge the mechanism with `settle_outbox_enabled=False` — dead code, zero
   settle-path change; the drain runs against an empty table.
3. Enable enqueue in **shadow** first if desired (write outbox rows, still rely
   on inline settle + reaper) and watch `dead`-row count + a "recovered by drain"
   metric.
4. Flip `settle_outbox_enabled=True` + the reaper guard once shadow is clean.
   This is a **billing prod-behavior flip → needs Joseph's explicit go** (same
   bar as the typed-enforcement cutover).

## 6. Open question for the owner — enclave cooperation

The in-repo outbox recovers **control-plane-side** losses (settle arrived at the
control plane at least once, then the finalize failed/crashed). It does **not**
cover the enclave computing actuals but *never delivering* the settle call at
all (total network partition beyond the enclave's own retry budget). Fully
closing that requires the enclave to persist actuals and retry
delivery — a quill-cloud-proxy change (cross-repo, `NO PRs` / push-to-main-with-
Joseph's-go). Recommendation: ship the control-plane outbox first (covers the
common Spanner-blip / crash loss), then decide whether the enclave-side durability
is worth it based on the observed `dead`/lost rate.

## 7. Test plan (full pyramid)

- **Unit (fake Spanner):** enqueue idempotency (double-INSERT → one row); drain
  replay marks `done` when already settled; drain applies when not; backoff →
  `dead` after `max_attempts`; the fake models the new table's SQL shapes
  (watch substring-collision ordering — past fake-Spanner bug source).
- **Functional/route:** settle with flag on writes an outbox row and marks it
  `done`; a forced inline-settle failure leaves it `pending`; the drain endpoint
  then settles it; `already_settled` replay is a no-op.
- **Integration:** authorize → (inline settle fails) → reaper is *suppressed* by
  the pending outbox row → drain recovers the charge → balance reflects the real
  cost, not a free release.
- **Reaper guard:** a reservation with a pending outbox row is not free-released;
  one without is (unchanged).
