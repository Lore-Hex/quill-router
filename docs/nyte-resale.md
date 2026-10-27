# NYTE resale, step 1

Local implementation on `nyte-resale`; keep all changes uncommitted. Nothing in
this document authorizes a release or publication. Public terms: **2026-10-27-v5**.

## Storage decision and schema

Reuse the reserved-notification entity pattern from checkout #2125 through
`Store.reserved_resale(command)`. Native Spanner and Postgres use their existing
`tr_entities(kind,id,body,updated_at)` table. In-memory executes the same command
function under its store lock. The transaction function buffers writes before
committing, including read-your-writes for Spanner mutation semantics. No credit,
ledger, workspace or balance method is called.

A single `nyte_mutex/v1` row serializes this low-volume program. Postgres seeds
and locks it; native Spanner reads and writes a revision in the transaction.
All state reads use complete primary keys. No body search, table scan or
cross-cloud transaction is introduced. This simple serialization prioritizes
correctness for step 1; throughput is intentionally bounded by one NYTE mutex.
A higher-volume rollout should partition by grade with a reviewed lock order.

| Entity kind / key | Durable fields |
| --- | --- |
| `nyte_position` / Stripe Checkout Session | Stable position ID, grade, original lots, per-lot cents, original PaymentIntent, first `purchase_completed_at`, holder hash, terms version, sold/listed lots, status, payment-review marker, allocation status (pending/complete/historical), listing/fill IDs, new-inventory lots, holder-next pointer, event ID |
| `nyte_payment` / original PaymentIntent | Session reference; isolates Dashboard refunds without inherited metadata from credit/adverse handlers |
| `nyte_payment_review` / original PaymentIntent | Sticky disputed/unpaid flags, maximum observed external refund cents, observation time; retains adverse state for mapped payments, without raw Stripe payloads or PII |
| `nyte_holder` / domain-separated normalized email hash | Head of the holder's position list; no plaintext email |
| `nyte_listing` / hash of holder + request ID | Position session, lots listed/sold, listed_at, active/sold/withdrawn status, FIFO previous/next |
| `nyte_fill` / hash of buyer session + listing | Buyer session/event, listing, lots, resale price per lot, fee bps/total cents, capped refund total cents, seller PaymentIntent, Stripe refund ID, pending/in_flight/retry/refunded/needs_ops status, transition and stale-write history, seller position ID, failure status, first attempt, lease owner/expiry, refund-queue previous/next |
| `nyte_refund` / Stripe refund ID | Typed obligation reference (`resale_fill` or `full_refund`), original PaymentIntent and amount; legacy fill-only bindings remain readable |
| `nyte_refund_failure` / Stripe refund ID | First unmatched failure/cancellation: PaymentIntent, amount, status, timestamp and optional typed identifiers; replayed when the obligation is bound, with no raw provider payload |
| `nyte_queue` / `grade:{grade}`, `refunds`, `refunds_needs_ops`, `full_refunds`, `full_refunds_needs_ops` | Linked queue head/tail; completed entries are unlinked; issued refunds needing manual reconciliation have a separate queue |
| `nyte_event` / Stripe event ID | Buyer session; session identity also prevents duplicates across different event IDs |
| `nyte_action` / hash of holder + request ID | Original command and result; rejects reusing an ID for a different action |
| `nyte_access` / hash of capability | Holder hash, expiry, consumed marker or session purpose; never raw capability |
| `nyte_refund_request` / original session | Full-refund obligation: accepted request time, position ID, original payment/amount, bound refund ID, pending/in_flight/refunded/needs_ops status, transition/stale-write history, queue links |

Lists of holder positions and refund work paginate at 100. Grade queues and
holder indexes do not grow into one history-sized JSON row. Per-position
listing/fill history and consumed capability records are retained in step 1;
expiry is enforced on every access, not dependent on a background TTL deletion.

**No new DDL migration:** the user authorized the #2125 storage pattern as an
alternative to new tables. Physical schema stays unchanged. The production
migration source/manifest machinery is `tests/conformance/spanner_schema_source.py`,
`spanner_ddl.py` (including source digests), and `spanner_sql_manifest.json`.
`test_spanner_schema.py` guards drift. Native acceptance provisions that complete
migration-derived schema and runs the new conformance tests:

```sh
TR_CONFORMANCE_EMULATOR_SCHEMA=1 SPANNER_EMULATOR_HOST=127.0.0.1:9010 \
  uv run pytest -q -rsx tests/conformance/test_nyte_resale_ledger.py -k spanner-emulator
```

The CI emulator image/gate is in `.github/workflows/ci.yml`, job
`spanner-emulator`. Missing opted-in emulator infrastructure must fail, not skip.

## Purchase, allocation and payouts

The signed reserved-token webhook validates paid USD totals and locked metadata
before any credit handling. With `TR_RESALE_ENABLED=false`, paid checkout follows
the original #2125 notification path and never calls the resale ledger. Every
holder API action (including link requests and preflight) returns the inert
generic response without email or ledger access. Existing refund obligations
continue to reconcile lifecycle events and alert ops regardless of the sales
flag. The sole event scope is the existing PaymentIntent-to-NYTE-position mapping.
For mapped payments, even metadata-free, unbound failures are retained in either
mode. Unmapped payments create no NYTE state, regardless of metadata or flag.
Charge/dispute reconciliation uses the same mapping scope and is not flag-gated.
With the flag enabled, positions
are written before the notification's already-sent return. A successful notification
from before resale storage existed imports that position without allocating
historical sales. New paid purchases consume active grade listings in FIFO order,
then new inventory. One transaction writes all fills and the buyer position;
competing purchases and withdrawals cannot oversell. Withdrawal removes only
unsold lots. Relisting joins the tail. New buyers pay the same grade price for
resold and newly issued lots and own a new position.

The card price is the index price minus the discount locked at checkout; a paid
session is never repriced when its webhook is delivered late. Purchase time is
**the persisted first successful-payment webhook time**, never session creation.
The notification row is the first-write source across retries and older imports.
The full-refund window includes its exact deadline; listing requires strictly
more than 30 days. A timely full-refund request locks the position and queues ops
work even if notification delivery fails.

For each sold lot:

