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

The hourly catalog workflow refreshes this snapshot when it is at least 24 hours
old. It publishes the generated JSON only after the same exact-SHA CI and guarded
deployment as the catalog. A failed evidence fetch preserves the previous file
and emits a workflow warning without blocking unrelated prices. The runtime's
14-day expiry remains fail-neutral.

For an immediate reviewed refresh, run
`PYTHONPATH=src python scripts/update_provider_throughput_rank.py --write`.
The script refuses empty, malformed, future or >1-day-stale source snapshots.
It does not change the public leaderboard or generate traffic to improve a score.

The leaderboard's "Limited evidence" label applies only to measurements in the
selected window. It is not an activation gate. A low-volume provider may have
fewer than 30 samples in the 24-hour view while qualifying in the seven-day view
used for routing evidence. Explicit provider selection remains available.

Cloudflare Workers AI is a lower-priority fallback for default prepaid routing
as of 2026-10-11. Cloudflare AI Gateway unified billing is excluded from the
startup credits, so the earlier first-place commercial preference was incorrect.
`_PREPAID_PROVIDER_MIN_RANK` puts it behind healthy and unknown hosts within the
requested model. Healthy Cloudflare routes can still precede measured degraded
hosts; the floor never improves Cloudflare's own degraded rank. Explicit provider
ordering, price/latency/throughput sort, privacy and provider filters, model
fallback order and BYOK are not overridden. Cloudflare stays usable when it is
the only eligible route. Published prices and leaderboard measurements do not
change.
