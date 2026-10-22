# Routing snapshot v1

Status: published dark. E2's routing snapshot
([`fast-admission-enclave-direct.md`](fast-admission-enclave-direct.md), E2
and §4 hard parts 1, 2 and 7): a compiler, a signer and golden files, the Go
reader and evaluator that reproduce Python's candidates from them (see "What
Go evaluates"), and a publisher behind a setting that is off (see
"Publishing"). Off, nothing at runtime imports the Python modules: no route,
no publisher. `tests/test_routing_snapshot.py` checks in a subprocess that
importing `trusted_router.main` loads neither module nor the publisher, and a
Go test checks that no other Go package imports the evaluator.

| File | Role |
|---|---|
| `src/trusted_router/routing_snapshot.py` | Schema, `compile_routing_snapshot`, `CatalogInputs.live`, canonical bytes, digest |
| `src/trusted_router/routing_snapshot_signing.py` | Manifest claims, `RoutingSnapshotSigner`, `verify_manifest` |
| `scripts/fastpath/routing_snapshot_vectors.py` | Synthetic catalog, golden files, the test key |
| `fastpath/testdata/routing_snapshot_v1.body.json` | The golden body: exact canonical bytes, no trailing newline |
| `fastpath/testdata/routing_snapshot_v1.manifest.json` | The golden manifest: JWS, test public key, kid, issuer, audience |
| `fastpath/internal/routingsnapshot` | Go: `Verify` (manifest and strict body), `Snapshot.Evaluate` (candidates and estimate), `Cost` |
| `tests/test_fastpath_routing_snapshot_vectors.py` | Staleness, signature refusals, fail-closed compiles |
| `tests/test_routing_snapshot.py` | Live-catalog properties, determinism, field allowlist, dormancy |
| `src/trusted_router/services/routing_snapshot_publisher.py` | The window, the lazy compile, signing, the cached response |
| `src/trusted_router/routes/internal/fastpath_snapshot.py` | `GET /internal/fastpath/routing-snapshot` |
| `infra/routing_snapshot_key.tf` | The signing key's secret and its one reader |
| `docs/runbooks/routing-snapshot-key.md` | Adding version 1, turning publishing on and off, rotation |
| `tests/test_routing_snapshot_publish.py` | Route, authentication, determinism, refresh, key custody, budget |

## What it is for

Go must answer an authorize for the E2 cohort with Python's candidates and
prices, without Python. The snapshot carries the catalog facts that decision
reads, frozen for a window of at most 24 hours, so that Go can evaluate any
instant of that window as Python would at that instant. It carries no
per-workspace or per-key input (BYOK presence, broadcast destinations, key
caps): those come from the separate per-workspace cache and key snapshot.

## Byte contract

- The body is `json.dumps(model_dump(mode="json"), sort_keys=True,
  separators=(",", ":"), ensure_ascii=True, allow_nan=False)`, the
  `billing_snapshot.canonical_bytes` encoding. Printable ASCII, no trailing
  newline. Go hashes the bytes it received and never re-serializes them.
- Every number is an integer. Floats are refused by the strict field types
  and by `allow_nan=False`.
- Every object has a fixed field set (`extra="forbid"`); every field is
  present, with `null` for absent values.
- Sets are sorted lists. Lists Python iterates in order keep that order:
  `models` and `endpoints` in catalog (registry) order, `regions` in
  configured order, decision chains and expansions in Python's order.
- Instants are integer Unix seconds, except the ranking window, which is
  integer microseconds because Python compares the evidence's millisecond
  timestamp against a microsecond clock.
- `parse_routing_snapshot` validates a body through the strict wire decoder
  (`billing_snapshot.Frozen`: duplicate keys, surrogates and non-UTF-8 are
  refused), and the manifest check also requires the body to be its own
  canonical encoding.

## Schema

Top level: `v` (1), `kind` (`"tr-routing-snapshot"`), `compiled_at`,
`not_after`, `epoch_seconds` (3600), `source_revision`, `plane` `{cloud,
region}`, `settings`, `providers`, `models`, `endpoints`, `aliases`,
`requested_models`, `decision_chains`, `ranking`, `fees`. The snapshot is
valid on `[compiled_at, not_after)`; past `not_after` Go passes every request
to Python.

**settings** (the allowlisted Settings values authorize reads):
`regions` (`regions.configured_regions`), `primary_region`, `region_payload`
(exactly `regions.region_payload`, the response's `regions`: `id`, `name`,
`primary`, `enabled`, `api_base_url`, `control_plane_url`), `stage_d`
(`eligibility_enabled`, `heartbeat_enabled`, `require_heartbeat_declaration`,
`pilot_workspaces` sorted), `request_record_write_mode`. Region choice is
`choose_region`: the requested region when it is configured, else
`primary_region`. The chosen region is what the response reports; the region
a candidate's availability is checked against is a different value (see
`gateway_regions` below).

**providers[]**, sorted by slug, every provider: `slug`, `name` (the
response's `provider_name`), `headquarters_country` (`provider.jurisdiction`
matches only `"US"`; null never matches), `byok_storage_slugs`
(`provider_compat.byok_storage_provider_candidates`, the order BYOK rows are
looked up in).