```
net_cents = floor((resale_price_cents * (10000 - fee_bps) + 5000) / 10000)
fee_cents = resale_price_cents - net_cents
refund_cents = min(net_cents, original_per_lot_cents)
```

Round net proceeds half-up per lot, then cap and multiply by filled lots. Never
round an aggregate instead. With 500 bps, a holder who paid **$100 per lot** and
sells two lots at **$120** gets `min($114, $100) × 2 = $200` refunded on the original
payment. NYTE retains **$40**: $12 fee and $28 above the cap. The new buyer paid
$240 and holds two lots at $120. At a $90 resale price the holder gets **$85.50**
per lot, with $4.50 retained as the fee. A 30-cent lot yields 28.5 cents net,
rounded to **29 cents**, before applying the original-price cap.

After the allocation commit, the webhook schedules Stripe refunds as background
work. A refund failure cannot undo allocation, return an unpaid buyer position,
or credit a balance. Each fill has a durable lease and a stable idempotency key
`nyte-resale-{fill_id}`. Before creating, the worker searches only refunds on
that original PaymentIntent for `metadata.fill_id` (also accepting legacy
`nyte_fill_id`). Created refunds carry only kind, fill ID and seller position ID
metadata (`kind=resale_fill`), with no description or contact data. The existing
fill-derived idempotency key is unchanged. Full refunds are still issued by ops,
not this worker; acknowledgement binds their ID, original payment and full amount.
Programmatic ops issuance should use `kind=full_refund`, the public `position_id`,
and an idempotency key derived from that position (for example
`nyte-full-refund-{position_id}`). Metadata-free Dashboard refunds are supported
through the verified binding at acknowledgement. Known refunds are
retrieved until Stripe reports `succeeded`; pending refunds remain retryable.
Failed/canceled issued refunds move to `needs_ops`, retaining their Stripe refund
ID. A timeout after Stripe acceptance is reconciled to the same refund.

All `refund.created`, `refund.updated`, `refund.failed` and
`charge.refund.updated` events use the same lifecycle processor as worker results
and full-refund acknowledgements. Failed/canceled status reopens even a
previously successful obligation into `needs_ops`. The transaction resolves a
bound refund ID or typed metadata (`fill_id` / `position_id`) and validates the
original PaymentIntent and amount against the durable obligation. Metadata can
resolve it before the worker records the refund ID; a binding remains authoritative
if metadata later changes. Legacy `nyte_fill_id` metadata remains supported.
Mismatched payment/amount is rejected; unmatched failures are retained for later
verified binding without changing an unrelated obligation. The event retains its
refund ID and moves the obligation to `needs_ops`, removing retry work and
clearing the lease. Full-refund failure also blocks the associated position.
This state is terminal for automation for both kinds. Claims explicitly enter
`in_flight`; success is conditional on that state, and worker completion also
checks its lease owner.
A late success cannot overwrite `needs_ops`, and is recorded as a `stale_write`
in the obligation history. Both failure-before-success and success-before-failure
therefore end in manual reconciliation without a second refund. Ops alerts
contain only fill/refund identifiers, never Stripe descriptions or buyer email.

Stripe retains idempotency keys for at least 24 hours. After **23 hours** from the
first attempt, an absent matching refund is an ambiguous case: the worker alerts
ops and refuses another create. This deliberately favors reconciliation over a
possible duplicate payment. The seller's original charge and per-lot cap remain
the only payout source; no Connect account, transfer, cash payout or balance
credit is created. Manual external refunds/disputes must be reconciled by ops
before selling can resume. Refund/dispute webhooks and Stripe imports reconcile
existing positions atomically: an external refund or dispute marks holdings for
review and withdraws every active listing. Already requested/completed full
refunds keep their blocking status so ops can still acknowledge them. Allocation
also checks seller status. Stripe refunds are exempt only when their payment,
amount, fill metadata and (when recorded) refund ID match a durable NYTE fill.
The match works before the refund worker commits success; repeated evidence
counts once per fill. Refund listing reads are bounded and payment-scoped.

Adverse webhook evidence is retained only when the PaymentIntent already maps
to a NYTE position. Mapped evidence is sticky: healthy/stale observations cannot
erase it. Unmapped events are inert, including events delivered before checkout
creates that mapping. Every enabled paid webhook, including replays, and every
historical import still reads current Stripe PaymentIntent and charge state
before writing a held position. External refunded amounts and disputes block it,
while verified resale refunds are exempt. Stripe read failure leaves the webhook
retryable instead of assuming a healthy payment. Disabled paid checkout keeps
the original #2125 path and does not touch the resale ledger.

Refund failure/cancellation evidence for a mapped payment is retained even when
there is no refund-ID binding or refund metadata yet. A later acknowledgement
checks and applies that evidence before it can complete the obligation. Refund
lifecycle, evidence retention and acknowledgement contain no sales-flag branch.

## Holder and operational access

`/manage-reservation` is a static NYTE page. Its API,
`POST /token-exchange/reservations`, runs on **control**, not the memory-only
actions bulkhead. The Python and Terraform URL maps include that route.

When enabled, email requests always return the exact same generic message. Valid addresses get
a capability regardless of whether holdings exist, avoiding lookup-dependent
responses/timing. Tokens use the shared OAuth domain-separated HMAC helper with
the existing Stripe secret, a random nonce, a 15-minute signed expiry and an
atomic single-use durable marker. Redemption returns a separate 15-minute bearer
session. Both are stored hashed. Contact-bearing Stripe and SES calls suppress SDK/transport payload logs in
their own thread context, and disable Sentry outbound-query tracing in that
scope. Stripe direct console diagnostics must be off for
all private Stripe calls; that mode bypasses Python logging filters, so calls
refuse it before changing the ledger. The same context protects refund worker
create/list/retrieve calls and full-refund acknowledgement, including SDK DEBUG
response payloads, child transport logs and Sentry tracing.
Email links put the token in the fragment, and
the page removes it immediately with `history.replaceState`; no token in query
strings, referrers or analytics. An explicit redemption button prevents ordinary
email scanners using the link. The session lives only in browser memory.

