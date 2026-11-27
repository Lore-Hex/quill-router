# Models catalog interaction analytics

The `/models` explorer posts to the existing `/analytics/events` endpoint. It
uses the existing encrypted attribution cookie and anonymous fingerprint, with
no new identity, storage, page-view event, third-party script, or visible UI.
Homepage catalog events remain separate.

## Measurement contract

All names below receive the `acquisition.` prefix in logs and Axiom.

| Event | Meaning | Additional exported fields |
|---|---|---|
| `models.search_used` | Nonempty typed search after 500 ms idle, blur, or page exit | None |
| `models.search_zero_results` | Subset of those searches with zero matches | None |
| `models.popular_pick_clicked` | Popular-search button activated | `catalog_pick` |
| `models.filter_changed` | A different filter link activated | `catalog_filter` |
| `models.sort_changed` | Sort changed, including reset to Featured | `catalog_sort` |
| `models.model_clicked` | Catalog model title or routing-details link activated | `catalog_model_id` |
| `models.provider_clicked` | Publisher, serving-provider, or loaded comparison link activated | `catalog_model_id`, `catalog_provider_id` |
| `models.comparison_expanded` | Closed provider comparison opened, including reopening | `catalog_model_id` |

Every catalog event has `page_path=/models`. Search text is retained only in
page memory for deduplication: no text, hashes, lengths, search URL, or arbitrary
properties are submitted. The new requests suppress Referer. The existing
search URL behavior is unchanged. GPC/DNT suppress transmission and server
attribution; requests without attribution do not mint an identity.

The endpoint accepts only fixed pick/filter/sort values and model/provider IDs
present in the server's catalog (internal-only models are rejected). Metadata
is scoped to the relevant event. The normal log scrubber still applies.

Initial URL-restored searches, renders, pagination, comparison closes, and
unchanged filter clicks do not emit interactions. Popular picks do not also
emit typed-search events. Repeated normalized input is deduplicated until the
search changes or is cleared. Search outcomes are captured before a later sort
or popular-pick action can alter the result set. Clicks measure intent, not
successful destination loads; expansions do not prove endpoint-load success.

## Analysis and attribution

Count search events and divide zero-result searches by search events for the
observed zero-result rate. Group clicks by the catalog fields to find popular
picks, models, providers, filters, and sort modes. Use distinct
`anonymous_fingerprint` for observed visitor counts, not people counts.

Join catalog interactions to existing `signup_completed`, `api_key_created`,
`first_successful_api_call`, and later conversion milestones on
`anonymous_fingerprint`, requiring the interaction timestamp to precede the
milestone. Existing first-touch and last-touch attribution remain unchanged.
These joins show association, not causal credit. Cookie loss, privacy opt-outs,
blocked requests, different browsers/devices, and ambiguous identities leave
coverage gaps. No raw-query demand analysis is available by design. Initial
URL searches and popular-pick outcomes are outside the typed-search denominator.

`trusted-router-logs` receives the normal Axiom logger. Growth dashboards read
`trustedrouter-marketing`, whose Cloud Logging source filter and worker use
`BROWSER_EVENTS`. This change extends that exact allowlist, Cloud Logging's
field formatter, and the worker's field projection. It does not publish new
Axiom dashboard panels. When querying growth events, retain the established
`arg_max(exported_at, *) by event_id` deduplication before counting retries.

Example source-dataset check (bound the query to the release verification window):

```apl
['trusted-router-logs']
| where event startswith 'acquisition.models.'
| summarize interactions=count(), visitors=dcount(anonymous_fingerprint) by event
```

## Verification and release status — 2026-11-27

Focused endpoint/export/privacy tests and five Chromium interaction checks pass.
The attribution test checks the same anonymous fingerprint across a catalog
interaction, signup, and first successful API call. No full local CI was run,
as requested. Focused runtime mypy and Ruff checks pass; directly checking the
worker script with mypy exposed 15 pre-existing typing issues outside the
repository's configured `src/trusted_router` mypy scope.

A bounded live Axiom query confirmed current acquisition events, including
signup, API-key creation, and first successful API calls. No new catalog events
were present. An explicitly labeled synthetic run through the new local HTTP
route and the existing scrubbed, queued Axiom logger was rejected by Axiom:
`403: user does not have access to resource: ingest with action: create`.
The operator login can query but cannot ingest. This is **not** successful
end-to-end verification of the new events, and this change is not deployed.

Backend/release follow-through:

1. Release the app through its normal CI and deployment gates.
2. Deploy the growth worker through `deploy-growth-sync`, preserving collector
   flags. Its release workflow reapplies the exact source allowlist via
   `scripts.axiom_growth.provision source-filter`; adding Python names alone
   does not update the live Cloud Logging sink.
3. With the authorized service identity, exercise the live catalog using a
   clearly labeled synthetic campaign and confirm each event and its allowed
   fields in `trusted-router-logs`. Check GPC/DNT and the absence of search text.
4. Confirm the same events in `trustedrouter-marketing` after successful worker
   cycles; exclude the verification campaign from product reporting. Do not
   manufacture signup/payment milestones to test the catalog.

No Spanner reads, migrations, money-code changes, new credentials, or changes to
attribution retention are required.
