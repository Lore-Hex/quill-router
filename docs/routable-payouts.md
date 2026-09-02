# Routable creator payouts

Routable payouts are a control-plane feature for identity-verified creators.
They are disabled by default and never run in the attested inference path.
The first release pays US bank accounts by standard ACH. Add international
payment methods and delivery types only after separate sandbox canaries.

## Activation checklist

1. Create the Lore Hex Corp Routable production workspace and funding account.
2. Create an API token and identify the company, acting team member, and
   withdrawal account IDs.
3. Configure one Routable webhook at
   `https://trustedrouter.com/v1/internal/routable/webhook`.
4. Put the five values below in the private deployment key file and run the
   reviewed secret sync. Do not commit their values.
5. Run the sandbox onboarding, $100 cash-out, timeout retry, failed-payment,
   webhook replay, and completed-payment canaries.
6. Change `TR_ROUTABLE_ENABLED` to `true` in the production rollout only after
   all canaries pass.

```text
ROUTABLE_API_TOKEN=
ROUTABLE_WEBHOOK_SECRET=
ROUTABLE_COMPANY_ID=
ROUTABLE_TEAM_MEMBER_ID=
ROUTABLE_WITHDRAW_FROM_ACCOUNT_ID=
```

## Safety properties

- Full Veriff identity verification and a verified email are required.
- The minimum USD cash-out is $100 and amounts use whole cents.
- Earnings are reserved transactionally before Routable is called.
- Client retries require an idempotency key. Routable receives a separate
  stable idempotency key and opaque external ID.
- Each cash-out freezes its Pacific send date when the reservation is created,
  so retries keep the same request and an accepted cash-out enters transfer.
- Ambiguous transport failures stay reserved until reconciliation; they are
  never blindly released or duplicated. The owner can safely retry by payout
  ID with the stored provider idempotency key after closing the browser.
- Failed and issue states remain reserved because Routable can restart them.
  Only a final cancellation releases the reservation, exactly once.
- Bank and tax details remain in Routable. TrustedRouter stores only opaque
  identifiers, status, and integer-microdollar ledger entries.
- Disabling new payouts does not disable signed webhook processing for
  already-created payables.