The page supports paginated positions, list-N, withdrawal, refund requests and
an authenticated “Find older reservations” action. That action uses Stripe's
exact-email customer listing (enter the original email capitalization) and
customer-scoped completed sessions, bounded to
100 customers and 1,000 sessions per customer. It compares each session's actual
purchase email. No entity scan is permitted. Missing historical completion time
stays unknown and blocks self-service mutations pending ops reconciliation. Allocation completion has its own durable
`allocation_status`, independent of `purchase_completed_at`. An import racing a
webhook that has already persisted the purchase timestamp remains `pending`;
the webhook must allocate it before setting `complete`. Only an already completed
legacy notification establishes `historical`, which never consumes today's
listings. Pending positions cannot list or request refunds before allocation.
Neither interleaving duplicates the holder index or allocation.

Before enabling, Joseph/operator must:

1. Release the backend, control route maps, growth source allowlist and static
   holder/terms/buy assets through normal reviewed gates. Nothing was deployed
   during this task. Keep `TR_RESALE_ENABLED=false` until native emulator validation
   and a Stripe test-mode purchase/resale/refund cycle pass.
2. Keep `TR_RESALE_FEE_BPS=500` for these approved 5% terms. Config loading
   rejects every other integer with “a fee change needs a new terms version.”
   Every fill persists the applied `fee_bps`. Checkout retains its
   separate existing enable/discount settings. Rotating `TR_STRIPE_SECRET_KEY`
   invalidates outstanding holder links; new links use the new key.
3. Verify the existing Stripe key can read Customers, Checkout Sessions,
   PaymentIntents, Charges and Refunds, and create refunds. No new Stripe Connect or
   webhook endpoint is needed. Existing paid-checkout events remain required. Ensure this endpoint also
   receives `charge.refunded` and `charge.dispute.*` events for prompt external
   payment reconciliation, plus `refund.created`, `refund.updated`, and
   `refund.failed` for refund lifecycle reconciliation.
4. Verify SES sends holder links and ops alerts from the control service.
   `TR_PARTNER_INQUIRY_EMAIL` is the ops recipient (enterprise fallback).
5. Arrange periodic execution of `uv run python -m scripts.retry_nyte_resale_refunds`
   with the **control** deployment configuration, and run it after any refund
   alert. The durable queue survives process exits; scheduled infrastructure is
   not installed by this local change. Continue with the printed `--cursor`.
6. Review `--full-refunds`. Issue each full refund in Stripe against its original
   PaymentIntent, then run `--ack-full-refund-session cs_... --refund-id re_...`.
   The script verifies refund ID, full amount and original payment, then applies
   the shared monotonic transition. It prints the actual resulting status; a
   stale success cannot close failed work. Pending refunds remain visible, and
   failed/canceled refunds move to the full-refund ops queue. Do not grant balance credits. Review old ambiguous or
   failed resale refunds against `nyte_fill_id` before any manual repair.
7. Inspect `uv run python -m scripts.retry_nyte_resale_refunds --needs-ops` after
   an issued-refund failure alert. This command only lists identifiers and never
   creates refunds. Reconcile Stripe's failure with the retained original refund
   ID before a separately reviewed manual repair; there is no automatic reset or
   reissue for these obligations. Use `--needs-ops --full-refunds` for failed
   full refunds; the same command without `--full-refunds` shows failed resale
   fills. Both views are read-only.

Browser analytics `nyte.resale_listed` / `nyte.resale_withdrawn` contain only a
bounded grade, existing visitor/campaign metadata and `/manage-reservation` page.
The JS sanitizer, collector, structured log fields and growth source filter all
accept them. They honor GPC/DNT. Buyer emails, order IDs, tokens, quantities and
amounts never enter these events.

## Changed files

- `docs/nyte-resale.md`
- `infra/control_lb_url_map.tf`
- `scripts/axiom_growth/model.py`
- `scripts/deploy/rollout.sh`
- `scripts/deploy/service_surface_url_map.py`
- `scripts/retry_nyte_resale_refunds.py`
- `sites/token-exchange/README.md`
- `sites/token-exchange/analytics.js`
- `sites/token-exchange/nyte-v2/buy.html`
- `sites/token-exchange/nyte-v2/manage.html`
- `sites/token-exchange/nyte-v2/manage.js`
- `sites/token-exchange/nyte-v2/reserved-terms.html`
- `sites/token-exchange/nyte_v2.py`
- `sites/token-exchange/test_resale.py`
- `sites/token-exchange/test_reserved_checkout.py`
- `src/trusted_router/config.py`
- `src/trusted_router/exchange_analytics.py`
- `src/trusted_router/main.py`
- `src/trusted_router/routes/internal/webhook.py`
- `src/trusted_router/routes/oauth.py`
- `src/trusted_router/routes/resale.py`
- `src/trusted_router/security.py`
- `src/trusted_router/services/resale.py`
- `src/trusted_router/services/reserved_tokens.py`
- `src/trusted_router/storage.py`
- `src/trusted_router/storage_gcp.py`
- `src/trusted_router/storage_postgres.py`
- `src/trusted_router/storage_resale.py`
- `src/trusted_router/store_protocol.py`
- `tests/conformance/test_nyte_resale_ledger.py`
- `tests/js/exchange_analytics.test.cjs`
- `tests/js/nyte_holder.test.cjs`
- `tests/nyte_resale_helpers.py`
- `tests/test_exchange_analytics.py`
- `tests/test_nyte_resale.py`
- `tests/test_reserved_tokens.py`

## Original implementation validation (before review round 1)

All changes are uncommitted; no push, publication, migration or live Stripe
charge/refund was performed.

| Gate | Result |
| --- | --- |
| `uv run ruff check .` | Passed |
| `uv run mypy` | Passed, 551 source files |
| Mypy and `--help` for refund CLI | Passed |
| Reservation/resale/analytics pytest | 152 passed, 18 explicitly skipped server-backend cases; 86.10% coverage across the three new resale modules |
| Protocol, route ownership, schema guards, growth filter, OAuth integration pytest | 282 passed |
| Site unittests | 94 passed |
| JS analytics and holder UI tests | 17 passed |
| Site build | Passed; 13 markets / 30 domains in `/tmp/nyte-resale-site` |
| Native Spanner emulator | **Blocked**: six setup errors, connection refused at `127.0.0.1:9010`; Docker is not installed and the SDK emulator component is Linux-only on this ARM Mac. No native SQL/transaction proof is claimed. |
| Full repository pytest with coverage | **Not green / incomplete**: 22,492 passed, 1 failed, 1,422 skipped, 15 xfailed. The existing AWS deploy harness fails because macOS's `shasum` launcher cannot locate its versioned Perl companion through the temporary harness path. Interrupted the remaining queued worker after the failure; no complete-repository coverage result is claimed. |

