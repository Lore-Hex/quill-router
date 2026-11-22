# Request tagging and OpenRouter attribution compatibility

Status: reviewed; implementation in pull request 157

Owner: Lore Hex Corp / TrustedRouter

Public documentation target: `https://trustedrouter.com/docs/tagging`

## 1. Objective

TrustedRouter needs two related but distinct metadata contracts:

1. OpenRouter-compatible attribution and observability fields so an existing
   OpenRouter client can change `base_url` without losing user, session, trace,
   or app attribution.
2. A first-class request tagging system modeled on AWS resource tags so teams
   can allocate LLM cost and usage by environment, application, team, project,
   cost center, or other non-sensitive business dimensions.

Tags are metadata only. They never enter a prompt, provider request, model
context, durable log message, Sentry event, or public analytics page.

## 2. Source compatibility

OpenRouter documents the following request metadata:

- `user`: caller-defined stable end-user identifier.
- `session_id`: groups related requests; `X-Session-Id` is an alternate input.
- `trace`: arbitrary JSON metadata forwarded to Broadcast destinations.
- `HTTP-Referer`: app URL used for attribution.
- `X-OpenRouter-Title` and legacy `X-Title`: app display name.
- `X-OpenRouter-Categories`: comma-separated app categories.
- `X-OpenRouter-Metadata: enabled`: opts into routing metadata on responses.

References:

- <https://openrouter.ai/docs/cookbook/administration/user-tracking>
- <https://openrouter.ai/docs/guides/features/broadcast/overview>
- <https://openrouter.ai/docs/app-attribution>
- <https://openrouter.ai/docs/guides/features/router-metadata>

TrustedRouter will accept these fields on Chat Completions, Responses,
Anthropic Messages, and Embeddings wherever their request shape permits it.
Compatibility metadata is consumed by the attested gateway and control plane;
only provider-native fields explicitly allowed by an adapter may reach an
upstream provider. `user`, `trace`, `session_id`, app attribution, and
TrustedRouter tags are never forwarded upstream. This also applies to internal
panel and advisor calls made by combination models.

### 2.1 Precedence

- Body `session_id` wins over `X-Session-Id`.
- `X-OpenRouter-Title` wins over `X-Title`.
- `HTTP-Referer` is retained separately from the display title.
- A missing title with a present referer uses the referer host as the app label.
- Invalid categories are rejected by TrustedRouter rather than silently ignored.
- `X-OpenRouter-Metadata` accepts `enabled` or `disabled`, case-insensitively.

### 2.2 Limits

- `user`: 256 Unicode characters.
- `session_id`: 256 Unicode characters.
- `trace`: JSON object, at most 8 KiB after compact UTF-8 encoding, depth at
  most 8, and at most 256 total object keys and array elements.
- App title: 120 Unicode characters.
- Referer: 2,048 Unicode characters and a valid `http` or `https` URL.
- Categories: at most 2 values per request, each lowercase kebab-case and at
  most 30 characters. They are request metadata, not an accumulated public
  marketplace profile.

## 3. TrustedRouter request tags

An inference request is the tagged resource. Tags are attached atomically when
the request is authorized and become immutable generation metadata after
settlement.

Canonical request shape:

```json
{
  "model": "trustedrouter/zdr",
  "messages": [{"role": "user", "content": "Summarize this contract."}],
  "tags": {
    "environment": "production",
    "team": "legal",
    "application": "contract-review",
    "cost-center": "legal-01"
  }
}
```

The OpenAI SDK can send this with `extra_body={"tags": {...}}`. TrustedRouter
SDKs expose a typed `tags` argument.

### 3.1 AWS-compatible semantics

The contract follows the common AWS tag limits documented for EC2 and ECS:

- Maximum 50 user tags per request.
- Every key is unique and maps to one value.
- Key length is 1 through 128 Unicode characters.
- Value length is 0 through 256 Unicode characters.
- Keys and values are case-sensitive.
- Portable characters are Unicode letters, numbers, and spaces plus
  `+ - = . _ : / @`.
- `aws:` and `trustedrouter:` prefixes, in any letter case, are reserved.
- Values must be strings. Nested values, arrays, numbers, booleans, and null
  are rejected rather than coerced.
