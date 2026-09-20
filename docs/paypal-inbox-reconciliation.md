# Refunded, uncredited PayPal captures

A capture held by PayPal can be fully refunded before TrustedRouter receives a
completed capture. No local credits were issued, so there is no payment fact for
the refund/dispute inbox to reconcile against. This is different from a missing
credit for a completed purchase.

The trust-tier worker now classifies this case using bounded canonical PayPal
GETs before its unchanged stale-inbox alert pass. It requires:

- The capture ID matches, its canonical status is `REFUNDED`, its USD amount
  matches the checkout reference, and that reference identifies a local workspace.
- A successful full-refund observation is already durably in the inbox.
- A canonical refund GET confirms that refund's identity, capture, currency,
  successful status, and entire charged amount.
- In one local transaction, no payment fact or canonical credit marker exists,
  and every inbox payload still matches the observations verified.

The transaction writes `trust_inbox_resolution` audit receipts only. It does not
create a payment fact or credit marker, change a balance, issue a refund, or
delete an adverse observation. Receipts include exact payload hashes and the
verification timestamp. A payment-indexed pointer retains the original refund
evidence for verifying later lifecycle observations with fresh PayPal GETs.

The stale-inbox query excludes only observations with these committed receipts.
All original inbox rows remain visible to the existing credit transaction. If a
delayed completed-capture callback arrives, it atomically credits and recovers
the refunded principal using those rows. Duplicate callbacks cannot credit twice.
No changes to that money-moving path are required.

Partial refunds, disputes without a full refund, provider failures, missing
attribution, or conflicting evidence stay unresolved and alertable. Provider
response bodies and credentials are never logged. At most three captures are
checked per run with at most 100 observations per capture. A rotating selection
prevents unknown captures from starving newer work. Existing alert thresholds
and the three-hour PayPal consistency window are unchanged.

Deployment uses the existing trust-tier job, service account and secret IDs.
The job needs the existing PayPal client ID, client secret, and webhook ID
bindings. Startup validation requires all three together, even though this
worker uses only OAuth and canonical GETs. No IAM grants or new resources are
part of this change. After rollout, verify `trust.inbox_reconciled` job logs,
unchanged balances, retained original events, committed receipts, and a subsequent
scheduled tier pass without stale alerts for the reconciled observations.

Tests cover all three storage implementations, delayed/duplicate/concurrent
completion, both transaction orderings, receipt-write rollback, later lifecycle
updates, partial refunds, identity/amount/currency mismatches, provider timeout,
fairness, and worker execution before alerting. The worker regression fails
against the prior worker body and passes with reconciliation enabled.