The emulator command opts in with `TR_CONFORMANCE_EMULATOR_SCHEMA=1`; its failure
is not a green skip. Memory, Spanner-fake and the offline Postgres SQL test cover
ledger behavior, but do not replace native Spanner validation. Keep resale off
until that gate and the Stripe test-mode operational cycle are complete.

The broad-suite failure is
`tests/test_aws_postgres_delivery_verifier_install.py::test_drain_install_succeeds_when_the_tr_verifier_role_is_missing`.
It also reproduces in isolation. That test, `tests/deploy_script_harness.py`, and
`scripts/deploy/aws_eu_clickhouse_drain_install.sh` are unchanged. The harness
symlinks system executables into a temporary `PATH`; Apple's `shasum` exits 255
there with “perl version 5.34.1 can't run …/bin/shasum.” Run the full repository
gate on its supported Linux CI environment (or repair that independent local
harness compatibility issue) before treating the full suite as passed.

## PR #2129 review round 1

These changes are based on `b5df8e76e` and remain uncommitted. No push,
publication, deployment, or live Stripe mutation was performed.

| Fix | Implementation | Regression tests |
| --- | --- | --- |
| External refunds/disputes block existing holdings | `src/trusted_router/storage_resale.py:88` withdraws active listings and marks payment review atomically; `:102` matches original payment, amount and refund identity against durable fills. Reimports reconcile before the existing-position return. `src/trusted_router/services/resale.py:254` gathers bounded payment-scoped evidence; `:270` handles refund/dispute events, routed before the reserved-kind early return at `src/trusted_router/routes/internal/webhook.py:100`. | `tests/conformance/test_nyte_resale_ledger.py:131` covers external refunds/disputes, listing rejection, withdrawal and FIFO skipping; `:154` covers own refunds before worker completion and external excess. `tests/test_nyte_resale.py:404`, `:422`, and `:445` cover HTTP events, own refunds and reimport of existing refunded holdings. Full-refund acknowledgement remains covered at `:193`. |
| Disabled flag isolates the feature | `src/trusted_router/routes/resale.py:34`, `:62`, and `:74` return the inert generic response before ledger/email work for direct handling, OPTIONS and POST. `src/trusted_router/services/reserved_tokens.py:152` gates all resale allocation/refund work; the original notification and first-write timestamp path remains active. Refund/dispute interception is also gated at `src/trusted_router/routes/internal/webhook.py:100`. | `tests/test_nyte_resale.py:212` tests every public action and preflight with a ledger that raises, no email, and unchanged state. `:238` proves a paid webhook and retry succeed with a broken resale ledger and retain the old notification timestamp/dedup behavior. |
| Timestamped imports still allocate | `src/trusted_router/storage_resale.py:134` tracks allocation separately as pending/complete/historical. Importing after the notification claim does not complete allocation. The paid webhook allocates once and preserves the first completion time and holder index. | `tests/conformance/test_nyte_resale_ledger.py:174` reproduces claim → timestamped import → allocation → retry on every registered backend; `:194` separately proves historical purchases never consume current listings. The original import-before-claim contract remains. |
| Fee locked to approved terms | `src/trusted_router/config.py:1435` rejects non-500 fees with the required new-terms-version explanation. Checkout metadata validation also requires 500. `src/trusted_router/storage_resale.py:184` retains the applied `fee_bps` on each fill. Terms remain v4 because the approved fee/rules did not change. | `tests/test_nyte_resale.py:256` checks environment and constructor validation for -1, 0, 499, 501 and 10000, and accepts 500. `tests/conformance/test_nyte_resale_ledger.py:23` checks the applied fee on both FIFO fills. |

The new non-threaded contracts also run through the actual Postgres transaction
implementation on the offline SQL adapter in `tests/test_nyte_resale.py:266`.
They do not replace validation against a live server.

| Round 1 validation | Result |
| --- | --- |
| Requested seven pytest files, with the two conformance paths grouped first | **235 passed, 39 skipped**, 83.21% coverage across the three resale modules |
| Final resale unit rerun after preflight and offline Postgres assertions | **44 passed** |
| Standalone conformance files | **26 passed, 39 skipped**; skips are unconfigured Postgres, Spanner-PG and native Spanner backends |
| Site unittests | **94 passed** |
| `node --test tests/js/nyte_holder.test.cjs` | **3 passed** |
| Ruff / mypy | Passed; repository-wide ruff and mypy (551 source files), including all changed source |
| Explicit native Spanner gate | **Blocked**: 11 setup errors, connection refused at `127.0.0.1:9010`; Docker remains unavailable |
| Full repository attempt (`uv run pytest -q --ff -x --tb=short`) | **Blocked** by the same unchanged macOS `shasum` deployment-harness failure documented above. Failed-first stopped on that one failure; the remaining suite was not run and no full-suite pass is claimed. |

The initial interleaved pytest path order lost the nested `store` fixture for
`test_reserved_notifications.py`. Grouping the conformance files first resolved
collection without changing their fixtures or weakening tests. The successful
requested run used:

```sh
uv run pytest -q --tb=short \
  tests/conformance/test_nyte_resale_ledger.py \
  tests/conformance/test_reserved_notifications.py \
  tests/test_nyte_resale.py tests/test_reserved_tokens.py \
  tests/test_billing.py tests/test_stripe_processing_fees.py \
  tests/test_service_surfaces.py \
  --cov=trusted_router.storage_resale --cov=trusted_router.services.resale \
  --cov=trusted_router.routes.resale --cov-report=term --cov-fail-under=70
```

## PR #2129 review round 2

Based on `9498fc980`. All six changed files remain uncommitted; no push,
publication, deployment or live Stripe mutation was performed. The entity
additions reuse the existing transactional store protocol on every backend;
no DDL or terms-version change is needed.

