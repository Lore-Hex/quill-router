# Request tagging and OpenRouter attribution compatibility

Status: proposed for implementation

> 🔎 **FABLE REVIEW (2026-07-11) — verdict: sound design, approve with changes.**
> The trust boundary, AWS-shape limits, and freeze-at-authorization are right.
> Blocking items before implementation: (1) §4's settle-rejection-on-tag-mismatch
> is a money-risk — settlement must NEVER fail over metadata; (2) §3.3's
> idempotency fingerprint must cover REQUEST tags only (not effective tags) and
> must be backward-compatible for tagless retries across the deploy boundary;
> (3) add a TOTAL tag-bytes cap — 50×(128+256) chars/request inflates every
> Bigtable activity-scan and settle-outbox row; (4) §6.3 tag group-by needs the
> same truncation honesty we just shipped for usage graphs (#142). Inline
> comments marked 🔎 below; remove them when addressed.

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
upstream provider. In particular, `trace`, `session_id`, app attribution, and
TrustedRouter tags are never forwarded upstream.

> 🔎 **FABLE:** `user` is conspicuously absent from the never-forwarded list —
> decide it explicitly. OpenAI's native API has a `user` param used for abuse
> detection, and some OpenAI-compat providers accept it, so "forward" is
> defensible — but the privacy-preserving default for an attested router is
> STRIP everywhere and state it here. Whichever way, name it in this paragraph
> and add a trust-boundary test for it (§11 currently tests tags/trace/session/
> app headers but not `user`). Also specify the fusion/synth panel path: internal
> panel subrequests must not inherit `user`/`session_id` into provider payloads
> either.

### 2.1 Precedence

- Body `session_id` wins over `X-Session-Id`.
- `X-OpenRouter-Title` wins over `X-Title`.
- `HTTP-Referer` is retained separately from the display title.
- A missing title with a present referer uses the referer host as the app label.
- Invalid categories are rejected by TrustedRouter rather than silently ignored.
- `X-OpenRouter-Metadata` accepts `enabled` or `disabled`, case-insensitively.

### 2.2 Limits

- `user`: 128 Unicode characters.
- `session_id`: 128 Unicode characters.
- `trace`: JSON object, at most 32 KiB after compact UTF-8 encoding, depth at
  most 8, and at most 256 total object keys and array elements.

> 🔎 **FABLE:** 32 KiB is too generous once you trace where `trace` actually
> transits DURABLY: the settle body is frozen into the tr_settle_outbox row
> (enqueue-at-settle) and into the broadcast delivery queue entity until
> drained. So "broadcast-only, not persisted" is not quite true — it is
> persisted in two Spanner queues for the life of the row. Either exclude
> `trace` from the frozen settle body (rebuild it for broadcast from a separate
> channel) or cut the cap to something queue-friendly (4–8 KiB). State the
> chosen transit path in §4's diagram.
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

> 🔎 **FABLE:** Add two things here. (1) A TOTAL-size cap, e.g. "the compact
> UTF-8 encoding of the effective tag map must not exceed 4 KiB". The per-field
> AWS limits allow ~19 KB of tags per request; tags ride the generation body
> that every Bigtable activity/usage scan reads (usage_series reads FULL bodies
> at up to 200k rows/scan — we sized those scans before tags existed), plus the
> outbox settle body. AWS charges nothing for fat tags; our scan path does.
> (2) State Unicode comparison semantics explicitly: keys are compared by raw
> codepoints — no NFC/NFKC normalization, consistent with case-sensitivity —
> so visually identical keys can be distinct; docs should warn alongside the
> `Team`/`team` example.

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
effective set may never exceed 50. Updating an API key changes only future
requests; historical generations remain immutable.

> 🔎 **FABLE:** The merged-limit rejection needs its own error string: a caller
> sending 3 tags can be rejected because the key silently carries 48 defaults
> they can't see from the error. Add to §9: `effective tags exceed 50 after
> merging key default tags (key defaults: N, request: M)` — counts only, never
> the tag contents. Also "may never exceed" reads as permission; say "must not
> exceed 50; requests violating this after merge are rejected".

The key list/get responses return default tags. Raw API keys and tag values are
never included in logs.

### 3.3 Idempotency

Tags are part of the authorization idempotency fingerprint. Reusing an
idempotency key with different tags returns `409 conflict`. This prevents a
retry from silently changing cost attribution.

> 🔎 **FABLE — blocking, two corrections:**
> 1. Fingerprint the **request tags as sent**, NOT the effective (merged) set.
>    If key defaults are in the fingerprint, a `PATCH /v1/keys` between a
>    request and its innocent retry changes the fingerprint and turns the retry
>    into a spurious 409 — and retry-replay is our recovery mechanism for lost
>    responses (the committed reservation must be returned, not refused).
> 2. Deploy-boundary compatibility: a tagless request authorized on the OLD
>    revision and retried on the NEW one must produce the SAME fingerprint.
>    Canonicalize "no tags" as the absence of the field (not `{}` hashed in),
>    i.e. the new fingerprint function must be byte-identical to the old one
>    whenever tags are absent. Add both cases to §11's tests.
> Also name the 409's `error.type` (e.g. `idempotency_conflict`) in §9.

## 4. Data flow and trust boundary

```text
client request
  -> attested gateway validates body/header metadata
  -> control-plane authorize validates again and merges API-key defaults
  -> authorization response returns effective tags
  -> gateway invokes provider without TrustedRouter tags/trace/app fields
  -> gateway settlement carries effective tags and compatibility metadata
  -> Spanner generation record + Bigtable activity body store metadata
  -> optional metadata-only Broadcast exports tag attributes asynchronously
```

Validation is duplicated deliberately:

- The enclave rejects malformed or oversized metadata before authorization and
  guarantees it is not sent to providers.
- The control plane treats the enclave as authenticated but still validates at
  its persistence boundary.

The effective tag set is frozen on the gateway authorization. Settlement may
echo it, but the control plane rejects any settlement whose tags differ from
the authorization. Refunds do not create generation rows.

> 🔎 **FABLE — blocking (money-safety):** do NOT reject settlement over a tag
> mismatch. A rejected settle is a charge that fails to book — that trips the
> settle-outbox lost-charge machinery and pages a human, all for metadata. The
> authorization already holds the frozen, authoritative tag set, so the control
> plane should **ignore settlement-supplied tags entirely** (or better: don't
> carry tags in the settle body at all) and build the generation from the
> authorization's stored tags, logging a metadata-only WARNING with tag_count
> on mismatch. Settlement outcome must be a pure function of money state,
> never of metadata equality. Update §11's "Settlement cannot alter
> authorization-frozen effective tags" test to assert tags-ignored-with-warning
> rather than settle-rejected.

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

> 🔎 **FABLE:** two launch requirements for `group_by=tag:<key>` on a bounded
> scan. (1) Truncation honesty: when the underlying scan is truncated, the
> grouped sums are lower bounds — return `truncated: true` and have any UI
> render totals with the same "≥" treatment we shipped for usage graphs (#142);
> a partial sum presented as exact is precisely the bug class we just fixed.
> (2) Cardinality bound: a tag like `request-id` yields unbounded groups; cap
> at e.g. 100 groups + an `other` bucket + `groups_truncated: true`. Also note
> rare-tag filters over a bounded recent scan can legitimately return empty —
> point users to the future rollup for long-horizon allocation.

`group_by=none|request|generation` returns `tags`, `user`, `session_id`,
`http_referer`, and `app_categories` on each metadata-only event.

Tag filtering is workspace-scoped and applies after API-key/date filtering.
No prompt or response content is exposed.

### 6.4 Generation metadata

`GET /v1/generation?id=...` returns the same tag and attribution fields. The
existing `/generation/content` behavior remains `404 content_not_stored`.

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

> 🔎 **FABLE:** name the tension explicitly so implementers don't "fix" it in
> either direction: §10 bans tag VALUES from our own logs (Sentry/Axiom/Cloud
> Logging — keep them out of the #141 scrub surface entirely, not scrubbed),
> while this section intentionally exports them to customer-configured third
> parties (PostHog/OTLP) because export is their purpose. Both are correct;
> the boundary is "our observability: never — customer's chosen destinations:
> by design". One addition: since KEY-DEFAULT tags are set by the key owner but
> exported on every requester's traffic, the key-management UI should show the
> broadcast warning at default-tag creation too.

## 8. Response routing metadata

TrustedRouter continues returning its existing `trustedrouter.routing` object.
When `X-OpenRouter-Metadata: enabled` is present, successful Chat Completions
and Responses also receive an `openrouter_metadata` compatibility object built
from the same selected route and fallback attempts. Streaming sends it on the
final metadata-bearing event before `[DONE]`.

The compatibility object never contains tags, user/session IDs, secrets,
prompts, or outputs.

## 9. Error contract

Invalid tags return an OpenAI-shaped `400 invalid_request_error` with a stable
`error.type` of `invalid_tags`. Examples:

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

1. Land the control-plane schema/storage changes with backwards-compatible
   defaults.
2. Land the enclave parser and settlement propagation.
3. Deploy control plane before enclave so old gateways remain accepted.

> 🔎 **FABLE:** add the reverse-skew case to step 3's acceptance: NEW control
> plane + OLD enclave means authorize arrives with no tags while the API key
> has defaults — the CP must still apply key defaults (defaults are CP-side)
> and the idempotency fingerprint for those tagless authorizes must match what
> the old enclave's retries produce (see §3.3 comment). And per the §4 comment,
> settlement propagation (step 2) should shrink to "no tag propagation at
> settle" if the CP builds generations from the authorization's frozen tags —
> that removes the skew surface entirely.
4. Roll enclave regions one at a time and run tagged Chat, Responses, Messages,
   and Embeddings smokes.
5. Publish `https://trustedrouter.com/docs/tagging` and add it to `/docs`,
   `llms.txt`, `llms-full.txt`, and the core sitemap.
6. Release SDK updates after production accepts the field.

Rollback is code-only. Old generation rows deserialize with empty/default tag
fields, and new rows remain readable by code that ignores unknown JSON fields.