**models[]**, catalog order: `id`, `provider`, `supports_chat`,
`supports_embeddings`, `supports_decide` (the three facts authorize branches
on), and `context_length` (`Model.context_length`, the window
`routing.candidates_fitting_context` holds a chat request to; 0 is unknown,
and the filter keeps such a model whatever the request's size). The window is
the model's, not an endpoint's: the filter reads nothing else, and no
max-output field. It is a registry fact fixed at import, so it needs no epoch.

*Adding `context_length` to v1.* It is an additive field inside v1, not a v2.
No snapshot has been published or consumed: nothing compiles one on a
schedule, and the Go reader is not wired. Both decoders take exactly the
schema's fields, so the change fails closed in both directions: the readers
before it (`parse_routing_snapshot` with `extra="forbid"`, Go's `Parse`)
refuse a body that carries `context_length` (an unknown field, checked
against the merged code: `$.models[0].context_length: unknown field` and
pydantic's `extra_forbidden`), and the readers after it refuse a body without
it (a missing field; `TestParseRefusals` holds Go to it). A refused snapshot
is never evaluated; every request goes to Python. A v2 would rename the
manifest `typ`, the golden files and every reference for no reader's
benefit. Once snapshots are published, a shape change bumps `v` and `typ`
instead, so a reader can tell a newer snapshot from a corrupt one in its
logs; the fail-closed property does not depend on it.

**endpoints[]**, `MODEL_ENDPOINTS` order: every endpoint the registry built
at its import instant (`catalog_registry.CATALOG_RESOLVED_AT`), including ones
whose provider manifest has expired and ones retired after that instant. The
import drops endpoints already retired then, so the compiler refuses unless no
retirement falls in `(compiled_at, CATALOG_RESOLVED_AT]` (see below): every
endpoint absent from the snapshot is therefore retired, or excluded for a
reason that does not depend on time, at every instant of the window.

- `id`, `model_id`, `provider`, `usage_type` (`"Credits"` or `"BYOK"`),
  `upstream_model` (`upstream_id or model_id`, the response's value).
- `supported_parameters` and `input_modalities`, sorted; `input_modalities`
  is `["text"]` where the catalog has none (`endpoint_supports_input_modalities`).
- `privacy`: `stores_content` (`endpoint_stores_content`, for
  `data_collection=deny`) and `meets`, the requirement values of
  `PRIVACY_TIER_*` (0 to 3) for which `endpoint_meets_privacy_requirement` is
  true. Any other requirement value matches nothing.
- `gateway_regions`: available from gateway region `r` iff
  `(only is null or r in only) and r not in deny`
  (`provider_model_available_from_gateway_region`). `r` is the request body's
  `region` exactly as sent, even when this plane does not configure it (a
  request naming `europe-west4` to a plane without it still meets Sakana's
  exclusion), and only when the body names none is it the chosen region.
  When `requires_explicit_origin` (Bedrock), a body without a region gives
  `""`, never the control plane's region (gateway.py: `body.region or ("" if
  bedrock else region)`).
- `catalog_valid_until` (signed seconds; the expired-manifest sentinel is year
  1, a negative value) and `retired_at`: the endpoint is available at `t` iff
  `(catalog_valid_until is null or t < catalog_valid_until) and (retired_at is
  null or t < retired_at)`. That is `catalog_is_current(at=t) and not
  provider_model_retired(..., at=t)` exactly for each listed endpoint at every
  instant; with the completeness above, the available set is exact for every
  `t` in `[compiled_at, not_after)`, so availability needs no epochs.
- `epochs[]`: `starts_at`, `headline` `{prompt, completion}` (the endpoint's
  effective headline rates, which `sort=price` and `max_price` compare), and
  `price`, exactly `stage_d.endpoint_pricing_candidate(effective_endpoint(e,
  at=t))`: the frozen Stage D candidate, which is also what the estimate and
  `candidate_prices` are computed from. The price at `t` is the last epoch
  with `starts_at <= t`. Epochs start at `compiled_at` and at whole UTC hours;
  an epoch is listed only where the price changes, so most endpoints have one.
- `response`: `inference_location` (exactly
  `provider_locations.inference_location_metadata(provider, model_id)`) and
  `wafer_zdr_required` (`_gateway_provider_route_payload`).

**aliases**, the model-id rewrite chain and provider-filter spellings. A
model string resolves as `routing.canonical_model_id`: strip the first
matching `variant_suffixes` entry (`suffix`, and the route preference
`preference` = `value` it forces); apply `router_prefix_rewrites`; replace the
part before the first `:` by `router_base_rewrites`; then return
`alias_targets[id]` if present, else `id` if it is a model, else for `id` and
`id` with `dated_suffix_pattern` removed, that string or
`bare_vendor_prefix + it` if it is a model; else `id` unchanged. A provider
filter item is lower-cased, `_` and spaces become `-`, then
`provider_group_aliases` (several slugs) or `provider_aliases` (one) apply; a
slug in `router_provider_slugs` or not in `providers` is a 400.

**requested_models[]**, sorted by id: each canonical id whose meaning is not
"itself, with no overrides": meta models, privacy aliases, private proxies,
the US-only and green models, the refused orchestration models. `expands_to`
(the model ids, in order), `refused_status` (non-null: the request fails with
that status and Go passes it to Python), `private_proxy` (the response's
`response_model` is the requested id), `overrides` (`usage`,
`renewable_only` (the renewable provider slugs to intersect `only` with),
`provider_jurisdiction`, `privacy_requirements`, `order`, `only`; null is
"not assigned"). For a request's `model` and `models`, in order: strip and
resolve each (a variant suffix assigns its preference); an unlisted id
contributes itself; a listed one contributes `expands_to`, assigns its
non-null overrides (later models win) and adds its `privacy_requirements` to
the union. No model ids at all is a 400. Which strings enter that list is
request parsing, which the next PR owns: with `allow_fallbacks` true it is
`model` then every `models` entry; with `allow_fallbacks=false`
(`routing._requested_model_ids(include_fallback_models=False)`) `models` is
ignored when `model` is set, and only `models[0]` is taken when it is not.
Capability filtering also derives the pool-selected set: collect expansions
that differ from the canonical requested id, then subtract every model named
directly anywhere in the request. This is Python's `pool_model_ids`; it is
reconstructible from `expands_to` and is not an additional snapshot field.
`tests/test_routing_snapshot.py` holds this rule to
`routing._requested_model_ids` for every listed id, every catalog id and its
spellings, and for model lists.

**decision_chains**: `NAMED_DECISION_MODEL_PROVIDERS`, the pinned host order.

**ranking**: `measured_from_us` and `measured_through_us` (the evidence's
`generated_at` and `generated_at + 14 days`), `unknown_rank` (1000),
`default`, `latency`, `throughput` (the evidence's ranks by provider),
`prepaid_min_rank` and `model_provider_preference` (routing's constants). The
measured rank of provider `p` for sort `s` at request time `now` (in
microseconds) is the `s` table's value (`default` for no sort; no table for
any other sort), or `unknown_rank` if absent or if `now` is outside
`[measured_from_us, measured_through_us]`. Go reproduces
`_sort_endpoint_candidates` with these: key `(model rank unless sort
partition none, provider order rank, sort rank, catalog index)`. With no
sort, the sort rank is the measured default rank, raised for a Credits
endpoint to its provider's `prepaid_min_rank`; when every candidate is of one
model, that model's `model_provider_preference` entry for the provider
replaces it.

**fees**: `standard_markup_bps` (550), `signed_receipt_total_fee_bps` (1200),
`receipt_rounding` (`ceil_ratio`: `ceil(price * (10000 + total) / (10000 +
standard))`), `token_rounding` (`half_up_per_million`).

## The window, and failing closed

`compile_routing_snapshot(at=..., horizon=...)` takes integer seconds. It
refuses (`SnapshotRefused`) rather than approximate:

- **A price that moves off the hour.** Every price schedule today
  (DeepSeek's UTC peak windows and V4.1 cutovers, Tencent's Beijing hours,
  Fireworks, Phala) changes on a whole hour. For every provider and model the
  compiler compares the effective endpoint at each minute of each epoch with
  the epoch's start. Minute resolution is a guard on that assumption, not a
  proof about seconds.
- **A retirement between `compiled_at` and the catalog's import, on either
  side.** The registry is built once, at `CATALOG_RESOLVED_AT`, and drops what
  is retired then; every import-time time dependence goes through
  `provider_model_retired`. `CatalogInputs.catalog_resolved_at` carries that
  instant (floored to the second, which is exact against whole-second
  retirements). The import instant is not in the body, so the body must not
  depend on it: a retirement between `min(compiled_at, catalog_resolved_at)`
  and `max(compiled_at, catalog_resolved_at)` refuses. After `compiled_at`,
  the registry has already dropped endpoints still available at
  `compiled_at`, and with them models and members of the privacy pools.
  Before `compiled_at`, the registry still holds endpoints, models and pool
  members that a process imported after the retirement has dropped (on the
  live catalog around 2026-10-23 00:00, 1823 endpoints and 696 models against
  1808 and 688), so the two would publish different bytes for one window.
  `not_after` is clamped to the first retirement after `max(compiled_at,
  catalog_resolved_at)`, so catalogs imported at any two instants of the
  window hold the same endpoints. A process imported before a retirement
  therefore cannot compile any window starting after it, and the publisher
  answers 503 there until the instance is replaced. The edge's last good copy
  does not cover that gap: every window a process imported before the
  retirement compiled ends at the retirement itself (the clamp above), so at
  the retirement every published copy expires, Go passes every request to
  Python, and no snapshot is published until an instance imported after the
  retirement serves the poll. Retirements fall at 00:00 UTC and deploys are 3
  to 12 hours apart, so that is hours of no fast path, overnight, at each
  retirement. Publishing must not be turned on until the catalog can be rebuilt
  as of `at` (see "Not yet"). The rule is the compiler's, not the publisher's, so no caller can produce
  two documents for one window. (Requiring `compiled_at >=
  catalog_resolved_at` instead would also be correct, but would refuse every
  hourly compile in the first partial hour of a process's life even when
  nothing retired.)
- **A catalog deadline the requested-model expansions straddle.** The
  privacy pools behind `trustedrouter/zdr`, `eu`, `e2e` and `green` read
  catalog currency on the catalog clock (`catalog_data._utc_now`), not on any
  `at`. `CatalogInputs` records that clock before and after the expansions;
  a `catalog_valid_until` between it and `compiled_at` refuses, and
  `not_after` is clamped to the next deadline after both, so the expansions
  hold on the whole window. With fresh provider manifests the next deadline
  is days away and the clamp does nothing.
- **A provider other than the catalog's.** The privacy, BYOK-slug and
  inference-location rules read the global `catalog_data.PROVIDERS` (see
  "What the compiler reads"), so `inputs.providers` must hold exactly those
  entries; otherwise `providers[]` and `endpoints[].privacy` could disagree.
- **Sub-second catalog instants**, unknown requested-model overrides,
  inference-location or pricing fields the schema does not know, references
  to unknown models or providers, and a horizon over 24 hours.

## Signing

The body (about 3 MB for the live catalog) exceeds `detached_jws`'s 64 KiB
token bound, so the compact JWS signs a manifest: header `{"alg":"EdDSA",
"kid", "typ":"tr-routing-snapshot-v1"}`, purpose `routing-snapshot`, claims
`{v, sha256, size, compiled_at, not_after, source_revision, plane, iss, aud}`.
`verify_manifest(token, keys, body, now=..., expected_plane=...,
expected_aud=...)` checks the signature, type, purpose and issuer; that the
audience is the key's and the verifier's own (`expected_aud`) and the plane is
the verifier's (`expected_plane`), so a snapshot signed for another plane or
consumer is refused under a trusted key; that the claims are exactly what the
body implies (parsed, canonical, hashed); and freshness `compiled_at <= now <
not_after`. Ed25519, not HMAC: a shared key would let every verifier forge
routing. `RoutingSnapshotSigner` refuses a key whose kid or public key any
other signing purpose of the process uses (`reserved`), as the settle-ticket
key is kept apart from the speculation-shadow key.

The golden manifest is signed by a TEST key derived from a fixed seed in the
generator (`sha256` of a sentence saying it is a test key); its kid, issuer
and audience say `test`, and the manifest says `"test_key": true`. No
private key is committed, and no production key may be derived this way.
Because anyone can derive it, `RoutingSnapshotSigner` and `verify_manifest`
refuse its kid (`TEST_KID`) and its public key (`TEST_PUBLIC_KEY_B64URL`)
unless the caller passes `allow_test_key=True`, which only the generator and
the golden-file tests do.

## Publishing

Off unless `TR_ROUTING_SNAPSHOT_PUBLISH_ENABLED` is true; `rollout.sh`
renders it false as a literal, never inherited. Off, the route below is not
registered (404) and no publisher is built.

**The route.** `GET /internal/fastpath/routing-snapshot` (and its `/v1`
twin, as every internal route), registered only on the `combined` and
`internal` surfaces, where the gateway routes live, and authenticated by the
internal gateway token (`require_internal_gateway`), the credential the
edge already holds for its upstream. The edge's upstream is the us-central1
`trusted-router` service, which `rollout.sh` deploys; the split internal
surface (`internal_surface.sh`) does not copy the setting, so it stays off
there, and `trustedrouter.com/internal/*` reaches that surface. The answer is
`{"v": 1, "kind": "tr-routing-snapshot-publication", "manifest", "body",
"refresh_at"}`, compact JSON: the manifest JWS, the body's exact bytes in
standard base64 (the verifier hashes the decoded bytes), and when to poll
again (advisory; the manifest's `not_after` is what binds). `ETag` is the
body's sha256 and a matching `If-None-Match` is a 304, so a poll between
windows costs no body. `Cache-Control: no-store`. 503 with `status`
`signer-unavailable` (no usable key), `snapshot-refused` or `snapshot-error`
(both with `retry_at` and `Retry-After`) when no window is current; the edge keeps its last good
copy.

**The window.** `publication_window(now, inputs)` starts a window at the
hour, or at the latest retirement or catalog deadline in `(hour, now]`: the
instants at which a window compiled earlier in the hour ends, because the
compiler clamps `not_after` to them. It ends at the next hour or the next such
instant. Every instance of a revision, cold or warm, derives the same start
for the same instant, and compiles with the same horizon (6 hours, a
constant), the same settings and the same `source_revision`, so every
instance produces identical bytes and, Ed25519 signatures being
deterministic, an identical manifest. A publication is served while
`compiled_at <= now < min(next window start, not_after)`. A refused window is
not compiled again; the next window start tries again. Starting at the
latest event, not at the hour, is what lets a window compiled after a
catalog deadline pass: compiled at the hour, that deadline would fall between
`compiled_at` and the expansions clock and refuse.

**The compile.** 1.5 to 4.5 s of CPU for the live catalog (3 MB) on
development machines; review measured 4.3 s. It runs lazily: the first poll
that finds no current publication compiles under the publisher's lock (single
flight; concurrent polls wait and get the same bytes), in the threadpool
worker of the sync route, never on the event loop and never on authorize.
Only instances the edge polls compile, once a window. A background refresher
would compile on every instance every hour for a reader that polls one. The
compile is CPU-bound Python: on the combined service it holds the GIL against
that instance's authorize requests for its whole duration, so it must be
measured on the release image and the service's CPU shape before publishing
is turned on (`docs/runbooks/routing-snapshot-key.md`, step 5). A failure other than the catalog's refusal (status
`snapshot-error`) is retried after 5 s; a refusal waits for the next window.

**Key custody.** The Ed25519 private key is the Secret Manager secret
`trustedrouter-routing-snapshot-signing-key` (`infra/routing_snapshot_key.tf`),
with one accessor at the secret, the control plane's runtime identity
`trusted-router-control-run@`. `rollout.sh` mounts one version, pinned by its
number and never `latest`, as the file `/secrets/routing-snapshot/signing.pem`,
and renders its public key and kid as literals. No version exists yet: the
version literal is empty, nothing is mounted, and the rollout refuses to
turn publishing on without a version and a public key. The publisher refuses
a mounted key whose public half is not the pinned public key, and a key file
or kid that the async-settle ticket or speculation-shadow signer uses. The
issuer (`https://trustedrouter.com`) and audience (`tr-fastpath-edge`) are
constants. The Go side's trust anchor is the public key and kid, never the
secret: the edge's nodes have no access to it. The operator steps are in
[`docs/runbooks/routing-snapshot-key.md`](../runbooks/routing-snapshot-key.md).

**Logs.** Compiles, publications (instants, size, sha256) and refusals (the
compiler's reason, which names instants, providers or fields; any other
failure by its exception class alone). Never the key, the token or the body.

## What the compiler reads

`CatalogInputs` carries the catalog lists, the expansions, the alias tables,
the rank evidence and constants, and the two clocks. The compiler also reads
module-level rules that are code, not catalog data, and are the same for
every input: `catalog_data.PROVIDERS` and the per-model privacy overrides
(through `catalog_privacy`), the Wafer manifest's ZDR flags, the
provider-contract region tables (Bedrock regions, per-model exclusions), the
lifecycle module's price schedules and retirements, the pricing module's
markup and cache multipliers (through `effective_endpoint` and
`endpoint_pricing_candidate`), `provider_compat`'s BYOK slug rule, and the
provider location declarations and Telnyx location file (through
`inference_location_metadata`). The golden fixture puts its endpoints on real
provider slugs with the real provider entries for exactly this reason.

## What stays out

Never in the snapshot, the golden files, logs or tests: provider or API keys,
BYOK secrets or their encrypted envelopes (only the storage slugs to look
them up by), broadcast destinations (encrypted keys, customer URLs), prompts,
custom models (a hidden prompt is prompt content) and user-provided models,
the live `ROUTING_STATE` (process-local health and session affinity, keyed by
API key hashes), and anything per key or per workspace except the Stage D
pilot list Settings already holds. `tests/test_routing_snapshot.py` lists
every field path the schema may carry and fails on any other, and scans the
live document for key-shaped strings and for URLs other than the region base
URLs and TrustedRouter's provider pages. The golden fixture uses synthetic
prices, models, rankings and settings (`ws_test_pilot_*` workspaces) on real
provider slugs.

## Determinism

The same inputs give the same bytes: twice in one process, and in two
processes under different `PYTHONHASHSEED`s (the test compares digests of the
live catalog compiled in subprocesses with the lifecycle and catalog clocks
pinned).

## Changes from the plan

- No `available` flag in epochs: catalog deadlines (for example 12:05:14) and
  some retirements are off the hour, so availability is carried as the two
  exact instants instead, which is exact at every second.
- `privacy.zdr` dropped: `meets` contains 2 exactly when it is true, and
  `wafer_zdr_required` carries the one response use.
- `byok_storage_slugs` moved from endpoints to providers: it is a function
  of the provider slug.
- `ranking.generated_at` became `measured_from_us`/`measured_through_us`: the
  evidence timestamp has milliseconds, so whole seconds would misjudge its
  edges.
- `settings.auto_model_order` dropped: its only effect is
  `trustedrouter/auto`'s expansion, which `requested_models` carries.
- `constants` dropped (`request_metadata_version`, `content_storage_enabled`,
  `estimated_usage_snapshot_models`): protocol constants of the response, not
  catalog data; the Go handler and its comparator own them.
- `provider_aliases`, `provider_group_aliases` and `router_provider_slugs`
  added: Go cannot validate `provider.only`/`order`/`ignore` without them.
- `requested_models[].private_proxy` added for `response_model`; the green
  model's override is the renewable slug list itself (`renewable_only`).
- `CatalogInputs` is required, and also carries the import instant, the
  expansions clock, the alias tables and routing's rank constants, so the
  golden fixture's catalog is synthetic; the compiler still reads the
  module-level rules listed under "What the compiler reads".

## The candidate pipeline and the routing cases

`trusted_router.authorize_candidates` is authorize's candidate pipeline,
lifted out of `_authorize_gateway_sync_impl` without changing a byte of its
answers (`tests/test_authorize_candidates_differential.py` runs a generated
matrix through main's route and the new one). Its three steps take explicit
inputs:

- `plan_route(request, settings, region)`: routing normalization, the route
  kind, and the refusals the request makes on its own.
- `resolve_candidates(plan, request, region, at, user_model_candidate)`: the
  route type's catalog candidates, narrowed by the gateway-region rule above.
- `finish_candidates(plan, resolved, byok_providers, estimated_input_tokens,
  output_tokens, service_tier, additional_cost_reservation, video_resolution,
  at, user_model_prices, partner_mode, rerank)`: BYOK presence (the provider
  slugs whose credentials the workspace holds), the named decision chain, the
  service tier, operator-funded costs, `allow_fallbacks`, and the model
  estimate, the largest candidate's price at `at`.

The gateway keeps what has I/O or process state: the key, workspace and boot
reads, custom and user models, idempotent replays, the BYOK row read, the
clock reading, and `ROUTING_STATE`, which it passes as `rerank` (applied to
live-routing requests before `allow_fallbacks` keeps the first candidate).
The routing functions the pipeline calls still read the process clocks
(lifecycle, catalog freshness, rank-evidence age), as before.

`fastpath/testdata/routing_cases_v1.json`
(`scripts/fastpath/routing_cases_vectors.py`,
`tests/test_fastpath_routing_cases_vectors.py`) maps requests over this
document's golden fixture catalog to the candidate ids in order, the model
estimate, the normalized service tier and the Stage D reason (with the E2
cohort's fixed inputs in its header), or to the refusal's status, type and
message, with `rerank` absent. It names the snapshot body by sha256, every
instant lies in the snapshot's window, and the test checks that the snapshot
alone admits every candidate and prices every estimate. The fixture's clocks
are pinned, so the hourly price refresh cannot make it stale.

## Not yet

- Request parsing as data (`provider` fields, `max_price`, privacy-tier
  spellings, `require_parameters` aliases): the routing cases exercise it
  through Python, but the snapshot does not carry its tables yet.
- In the routing cases: meta models, decision chains and the full fee
  stack (receipt fee, custom-model markup, additional cost, app markup), and
  `candidate_cost_reporting`, which authorize derives from the stored row.
- Wiring the Go evaluator into the edge: the cohort predicate, the per-workspace
  cache, the response, the shadow comparator. Until then nothing calls it.
- The edge's poll of the published snapshot, its last-good copy and its
  trusted keys (Go).
- **Before publishing is turned on: the catalog rebuilt as of `at`.** The
  registry is built once, at import, and drops what is retired then, so a
  process imported before a retirement holds endpoints and models (and with
  them privacy-pool members and requested-model expansions) that one imported
  after it does not; on the live catalog around 2026-10-23 00:00, 1823
  endpoints and 696 models against 1808 and 688. The compiler refuses rather
  than publish two documents for one window, which leaves no snapshot from
  each retirement until instances imported after it serve (see "The window,
  and failing closed"). The fix is for `CatalogInputs` to rebuild the
  registry's endpoints and models, and everything derived from them, as of
  `at` (or as of the latest retirement at or before `at`), so a warm process
  compiles the same bytes as a cold one and the retirement refusal can return
  to `(at, catalog_resolved_at]`. The weaker alternative is to replace every
  control-plane instance at each retirement (a scheduled restart or deploy at
  the retirement instant). It is weaker because the snapshot's availability
  then rests on an operational action timed to every retirement, including
  ones the hourly price refresh adds: a late or failed replacement, a held
  deploy mutex or a paused rollout reopens the gap; the replacement itself
  leaves a gap of minutes while revisions roll; and it changes nothing about
  the compiler, so any other caller compiling in a warm process still
  refuses.
- Publishing on: version 1 of the key and the switch
  (`docs/runbooks/routing-snapshot-key.md`), after the item above.
- Other clouds' planes: the publisher signs for `gcp` only (E5).
- Out of the cohort, so out of v1: OpenAI priority and auto service-tier
  prices, video tariffs (`output_token_price_per_m_by_resolution`), image,
  speech, transcription and fetch routes, partner and custom-model pricing,
  held prepaid settlement endpoints, timeouts, the abliterate
  estimated-usage reason.
- Live health re-ranking and session affinity (`ROUTING_STATE`); for the
  order, Go answers as the compiling process would with an empty
  `ROUTING_STATE` (the catalog itself is the compiling process's, imported at
  `catalog_resolved_at`), and requests with
  `preferred_max_latency`/`preferred_min_throughput` stay on Python.
- Non-ASCII model strings: Python's `\d` in the dated-suffix pattern matches
  Unicode digits and Go's does not, so Go passes them to Python.

## What Go evaluates

`fastpath/internal/routingsnapshot` is the Go side of this contract. Nothing
imports it yet.

**Verify** checks a manifest as `verify_manifest` does, in its order: the
compact JWS (at most 64 KiB, strict base64url, a header that is exactly the
canonical `{alg EdDSA, kid, typ tr-routing-snapshot-v1}`); exactly one
accepted key with that kid, of purpose `routing-snapshot`; its Ed25519
signature; the public test key (by kid or by public key) refused unless the
caller sets `AllowTestKey`; claims that are exactly the v1 fields with strict
non-negative integers, whose canonical encoding is the signed payload and
whose `iss` and `aud` are the key's; `aud` the verifier's own and `plane` the
verifier's own; `sha256` and `size` of the bytes received (Go hashes them and
never re-encodes); the body's own `compiled_at`, `not_after`,
`source_revision` and `plane` equal to the claims'; and `compiled_at <= now <
not_after`. It does not re-check that the body is its own canonical encoding:
the signer refuses to sign one that is not, and the signature binds these
exact bytes.

**Parse** decodes the body strictly: printable ASCII only, one JSON value, no
duplicate key, no float or exponent, no lone surrogate escape, every object
exactly its schema fields (none missing, none extra, null only where the
schema allows it). It then refuses literals Go does not evaluate (another
version, kind, epoch length, rounding, usage type, `dated_suffix_pattern`, a
variant suffix that is not a sort), a window over 24 hours, and dangling
references. A refused snapshot is never evaluated; every request goes to
Python.

**Evaluate** takes the authorize body's routing fields as the enclave sent
them (`model`, `models`, `provider`, the top-level `allow_fallbacks`,
`route_type`, `region`, `service_tier`, `requested_parameters`,
`input_modalities`, `estimated_input_tokens`, `max_tokens`,
`max_completion_tokens`, `max_output_tokens`), the workspace's BYOK provider
slugs and the instant. It first applies what `schemas.GatewayAuthorizeRequest` does to
them: `str_strip_whitespace` on every string and list-of-strings field (not on
`provider` values or the extra `allow_fallbacks`, which the schema keeps as
sent), and `min_length`/`max_length` on `model` and `service_tier`. Go strips
only the space; a string with any other byte outside printable ASCII, where
pydantic's or Python's whitespace could differ from Go's, goes to Python, as
does a string the schema would refuse. It then answers as `plan_route`, `resolve_candidates` and
`finish_candidates` do with `rerank` absent: the candidate endpoint ids in
order and the largest candidate's price at the instant, or Python's refusal
(status, type and message). `Cost` is
`endpoint_cost_microdollars_from_candidate` on exact integers.
`StageDReason` is `_stage_d_eligibility_reason` for given Stage D inputs. Go
covers the chat resolver for chat models (route types `chat.completions`,
`responses`, `messages`, `embeddings`, `decide` or none), the model rewrite
chain above, `provider.order`, `only` and `ignore` with their aliases,
`allow_fallbacks` (top-level and nested, with their type and conflict
refusals), `sort` (string or object, with `partition`),
`data_collection` (with the soft `deny`), `usage` (`byok`, `credits`),
`jurisdiction` (`us`), `zdr`, input modalities, the context window, the
gateway-region rule, BYOK presence, the `default` service tier and the
refusals these make.

*The context window.* Authorize calls `resolve_candidates` with
`prompt_tokens` = `estimated_input_tokens` (plus a custom model's hidden
prompt; custom models are outside the cohort) and `completion_tokens` =
`max_tokens or max_completion_tokens or 0`. The prompt estimate is a number in
the authorize body: the enclave counts the messages (chars/4) and sends it.
Go receives that number and does not reproduce the heuristic; it is the same
number Python routes and prices on, so there is nothing to derive. Go then
does what `candidates_fitting_context` does, on the sorted candidates before
the gateway-region rule: keep a candidate whose model's window is 0 or at
least prompt plus completion; if none is kept, refuse with OpenAI's
context-length error (400 `context_length_exceeded`, `param` `messages`)
naming the largest window among the candidates, in the wording with or
without the completion as Python's has it. The estimate prices
`output_estimate`: `max_tokens`, else `max_completion_tokens`, else
`max_output_tokens`, else 512. A `max_*` below 1 or a negative prompt
estimate (which the schema refuses) and a prompt plus completion beyond int64
go to Python.

Evaluate assumes absent the inputs of `authorize_candidates` that are not in
its request, and whoever wires the cohort predicate must pass a request
carrying any of them to Python: `additional_cost_reservation` (a hosted tool or
media reservation), a partner billing mode (`partner_mode`), a custom model or
a user-provided model (`custom_model`, `user_model_candidate`,
`user_model_prices`), `video_resolution` and `video_allowed_providers`, and
the process-local health re-rank (`rerank`, which a live `ROUTING_STATE`
supplies; Go answers as an empty one would).

It passes to Python, with the reason, and never guesses:

- An instant outside `[compiled_at, not_after)`; token counts the schema
  refuses, or whose sum leaves int64.
- Other route types (images, videos, audio, fetch, orchestration
  sub-routes), and a requested model that is embeddings-only or
  decision-only (the other resolvers).
- Any model resolving to a `requested_models` entry, a decision chain, an
  `alias_targets` target, or an unlisted `trustedrouter/` id; a
  `system1models/` or `system1models-eu/` model
  (`canonical_router_model_id` rewrites these to `decisionmodelsio`, which
  `router_prefix_rewrites` does not carry); an empty or non-printable-ASCII
  model string.
- `provider` fields and spellings outside the list above: `min_privacy`,
  `max_price`, `preferred_max_latency`, `preferred_min_throughput` when not
  null, `require_parameters` other than `false`, `usage_type`, `billing`,
  `country` and the other jurisdiction keys, alternative spellings (`prepaid`,
  `usa`, ...), an unknown field, any non-printable value, and a provider slug
  the snapshot does not list (Python refuses only slugs its process does not
  know; see below), and an order, only or ignore list with a null, non-string
  or blank item (Python validates the whole list, and refuses such an item,
  before it looks up any name).
- On `chat.completions`, `responses` or no route type, any non-empty
  `requested_parameters`: `CAPABILITY_REQUIRED_PARAMETERS` is not in the
  snapshot.
- `auto` or `priority` service tier with an OpenAI candidate (priority
  prices), and `priority` over 272,000 prompt tokens. That bound is
  `OPENAI_PRIORITY_MAX_PROMPT_TOKENS`, copied into Go; the cases pin it only
  to the extent that a 1,000-token priority request is refused as unavailable.
- An estimate, or a `sort=price` headline sum, outside int64.

Against `routing_cases_v1.json` (198 cases), Go answers 124 and refuses 49
exactly as Python (candidates and order, estimate, service tier and Stage D
reason; or status, type, message and param), and passes 25 to Python. The test
lists each passed case with its reason and fails if Go answers one of them or
passes any other:

| Passed | Cases | Reason |
|---|---|---|
| 4 | `*+confidential` | `provider.min_privacy`: privacy-tier spellings |
| 4 | `*+max_price` | `provider.max_price`: decimal parsing |
| 4 | `*+perf` | `provider.preferred_max_latency`: thresholds, and stays on Python |
| 2 | `required_parameters`, `missing_parameters` | `provider.require_parameters`: its aliases |
| 1 | `route:images` | the image resolver |
| 2 | `router_name_then_blank_item`, `router_name_then_null_item` | a malformed provider list |
| 4 | `*+group_alias` | `google` names `google-vertex`, which the fixture omits |
| 4 | `*+unknown_provider` | `nope-provider` is not in the snapshot |

The last two rows are the fixture, not the rule. The fixture snapshot lists 13
providers, but the case generator runs routing against the process's whole
`catalog_data.PROVIDERS`, which knows `google-vertex`: so a slug missing from
the snapshot is not evidence Python refuses it, and Go passes it. A live
snapshot carries every provider, where the doc's "a slug not in `providers`
is a 400" holds; refusing there needs the fixture made complete first
(patching `routing.PROVIDERS` in the generator), which changes these eight
answers.

The generator calls `resolve_candidates` with the prompt and completion
tokens as the gateway does, and prices `output_estimate` (a case's
`output_tokens` must equal it). Fourteen `context_*` cases cover a request
that fits, the limit exactly and one over (with and without a completion),
`max_completion_tokens`, `max_tokens` over it, `max_output_tokens` pricing
without counting, a window that excludes some models and the first model,
one that excludes all (the error names the largest), the window of the
filtered candidates only, an unknown window kept, and the context error
before the gateway-region rule. The fixture's models now have three windows
(65,536, 32,768 and 131,072) and Gemini an unknown one (0), so that no
earlier case's answer changed. The generator also builds each case's body
through `GatewayAuthorizeRequest`, as authorize does, so the cases see its stripping and its extra fields; no
earlier case's answer changed. It gained twelve cases for the top-level
`allow_fallbacks` (false, true, agreeing, conflicting, not a boolean, the
nested one not a boolean, after a router name), malformed provider lists after
a router name, and spaced `region`, `model`, `models` and `route_type`; and
seven cases (`order_reorders`,
`order_ceiling_without_fallbacks`, `order_and_only_disjoint`,
`prepaid_floor_across_models`, `variant_suffix_over_body_sort`,
`variant_suffix_later_wins`, `latency_table_across_models`) because in the
fixture provider order, the prepaid floor, a variant suffix's sort, the
`only` and `order` ceiling and the latency table were tied with what a wrong
rule would give: mutating any of them in Go left every earlier case passing.
Still unpinned by any case, for want of a fixture that separates them: the
single-model condition on `model_provider_preference` (applying it to
several models orders this fixture the same), availability
(`catalog_valid_until`, `retired_at`: the compiler clamps the window so no
fixture endpoint changes inside it), and the rank evidence's age bounds (the
fixture's evidence spans the whole window).

`tests/fixtures/usage_cost_vectors.json` holds `Cost` to Python on all 29
vectors (cache reads and writes, tiers, the private tier basis, rounding
before the sum, the one-microdollar minimum, a rate above 2^53).

## Found while building

A dated spelling of an alias key that is itself a catalog model escapes the
alias: `trustedrouter/confidential-2025-04-14` resolves to
`trustedrouter/confidential`, not `trustedrouter/e2e`, and so expands to the
e2e model pool without the Confidential privacy requirement (3) or the
`tinfoil,phala` order that `trustedrouter/confidential` gets. The snapshot
reproduces Python exactly (that id has its own `requested_models` entry); the
routing behaviour itself is a separate fix.

`canonical_router_model_id` rewrites `system1models/` and `system1models-eu/`
to `decisionmodelsio/` and `decisionmodelsio-eu/` before anything else; the
snapshot's `router_prefix_rewrites` carries only `nyte/`, and
`tests/test_routing_snapshot.py`'s spellings include no `system1models` id, so
the compiled alias table and routing disagree there. Go passes those strings
to Python.
