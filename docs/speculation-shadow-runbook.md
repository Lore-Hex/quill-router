# Speculative invocation PR3: router shadow only

This binary deploys with `TR_SPECULATIVE_PROVIDER_SHADOW_ENABLED=false`.
**The additive migration is a precondition for enabling observation, not for
landing or deploying this off binary.** Ordinary authorization, settlement and
refund work against the old schema. No production migration or enablement is
part of this change. Stage A echo is not an input or dependency. The surviving
billing-pause and Stage D settings retain their existing meanings.

The shadow issuer has purpose `shadow-grant` and emits only
`speculation-eligibility-shadow+jws`. Its independently provisioned Ed25519
private key is a mounted PEM file, selected by
`TR_SPECULATION_SHADOW_PRIVATE_KEY_FILE`; configure its kid, issuer, audience,
plane and environment independently. Never mount a real-grant or receipt key
there. No fixture keys ship. Every issued token self-verifies through the
frozen v1 verifier with independently resolved context. Tokens confer no
dispatch, acceptance, billing, customer-latch or real-permit authority.

## Observation and ordinary-path isolation

Only the outer authorize timing owner emits. Nested async/thread wrappers
share the allowlisted holder; direct sync callers own one holder. Emission
follows the completed timing snapshot, including error snapshots. The queue
contains immutable identities, invocation/authorization correlation, reason
codes, verified-boot status, stored endpoint IDs and integer timing fields.
It never contains a response, prompt, raw credential, BYOK payload, hidden
model text or request body. A replay captures the stored authorization's
route and never adds a success, including when its original was missed.

Off takes the original timing path before importing the observer. It creates
no observation holder, enqueue, worker, issuer load or schema read. The
refresh handler authenticates the internal caller and returns
`feature-disabled` before reading its body or accessing storage. Settle,
refund and heartbeat have no observation scope in either mode.

Submission is bounded at 1,024 records and never waits for worker IO. One
independent thread processes the projection with its own two-second Spanner
budget and fresh ContextVars. Overflow, lock contention and writer failure
set a separate sticky loss bit; the full queue cannot hide it. The worker
never owns the submit lock. Ordinary status, headers, body, economics and RPC
transcript remain unchanged when the observer fails.

## Schema and projection

Run `scripts/deploy/migrate_speculation_shadow.sh` through the reviewed
migration workflow outside a rolling deployment, with the usual explicit
Spanner instance/database/project inputs. The script checks object existence
and performs additive DDL only. No customer backfill, old lease table reuse,
Bigtable, or money mutation is involved. Its DDL is extracted into
`tests/conformance/spanner_ddl.py`, not exempted from the carrier inventory.

| New table | Key after plane | Purpose |
|---|---|---|
| `tr_speculation_shadow_event` | producer incarnation, sequence | Event identity, authoritative observation time, receipt time, commit timestamp |
| `tr_speculation_shadow_success` | hashed authorization or key/invocation identity | Distinct-success uniqueness |
| `tr_speculation_shadow_scope` | hashed workspace or workspace/key identity | Clean interval, last 20 successes, shadow epochs, route reference |
| `tr_speculation_shadow_producer` | configured producer identity | Incarnation, submitted/committed watermark, five-second lease, membership digest, sticky loss |
| `tr_speculation_shadow_paid` | workspace ID | Versioned, expiring paid-source evidence and coverage anchor |
| `tr_speculation_shadow_route` | endpoint ID | Reviewed route capability and vendor-price evidence |
| `tr_speculation_shadow_grant` | hashed workspace/key/boot | Replay-stable signed token and generation |
| `tr_speculation_shadow_exposure` | workspace, slot or fleet identity | Hypothetical retained exposure, owner and next ordinal |

Every table has an allowlisted JSON body and a server commit timestamp. The
adapter's mutation target is a closed shadow-only table map. Event insertion,
success uniqueness and scoped projection updates share one transaction.
Source reads are bounded by the independently resolved workspace/key; the
trust-event scan stops at 1,001 rows and treats reaching the bound as
incomplete. All new SQL expressions have typed inventory scenarios.

## Evidence required before future enablement

Keep workspace, route, image, producer and slot lists empty in this PR.
Provision them later through config as code, including a positive image-policy
version and absolute policy expiry. A boot's `verified` registration is
insufficient: its image must still be accepted. Each batch member independently
resolves to its requested stored key and workspace. Missing stable slot/region,
unsupported plane/backend, stale policy, missing tables or source facts produce
misses. Memory and PostgreSQL report `shadow-not-supported` and ordinary traffic
continues.

Each process needs a distinct configured producer identity and fresh random
incarnation. Every relevant router writer must belong to the configured
roster; an external release audit must establish that this roster is complete.
All roster leases and contiguous watermarks must be current for a grant.
Changing membership, restarting, observing an unresolved outcome, missing a
sequence or receiving a late denial rebuilds a full 15-minute clean interval.
An expired producer lease blocks grants. Sticky local loss requires a clean
restart and rebuilding coverage; a later success or refresh cannot clear it.

