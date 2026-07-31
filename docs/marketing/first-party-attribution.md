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

Signup and settled credit-purchase conversions are sent directly from a
scheduled Cloud Run job to Google's Data Manager REST endpoint:

```text
POST https://datamanager.googleapis.com/v1/events:ingest
```

The job calls raw HTTPS and does not load a Google browser tag or Google client
library. Its Cloud Run service identity requests only the
`https://www.googleapis.com/auth/datamanager` scope. Google accepts at most
2,000 events per request; TrustedRouter uses bounded 500-row batches and a
five-minute schedule.

Delivery state is durable:

- `pending`: eligible for a worker lease
- `submitted`: Google accepted the request and returned a request ID
- `dead`: a permanent failure or the retry limit was reached

Leases prevent concurrent workers from claiming the same row. Transient
network, `408`, `409`, `425`, `429`, and `5xx` failures use bounded exponential
backoff. The deterministic transaction ID lets Google deduplicate an upload if
the worker crashes after Google accepts it but before Spanner records the
request ID.

Activation and seven-day retention remain available in the authenticated CSV
recovery feed:

```text
GET /v1/internal/marketing/google-ads-conversions.csv
```

The feed is private, uncached, excluded from indexing, and fails with `503`
rather than silently truncating at its configured row ceiling.

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

### Production Setup

The production Google Ads resources are:

```text
operating account: 8424034078
signup conversion action: 7701333837
purchase conversion action: 7701333966
```

These identifiers are ordinary deployment configuration, not credentials. They
can be overridden with `TR_GOOGLE_DATA_MANAGER_*`. When a manager account makes
the call, also configure:

```text
TR_GOOGLE_DATA_MANAGER_LOGIN_ACCOUNT_ID
```

Authorize
`tr-google-data-manager@quill-cloud-proxy.iam.gserviceaccount.com` with Standard
access to Google Ads account `8424034078`. The worker has only Spanner database
access and Service Usage Consumer in GCP. It has no application secrets,
provider keys, BYOK decrypt permission, or prompt-path Bigtable access. It
retrieves a short-lived access token from the Cloud Run metadata server with
only the Data Manager OAuth scope; no service-account key is created or stored.
The worker initializes a Spanner-only outbox adapter and never constructs the
application's Bigtable client.

`scripts/deploy/infra.sh` creates the dedicated identity.
`scripts/deploy/google_data_manager.sh` deploys the uploader job and scheduler,
and skips safely until the identity exists.

Google reference:

- [Data Manager event ingestion](https://developers.google.com/data-manager/api/reference/rest/v1/events/ingest)
- [Data Manager access setup](https://developers.google.com/data-manager/api/devguides/quickstart/set-up-access)
- [Data Manager request status](https://developers.google.com/data-manager/api/reference/rest/v1/requestStatus/retrieve)

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

Google does not need TrustedRouter conversion data for internal measurement.
TrustedRouter records the metadata-only funnel and can compare campaigns and
creative cells through signup, first successful API call, payment, and
seven-day retained usage:

```bash
uv run python scripts/marketing_funnel_report.py \
  --source google \
  --campaign high_intent_search_20260725 \
  --days 30
```

Use `--format json` for analysis or dashboards. Add `--creative <utm_content>`
to inspect one creative cell. Revenue remains integer microdollars until the
final display conversion.

One `utm_content` value is one measurable creative cell. Multiple headlines
inside one responsive search ad share that cell, so create separately tagged
ads when headline-level downstream measurement is required.

## Initial Optimization Policy

Use `signup_completed` as the first primary conversion while volume is low.
Report activated CAC separately using `first_successful_api_call`. Move bidding
optimization toward activated use or credited purchases only after each event
has enough weekly volume to avoid unstable learning.
