# Provider routing evidence

The 2026-10-09 snapshot replaces June-era provider priors. It is a seven-day
sample from the ClickHouse-backed public leaderboard, not a complete request
census and not a comparison of identical model mixes.

For chat routing, provider-wide evidence is now an **availability prior only**:
at least 25 availability samples and 95% completion qualify a host as healthy;
unknowns follow, then measured degraded hosts. A fast measurement on one model
does not promote that provider on another model. Current health filters,
explicit provider order, model fallback order and narrowly documented host
exceptions remain independent. Other modalities retain their existing ranking.

## Model and workload ranking

`model_routing_evidence.json` contains seven-day, metadata-only measurements for
each exact model/provider and prompt-size band: up to 4,096, 32,768, 131,072, and
over 131,072 input tokens. A cell needs at least 25 measured TTFT samples.
Only successful, streamed, non-synthetic prepaid generations contribute.
Throughput needs 25 observations with at least 64 output tokens. These success
timings **do not establish a failure rate**. Availability and live health remain
separate. Current evidence is from the GCP analytics plane; regional latency,
cache mix, concurrency, and account-specific/BYOK tiers can still differ.

The gateway's existing input and output estimates choose the workload. Output
budgets up to 256 / 2,048 / 8,192 / above 8,192 use representative lengths of
128 / 1,024 / 4,096 / 16,384 tokens. Default ordering compares TTFT plus estimated
decode duration when all measured hosts in the cell have sufficient throughput;
otherwise it compares TTFT alone. Price is unchanged and remains an explicit sort.

Speed groups treat differences within **20% or 250ms** as ties. Within the same
availability and speed group, **ZDR ranks ahead of Standard**, then measured
routes ahead of unmeasured routes, then stable catalog order. Missing model or
workload evidence is neutral (the first speed group), never borrowed from another
model. Unknown routes therefore keep a privacy/catalog fallback rather than
being permanently starved of traffic. A ZDR preference is not a privacy guarantee:
use `provider.zdr`, `min_privacy`, or the privacy-specific model aliases for a
hard requirement. Those filters still apply before ranking.

The refresh preserves each previously published timing anchor until TTFT changes
by **both more than 20% and more than 250ms**; throughput changes must exceed 20%.
Small cumulative changes eventually cross the original anchor. New/expired cells
start fresh. Evidence expires after seven days and never triggers request-path IO.
Explicit `latency` and `throughput` sorts use the matching model/workload evidence
without the default ZDR tie-break, retaining their existing availability gate;
explicit provider pins and price sorts win.

The existing public snapshot worker builds bounded aggregates every six hours
(30 seconds, 1 GiB, overflow throws, at most 20,000 groups). Its failure cannot
block health publication. `/routing-evidence.json` serves that precomputed payload
and returns 503 when unavailable or over 12 hours old; it never scans raw data.
The hourly catalog workflow reviews a refresh at most daily using
`scripts/update_model_routing_evidence.py --write --min-age-hours 24`. Fetch or
validation failure preserves the prior file. Runtime performs in-memory lookups.

The signed fast-path snapshot contains the same compiled workload tables and
expiry times, with Python/Go differential vectors. Old readers reject the new
field and fall back to Python; rollout must verify the new reader before relying
on fast-path coverage. Roll back through the ordinary release workflow, keeping
health, attestation and privacy gates intact.

## Provider-wide availability evidence

The hourly catalog workflow refreshes the provider-wide snapshot when it is at least 24 hours
old. It publishes the generated JSON only after the same exact-SHA CI and guarded
deployment as the catalog. A failed evidence fetch preserves the previous file
and emits a workflow warning without blocking unrelated prices. This separate
availability evidence retains its 14-day fail-neutral expiry.

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
