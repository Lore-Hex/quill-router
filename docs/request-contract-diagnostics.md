# Request Contract Diagnostics

Chat and Responses validate request options before billing authorization. A
rejected request creates no credit hold and does not invoke an upstream provider.

## Compatibility

`usage: {"include": true}` is accepted by both endpoints. In Chat streaming it
requests the same final usage chunk as `stream_options.include_usage=true`.
Non-streaming and Responses usage is returned regardless of this legacy option.
`usage.include=false` never disables billing or explicit stream usage reporting.
Malformed usage options and unknown usage subfields return a parameter-specific
400 rather than disappearing silently.

`prompt_cache_retention` is recognized on both endpoints but returns
`501 not_supported_in_alpha` for non-null values. It is not a universal no-op:
supported retention values depend on the upstream model and retention policy.
Null is treated as absent. Omit this option to use the provider's existing
automatic caching. Do not promise that omitting it disables upstream caching.

References: [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
and [legacy usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

## Retained Diagnostic Metadata

Every enclave contract-rejection log records the request ID, route, status,
bounded public parameter category, and a `parameter_path` when it is a safe
field name. The request-end record provides verified workspace and credential
attribution when identity validation succeeds. Conventional unknown names such
as `usage.future_option` are retained, not reduced to `other` alone.

Parameter paths contain names only. Paths are limited to 100
characters and eight identifier segments, with bounded array indices. Free-form
names, credential-like strings and arbitrary punctuation are omitted. Known
public paths such as `plugins.web-fetch` are also allowed. No request bodies,
prompts or outputs are exported by this mechanism.

Oversized paths are omitted entirely, not truncated. The rejection's request ID,
route, status, and verified workspace/credential IDs remain available even when
the name is dropped. Both the enclave and control-plane diagnostic boundary
enforce the limit.

`value_preview` contains JSON for the rejected configuration value, capped at
100 characters in total, with `value_truncated=true` when a structured preview
has had fields removed to fit. Redaction happens before truncation. Known
configuration booleans/numbers and finite enum values (for example `false`,
`0.7`, `"24h"`, and `usage.include`) are retained. Container previews include
only approved configuration children and mark omitted children `_redacted`.
Prompts, messages, identifiers, credentials, tool definitions, arrays, and
unknown/free-text values are replaced with type-specific redaction markers,
even when they are short. A character cap alone is not a privacy filter.
The control plane independently revalidates previews before export to Sentry;
raw bodies and unredacted values never cross the diagnostic boundary.

The control plane validates paths again before adding them to Sentry context.
Sentry uses the bounded category, not the unknown path, for grouping and flood
control. Sentry is sampled; use enclave logs joined by request ID to inspect all
rejections. Previously discarded names cannot be reconstructed retroactively.

## Release Order

Deploy the control-plane schema and diagnostic handler before the gateway sends
the optional `parameter_path`, `value_preview`, and `value_truncated` fields. Old gateways remain compatible with the
new handler. Never bypass the attestation or multi-cloud rollout gates.