- Empty values are allowed; empty keys are not.
- The compact UTF-8 encoding of the effective map may not exceed 4 KiB.
- Unicode space separators use category `Zs`; line and paragraph separators
  are rejected.
- Keys are compared as raw code points with no Unicode normalization. Teams
  should standardize spelling because visually identical keys may be distinct.

Reference: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Using_Tags.html>

TrustedRouter accepts only the canonical object shape. An AWS-style array of
`{"Key": ..., "Value": ...}` objects is intentionally not accepted inside an
inference body because accepting two shapes complicates OpenAI SDK schemas and
duplicate-key detection. SDK helpers may convert an AWS list to the canonical
object before sending it.

### 3.2 API-key default tags

`POST /v1/keys` and `PATCH /v1/keys/{hash}` accept a `tags` object with the same
rules. These are private default tags for requests made with that key.

Effective request tags are:

```text
effective_tags = key_default_tags overlaid by request_tags
```

Request values win for duplicate keys. Validation runs after the merge, so the
effective set must not exceed 50. Updating an API key changes only future
requests; historical generations remain immutable.

The key list/get responses return default tags. Raw API keys and tag values are
never included in logs.

### 3.3 Idempotency

Caller-supplied request tags are part of the authorization idempotency fingerprint. Reusing an
idempotency key with different tags returns `409 conflict`. This prevents a
retry from silently changing cost attribution.
API-key defaults are deliberately excluded from the fingerprint. A retry after
an API-key default edit replays the original authorization and its frozen tags.
An absent request tag map is omitted from the fingerprint so ordinary tagless
requests remain byte-compatible across the rollout boundary. An explicitly
supplied empty object remains in the fingerprint because the pre-tagging
lenient router included that unknown field; preserving it allows an
idempotent `tags: {}` retry to span the deployment without a false conflict.

## 4. Data flow and trust boundary

```text
client request
  -> attested gateway observes, sanitizes, and optionally enforces body/header metadata
  -> control-plane authorize validates again and merges API-key defaults
  -> authorization response returns effective tags
  -> gateway invokes provider without TrustedRouter tags/trace/app fields
  -> gateway settlement carries compatibility metadata, but not tags
  -> Spanner generation record + Bigtable activity body store metadata
  -> optional metadata-only Broadcast exports tag attributes asynchronously
```

Validation is duplicated deliberately:

- The enclave initially runs metadata validation in observe mode: malformed
  metadata is dropped and logged by error class only, while inference remains
  compatible. `TR_REQUEST_METADATA_ENFORCEMENT=enforce` enables stable 400s
  after production telemetry is clean. In both modes, router-only metadata is
  excluded from every provider wire serializer.
- The control plane treats the enclave as authenticated but still validates at
  its persistence boundary.

The effective tag set is frozen on the gateway authorization. The gateway does
not send tags during settlement. If an older or third-party gateway supplies
settlement tags, the control plane ignores them and logs counts only. Billing
and settlement never fail because metadata differs. Refunds do not create
generation rows.

## 5. Storage and scale

The `Generation` record gains these metadata fields:

- `user`: optional string.
- `session_id`: optional string.
- `app`: existing app title.
- `http_referer`: optional string.
- `app_categories`: list of strings.
- `tags`: canonical `dict[str, str]`.

`trace` and free-form `metadata` are not copied into the durable generation
record. They remain available to the asynchronous Broadcast job only. This
limits accidental persistence of customer-provided free-form text.

Spanner stores the generation JSON as the billing source of truth. Bigtable
stores the same generation body in the existing workspace/date and recent-row
indexes. Adding tags therefore adds no extra synchronous database writes.

The launch activity APIs filter a bounded recent/date scan and report whether
the result was truncated. They do not fan out one Bigtable write per tag; doing
50 additional writes per generation would be unacceptable at the stated
billion-message scale.

For high-volume cost allocation, a later asynchronous rollup worker will
aggregate only workspace-activated tag keys into hour/day/month records. That
preserves write amplification bounds while matching AWS's concept that cost
allocation tags must be activated before they appear in billing reports.

