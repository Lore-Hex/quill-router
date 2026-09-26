# Routing controls and strict budget admission

## Compatibility

OpenRouter documents `provider.preferred_max_latency` (seconds) and
`preferred_min_throughput` (tokens/second). Both accept a number, meaning p50,
or an object with p50/p75/p90/p99 cutoffs. These are preferences, not timeouts.
TR uses recent successful streaming settlements with provider-reported token
counts; unknown routes remain eligible fallback candidates. Explicit provider
order and hard privacy/provider filters take precedence.

Source: https://openrouter.ai/docs/guides/routing/provider-selection

## Cache affinity

Successful explicit sessions can prefer their last successful endpoint. Implicit
text sessions activate only after a discounted provider cache hit. Idle expiry
is ten minutes. The enclave hashes the opening messages using a private,
ephemeral HMAC secret and a tenant key identifier. Only an opaque digest crosses
the enclave boundary. The secret is not derivable from control-plane metadata.
Digests are excluded from serialized authorization, settlement outbox and
activity records. Existing user-supplied session metadata remains governed by
the existing attribution contract.

The cache is best-effort, scoped to enclave and control-plane instances, tenant,
requested model and gateway region. Cross-instance requests and spend-lease
local admissions may miss it. It is not a distributed state or content store.
Successful fallback refreshes the selected route. Failure does not refresh TTL.
Explicit provider order disables affinity. A cached route cannot resurrect an
endpoint excluded by privacy, region, capabilities or provider filters.

Per-process limits: 10,000 session hints; 1,024 endpoint/region performance
entries with at most 128 samples each, covering five minutes. No database reads
or writes are added for these hints. Eviction and restart restore normal routing.

Source: https://openrouter.ai/docs/guides/best-practices/prompt-caching

## Strict spending

`budget_strict` is an immutable creation-time API-key flag, default false.
Strict keys use one key counter, include outstanding estimates in each enforced
UTC window, and bypass regional/local admission leases. GCP performs a single
conditional counter update followed by a locked point read in the same credit
reservation transaction. PostgreSQL uses a conditional update with transaction
retries. Existing settlement and refund paths release each recorded key hold.
In-flight holds continue to count across window resets.

This is strict estimated-cost admission, not an absolute guarantee about final
provider usage. Alert-only budgets remain alert-only. It can be much slower
than ordinary admission. Per process, at most 16 keys may authorize strictly
at once, one authorization per key, with immediate retryable 503 rejection
instead of queuing. The database work budget is five seconds; other request
work and pool acquisition remain covered by existing request/storage bounds.
Generation itself does not retain an admission slot. Window exhaustion returns
429 and UTC reset headers. API clients should back off with jitter on 503.

There is no schema migration. Never reshard a strict key: validation rejects it.
Keep the flag immutable so in-flight requests cannot cross accounting modes.

## Precision evidence

No currently integrated attestation has been established to bind model weights,
revision and serving precision. A TEE flag or verified TLS/workload identity is
not sufficient to invent that metadata. `provider.quantizations` remains an
explicit 501 until a provider supplies verifiable precision evidence.

## Release and rollback

Deploy control-plane support before the enclave wire changes. Default keys
continue to use their existing paths. Monitor authorization 503s, database
contention and key-window 429s separately from upstream provider failures.
Routing hints can be lost without affecting authorization correctness.

After strict keys exist, do not roll back to a release that ignores the flag:
it would lose their admission guarantee. Disable affected keys using existing
key controls if strict admission must be stopped, and use a forward fix. Do not
convert them to approximate mode or reshard them while requests are in flight.
