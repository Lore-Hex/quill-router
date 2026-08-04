# Adyen Checkout Rollout

Adyen is integrated as a dark, one-time prepaid funding rail. It does not
introduce a second balance system. An embedded Adyen Drop-in creates a payment,
and only an HMAC-verified successful `AUTHORISATION` webhook can add credits to
the existing typed ledger.

## Safety boundary

- `TR_ADYEN_ENABLED=false` is the deployment default. The Adyen option is not
  rendered and authenticated session creation rejects requests while dark.
- Webhook verification is independent of checkout enablement. Disabling new
  sessions does not prevent a late successful payment from being credited.
- The browser receives only Adyen's client key and opaque session data. The API
  key and webhook HMAC key stay in Secret Manager.
- Money stays in integer USD cents and microdollars. The merchant reference
  binds workspace, credit principal, charge total, and a nonce under a separate
  TrustedRouter HMAC key. Adyen then signs the complete reference in its
  webhook. A valid Adyen event for a reference issued by another integration
  cannot mint TrustedRouter credits.
- Duplicate successful events are idempotent by checkout reference, even when
  Adyen sends a different PSP reference on an authorization retry.
- Refunds and chargebacks are logged for manual review. V1 never silently
  debits a customer's credits.
- Adyen does not save cards for TrustedRouter auto refill in V1. Stripe remains
  the saved-card and auto-refill provider.

## Current test resources

| Resource | Value |
|---|---|
| Environment | Adyen test |
| Merchant account | `TrustedRouterUS` |
| Allowed browser origin | `https://trustedrouter.com` |
| Checkout API | v72 Sessions |
| Adyen Web | 6.41.0, pinned with SRI |
| Webhook URL | `https://trustedrouter.com/v1/internal/adyen/webhook` (created inactive) |

The merchant must report `Active`, not `PreActive` or `Pending`, before Checkout
can return payment methods or sessions.

## Remaining activation sequence

1. Wait for `TrustedRouterUS` to become `Active` in the Adyen test account.
2. Create an active Standard webhook for the URL above. Generate its test HMAC
   key and save it as Secret Manager secret `trustedrouter-adyen-test-hmac-key`.
   Generate a separate 32-byte random reference key and save it as
   `trustedrouter-adyen-test-reference-key`. Keep this stable while any Adyen
   sessions can still settle.
3. Test the webhook from Adyen Customer Area. Require HTTP 200 with
   `[accepted]`, then repeat the same event and verify no second credit.
4. Run the non-charging readiness check:

   ```bash
   ADYEN_API_KEY=... \
   ADYEN_HMAC_KEY=... \
   ADYEN_REFERENCE_KEY=... \
   ADYEN_MERCHANT_ACCOUNT=TrustedRouterUS \
   uv run python scripts/check_adyen_readiness.py
   ```

5. Configure the signed commercial processing rates in
   `TR_ADYEN_CARD_FEE_BASIS_POINTS` and `TR_ADYEN_CARD_FEE_FIXED_CENTS`. Do not
   launch with guessed rates. The checkout displays credits and the processing
   fee separately.
6. In test, exercise Adyen's successful card, refused card, 3DS challenge,
   abandoned checkout, delayed webhook, duplicate webhook, and malformed HMAC
   scenarios. Confirm only one successful authorization adds the requested
   principal.
7. Change the rollout default to `TR_ADYEN_ENABLED=true` for one internal
   workspace canary. Verify balance, acquisition event, Sentry, and Adyen event
   logs before exposing it generally.
8. Repeat webhook and credential setup separately in Adyen live. Test and live
   HMAC keys are intentionally different. Configure
   `TR_ADYEN_LIVE_ENDPOINT_PREFIX`, change the environment to `live`, and use
   live-named secrets before a small real-payment canary.

## Rollback

Set `TR_ADYEN_ENABLED=false` and deploy. This immediately removes Adyen from
checkout and rejects new sessions while preserving webhook processing for
payments already in flight.

## Automated coverage

`tests/test_adyen_billing.py` covers the official Adyen HMAC vector, exact
integer fee/charge construction, no balance mutation during session creation,
merchant inactivity, duplicate authorization, forged batch rejection,
merchant/currency/amount/environment mismatches, failed payments, adverse
events, CDN SRI pins, and the dark console gate.
