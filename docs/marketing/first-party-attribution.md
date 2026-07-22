# First-Party Acquisition Attribution

TrustedRouter measures paid and organic acquisition without sending inference
content to an advertising platform.

## Collection Boundary

The public website captures a signed, HttpOnly, SameSite=Lax cookie for 90
days. Production sends it only over HTTPS. The cookie contains:

- an anonymous random identifier
- first and last source, medium, campaign, term, and creative
- first and last landing path and external referring host
- Google `gclid`, `gbraid`, and `wbraid`, when supplied
- X `twclid`, when supplied
- capture timestamps

The cookie and durable attribution record never contain prompts, outputs, raw
API keys, BYOK keys, email addresses, payment credentials, request bodies, IP
addresses, or full referring URLs. Click identifiers are retained only in the
private Spanner record for future consented offline conversion uploads. Logs
contain booleans indicating which click identifier was present, never its raw
value.

Requests carrying `Sec-GPC: 1` or `DNT: 1` do not create or use attribution.
Known crawler user agents do not receive attribution cookies.

## Durable Record

At first account creation, the campaign context is written once under the new
workspace in Spanner's generic entity store. The record keeps first touch
immutable and updates last touch in the browser before signup. Repeated OAuth
callbacks and duplicate signups cannot overwrite the original acquisition
record.

## Funnel Events

Structured metadata-only events are shipped through the existing Axiom logger:

1. `acquisition.sign_in_opened`
2. `acquisition.signup_completed`
3. `acquisition.api_key_created`
4. `acquisition.first_successful_api_call`
5. `acquisition.credit_purchase_completed`
6. `acquisition.retained_api_usage_7d`

Workspace and anonymous identifiers are SHA-256 fingerprints in logs. Purchase
amounts remain integer microdollars. Stripe, PayPal, and stablecoin events are
recorded only after the credit ledger's existing idempotency check succeeds.
The first API call is recorded only after settlement commits. The seven-day
event is recorded on the first successful settled call at least seven days
after signup.

Attribution writes are failure-isolated. They cannot fail signup, inference,
settlement, payment acknowledgement, or streaming.

## Campaign Conventions

Every paid destination must set:

```text
utm_source=<google|x>
utm_medium=<paid_search|paid_social>
utm_campaign=<stable_campaign_name>
utm_content=<creative_name>
```

Google and X click identifiers can be appended by their respective auto-tagging
features. Creative-specific `utm_content` values are required so Axiom can
compare privacy, migration, and reliability messages within one campaign.

## Initial Optimization Policy

Use `signup_completed` as the first primary conversion while volume is low.
Report activated CAC separately using `first_successful_api_call`. Move bidding
optimization toward activated use or credited purchases only after each event
has enough weekly volume to avoid unstable learning.
