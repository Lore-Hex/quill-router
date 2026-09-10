# DeepSeek September 10 launch

DeepSeek's customer notice schedules new direct API prices for **2026-09-10
04:00 UTC**. Until V4.1 Pro launches, its rolling Pro API will serve V4.1 Flash
and charge Flash prices. This applies only to DeepSeek, not other providers.

## Provider Rates

USD per million tokens, before TrustedRouter's existing markup/minimum:

| Tokens | Off-peak | Peak |
| --- | ---: | ---: |
| Cached input | $0.003 | $0.006 |
| Uncached input | $0.15 | $0.30 |
| Output | $0.60 | $1.20 |

Peak windows are Monday-Friday **01:00-04:00 and 06:00-10:00 UTC**, with
exclusive end times. Other hours, including weekends, are off-peak. Quotes
are locked at authorization, including requests settling across the cutover.

## Live Checks

Checked at approximately 2026-09-10 01:55 UTC, before the pricing cutover:

- Authenticated `/models` lists `deepseek-flash` and `deepseek-v4-pro`.
- `deepseek-flash`: HTTP 200, PONG, integer usage.
- `deepseek-v4-flash`: HTTP 200; response model is `deepseek-flash`.
- `deepseek-v4-pro`: HTTP 200; response model is still `deepseek-v4-pro`.
- The [official pricing page](https://api-docs.deepseek.com/quick_start/pricing/)
  still describes V4 Flash 0731 and Pro 0813 at the previous rates.

The native Flash rename is therefore verified, but the live API has not yet
proven an immutable V4.1 release ID. Publish `deepseek/deepseek-flash` as a
rolling route; do not invent a dated API ID or change frozen combo presets.
Re-check the response model after launch before claiming the Pro redirect is live.

## Implementation And Verification

- `provider_lifecycle.py` schedules exact runtime prices without relying on an
  hourly refresh landing at the cutover. Both old and new quotes remain reproducible.
- Discovery accepts the verified Flash rename and retains the verified legacy
  rolling alias. Official pricing rowspans are parsed rather than accidentally
  falling back to stale hard-coded prices.
- First-party pinned `deepseek-v4-pro-0813` and `deepseek-v4-flash-0731` routes
  are unavailable at the cutoff, even in an already-running process. Other
  providers and frozen combo definitions are unchanged.
- Pro's public pricing schedule explicitly discloses the upstream Flash redirect.
- Public catalog/picker projections invalidate at scheduled cutovers and peak
  boundaries rather than keeping startup prices indefinitely. Existing HTTP
  cache lifetimes still apply; billing always quotes at authorization time.
- Existing billing markup and cached-input minimum remain unchanged. These
  are provider rates, not an assertion that retail cached input costs $0.003/M.
- 44 new regression cases were run against unchanged implementation: all failed.
  They cover rates, boundaries, discovery, pinned identities, metadata, and
  cached-token settlement across launch. Additional tests cover refresh requirements.

Deployment uses normal CI and regional rollout gates. No database changes,
new secrets, or enclave image changes are required.
