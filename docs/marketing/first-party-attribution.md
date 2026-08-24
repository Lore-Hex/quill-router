# First-Party Acquisition Attribution

TrustedRouter measures paid and organic acquisition internally. For visitors
who arrive through a Google ad, it also reports three narrowly scoped
server-side conversion classes to Google Ads: signup, first successful API
call, and settled credit purchase. X and other advertising platforms receive
no downstream conversion events.

## Collection Boundary

The public website captures an encrypted, authenticated, HttpOnly,
SameSite=Lax cookie for 90 days. Production sends it only over HTTPS. The
cookie contains:

- an anonymous random identifier
- first and last source, medium, campaign, term, and creative
- first and last landing path and external referring host
- keyed, non-reversible fingerprints indicating a Google or X click
- for a Google ad click only, Google's click identifier inside the encrypted
  cookie so a later conversion can be attributed without browser tracking code
- capture timestamps

The cookie and durable attribution record never contain prompts, outputs, raw
API keys, BYOK keys, email addresses, payment credentials, request bodies, IP
addresses, or full referring URLs. X `twclid` values are converted immediately
to a keyed HMAC fingerprint and discarded. Google `gclid`, `gbraid`, and
`wbraid` values are fingerprinted for internal reporting and also retained only
as encrypted ciphertext for a maximum of 90 days. They never appear in logs,
public APIs, dashboards, or plaintext database fields.

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
6. `acquisition.free_credit_exhausted`
7. `acquisition.checkout_started`
8. `acquisition.payment_method_saved`
9. `acquisition.credit_purchase_completed`
10. `acquisition.retained_api_usage_7d`

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

`free_credit_exhausted` is claimed only when the authoritative typed ledger
shows settled prepaid usage at or above the starter grant. The check runs after
an insufficient-credit authorization or when checkout begins, outside the
successful inference path. `checkout_started` is claimed after Stripe or PayPal
successfully creates a checkout session. `payment_method_saved` is claimed only
after Stripe confirms a reusable payment method. All three are
once-per-workspace milestones. Checkout and saved-payment-method events remain
first-party only.

Attribution writes are failure-isolated. They cannot fail signup, inference,
settlement, payment acknowledgement, or streaming.

## Advertising Platform Boundary

TrustedRouter does not load Google Analytics, Google Tag Manager, a Google Ads
browser tag, an X pixel, or another advertising SDK. It exposes no conversion
CSV feed.

A separate server-side Google Data Manager worker sends only Google's original
click identifier, one of the three conversion classes, event time, an opaque
deduplication ID, and, for a settled purchase, exact amount and currency. It
does not send email, name, account ID, user ID, workspace ID, API key, model,
provider, IP address, prompt, output, request body, or retention activity. The
worker decrypts click identifiers in memory with a dedicated KMS key that
cannot decrypt customer BYOK credentials. Delivery is durable, idempotent, and
confirmed through Google Data Manager's asynchronous request-status API before
the outbox marks an event submitted.

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

## First-Party Funnel Report

TrustedRouter records its metadata-only funnel internally and can compare
campaigns and creative cells through signup, first successful API call,
payment, and seven-day retained usage:

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --campaign high_intent_search_20260725 \
  --days 30
```

Use `--format json` for analysis or dashboards. JSON reports include a
measurement-health decision, aggregate Google Ads spend when configured, and
the creative-level funnel. Add `--creative <utm_content>`
to inspect one creative cell, or `--landing <path>` to inspect one exact
destination. Reports retain the landing path as its own dimension and show
signup, activation, and purchase rates against engaged visitors. Revenue
remains integer microdollars until the final display conversion.

For Google reports, the command distinguishes a UTM-labeled visit from a real
Google Ads click. It counts only visits carrying `gclid`, `gbraid`, or `wbraid`
as eligible for server-side conversion delivery. The report holds scale when
those identifiers are absent, a click-backed signup was not durably encrypted,
native spend is unavailable, or paid traffic has spend but no settled purchase
in the window. Click persistence is reported separately from click capture so
KMS permission regressions cannot silently empty the conversion outbox.

Native spend uses Google's aggregate reporting API. Configure the report with:

```text
TR_GOOGLE_ADS_REPORTING_CUSTOMER_ID=<Google Ads customer ID>
TR_GOOGLE_ADS_REPORTING_LOGIN_CUSTOMER_ID=<manager ID, when applicable>
TR_GOOGLE_ADS_DEVELOPER_TOKEN=<Google Ads API developer token>
TR_GOOGLE_ADS_REPORTING_TIME_ZONE=America/Los_Angeles
```

The reporting identity needs read access to the Google Ads customer. The API
returns campaign name, impressions, clicks, and `cost_micros`; TrustedRouter
does not request search text, user identifiers, or audience data. Spend and
revenue remain integer microdollars. Use `--google-ads-spend required` in a
decision report so missing credentials or permissions fail closed.

One `utm_content` value is one measurable creative cell. Multiple headlines
inside one responsive search ad share that cell, so create separately tagged
ads when headline-level downstream measurement is required.

For landing-page tests, keep campaign, keywords, bids, ad copy, geography, and
device settings identical. Change only the destination and use one stable
`utm_content` value per arm. This prevents a strong headline or a different
search term from being mistaken for a landing-page improvement.

## Initial Optimization Policy

Use `signup_completed` as a secondary early indicator while volume is low.
Optimize bidding toward activated users and credited purchases after each
action is verified as recording and has enough weekly volume to avoid unstable
conclusions. First-party attribution remains the source of truth for funnel and
revenue reporting.
