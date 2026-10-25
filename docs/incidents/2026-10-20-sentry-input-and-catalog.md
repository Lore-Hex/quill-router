# Sentry input failures and stale catalogs, 2026-10-20

## Confirmed causes

- QUILL-ROUTER-BE, issue 154774095: `/v1/activity?date=2026-10-20%27`
  reached a parameterized ClickHouse date filter and generated HTTP 400 upstream,
  exposed as an internal server error. Validate canonical, real calendar dates
  before either activity storage call. This is not evidence of SQL injection.
- QUILL-ROUTER-BD, issue 154773518: Stripe rejected a checkout return URL as
  invalid. The API schema accepted arbitrary strings. Validate absolute HTTP(S)
  URLs for checkout and the adjacent billing portal before contacting a provider,
  preserving Stripe's literal checkout-session placeholder.
- QUILL-ROUTER-BF, issue 154774144: a Unicode `next` path failed Starlette's
  Latin-1 cookie-header encoding. Percent-encode local URL paths, preserve existing
  escapes, and reject control characters and backslashes in redirect paths.
- Catalog freshness issues 146574735, 146574728, and 146574722:
  AkashML was held on the Kimi K3 cached-input price transition; Reka removed the
  documentation price table in favor of authenticated `/v1/models` rates;
  Upstage's published promotion ended at 2026-10-20T00:00:00Z.
- Refresh run 39380883123 stopped waiting after 45 minutes. Its exact-SHA CI run
  39381267520 subsequently passed at 07:29:04Z, about 54 minutes after dispatch.
  Allow up to 120 minutes for queueing and the full matrix, without accepting a
  pending/failed run or bypassing exact-SHA publication and price gates.

## Price evidence

Read the authenticated model feeds directly on 2026-10-20, keeping credentials
out of files and logs:

- <https://api.akashml.com/v1/models>: Kimi K3 input $1.20/M, output $14/M,
  cache read $1.20/M. Approve only the exact cache transition from $0.30/M
  for the existing AkashML native endpoint, not a wildcard price increase.
- <https://docs.reka.ai/pricing>: prices now come from `/v1/models`, denominated
  in USD per token. The live `reka-edge-2603` entry reports $0.10/M for input
  and output, and zero cache-read price. Keep new unreviewed native model
  identities out of this repair.
- <https://www.upstage.ai/pricing/api>: Solar Pro 4 input $0.15/M,
  output $0.60/M and cache read $0.03/M from October 20.

Only these three provider manifests were regenerated. Other providers' prices,
retirements, quarantine decisions, and freshness deadlines were not changed.

## Other alerts

The earlier shadow-session incident is documented in
`2026-10-20-shadow-session-leak.md`. On this follow-up, session count was 1,443
at 09:54Z; Google resolved the session alert at 09:56:43Z. The API-failure alert
had resolved at 08:59:40Z. Cloud Run request logs contained no HTTP 5xx from
09:00Z through the follow-up read. No alarms were loosened and no unlabeled
Spanner sessions were bulk-deleted.

The 07:51Z firewall notification corresponds to the deployment identity adding
`tr-fastpath-edge` at 07:47Z, not an unidentified actor.

Recent provider evidence is mixed and must not be marked globally repaired:

- Relace's DeepSeek route produced upstream 429 capacity errors. A pinned
  production stream completed in 1.35 seconds on this audit.
- Abliterate Clever produced repeated synthetic timeouts and one recorded
  organic 502. A pinned production stream completed in 24.93 seconds, with
  first content at 24.64 seconds. One successful request does not resolve
  intermittent latency.
- QUILL-ROUTER-9T is an unsupported `tools[0].type` rejection before authorization.
  The stored event has no value preview, so its precise tool type cannot be
  reconstructed. Do not silently execute or accept an unknown hosted tool.

The two PONG checks used the dedicated monitor credential, disabled fallback,
sent no customer content, and charged 55 microdollars in total.

## Regression evidence

Before fixes, 37 new input-boundary cases failed; the 45-minute workflow case
and the Reka API-price case also failed against the prior implementation.
After the input fixes, 192 focused management/billing/OAuth tests passed.
Publication still requires the full release gates and post-deploy checks;
local success alone does not mean these repairs are deployed.
