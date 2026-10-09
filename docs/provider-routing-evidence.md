# Provider routing evidence

The 2026-10-09 snapshot replaces June-era provider priors. It is a seven-day
sample from the ClickHouse-backed public leaderboard, not a complete request
census and not a comparison of identical model mixes.

Default ranking admits at least 25 availability samples, puts providers with
at least 95% completion ahead of unknowns, and uses measured TTFT within that
group. Degraded sampled providers follow unknowns. Throughput ordering also
requires at least five positive sustained-throughput samples. Latency and
throughput are separate rankings. Missing, future-dated or >14-day-old evidence
is neutral. Current health filters and narrowly documented model-specific
exceptions remain independent.

Refresh with `PYTHONPATH=src python scripts/update_provider_throughput_rank.py --write`.
Review and deploy the generated JSON diff; the script refuses empty, malformed,
future or >1-day-stale source snapshots. It does not change the public leaderboard.

Cloudflare Workers AI has an explicit commercial preference while its credits
are available. It is first only for default eligible credits endpoints, within
the requested model. Explicit provider ordering, price/latency/throughput sort,
privacy and provider filters, model fallback order and BYOK are not overridden.
Remove `_CREDIT_PROVIDER_PREFERENCE` when the credit arrangement ends; this is
not evidence that Cloudflare is the fastest or most reliable provider.
