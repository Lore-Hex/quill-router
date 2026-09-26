# Provider geography metadata

Geography is informational. It does not change selection, billing, privacy
filters, or enforce an inference-region pin.

## Catalog and pages

`GET /v1/providers` includes `geography` on every provider, including blocked
providers. The same records appear on `/providers/<provider>`:

- `operator_country`: legal home of the API operator, ISO country code or null.
  The legacy `provider_headquarters_country` field retains this meaning. It is
  not a GPU-country claim or necessarily the physical headquarters.
- `headquarters`: separately reviewed physical HQ, evidence, source URL and
  review date. Missing evidence is null, not copied from incorporation data.
- `inference`: declared locations, scope, dynamic routing, upstream pinning,
  pinning through TrustedRouter, infrastructure-register wording and sources.

Unknown means unconfirmed, not worldwide. Provider declarations are not
exhaustive model placement lists unless their scope explicitly says so.
All 99 current providers have records; not all disclose their inference
countries or headquarters. New providers get explicit unknown fields until
evidence is reviewed.

`GET /v1/models` publishes `trustedrouter.endpoints[].inference_location`.
`GET /v1/models/<model>/endpoints` publishes
`data[].trustedrouter.inference_location`. These records are provider-specific;
the union of endpoints is not a per-request location guarantee.

## Chat and Responses

Direct Chat Completions and Responses replies include the winning endpoint's
`usage.inference_location`. Streaming chat requires
`stream_options.include_usage=true`; read its final usage chunk. Streaming
Responses includes it in `response.completed.response.usage`.

Example shape (illustrative dates and availability):

```json
{
  "usage": {
    "inference_location": {
      "advertised_regions": ["United States"],
      "advertised_region_scope": "model_default_tier",
      "catalog_updated_at": "2026-09-27T00:00:00Z",
      "provider_declared_locations": ["United States", "Europe"],
      "provider_declaration_reviewed_on": "2026-09-26",
      "serving_region": null,
      "serving_region_status": "not_reported",
      "region_pinning_enforced": false,
      "documentation_url": "https://trustedrouter.com/providers/telnyx#inference-locations"
    }
  }
}
```

`advertised_regions` is a dated model-availability snapshot. Telnyx's native
catalog publishes default-tier regions; the existing price/catalog refresh
retains them. Missing, withdrawn, unknown-code or malformed declarations are
not replaced with a narrower inferred location set.

`provider_declared_locations` is broader provider-level evidence. Consult the
linked page for its scope: an announced data center does not establish that
this model runs there. Scaleway, Privatemode, Pearl, Neurometric, ScaleDown and
DeepInfra also have public or provider-submitted declarations.

`serving_region` stays null: no integrated adapter currently verifies an
actual-serving-region response field. Live Telnyx probes did not report one.
Never infer it from HQ, a regional hostname, Cloudflare POP, ZDR/E2EE flags, or
the existing gateway `region` field.

Fallback uses the winner's metadata. Private aliases and custom models omit
it to avoid revealing backing configuration. Orchestration may execute in
multiple locations and does not claim a single serving region. Older enclaves
or control planes may omit this optional extension. Other API shapes do not
yet promise it. Billing and usage-token fields remain unchanged.

## Residency controls

Telnyx documents upstream `region` with `mode: "strict"` on a matching regional
ingress domain. TrustedRouter does not expose or enforce those controls yet.
Do not send these fields expecting a regional pin. Selecting a provider,
`provider.jurisdiction`, or a regional gateway does not establish GPU location.
