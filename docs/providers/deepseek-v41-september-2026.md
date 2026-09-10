# DeepSeek September 10 launch

DeepSeek V4.1 Flash launched with new direct API prices on **2026-09-10
04:00 UTC**. The launch-day [official pricing page](https://api-docs.deepseek.com/quick_start/pricing/)
supersedes the earlier customer email: Pro remains V4 Pro 0813 at its own prices
until **2026-09-14 04:00 UTC** (12:00 Beijing time). Only then does rolling Pro
serve V4.1 Flash at Flash prices, until V4.1 Pro launches. Other providers are
unaffected.

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

Until September 14, first-party Pro costs $0.66/$1.98 per million uncached
input/output tokens off-peak, $1.32/$3.96 peak, and $0.022/$0.044 cached input.
The correction changes new quotes, not the immutable prices already authorized.

## Launch-Day Verification

At approximately 15:20 UTC on September 10, authenticated calls to
`deepseek-flash` and `deepseek-v4-pro` both returned PONG with integer usage.
Their response model IDs remained distinct. Official docs now identify Flash as
**DeepSeek-V4.1-Flash**, document vision, and explicitly defer Pro's redirect.
The public rolling Flash name includes V4.1 so users can find it; its API ID
remains `deepseek/deepseek-flash`. No immutable upstream V4.1 ID is invented.

The earlier shared cutoff incorrectly discounted Pro early and removed the
first-party pinned Pro route early. Seventeen regression cases reproduced that
failure before the correction. Separate family cutoffs now drive runtime prices,
discovery baselines, retirement and public schedule metadata. The price-spike
gate accepts only the exact reviewed restoration on the first-party Pro route;
nearby values and other providers remain blocked.

## Prelaunch Checks

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
- First-party pinned `deepseek-v4-flash-0731` retires September 10 and
  `deepseek-v4-pro-0813` retires September 14, even in an already-running process. Other
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
