# NYTE resale, step 1

Local implementation on `nyte-resale`; keep all changes uncommitted. Nothing in
this document authorizes a release or publication. Public terms: **2026-10-27-v4**.

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
| `nyte_holder` / domain-separated normalized email hash | Head of the holder's position list; no plaintext email |
| `nyte_listing` / hash of holder + request ID | Position session, lots listed/sold, listed_at, active/sold/withdrawn status, FIFO previous/next |
| `nyte_fill` / hash of buyer session + listing | Buyer session/event, listing, lots, resale price per lot, fee bps/total cents, capped refund total cents, seller PaymentIntent, Stripe refund ID, pending/retry/refunded status, first attempt, lease owner/expiry, refund-queue previous/next |
| `nyte_queue` / `grade:{grade}`, `refunds`, `full_refunds` | Linked queue head/tail; completed entries are unlinked |
| `nyte_event` / Stripe event ID | Buyer session; session identity also prevents duplicates across different event IDs |
| `nyte_action` / hash of holder + request ID | Original command and result; rejects reusing an ID for a different action |
| `nyte_access` / hash of capability | Holder hash, expiry, consumed marker or session purpose; never raw capability |
| `nyte_refund_request` / original session | Accepted request time, position, ops queue links |

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
before any credit handling. With `TR_RESALE_ENABLED=false`, the webhook follows
the original #2125 notification path and never calls the resale ledger. Every
holder API action (including link requests and preflight) returns the inert
generic response without email or ledger access. With the flag enabled, positions
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
that original PaymentIntent for `metadata.nyte_fill_id`. Known refunds are
retrieved until Stripe reports `succeeded`; pending/failed refunds remain visible
for retry/ops. A timeout after Stripe acceptance is reconciled to the same refund.

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
receipt lookup; that mode bypasses Python logging filters, so lookup refuses it.
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
   PaymentIntents and Refunds, and create refunds. No new Stripe Connect or
   webhook endpoint is needed. Existing paid-checkout events remain required. Ensure this endpoint also
   receives `charge.refunded` and `charge.dispute.*` events for prompt external
   payment reconciliation; refund lifecycle events are also handled.
4. Verify SES sends holder links and ops alerts from the control service.
   `TR_PARTNER_INQUIRY_EMAIL` is the ops recipient (enterprise fallback).
5. Arrange periodic execution of `uv run python -m scripts.retry_nyte_resale_refunds`
   with the **control** deployment configuration, and run it after any refund
   alert. The durable queue survives process exits; scheduled infrastructure is
   not installed by this local change. Continue with the printed `--cursor`.
6. Review `--full-refunds`. Issue each full refund in Stripe against its original
   PaymentIntent, then run `--ack-full-refund-session cs_... --refund-id re_...`.
   The script verifies full amount, original payment and successful status before
   closing the request. Do not grant balance credits. Review old ambiguous or
   failed resale refunds against `nyte_fill_id` before any manual repair.

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
