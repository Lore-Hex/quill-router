# StreamLake Qwen3 30B Thinking retirement

Sentry QUILL-ROUTER-CD (156069038), reported October 25 at 15:01 UTC,
records six failed rotation probes for StreamLake's
`qwen/qwen3-30b-a3b-thinking-2507` route. Its published catalog still marked
the route available.

A direct authenticated fixed-prompt request at 17:17 UTC returned HTTP 400
in 339 ms: `UnavailableModel`, with the explicit message that the model
has been deprecated and is no longer available. Upstream request ID:
`req-f0wskb-1792948633865153322`. This is a retired model, not evidence of
an invalid credential or an attestation failure.

Disable that manifest row and enforce retirement by canonical and native
model ID using the existing provider lifecycle registry. The enforcement
timestamp records our confirmed observation; it does not claim to be the
upstream's original retirement date. Do not silently substitute a different
model. Other providers of the same model and other StreamLake models remain
eligible. The lifecycle rule applies to prepaid and BYOK routes and prevents
stale discovery or pricing data from restoring the retired endpoint.

Both new prepaid/BYOK regression cases failed before the policy was added.
They cover the exact cutoff, native-ID aliases, provider isolation, stale
pricing exclusion, and stale endpoint exclusion.

Verify the route is absent from each deployed public catalog before resolving
the issue. This repair contains no prompts, responses, credentials, balance
changes, or monitoring-threshold changes.