| Fix | Implementation | Regression tests |
| --- | --- | --- |
| Adverse events before checkout, and refunded historical replays | `src/trusted_router/storage_resale.py:102` retains redacted evidence by PaymentIntent; `:145` applies it atomically during purchase creation before allocation. `src/trusted_router/services/resale.py:283` records events without requiring a position mapping; `:86` and `:257` verify current Stripe state on webhook creation/replay and import. | `tests/conformance/test_nyte_resale_ledger.py:210` exercises early refund/dispute, stale healthy evidence, listing rejection and no allocation; `:235` exercises refunded Stripe state on replay and historical import. `tests/test_nyte_resale.py:482` exercises real HTTP event → checkout ordering for charge refunds, refund creation and disputes. |
| Successful refunds that later fail/cancel | `src/trusted_router/storage_resale.py:307` moves validated fills to the durable ops queue; `:316` validates refund ID/payment/amount; `:333` and `:342` reject retry claims and stale commits. `src/trusted_router/services/resale.py:117` also catches terminal failures observed by the worker; `:283` handles failure webhooks and `:320` sends identifier-only ops alerts. `scripts/retry_nyte_resale_refunds.py:18` adds the read-only `--needs-ops` view. | `tests/conformance/test_nyte_resale_ledger.py:266` covers failed/canceled, unknown/mismatched IDs, duplicate events, queue integrity, stale writes and no duplicate issuance. `tests/test_nyte_resale.py:504` covers HTTP lifecycle events, legacy index fallback and email privacy; `:537` checks unknown IDs; `:556` checks worker-observed failure. |

These contracts are registered for every backend. Memory and Spanner-fake run
locally; `tests/test_nyte_resale.py:275` additionally runs the new contracts
through the actual Postgres implementation on the offline SQL adapter. The
unconfigured server backends remain explicitly skipped in the ordinary run;
this does not establish native server transaction behavior.

| Round 2 validation | Result |
| --- | --- |
| Requested seven pytest files, using the grouped command above | **258 passed, 57 skipped**; **86.79%** combined coverage across the three resale modules, above the 70% gate |
| Site unittests | **94 passed** |
| `node --test tests/js/nyte_holder.test.cjs` | **3 passed** |
| `uv run ruff check .` | Passed |
| `uv run mypy` and explicit refund CLI mypy | Passed: 551 source files plus the CLI |
| `git diff --check` | Passed |
| Explicit native Spanner emulator gate | **Blocked**: 17 setup errors, connection refused at `127.0.0.1:9010`; Docker unavailable. Not counted as a pass. |
| Full repository attempt (`uv run pytest -q --ff -x --tb=short`) | **Blocked** by the unchanged macOS `shasum` deployment-harness failure described above: 1 failed, 1,846 deselected. Failed-first stopped there; remaining tests were not run. No full-suite coverage/pass is claimed. |

Local commands used `UV_CACHE_DIR=/tmp/nyte-uv-cache` because the default cache
is outside the writable sandbox. The coverage run used
`COVERAGE_FILE=/tmp/nyte-r2-coverage`. The final requested-run output is
`/tmp/nyte-r2-verified.log`. An earlier run exposed a test-isolation assertion
against the entire shared refund queue; the regression now checks that existing
queue contents are preserved and exactly the tested fill enters the ops queue.


## PR #2129 review round 3

Based on `8a42f6771`; changes remain uncommitted, with no push, publication or
live Stripe mutation. The approved fee remains 500 bps. Public terms and new
checkout metadata are now **2026-10-27-v5**. Paid sessions under v3/v4 still
validate against their original accepted version.

The terms retain a visible email fallback even while self-service is disabled:
“To request a full refund within 30 days, use Manage my reservation or email
enterprise@trustedrouter.com with your position ID or Stripe payment reference.”

The fill transaction now has one automation rule: `needs_ops` is terminal.
Lifecycle failure events resolve the fill from metadata, verify original payment
and refund amount, and retain the event's refund ID even before the first worker
commit. Only an owned `in_flight` fill can commit a worker result. Transition
history records accepted transitions and stale completion writes; it contains
only time, action and state names. Existing fills acquire their seller position
ID from exact-key listing/position reads when next claimed. No DDL is needed.

The private Stripe context now encloses refund workers and full-refund
acknowledgement. Python DEBUG logging remains usable without recording provider
payloads; Stripe's direct console diagnostics refuse these operations before any
ledger or provider call. Created metadata contains only identifiers and kind,
never email, and no description is sent.

| Round 3 fix | Implementation | Regression tests |
| --- | --- | --- |
| Refund lifecycle race | `src/trusted_router/storage_resale.py:323` resolves metadata and validates payment/amount without requiring a saved refund ID; `:342` enters `in_flight`; `:355` conditionally completes or records a stale write. `src/trusted_router/services/resale.py:149` sends fill/position identifiers and a fill-derived idempotency key. | `tests/conformance/test_nyte_resale_ledger.py:313` covers failure → success and success → failure, failed/canceled statuses, identity validation, retained refund ID, stale-write history and no reissuance on every registered backend. `tests/test_nyte_resale.py:581` delivers the failure webhook inside Stripe create before its success response returns, then asserts one creation only. The offline Postgres adapter also runs all four order/status combinations. |
| Private refund provider calls | `src/trusted_router/services/resale.py:117` and `:334` apply the existing private provider context and console guard to the worker and full-refund acknowledgement. | `tests/test_nyte_resale.py:611` emits email-bearing Stripe/transport DEBUG payloads during list/create/retrieve and full-refund acknowledgement, proves they are absent from logs, and verifies context restoration. `:662` covers both direct-console settings at info/debug and proves no ledger/provider work starts. |
| Visible refund email route and terms version | `sites/token-exchange/nyte-v2/reserved-terms.html:13` contains the exact fallback sentence and mail link. `src/trusted_router/services/reserved_tokens.py:24` emits v5; `:138` continues accepting previously paid v3/v4 sessions. README and site/server assertions use v5. | `sites/token-exchange/test_resale.py:10` checks the rendered terms sentence and mail link. `tests/test_reserved_tokens.py:383` verifies v3/v4/v5 paid webhooks; existing checkout tests assert new-session v5 metadata. |


