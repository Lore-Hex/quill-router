# Requested-route traffic measurement

`Generation.requested_model` preserves the requested route frozen at gateway
authorization (after existing alias normalization, before model selection).
It does not promise the client's original spelling. `model` remains the actual settled model; `provider` remains
the serving provider. Settlement payloads cannot overwrite the requested alias.
No prompts, responses or tool arguments are added to analytics.

The field survives normal settlement, durable settlement replay, the shared
ClickHouse projection and all cloud drains. It is also returned as
`requested_model` in generation details and activity events. Missing legacy
attribution is `null` in the API and an empty string in ClickHouse. Local
non-gateway writers currently remain unknown. Do not backfill the alias from
the selected model or scan production Spanner to guess it.

## Report

Use each cloud's read-only ClickHouse credentials and its private endpoint
(`CH_URL`, `CH_USER`, `CH_PASSWORD`, `CH_DATABASE`). Run once against each
independent cloud database, not once per replica or mirror:

```bash
uv run python -m clickhouse.routing_traffic --cloud gcp \
  --start 2026-10-27T00:00:00Z --end 2026-10-28T00:00:00Z
```

The report includes UTC day, requested route and stored aliases, served model,
provider, billing mode, settled generations, input/output/cached tokens,
customer charges in integer microdollars, latency/TTFT sums and sample counts,
estimated-usage counts, and attribution coverage. `nyte/auto`, `nyte/auto-routing`
and `trustedrouter/auto-routing` group with `trustedrouter/auto`; privacy suffixes
remain separate. Divide latency sums by sample counts for means. Divide cached
input tokens by total input tokens for the token-weighted cache fraction.

This measures successful settled generations, not failed attempts, total API
requests, or unique parent requests for composite models. It does not establish
error rates, provider margin, future demand, or whether another provider can
serve a request's privacy/context/tool constraints. Compare equal windows
before/after a routing change and state attribution coverage and cloud scope.
An unavailable cloud/query is an error, never zero. Historical unknown rows
remain in totals and coverage, not in the auto-route numerator.

## Deployment order

1. Apply `027_activity_requested_model_replicated.sql` with the replicated
   migration runbook on all three GCP replicas. Apply `028` to every AWS/Azure
   standalone analytics database with the native schema installers. Verify the
   column on every target, including drain mirrors, before publishing code.
2. Deploy the committed ClickHouse worker bundles to the active GCP publisher
   and all AWS/Azure drain hosts. Old drains silently project away new fields;
   schema alone does not enable attribution. Verify delivery fingerprints and
   healthy drains. Old payloads default to empty without breaking replay parity.
3. Merge/deploy control-plane writers/readers through the ordinary CI and
   two-cloud release gates. No gateway binary or attestation pin changes.
4. Make a non-sensitive, low-token request through an auto alias; check its
   generation details and ClickHouse row agree on requested alias, selected
   model, serving provider, usage and charge. Run bounded reports per cloud.
   Synthetic checks do not appear in the organic report by design.

Rollback code without dropping the additive column. Leave old rows unknown.
