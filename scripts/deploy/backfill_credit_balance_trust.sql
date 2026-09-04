-- Run only after trust-unaware revisions have drained. Rerunnable.
UPDATE tr_credit_balance
SET trust_tier = COALESCE(trust_tier, 0),
    trust_computed_at = trust_computed_at,
    trust_latched_at = trust_latched_at,
    trust_override_tier = trust_override_tier,
    billing_pause_causes = COALESCE(billing_pause_causes, ARRAY<STRING>[]),
    pause_epoch = COALESCE(pause_epoch, 0),
    trust_reconciled_through = trust_reconciled_through
WHERE trust_tier IS NULL
   OR billing_pause_causes IS NULL
   OR pause_epoch IS NULL;