| Round 3 validation | Result |
| --- | --- |
| Requested seven pytest files, grouped conformance paths first | **275 passed, 69 skipped**, **86.56%** combined coverage across the three resale modules (70% gate passed) |
| Initial ledger/resale focused run | **103 passed, 63 skipped** |
| Site unittests | **94 passed** |
| `node --test tests/js/nyte_holder.test.cjs` | **3 passed** |
| Site build | Passed; 13 markets / 30 domains in `/tmp/nyte-r3-site` |
| Repository-wide ruff / mypy, plus CLI mypy | Passed: 551 source files plus CLI |
| `git diff --check` | Passed |
| Explicit native Spanner emulator gate | **Blocked**: 21 setup errors, connection refused at `127.0.0.1:9010`; emulator unavailable. Not counted as a pass. |
| Full repository attempt (`uv run pytest -q --ff -x --tb=short`) | **Blocked** by the same unchanged macOS `shasum` harness failure documented above: 1 failed, 1,846 deselected. Failed-first stopped there; no full-suite pass or full-suite coverage claimed. |

The ordinary run's 69 skips are unconfigured server-backend cases. The contracts
ran against memory and Spanner-fake, and the offline SQL adapter exercised the
actual Postgres implementation. These do not replace native server validation.
The final requested-run output is `/tmp/nyte-r3-requested.log`; coverage used
`COVERAGE_FILE=/tmp/nyte-r3-coverage` and all uv commands used the writable cache
`UV_CACHE_DIR=/tmp/nyte-uv-cache`.


## PR #2129 review round 4

Based on `369000d6b`; all changes remain uncommitted. No push, publication,
deployment or live Stripe mutation was performed. Terms remain v5 and the fee
remains 500 bps; this round changes reconciliation, not product rules.

### Shared mechanism

`storage_resale.py` uses `apply_refund` to verify the PaymentIntent and amount,
bind the refund ID to a typed obligation, and call the single `transition`
function. Both `nyte_fill` and `nyte_refund_request` use this mechanism. Success
is conditional on `in_flight`; `needs_ops` is terminal for automation; a late
completion records `stale_write` without unlinking ops work. A valid failure
wins regardless of the worker lease or prior successful completion.

Bindings take priority over later metadata edits. Without a binding, typed
`fill_id` or `position_id` metadata resolves the obligation using exact keys.
Legacy `nyte_fill_id` metadata and fill-only indexes are still accepted. Old
completed full refunds are recovered from the exact PaymentIntent mapping and
stored position refund ID. No production body search or table scan is needed.

A failure without an existing binding is stored by refund ID. Binding checks
its immutable payment/amount and any supplied obligation identity before
replaying it. Successful events cannot erase this evidence. This closes the
metadata-free ops race: failure → stale Stripe retrieve response → acknowledgement
ends in `needs_ops`, with the full-refund obligation retained in its ops queue.
No new DDL is required; the existing entity table and backend transaction locks
cover the evidence, binding, obligation, position and queue writes atomically.

### Completion-site audit

| Site reviewed | Final handling |
| --- | --- |
| Resale worker `Refund.create` response | Verified response enters `fill_finish` → `apply_refund` → `transition`; creation carries `kind=resale_fill`, fill/position IDs and the existing fill-derived idempotency key. |
| Resale worker `Refund.retrieve` and payment-scoped `Refund.list` reconciliation | The same path as create, including failures discovered after another worker's lease. A mismatched response cannot bind a refund ID. |
| Zero-cent fill completion and transport retry completion | Both use `transition`; zero-cent completion makes no Stripe call, and a nonzero success requires a refund ID. Neither path can overwrite `needs_ops`. |
| Full-refund CLI acknowledgement | `refund_acknowledge` validates and binds the retrieved refund, replays earlier failure evidence, claims the obligation only if eligible, and uses `apply_refund`. The old unconditional `refund_complete` command was removed. The CLI reports the actual status instead of always saying reconciled. |
| Refund lifecycle webhooks: created/updated/failed and charge.refund.updated | All enter `refund_lifecycle` → `apply_refund`; both kinds share identity checks, conditional success, terminal failure and identifier-only ops alerts. |
| Legacy already-completed full refunds | Existing durable completion is preserved during schema hydration; a later failure resolves the old saved refund ID and enters the same transition. Hydration does not infer a new payout from a Stripe response. |

The sweep found no other NYTE payout-completion writer. Purchase allocation,
listing sale counts and receipt-notification completion are separate durable
facts; they do not assert that a seller refund was paid. Credit/balance APIs
remain outside the NYTE path. Typed refund metadata is explicitly isolated from
credit handling even when resale is disabled.

### Regression coverage

- `tests/conformance/test_nyte_resale_ledger.py:354`: both obligation kinds,
  both failure/success orders, failed/canceled status, metadata and bound-ID
  matching, payment/amount mismatch rejection, stale-write history, ops-queue
  membership exactly once, and no reissuance. Registered for every backend.
- `tests/conformance/test_nyte_resale_ledger.py:428`: metadata-free failure before
  binding for both kinds; a later successful event cannot erase it.
- `tests/test_nyte_resale.py:688`: real HTTP lifecycle events during the Stripe
  retrieve inside acknowledgement, and after completed acknowledgement; all four
  event types, persistent full-refund ops work, no creation and email privacy.
- `tests/test_nyte_resale.py:727`: previously completed full refunds without the
  new index reopen through the original payment mapping.
- `tests/test_nyte_resale.py:745`: both typed refund kinds remain isolated from
  the ledger and credit handlers when resale is disabled.
- The offline Postgres SQL adapter runs the complete new kind/order/status/match
  matrix as well as unbound failures. This supplements memory and Spanner-fake
  tests but does not replace live-backend transaction validation.

Implementation references: `src/trusted_router/storage_resale.py:326`
(`transition`), `:357` (`bind`), `:377` (`apply_refund`), `:388`
(lifecycle dispatch), `:429` (full acknowledgement), `:451` (worker completion);
`src/trusted_router/services/resale.py:118` (worker), `:288` (webhook service),
`:340` (full-refund tool); `scripts/retry_nyte_resale_refunds.py:18` (ops views
and actual acknowledgement status).

