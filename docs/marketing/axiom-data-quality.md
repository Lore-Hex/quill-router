# Marketing data contract and repair

Schema 5 repairs the first-party `trustedrouter-marketing` projection. It does
not send customer data to advertising networks. The five-minute job remains
separate from inference, authorization and settlement. No additional database
reads are added to those paths. Signup emits metadata using the verified OAuth
profile and user ID it already has, after sending the redirect.

## Identities and privacy

`account_fingerprint` preserves the historical `SHA256('tr-account:' + user_id)`
namespace. `workspace_fingerprint` and `anonymous_fingerprint` preserve their
existing hashes. These are pseudonymous join keys, not raw customer identifiers,
and not proof of anonymization. The existing hashes use domain separation, not a
new secret salt; changing that scheme requires a separately reviewed migration.

Usage is attributed to an account only when the retained workspace/visitor
evidence agrees. Multiple visitors identifying the same account can agree;
conflicting accounts or incomplete multi-visitor evidence stay `ambiguous`.
Missing evidence is `orphaned`. Do not assign all of a team's usage to an
arbitrary visitor or equate workspaces with individual users. Coverage is
reported, not manufactured to hit a percentage target.
For a new signup without any pre-auth touch, `first_touch_basis` explicitly says
`missing_pre_auth_touch` and the journey source is unknown, not an invented
direct visit. Existing direct history is not relabeled without evidence.

OAuth carries an encrypted attribution snapshot in a short-lived HttpOnly
cookie, authenticated against the specific validated OAuth state. No campaign
data or identifiers are added to the provider-facing state URL. Privacy signals
suppress restoration and signup attribution. Email domains are emitted only
from a verified OAuth address; the email itself is never exported. Historical
verification retains its dated evidence basis. Unverified billing emails do not
establish domain ownership.

## Dimensions

First source, medium, campaign, creative, landing and referrer remain separate
from the later touch. Common search/social/AI referrer hosts canonicalize to
channel labels. Explicit creator/campaign source names survive. The raw public
referrer hostname is retained separately, without path, query, email or IP.

Paths are lowercase, query/fragment-free, and trailing-slash normalized. A small
set of public landings remains exact. Dynamic model/provider/blog/docs and
experiment routes use named path families. Private/account paths and unknown
paths remain classified without exposing arbitrary slugs. Lost historical
`(other)` paths cannot be reconstructed from the label alone.

The first observed valid experiment assignment is frozen when its redirect is
visited, even if the original landing preceded assignment. No experiment is
invented for unexposed traffic. Distribution and significance need real traffic.

## Funnel interpretation

- Deduplicate `event_id` using the latest `exported_at` before aggregation.
- Journey snapshots repeat. Reduce them to one current account (or unlinked
  anonymous visitor) before counting customers or summing journey purchases.
- Daily usage uses stable day/workspace/model/provider event IDs. New exports
  replace snapshots analytically; they are not incremental token deltas.
- `first_purchase_at` comes from an observed purchase or retained dated purchase
  evidence. Recovered lifetime snapshots never become additional purchase events.
- Purchases are prepaid credit purchases, not recognized revenue.
- `checkout_started` is a first-checkout milestone, not a record of every checkout.
  Repeated purchases, auto-refill, API stablecoin payments, and starts before a
  reporting window make `purchases <= checkout_started` an invalid invariant.
  `payment_method` and `purchase_path` distinguish newly exported purchase paths.
- Legacy `first_call_started`/`first_call_failed` were browser test clicks, not
  all inbound API requests. Never divide them by server activation counts to
  report API reliability. The paired `onboarding_call_*` events measure the
  welcome test; actual server activation is a separate milestone.

Full gateway first-attempt accounting, including failures before authorization
and cross-region retries, remains a separate gateway/outbox change. It must
cover regional leases and idempotency, avoid high-cardinality unbounded logging,
and avoid adding acquisition-table reads to every API call. This repair does
not relabel historical clicks as server requests or fabricate missing starts.

## Recovery and rollout

1. Run `python -m scripts.axiom_growth.quality_report --end <UTC-cutoff>` before
   deployment, then again with the same cutoff afterwards. Schema is discovered
   first; reports contain only aggregates. Source retention limits apply.
2. Run `python -m scripts.axiom_growth.repair_evidence` for a dry-run. It reads
   only the sanitized GCS checkpoint and at most 10,000 deduplicated Axiom
   historical evidence rows. It never queries Spanner.
3. Use `--apply` between scheduled runs. The tool acquires the worker lease,
   writes a generation-specific backup, enriches missing unambiguous evidence,
   and commits with a GCS generation precondition. Failed commits do not replace
   state. Existing observed IDs/times remain intact. Repaired records carry an
   `evidence_repaired_at` audit timestamp.
4. Deploy the tested worker image using the existing dedicated Cloud Run job
   and immutable image digest. Preserve its service account, secret references,
   CPU/memory caps, network settings and checkpoint. The next cycle reprojects
   retained state, exporting only changed records.
5. Validate every query with `scripts.axiom_growth.dashboards --validate` before
   `--publish`. Confirm two successful scheduled runs and fresh watermarks.

Schema 5 is a projection cutover, not a retroactive collection claim. Missing
browser history, missing pre-auth cookies, unknown domains and ambiguous
workspace identities remain explicit gaps. GCP usage scope is unchanged; do
not describe this dataset's tokens as every cloud's platform totals.

## Token-by-source query

Run over the retained journey history, filtering usage days inside the query.
Replace dates with the desired UTC interval. Deduplication precedes the join.

```kusto
let accounts = ['trustedrouter-marketing']
| where event == 'growth.journey' and isnotempty(account_fingerprint)
| summarize arg_max(exported_at, account_fingerprint, first_source) by event_id
| summarize arg_max(exported_at, first_source) by account_fingerprint;
['trustedrouter-marketing']
| where event == 'growth.daily_usage'
| summarize arg_max(exported_at, _time, account_fingerprint, input_tokens, output_tokens) by event_id
| where _time >= datetime(2026-09-15) and _time < datetime(2026-09-22)
| join kind=leftouter (accounts) on account_fingerprint
| extend Source=iff(isempty(first_source), '(unattributed)', first_source)
| summarize Tokens=sum(input_tokens)+sum(output_tokens) by Source
| sort by Tokens desc
```

Keep unattributed traffic in the denominator. Distinct-account counts exclude
anonymous visitors, while a visitors-to-signups funnel includes them. Do not
silently compare these different cohorts or time windows.
