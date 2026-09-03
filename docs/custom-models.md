# Custom models

Custom models combine a TrustedRouter catalog model with a hidden prompt and a
stable creator-owned model ID.

Public guide: <https://trustedrouter.com/docs/custom-models>

## Model IDs

Identity-verified creators claim one globally unique, permanent username. The
model ID is:

```text
tr-custom-model/{username}-{slug}
```

User-hosted endpoints use a separate namespace:

```text
tr-user-model/{username}-{slug}
```

## Markup

`markup_basis_points` accepts an integer from 0 through 30000. One hundred
basis points is 1%. The markup applies to routed model token charges. The model
owner receives 70% of collected markup in the earnings wallet and
TrustedRouter retains 30%. Billing and payouts use integer microdollars and an
authorization-scoped idempotency event.

## Earnings and cash-outs

Custom-model and registered-app markup enter the same creator earnings wallet.
Creators may transfer any available amount into a TrustedRouter workspace. At
$100 or more, an identity-verified creator with a verified email may request a
USD cash-out. A secure hosted payout flow collects bank and tax details;
TrustedRouter stores only opaque payout identifiers, payout status, and the
exact integer-microdollar ledger movement.

Cash-out requests require an idempotency key. TrustedRouter reserves earnings
before contacting the payout processor, reconciles ambiguous retries by
external ID, and keeps restartable failure states reserved. A final processor
cancellation releases the reservation exactly once.

## Create

```bash
curl https://trustedrouter.com/v1/custom-models \
  -H "Authorization: Bearer $TRUSTEDROUTER_MANAGEMENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Contract reviewer",
    "slug": "contract-reviewer",
    "base_model_id": "anthropic/claude-sonnet-4.6",
    "hidden_prompt": "Review the contract and cite each material clause.",
    "markup_basis_points": 1500,
    "enabled": true
  }'
```