### Round 4 validation

| Gate | Result |
| --- | --- |
| Requested seven pytest files, final code | **322 passed, 123 skipped**; **87.46%** combined coverage across the three resale modules (70% gate passed) |
| Initial ledger/resale focused run after the shared mechanism | **150 passed, 117 skipped** |
| Site unittests | **94 passed** |
| `node --test tests/js/nyte_holder.test.cjs` | **3 passed** |
| Site build | Passed; 13 markets / 30 domains in `/tmp/nyte-r4-site` |
| Repository-wide ruff / mypy and CLI mypy, final code | Passed; 551 source files plus CLI |
| `git diff --check` | Passed |
| Explicit native Spanner emulator gate | **Blocked**: 39 setup errors, connection refused at `127.0.0.1:9010`; emulator unavailable. Not counted as a pass. |
| Full repository attempt (`uv run pytest -q --ff -x --tb=short`) | **Blocked** by the unchanged macOS `shasum` deployment-harness failure documented above: 1 failed, 1,846 deselected. Failed-first stopped there; no full-suite pass or coverage claimed. |

The ordinary run's skips are unconfigured Postgres, Spanner-PG and native Spanner
cases. Memory, Spanner-fake and the offline Postgres adapter ran the contracts;
these do not prove native server behavior. Final output is in
`/tmp/nyte-r4-requested-final.log`; coverage used
`COVERAGE_FILE=/tmp/nyte-r4-coverage` and uv used the writable cache
`UV_CACHE_DIR=/tmp/nyte-uv-cache`.


## PR #2129 review round 5

Based on `4ee6a10e2`; all changes remain uncommitted. Nothing was pushed,
published, deployed or changed in live Stripe. Terms remain v5.

`TR_RESALE_ENABLED` controls holder access and new resale allocation, not servicing
refund obligations already incurred. All four lifecycle events (`refund.created`,
`refund.updated`, `refund.failed`, `charge.refund.updated`) reach the shared
refund processor regardless of the flag. Both resale fills and full-refund
obligations retain the same payment/amount validation, terminal `needs_ops`, ops
queue, identifier-only alert and stale-success protection.

When the flag is off, the processor passes `existing_only` to the transactional
ledger. Known refund-ID bindings, legacy bindings and valid metadata identifying
an existing obligation remain eligible. An unknown refund returns without
retaining failure evidence or reconciling a position; it keeps its former
ignored/generic-handler behavior. Turning sales back on does not reopen failed
obligations or issue another refund.

Paid checkout never enters this lifecycle branch. The round-2 regression with a
resale ledger that raises is retained unchanged, proving a disabled paid checkout
and retry still record the original #2125 notification and first-write purchase
time without any resale access. Holder actions, link email, listing and new
allocation remain disabled. Charge/dispute position reconciliation remains gated.

The round-4 test that forbade even a lookup for every disabled typed refund was
updated to the new requirement: unknown typed refunds may be looked up, but
must leave ledger contents unchanged, send no ops alert and never reach credits.
It covers success, failure and cancellation. The stricter no-ledger rule remains
unchanged for disabled paid checkout and every holder route.

| Round 5 change | Implementation / regression |
| --- | --- |
| Lifecycle servicing independent of sales flag | `src/trusted_router/routes/internal/webhook.py:100` routes all four refund lifecycle types independently; charge refunds/disputes remain gated. `src/trusted_router/services/resale.py:303` selects existing-only reconciliation when disabled and returns before any new-position/evidence work for an unknown refund. |
| Unknown refunds remain inert in disabled mode | `src/trusted_router/storage_resale.py:412` suppresses unmatched failure-evidence writes in existing-only mode. `tests/test_nyte_resale.py:748` asserts no ledger change, alert or credit call for both typed kinds and successful/failed/canceled unknown refunds. |
| Flag toggles cannot lose failure or trigger reissuance | `tests/test_nyte_resale.py:772` issues/acknowledges with the flag on, switches off, delivers every supported lifecycle type (including cancellation), verifies terminal state, exactly one ops entry and an alert, then enables again and proves no second issuance. It resolves bound IDs without metadata for both kinds. |
| Existing metadata matches before refund-ID commit | `tests/conformance/test_nyte_resale_ledger.py:453` covers both kinds with sales disabled before recording the refund ID, then reenables and proves no reissue. It is registered for every backend and also runs through the offline Postgres adapter. |
| Original checkout and holder isolation preserved | `tests/test_nyte_resale.py:247` (unchanged) proves disabled paid checkout/retry succeed with a resale ledger that raises. `:221` (unchanged) checks all holder actions/preflight with no ledger call or email. |

| Round 5 validation | Result |
| --- | --- |
| Requested seven pytest files | **340 passed, 129 skipped**; **87.51%** combined coverage across the three resale modules (70% gate passed) |
| Focused ledger/resale run | **168 passed, 123 skipped** |
| Site unittests | **94 passed** |
| `node --test tests/js/nyte_holder.test.cjs` | **3 passed** |
| Site build | Passed; 13 markets / 30 domains in `/tmp/nyte-r5-site` |
| Repository-wide ruff / mypy, plus CLI mypy | Passed; 551 source files plus CLI |
| `git diff --check` | Passed |
| Explicit native Spanner emulator gate | **Blocked**: 41 setup errors, connection refused at `127.0.0.1:9010`; emulator unavailable. Not counted as a pass. |
| Full repository attempt (`uv run pytest -q --ff -x --tb=short`) | **Blocked** by the unchanged macOS `shasum` deployment-harness failure documented above: 1 failed, 1,846 deselected. Failed-first stopped there; no full-suite pass or coverage claimed. |

The ordinary-run skips are unconfigured Postgres, Spanner-PG and native Spanner
cases. Memory, Spanner-fake and the offline Postgres adapter ran the new contracts;
these do not replace native-server validation. Requested-run output is in
`/tmp/nyte-r5-requested.log`; coverage used `COVERAGE_FILE=/tmp/nyte-r5-coverage`
and uv used `UV_CACHE_DIR=/tmp/nyte-uv-cache`.


## PR #2129 review round 6

Based on `44df7f812`; all changes remain uncommitted. No push, publication,
deployment or live Stripe mutation was performed. Terms remain v5.