## 6. Public and management APIs

### 6.1 Inference

The following routes accept `tags`, `user`, `session_id`, and `trace`:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `POST /v1/embeddings`

Headers accepted on all four routes:

- `X-Session-Id`
- `HTTP-Referer`
- `X-OpenRouter-Title`
- `X-Title`
- `X-OpenRouter-Categories`
- `X-OpenRouter-Metadata`

### 6.2 API keys

- `POST /v1/keys`: accepts default `tags`.
- `PATCH /v1/keys/{hash}`: replaces the complete default tag map when `tags`
  is present; `{}` clears it.
- `GET /v1/key`, `/v1/keys`, and `/v1/keys/{hash}`: return default `tags`.

### 6.3 Activity

`GET /v1/activity` adds:

- `tag_key`: exact case-sensitive tag key.
- `tag_value`: optional exact case-sensitive value; requires `tag_key`.
- `group_by=tag:<key>`: groups request count, tokens, and integer microdollar
  cost by the selected tag value.

Activity responses include `meta.truncated`, `meta.groups_truncated`,
`meta.scanned`, and `meta.scan_limit`. Tag grouping keeps the 100 most frequent
values and combines the rest into `__other__`. Tag filters are exact but run
over the bounded recent/date scan; `meta.tag_filter_scope=recent_window` makes
that limitation explicit. Activated-tag rollups will provide exact long-horizon
allocation in a later release.

`group_by=none|request|generation` returns `tags`, `user`, `session_id`,
`http_referer`, and `app_categories` on each metadata-only event.

Tag filtering is workspace-scoped and applies after API-key/date filtering.
No prompt or response content is exposed.

### 6.4 Generation metadata

`GET /v1/generation?id=...` returns the same tag and attribution fields. The
existing `/generation/content` behavior remains `404 content_not_stored`.

### 6.5 In the console

The Observability page (`/console/activity`) shows tags in three places:

- Request list: each request's tags as `key: value` chips. Clicking a chip
  filters by that tag.
- Tags panel, filter: a key and a value picked from the keys seen in the
  selected range and that key's most used values, or a value typed in when
  the list does not hold it. The filter narrows the request list and the
  breakdown. It does not narrow the usage chart.
- Tags panel, breakdown: for one key and the selected range, one row per value
  with requests, credit cost, tokens and median generation time, ordered by
  cost. The 50 most expensive values keep a row, the rest are summed into
  `other`, and requests without the key are shown as `untagged`.

A workspace with no tags in the range sees a short explanation and a link to
`/docs/tagging` instead of an empty table.

The panel follows the range of the usage panel: it reads that panel's
`data-usage-range` when it starts and then its `activity:range-change` event.
A filter is kept across range changes. A range with no request for it says so
and keeps the filter in view; only the viewer clears it.

The page reads two endpoints, both session-authorized exactly like the page's
other JSON endpoints and both scoped to the session's workspace:

- `POST /console/activity/tags.json` with `{range, group_by, tag_key,
  tag_value}` returns the keys, the breakdown and the filter key's values.
- `POST /console/activity/requests.json` with `{range, tag_key, tag_value}`
  returns the newest 50 matching requests in the range.

Reads are computed in ClickHouse from `activity_generations FINAL` over every
row of the tenant since the start of the range, so they are totals and not a
sample. They need no schema change, rollup or grant. Each read keeps a bounded
amount of state, however many rows, values or generation times the range has,
and each bound is in the statement that needs it: rows keep arriving between
two statements, so none of them relies on what an earlier one saw.

| Read | State it keeps |
| --- | --- |
| keys | a `topK` sketch with 10,000 counters |
| one key's values | a `topK` sketch with 5,000 counters |
| candidates | two `topK` sketches of 6,000 counters and one distinct-count sketch, with no `GROUP BY` |
| totals | one group per candidate, for at most 4,000 candidates named in the statement |
| histogram | at most 52 groups, each with the page's bounded elapsed-time histogram |

