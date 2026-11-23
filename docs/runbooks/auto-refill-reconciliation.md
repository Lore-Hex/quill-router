# Auto-refill requires reconciliation

A workspace with a pending Stripe PaymentIntent must not be charged a second
time to recover from a missing response or webhook. `reconcile_required` is a
stop condition, not permission to generate a fresh key. Other workspaces can
continue refilling.

1. Start from the authenticated billing workspace. Use a point read of
   `tr_entities` at `(refill_claim, workspace_id)`. The claim contains the
   Stripe idempotency key, the first dispatch time, and whether the create
   response was received. A legacy pending intent may not have a claim.
2. Inspect Stripe's request log for that key and its PaymentIntent. Match
   customer, workspace metadata, currency, amount, and (for new intents)
   `refill_attempt_key`. Distinguish successful, failed, processing, and
   unknown outcomes. An empty search is not proof of failure.
3. For a terminal intent, inspect the corresponding signed webhook delivery
   and ledger event. Redeliver the **original event** through Stripe's normal
   procedure if necessary. The existing ledger event ID deduplicates credit;
   the matching attempt key updates the pending claim. Never create a
   replacement payment as a diagnostic.
4. Escalate ambiguous, mismatched, or pre-claim records to billing operations;
   do not delete claims, edit balances, or replay expired keys. The automated
   path can retry an ambiguous create only with the same parameters and key
   within 23 hours of the first dispatch. See [Stripe's idempotency contract](https://docs.stripe.com/api/idempotent_requests).
