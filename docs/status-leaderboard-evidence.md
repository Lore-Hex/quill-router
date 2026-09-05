# Public status and leaderboard evidence

## Status history

Monthly views read persisted `period=month` rollups for the current month and
the preceding 23 months. They retain the histogram buckets, so historical
percentiles are merged from observations, not averaged from other percentiles.
The public snapshot combines those records with 48 hours of hourly rollups and
the bounded live sample. No historical samples are invented or rewritten.

Monthly reads have a 5,000-row ceiling and reject a truncated result rather
than publishing partial history. Monitor cardinality before raising that limit.

## Monitoring visibility

`TR_SYNTHETIC_STATUS_PROBE_TYPES` declares transaction probes run by the separate
monitor plane. It does not enable probes or grant permissions. The GCP public
surface declares authorize, settle, fallback, Chat PONG and Responses PONG
without receiving their API key or billing token.

Declared probes remain visible without credentials. Missing fresh evidence
degrades the coverage banner; it does not fabricate requests or change the
router-core uptime denominator. Provider-only failures remain separate from
router-core availability.

## Leaderboard windows

The default recent sample keeps its existing 24-hour measurement window and
ranking floors: 10 model samples, 30 provider samples, and 3 TTFT measurements.
`/leaderboard?window=7d` is a separately labeled evidence view, not today's
availability. The worker samples at most 30 observations per provider/model/source,
500 per provider, and 10,000 overall. Selection does not filter out failures.
Random probe scheduling and probe counts are unchanged.

The seven-day query is worker-only, with two threads, a 256 MiB query-memory
ceiling and a 15-second execution limit. Failure emits
`leaderboard_evidence_build_failed`, leaves its prior snapshot to expire, and
does not prevent the current status or recent leaderboard from publishing.
Public requests never scan seven days of raw telemetry.

Configuration-only routes are visible but unranked, with unknown availability
when there are no eligible attempts. Explicit model-validation errors are kept
separate from actual 429/5xx failures. Generic "service unavailable" text must
not reclassify provider downtime as a missing model. Reasoning-first families
get bounded completion headroom across hosts; no automatic retry hides failures.

## Rollout checks

1. Publish the committed public snapshot worker using the reviewed deployment
   helper. Its rollback gate now also requires a fresh `leaderboard_evidence`.
2. Roll out the public application and the separately scheduled probe image.
   A frontend-only rollout does not update probe request budgets.
3. Check monthly HTML and JSON for every month actually stored, plus histogram
   percentiles. Verify the status components without adding observer credentials.
4. Check recent and seven-day pages separately, including sample counts,
   configuration-only rows, pagination, filters and mobile layout.
5. Check subsequent probe results. Retired models and provider account/region
   restrictions require explicit catalog or entitlement repairs; do not relabel
   them as successful probes or promise a zero error rate.