A breakdown is the last three. The candidates read names, by a 64-bit hash,
the 2,000 values with the most cost and the 2,000 with the most generations,
and estimates how many distinct values the key has. The totals read returns
the text, generations and credit cost of those candidates and of no other
value. The 50 that keep a row are chosen from them by credit cost, then
generations, then text, which is the order the in-memory store uses. The
histogram read then totals those 50, every other value as one row, and
untagged generations as one row.

Credits and BYOK are told apart inside each statement that sums cost, by one
fixed expression that reproduces `UsageType.coerce` (`byok` in any case, with
any whitespace `str.strip` removes around it; everything else is Credits). A
statement therefore has two cost classes whatever spellings are stored, and
none is handed a list of usage types that an earlier one saw, so a tenant's
first BYOK generation arriving between two reads is counted as BYOK.

The bounds have five visible consequences:

- More keys or values than a sketch counts exactly. The list is then
  approximately the most frequent ones, with counts that can be too high, and
  the panel says so. A value the list leaves out can be typed, and the empty
  value has a control of its own. A typed value is sent as typed: its length
  is counted in characters, as the server counts it, and a value over the
  limit of 256 is refused on the page in words, not cut.
- Too many values to break down. A key with more than 500,000 distinct values
  is an identifier, not a dimension to allocate cost by, and is not broken
  down: the panel offers another key or a shorter range, and its values can
  still be filtered by. Each store refuses when its own count is over the
  limit. The in-memory store counts exactly; ClickHouse estimates the count
  within about one percent, in the candidates statement itself, so the panel
  says "about 500,000 or more". Nothing depends on this limit for memory: with
  it lifted, a key of twelve million values was still read in fixed space.
  When the page did not ask for a key, the three most used keys are tried in
  turn, so a request id on every request does not make the first view a
  refusal.
- Approximate ranking. The response says the ranking is approximate in two
  cases. One is a key with 2,000 or more distinct values, where ClickHouse
  ranks the heaviest values its sketches found and not every value. The other
  is read off the final rows by both stores: the Other row holds more credit
  cost (then generations) than the smallest named row. No single folded value
  can outweigh the row that sums them, so while Other is no larger, no value
  in it outweighs a named row; once it is larger, a value that grew or first
  appeared after the 50 were chosen may belong among them. That rule is conservative:
  an Other row made of many small values is reported the same way although
  the ranking is right, and the page says so. Every row's totals are exact
  either way, and the rows are ordered by the totals they show.
- The Other row has no count of the values in it. By the time it is totalled,
  values may have arrived that no earlier read counted.
- Medians. The median is generation time as the server measured it
  (`elapsed_milliseconds`), not client-observed latency. It has the definition
  of the reliability panel's server median and is computed by the same code
  (`elapsed_histogram_columns`, `settle_elapsed_median`): the middle value, or
  the floor of the mean of the two middle values, read from a histogram of
  1,024 ms slots in which only a group's middle slots are counted value by
  value. A breakdown keeps one histogram for each of its groups, and 52 full
  histograms of 65,536 slots ran the reader out of memory when measured, so
  the tag histogram counts every generation time of 4,194,304 ms (about 70
  minutes) or more in its open-ended last slot: at most 4,097 slots a group.
  A group whose median is that long is shown without one. The reliability
  panel's own ceiling is about 18.6 hours.

Totals and medians of one breakdown come from one statement, so they describe
one snapshot. Which values keep a row was decided by the two statements before
it: a value that first appears after them is counted in the Other row.

A value is identified by two 64-bit hashes of its text. The candidates are
named by the first alone. Two values that collide in it are both read by the
totals statement, which splits them again by the second hash, so each is
ranked on its own. Two values of one key that collided in both would have
their requests and cost attributed to one row, under the smaller text.

The in-memory store computes the same answers in Python.
`tests/test_activity_tags.py` holds the two to what ClickHouse 26.8 returned
for the same rows: `tests/fixtures/activity_tags_clickhouse.json` is a
recording of every statement and its answer, stored under a hash of the
statement's exact text, so a changed statement has no answer until it is
recorded again.