Paid evidence is deliberately conservative. Tier 2/3 and current balance are
not proof of remaining paid principal. A complete reconciled coverage anchor
must supply `coverage_version`, `source_ledger_digest`, `source_credits_micro`,
`source_as_of` and `source_expires_at` in the paid projection. The digest is
SHA-256 of the canonical bounded source-query rows. A reconciler/release owner
must establish source completeness, including historical credits and transfers;
PR3 does not invent that evidence from matching net totals. Without the anchor,
refresh writes diagnostic incomplete evidence and issues no token. Any adverse
or recovery event, incomplete ledger, missing shard, divergent trust state,
future/stale reconciliation or limited/delegated/federated key is a miss.
Refresh also persists an allowlisted key-policy digest and source-read expiry;
a changed digest advances the shadow key epoch and rebuilds its clean interval.
From conserved inflows, N bounds **all** nonqualifying funds above; proved paid
headroom is `max(0, credits - usage - reserved - N)`. Lifetime top-up is never
consulted. Promotional-only and ambiguous mixed funds cannot qualify.

A reviewed route row requires `complete`, an absolute `expires_at`, the exact
v1 `route`, and `applicability` containing vendor source/revision, certified
serialized cap/input method and policy version. The deterministic catalog hash
covers the complete route and applicability. Customer prices do not certify
vendor COGS. No provider endpoint has been certified or enabled by this PR.

## Refresh and retained simulation

`POST /internal/speculation/shadow/refresh` requires the ordinary internal
gateway token plus exactly one `X-TR-Boot-Auth` signature over method, actual
endpoint path and **exact entire body**:

```json
{"items":[{"lookup_digest":"<64 lowercase hex>","workspace_id":"<workspace>","key_id":"<stored key>"}]}
```

Maximum 64 items and 32,768 encoded bytes; duplicate JSON keys are rejected.
Duplicate canonical identities collapse. The implementation admits one refresh
worker, caps active boot/key entries at 256 and rate records at 256, and permits
one batch per boot per ten seconds. Clients should jitter refreshes and never
wait for them on a request. Miss codes appear per item; batch authentication,
size, rate, or worker failures refuse the batch. Immediate replays may be rate
limited; a later refresh returns the same still-valid token without extending
its original deadline or increasing the hypothetical allowance.

Grants expire within 30 seconds, with a two-second start margin and earlier
key/trust/price deadlines. Cached tokens are rechecked against current evidence
and shadow epochs before return. A current-second outcome cannot qualify the
saved predecision. Retained hypothetical exposure is shared across workspace,
stable slot and fleet, with one workspace owner. W uses tier ceilings
25,000,000/100,000,000 microdollars and the frozen allowance function; slot and
fleet bounds are 1,000,000/10,000,000. Each signed ordinal costs full B.
Exposure never refills on refresh, expiry, key change or boot restart. This
initial conservative projection does not release retained exposure; future
reconciliation must prove any release. These rows can never be promoted into
real allocation rows.

`GET /internal/speculation/shadow/status` uses internal authentication and no
storage reads. Off reports disabled. Missing schema/worker/issuer or unsupported
backend reports a named 503. `observing` describes the worker only; actual grant
readiness is evaluated per item and must not be inferred from HTTP health.
The response exposes separate worker/refresh RPC totals, queue depth and sticky
coverage loss; these counters never contribute to ordinary request timing.

Cached grants may outlive a newly unobserved denial: this is **shadow lag**, not
PR8's durable serialized fence. Join by plane, producer incarnation/sequence,
event ID, invocation nonce and authorization identity; retain unresolved events
and coverage failures in the denominator. Pair router/enclave release SHAs and
policy versions in the future release evidence. Shadow grants never justify a
provider request. Disable via reviewed config rollout; ordinary traffic needs
neither these tables nor the key when off. Keep Stage D and billing pause armed
as required by their own policy.

## Local gates and release limits

Run ruff, mypy, the new suites, the ordinary ordered RPC and timing/money
matrix, Stage D, boot, outbox and all conformance suites, followed by the full
four-worker test run. Native GoogleSQL execution requires a loopback Spanner
emulator and `TR_CONFORMANCE_EMULATOR_SCHEMA=1`; skipped emulator cases are not
SQL acceptance. Run `python -m tests.speculation_shadow_mutations` for the
baseline → mutant → restored-baseline gates. Every mutation uses disposable
copies, reports red/survived/build-broken, and leaves the worktree intact.
See `speculation-shadow-mutations.json` and `speculation-shadow-validation.md`
for this worktree's actual results. No deployment or git writes are performed.