### One scope, independent of the flag

Removed `existing_only` entirely. Refund lifecycle processing, failure evidence,
acknowledgement and card reconciliation have no `TR_RESALE_ENABLED` branch.
The existing `nyte_payment` mapping is the ownership boundary: a PaymentIntent
must map to a NYTE reserved-tokens position before an event may write NYTE state.
The service checks it before processing; the ledger checks it again inside the
transaction before retaining evidence or changing an obligation. Metadata alone
cannot authorize writes for an unmapped payment.

For a mapped payment, a metadata-free Dashboard failure or cancellation is
retained by refund ID even before acknowledgement binds that ID. A stale
successful retrieve cannot erase the evidence: binding applies it first, leaving
`needs_ops`, exactly one ops-queue entry, an identifier-only alert and no automatic
reissue. Bound refunds and metadata-identified obligations use the same path in
both flag modes. The earlier round-5 existing-only behavior is superseded.

The flag still disables holder actions, link emails, listing and new allocation.
Paid checkout with the flag off follows #2125 without consulting the resale
ledger; its broken-ledger regression is unchanged. Refund, charge and dispute
reconciliation for mapped payments continues in both modes.

### Regression scope

The lifecycle contract fixture runs each interleaving with sales on and off on
every registered backend. It covers both obligation kinds, both event orders,
failed/canceled states, bound-ID and metadata resolution, and the metadata-free
failure-before-acknowledgement case using the real service and mocked stale Stripe
responses. Tests assert terminal state, queue membership exactly once, ops alert,
stale-write history and no reissue. The offline Postgres adapter executes the
same complete matrix in both modes.

HTTP acknowledgement races also run in both modes for failure and cancellation
through all four lifecycle event types. Unmapped-payment tests cover both modes,
metadata that claims NYTE ownership, no ledger mutation/alert/credit call, and no
failure evidence that could poison a later binding.

The previous pre-position adverse-event tests were updated to this round's
explicit mapping boundary: events before the mapping exists are inert. Current
Stripe-state checks on creation, replay and import remain tested, so already
refunded or disputed purchases are still blocked before being marked held.
These tests replace the now-conflicting expectation that unrelated/unmapped
payments may create NYTE evidence; they do not relax mapped-payment safety.

| Round 6 change | Implementation / regression |
| --- | --- |
| Flag-independent lifecycle and reconciliation | `src/trusted_router/routes/internal/webhook.py:100` dispatches refund/charge/dispute events without the flag; `src/trusted_router/services/resale.py:288` uses only the payment mapping to establish NYTE ownership. |
| Transactional ownership and unbound evidence | `src/trusted_router/storage_resale.py:135` and `:386` require the mapping before reconciliation or refund lifecycle writes; unmatched failure/cancellation on mapped payments is retained without `existing_only`. |
| Every lifecycle interleaving in both modes | `tests/conformance/test_nyte_resale_ledger.py:14` supplies the on/off fixture; `:287`, `:340`, `:388` and `:515` cover late failures, both commit orders and metadata/bound-ID matching for both kinds. |
| Metadata-free failure/cancellation before binding | `tests/conformance/test_nyte_resale_ledger.py:468` exercises stale Stripe success, terminal ops state, one queue entry, alert and no reissue; `tests/test_nyte_resale.py:700` covers acknowledgement races through all four HTTP lifecycle types in both modes. |
| Unmapped payments remain inert | `tests/conformance/test_nyte_resale_ledger.py:227` and `:558` check event ownership and absence of retained evidence; `tests/test_nyte_resale.py:759` checks unchanged ledger contents, no alert and no credit for successful/failed/canceled events in both modes. |
| Backend parity | `tests/test_nyte_resale.py:275` also executes the full on/off lifecycle matrix against the offline Postgres adapter. All contract cases remain registered for native backends. |
| Disabled checkout and flag toggles | Unchanged `tests/test_nyte_resale.py:247` proves paid checkout/retry never touch the broken resale ledger when disabled; `:783` checks issued obligations across an on/off/on toggle. |

| Round 6 validation | Result |
| --- | --- |
| Requested seven pytest files | **448 passed, 237 skipped**, in 212.72 seconds; **87.68%** combined coverage across the three resale modules (70% gate passed) |
| Initial focused ledger/resale run | **276 passed, 231 skipped**; final requested run also includes the added duplicate-alert assertion |
| Site unittests | **94 passed** |
| `node --test tests/js/nyte_holder.test.cjs` | **3 passed** |
| Site build | Passed; 13 markets / 30 domains in `/tmp/nyte-r6-site` |
| Repository-wide ruff / mypy, plus refund CLI mypy | Passed; 551 source files plus CLI |
| `git diff --check` | Passed |
| Explicit native Spanner emulator gate | **Blocked**: 77 setup errors, connection refused at `127.0.0.1:9010`; emulator unavailable. Not counted as a pass. |
| Full repository attempt (`uv run pytest -q --ff -x --tb=short`) | **Blocked** by the unchanged macOS `shasum` deployment-harness failure in `tests/test_aws_postgres_delivery_verifier_install.py:432`: 1 failed, 1,846 deselected. Failed-first stopped there; no full-suite pass or coverage claimed. |

The requested run includes `tests/test_nyte_resale.py`,
`tests/conformance/test_nyte_resale_ledger.py`, `tests/test_reserved_tokens.py`,
`tests/conformance/test_reserved_notifications.py`, `tests/test_billing.py`,
`tests/test_stripe_processing_fees.py` and `tests/test_service_surfaces.py`.
Ordinary skips are unconfigured Postgres, Spanner-PG and native Spanner cases.
Memory, Spanner-fake and the offline Postgres adapter passed the new contracts;
they do not substitute for native-server validation.

Logs: `/tmp/nyte-r6-requested.log`, `/tmp/nyte-r6-full.log`,
`/tmp/nyte-r6-emulator.log`, `/tmp/nyte-r6-site-tests.log`,
`/tmp/nyte-r6-js.log` and `/tmp/nyte-r6-site-build.log`. Coverage used
`COVERAGE_FILE=/tmp/nyte-r6-coverage`; uv used `UV_CACHE_DIR=/tmp/nyte-uv-cache`.