Measured on ClickHouse 26.8 with the control reader's limits (1 GiB of memory,
30 seconds) and synthetic tenants of one to twelve million generations in 90
days: a breakdown of 200 values, a key with 495,000 values, 50,000 values of
256 bytes and 100,000 values of 1 KiB stay under 250 MiB a statement,
generation times that fill every slot of every group under 300 MiB, and a key
with a million or with twelve million values is refused after two statements
in under 100 MiB.

What these queries do not bound is the cost of reading the `tags` column under
`FINAL`, which every tag read pays, the existing activity reads included. It
grows with the tag text per request and with the number of unmerged parts in
the range, not with the state above. With about 2 KiB of tags on every request
(the limit is 4 KiB) a 90-day read peaked near 430 MiB on a merged table and
ran out of the 1 GiB with merges stopped and some twenty parts in every
month, where 1 and 7 days still loaded. A read that fails this way is an
error, never partial totals: the panel says the range could not be loaded and
that a shorter one may.

Only completed generations are counted. A request that fails before it
produces a generation has no row, so the page cannot show errors by tag.

Section 10 applies to these reads. What holds, and what does not:

- Tag keys and values from the console do not appear in the browser URL or in
  browser storage, in request logs, in application logs, or in Sentry.
- They do appear in ClickHouse's own query log on the analytics servers.
  ClickHouse records every statement with its parameters substituted, the
  control reader's profile does not turn that off, and the log is kept for 30
  days. It is readable only by administrators and lives on the servers that
  store the `tags` column itself, so it is not a wider exposure than the data
  at rest, but "never reaches a log" would be false. The same is true of the
  existing `GET /v1/activity` tag filter.

How the first holds:

- Tag keys and values reach the control plane in a JSON request body, never in
  a URL. A query string is written to the load balancer's request log and
  copied into Sentry's request context; a body is not.
- Console input is held to the limits of section 3.1 by the same validator as
  the inference path. Its error messages name the rule and never the text.
- ClickHouse normally receives bound values as URL parameters of an internal
  request, and a URL is written down by more than one layer: httpx logs it at
  INFO, Sentry's HTTP client integration records its query string in a span
  and a breadcrumb, and an HTTP status error carries it in its message. The
  console's tag reads therefore send the statement and every bound value as
  form fields in the request body (`bound_values_in_the_body`), which the
  ClickHouse HTTP interface reads the same way, so the URL carries only the
  database name. A Sentry scope with no integrations and a status error
  re-raised without its URL remain as a second line. The `GET /v1/activity`
  tag filter predates this: it takes `tag_key` and `tag_value` as query
  parameters and its ClickHouse read still binds them in the URL. Bringing it
  in line is a follow-up.
- The page writes tag text with `textContent` and template escaping only,
  never as markup or as an attribute.

Follow-up: mask or exclude tag literals in the ClickHouse query log, with
server-side masking rules or a per-profile logging setting. That is node
configuration across three clouds, and it applies equally to the existing
`GET /v1/activity` tag filter.

## 7. Broadcast mapping

PostHog `$ai_generation` properties:

- Existing `$ai_user_id`, `$ai_session_id`, and trace fields remain.
- Tags are emitted as `tag.<key>` properties.
- App attribution uses `trustedrouter.app`, `trustedrouter.http_referer`, and
  `trustedrouter.app_categories`.

OTLP attributes:

- `trustedrouter.tag.<key>`
- `user.id`
- `session.id`
- `trustedrouter.app`
- `trustedrouter.http_referer`
- `trustedrouter.app_categories`

Tag values are metadata and are exported even when content export is disabled.
The UI and docs warn users not to place secrets, personal data, client names,
matter names, prompts, or output in tags.
The boundary is deliberate: TrustedRouter's own logs never contain tag values,
while a customer's configured PostHog or OTLP destination receives tags by
design. Key management surfaces show the same warning because key defaults are
exported on every request that uses that key.

## 8. Response routing metadata

TrustedRouter continues returning its existing `trustedrouter.routing` object.
When `X-OpenRouter-Metadata: enabled` is present, successful Chat Completions
and Responses also receive an `openrouter_metadata` compatibility object built
from the same selected route and fallback attempts. Streaming sends it on the
final metadata-bearing event before `[DONE]`.

