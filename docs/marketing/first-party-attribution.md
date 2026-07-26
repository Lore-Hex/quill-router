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
addresses, or full referring URLs. Click identifiers are retained only in
private Spanner records and the metadata-only Google conversion rows described
below. Logs contain booleans indicating which click identifier was present,
never its raw value. Public landing events also contain a server-keyed HMAC
fingerprint of the click identifier. This lets reports deduplicate the same ad
click across cookie churn without exposing an identifier that an analytics
operator can reuse outside TrustedRouter.

Requests carrying `Sec-GPC: 1` or `DNT: 1` do not create or use attribution.
Known crawler, link-preview, prefetch, and prerender requests do not receive
attribution cookies.

## Durable Record

At first account creation, the campaign context is written once under the new
workspace in Spanner's generic entity store. The record keeps first touch
immutable and updates last touch in the browser before signup. Repeated OAuth
callbacks and duplicate signups cannot overwrite the original acquisition
record.

## Funnel Events

Structured metadata-only events are shipped through the existing Axiom logger:

1. `acquisition.landing_engaged`
2. `acquisition.sign_in_opened`
3. `acquisition.signup_completed`
4. `acquisition.api_key_created`
5. `acquisition.first_successful_api_call`
6. `acquisition.credit_purchase_completed`
7. `acquisition.retained_api_usage_7d`

`public.page_view` is a server-request metric and is deliberately labeled
`measurement_tier=server_request`. Paid-landing reports should use
`acquisition.landing_engaged`, which is emitted only after JavaScript has seen
the document remain visible for 1.5 seconds. Compare unique click fingerprints,
not raw server pageviews, with ad-platform clicks.

Workspace and anonymous identifiers are SHA-256 fingerprints in logs. Purchase
amounts remain integer microdollars. Stripe, PayPal, and stablecoin events are
recorded only after the credit ledger's existing idempotency check succeeds.
The first API call is recorded only after settlement commits. The seven-day
event is recorded on the first successful settled call at least seven days
after signup.

Attribution writes are failure-isolated. They cannot fail signup, inference,
settlement, payment acknowledgement, or streaming.

## Google Ads Data Manager

Google Ads imports attributed outcomes from:

```text
GET /v1/internal/marketing/google-ads-conversions.csv
```

The HTTPS feed requires a dedicated HTTP Basic username and a 32-character or
longer secret from Secret Manager. It is private, uncached, and excluded from
indexing. The feed covers the last 90 days and fails with `503` rather than
silently truncating if it reaches the configured row ceiling.

Each Google-attributed milestone creates an idempotent, month-partitioned
Spanner row:

1. `TrustedRouter Signup`
2. `TrustedRouter Activated API User`
3. `TrustedRouter Retained API User 7d`
4. `TrustedRouter Credit Purchase`

The row contains only `gclid`, `gbraid`, or `wbraid`, the conversion action and
timestamp, exact integer-derived USD value, currency, and a SHA-256 order ID
derived from the random anonymous attribution ID. It contains no workspace ID,
user ID, email, model/provider choice, API key, prompt, output, or request body.
Google can use the order ID and its own click ID for deduplication without
receiving a TrustedRouter account identifier.

Signup and product-use rows are committed atomically with their attribution
milestones. Purchase rows are created only after the payment ledger's
idempotency check wins. A protected backfill endpoint reconstructs historic
signup, activation, and retention rows; it deliberately does not synthesize
historic individual purchases from aggregate totals.

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
