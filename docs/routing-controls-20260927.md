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
requested model and gateway region. Cross-instance requests may miss it. It is
not a distributed state or content store.
Successful fallback refreshes the selected route. Failure does not refresh TTL.
Explicit provider order disables affinity. A cached route cannot resurrect an
endpoint excluded by privacy, region, capabilities or provider filters.

Per-process limits: 10,000 session hints; 1,024 endpoint/region performance
entries with at most 128 samples each, covering five minutes. No database reads
or writes are added for these hints. Eviction and restart restore normal routing.

Source: https://openrouter.ai/docs/guides/best-practices/prompt-caching

## Strict spending

`budget_strict` is an immutable creation-time API-key flag, default false.
Strict keys use one key counter and include outstanding estimates in each
enforced UTC window. GCP performs a single
conditional counter update followed by a locked point read in the same credit
reservation transaction. PostgreSQL uses a conditional update with transaction
retries. Existing settlement and refund paths release each recorded key hold.
In-flight holds continue to count across window resets.

This is strict estimated-cost admission, not an absolute guarantee about final
provider usage. Alert-only budgets remain alert-only. It can be much slower
than ordinary admission. Per process, at most 16 keys may authorize strictly
at once, one authorization per key. A brief collision retries admission for at
most 250 milliseconds, with at most two waiting callers per key and 16 waiting
callers per process. Excess callers fail immediately with retryable 503; a
caller still blocked at the deadline also receives 503. Admission waiting
consumes the original five-second database work budget and never retries a
transaction that has started. Other request
work and pool acquisition remain covered by existing request/storage bounds.
Generation itself does not retain an admission slot. Window exhaustion returns
429 and UTC reset headers. API clients should back off with jitter on 503.

Local saturation logs `billing.authorize_strict_budget_busy` with the workspace
and request IDs, and `billing.strict_budget_busy` at the HTTP boundary. These
are distinct from `billing.authorize_storage_unavailable` and
`storage.unavailable`: a busy local admission slot is not a database outage or
proof of an exhausted budget. The HTTP 503 alert remains active for both.

There is no schema migration. Never reshard a strict key: validation rejects it.
Keep the flag immutable so in-flight requests cannot cross accounting modes.

Native GoogleSQL checks can run locally with
`TR_STRICT_SPANNER_EMULATOR_HOST=127.0.0.1:19010 uv run pytest -q tests/test_strict_budget_spanner_emulator.py`.
The fixture requires a loopback emulator, uses anonymous credentials, and
creates and drops its own temporary database. It tests each UTC window,
concurrent reservations and BYOK exclusion using the exact production DML.

## Precision evidence

Reviewed per-endpoint weight formats live in `data/provider_precision.json`,
separate from automated pricing refreshes. Exact provider, canonical model and
upstream ID must match. The catalog and model pages expose the primary
quantization, mixed-weight details, KV-cache dtype, pinned model revision,
published serving-code sources and review date. Unknown routes remain unknown.
All reads are cached local lookups, with no inference-path network calls.

These are published configuration snapshots, not per-request proofs of model
weights or precision. `runtime_verified` is false. A verified workload/TLS
identity alone does not establish weight identity. `provider.quantizations`
filtering remains an explicit 501; this change supplies metadata only.

To update evidence, inspect the serving code and its pinned weight config at
the linked revisions. Quantization configs (including nested text configs)
take precedence over a default BF16 dtype; KV-cache flags describe cache only.
Check mixed quantization groups and exclusions. Chutes sources are mutable;
record the reviewed source SHA-256 and exact weight revision. Refresh the
review date only after inspection, never from the price-ingest job.

## Release and rollback

Deploy control-plane support before the enclave wire changes. Default keys
continue to use their existing paths. Monitor authorization 503s, database
contention and key-window 429s separately from upstream provider failures.
Routing hints can be lost without affecting authorization correctness.

After strict keys exist, do not roll back to a release that ignores the flag:
it would lose their admission guarantee. Disable affected keys using existing
key controls if strict admission must be stopped, and use a forward fix. Do not
convert them to approximate mode or reshard them while requests are in flight.