The compatibility object contains attempted routes only and never contains
tags, user/session IDs, secrets, prompts, or outputs. Custom models expose only
their published model ID and the `trustedrouter` provider label; base routes,
BYOK status, and region are masked.

## 9. Error contract

After strict enforcement is enabled, invalid tags return an OpenAI-shaped
`400 invalid_request_error` with a stable `error.type` of `invalid_tags`.
During the initial observe phase they are omitted from attribution and the
inference request continues. Examples:

- `tags must be an object`
- `tags may contain at most 50 entries`
- `tag key must contain 1 to 128 characters`
- `tag value must contain at most 256 characters`
- `tag key uses a reserved prefix`
- `tag key contains unsupported characters`
- `tag value must be a string`

Native Messages requests receive the equivalent Anthropic-shaped 400 error.

## 10. Security and privacy requirements

- Never log tag maps or individual tag values in Sentry, Axiom, Cloud Logging,
  panic output, or request-start/request-end logs.
- Logging may include only `tag_count` and validation error class.
- Tags never enter provider payloads, including OpenAI-compatible providers.
- Tags are never public and are never included in provider/model leaderboards.
- Management and activity endpoints remain workspace-authorized.
- API-key defaults cannot be changed with an inference key.
- Reserved prefixes prevent callers from forging router-owned metadata.
- Broadcast delivery remains asynchronous and failures never fail inference or
  settlement.

## 11. Testing gates

### Validation

- 0 and 50 tags accepted; 51 rejected.
- Key/value boundary lengths accepted; one character over rejected.
- Case-sensitive duplicate-like keys (`Team`, `team`) remain distinct.
- Empty value accepted; empty key rejected.
- Reserved prefixes rejected case-insensitively.
- Non-string and nested values rejected.
- Unicode letter/number/space handling matches the portable AWS character set.

### Compatibility

- Chat, Responses, Messages, and Embeddings preserve `user`, `session_id`, and
  `trace` through authorization and settlement.
- `X-Session-Id` fallback and body precedence are tested.
- OpenRouter title/referer/category headers become generation metadata.
- Router metadata opt-in works in streaming and non-streaming responses.

### Trust boundary

- Provider test servers never receive `tags`, `trace`, `session_id`, app
  headers, or TrustedRouter-only metadata.
- Prompt/output content never appears in tags, activity metadata, logs, or
  validation errors.
- Settlement cannot alter authorization-frozen effective tags.

### Persistence and activity

- In-memory and Spanner/Bigtable stores round-trip tags identically.
- Bigtable reconciliation restores tagged generation rows.
- Activity filters and tag grouping use exact case-sensitive matches and
  integer microdollar accounting.
- API-key default tags merge deterministically and historical rows do not
  change after key updates.

### SDKs

- Python sync/async and JavaScript clients put `tags` in the JSON body.
- Compatibility fields and attribution headers do not leak into model content.
- SDK-side validation matches server limits but server validation remains
  authoritative.

## 12. Rollout

1. Merge and deploy the control-plane schema/storage changes with
   backwards-compatible defaults. Verify authorize responses advertise
   `request_metadata_version: 1` before any enclave merge.
2. Merge the enclave parser in observe mode only after the new control plane is
   serving. A tagged request against an older control plane is refunded and
   fails with a retryable 503 instead of silently losing attribution.
3. Deploy control plane before enclave so old gateways remain accepted.
   Acceptance includes a new control plane with an old enclave: API-key
   defaults still apply, tagless request fingerprints remain unchanged, and
   settlement uses authorization-frozen tags without gateway propagation.
4. Roll enclave regions one at a time and run tagged Chat, Responses, Messages,
   and Embeddings smokes.
5. Review counts-only observe logs. Enable
   `TR_REQUEST_METADATA_ENFORCEMENT=enforce` region by region only after legacy
   shapes and invalid referers are understood.
6. Publish `https://trustedrouter.com/docs/tagging` and add it to `/docs`,
   `llms.txt`, `llms-full.txt`, and the core sitemap.
7. Release SDK updates after production accepts the field.

Rollback is code-only. Old generation rows deserialize with empty/default tag
fields, and new rows remain readable by code that ignores unknown JSON fields.
