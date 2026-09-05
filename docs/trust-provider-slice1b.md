# PayPal and Adyen trust facts (PR 1b)

The existing `/v1/internal/paypal/webhook` and `/v1/internal/adyen/webhook`
verify deliveries before calling the same transactional trust writers as Stripe.
No endpoint, setting, table or column is added by this provider slice. Defaults
remain `TR_TRUST_QUALIFYING_PROVIDERS=stripe,x402` and
`TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false`.

Canonical refund facts use the existing refund graph. PayPal reversals and
Adyen cancellations, capture failures/reversals, refund reversals and chargebacks
use the existing dispute graph to claim the full credited principal. Fraud and
chargeback notifications are pending, so they latch without claiming principal.
A chargeback win releases only its claim; another active claim still wins.
Adyen `REFUND_FAILED` normalizes to `reversed`, allowing an accepted refund's
claim to be restored through the existing succeeded-to-reversed edge.

References are object-namespaced (`refund:ID`, `dispute:ID`, `chargeback:ID`,
`reversal:ID`, etc.). This avoids collisions in the inherited provider/reference
inbox while fact uniqueness remains `(provider, adverse_ref, kind)`. The PayPal
capture or Adyen original authorisation remains `original_payment_ref`. An Adyen
notification lacking that reference is durable unmatched work under an
`unresolved:` reference; it cannot debit or produce a completion marker.

PayPal/Adyen inbox rows additionally carry an observation hash in their inbox
key. Pending and completed observations are retained separately until the
payment arrives; their payloads retain the canonical adverse reference. The
Spanner and memory payment paths drain these through their existing writers.
Postgres drains provider inbox observations inside the credit transaction;
the provider historical adapter also drains without issuing credits.

| Provider | Source | Version | Delay |
| --- | --- | --- | --- |
| PayPal | `paypal-transaction-search` | `paypal-trust-v1` | 10,800 seconds |
| Adyen | `adyen-payment-accounting-report` | `adyen-trust-v1` | 0 seconds |

`provider_marker_qualifies` is the pure PR-2 predicate: it checks provider,
account, environment, exact source/version, completion, counts, delay and range.
Pass `payment_occurred_at` to exclude payments older than enumerable history.
It does not enable either provider or arm leases. With the default 900-second
cadence, PayPal requires a maximum reconciliation age of at least 12,600 seconds;
settings reject the literal 3,600 default when PayPal qualifies.

Historical and rolling-deploy passes use `scripts/reconcile_provider_trust.py`
with `--mode backfill --provider paypal|adyen --account-id ... --history-start ...
--drained-at ... --apply`. The closed interval must cover the last old revision's
drain time and end outside the consistency delay. Re-run after each old handler
revision drains. Recurring passes use `--mode recurring` with the same provider
and account. Configure the existing scheduler to invoke that mode when preparing
qualification; this PR does not deploy or schedule either provider.

PayPal uses existing checkout OAuth credentials and sandbox/live URL selection.
Transaction Search is paginated in at most 31-day windows and merchant identity
is checked against `transaction_info.paypal_account_id`. Capture/refund retrieval
supplies canonical attribution and principal. A supplementary paginated dispute
list includes disputes without a balance-affecting transaction. Unavailable
objects, unrecognized adjustments, conflicting pages and failed enumeration
cannot close a marker. History starts at the later of first capture and the
calendar three-year retention boundary. An API that cannot enumerate an older
requested dispute range must fail the pass; a canonical archive is required
before such history can qualify.

Adyen takes `--report-manifest FILE`, whose JSON contains `account_id`,
`environment` (`live`/`test`), `covered_from`, `covered_through`, and `files`
(relative Payment Accounting CSV paths). Exports must be complete and contiguous
for that declared coverage. Rows validate the merchant, signed checkout
reference, currency and charged amount. `Psp Reference` identifies the original
authorisation; `Modification Psp Reference` identifies the adverse object.
Every recurring pass needs refreshed reports through its closed boundary and
must retain the authorisations and outstanding modifications outside the tail.

Both sources use 1d's ID-to-semantic-hash proof, overlapping tail and mandatory
outstanding-ID re-fetch. A failed re-fetch holds the watermark. These provider
sources do not supply a guaranteed final mutation deadline for every object:
unknown horizons remain outstanding until terminal, with an age alert after
30 days. The provider adapter supports terminal-by-horizon only when the
re-fetch supplies a proven final deadline; it does not invent Stripe's horizon
for PayPal or Adyen. Provider-specific automatic horizon expiry therefore remains
unconfigured pending a canonical deadline source.

Validation is offline against recorded fixtures and transactional backend
fakes. No live PayPal API, Adyen export, deploy, socket, or production database
was exercised. The working tree also contains the exact prerequisite changes
from locally merged slice 1d (`a83f6f10`); those are not new provider schema.
