# Model request capabilities

`GET /v1/models` publishes the same five fields at
`trustedrouter.capabilities` and `trustedrouter.endpoints[].capabilities`.
Endpoint discovery also publishes `data[].trustedrouter.capabilities`.
The public contract is at `/docs#model-capabilities`.

## Rules

- Per endpoint, `reasoning_effort` is a verified non-empty list, `[]` for
  verified rejection of all effort values (Mistral Large), or `null` for
  unverified support. A generic parameter name does not establish an enum.
  Reviewed vendor contracts and documented gateway normalization establish
  accepted values, ordered `none, minimal, low, medium, high, xhigh, max`.
  The string `none` is a value; `null` means not verified; `[]` means do not send.
- Endpoint `tools` and `seed` are membership checks in the corrected route's
  `supported_parameters`; `vision` is image membership in its corrected
  `input_modalities`. They describe function
  tools, a supported seed control (not guaranteed determinism), and image input.
- Model `tools` and `seed` match the final model row's `supported_parameters`;
  `vision` matches its final `architecture.input_modalities`. Model declarations
  are preserved alongside current endpoint declarations, so these discovery
  flags can be broader than the individual routes.
  Model effort is the union of verified lists, or `[]` when every endpoint
  is verified to reject effort, otherwise `null`. An empty endpoint pool
  gives `null` effort and false confidential availability; the other booleans
  still reflect model declarations. Expired/retired routes are excluded.
- `confidential` remains an availability union using the existing routing
  privacy predicate, limited to chat models. Confidential routing requires
  `provider.min_privacy: "confidential"`. Aliases and hidden orchestration
  omit the model-level object because their subcalls and selected models vary.
- Default routing does not filter by parameters. With
  `provider.require_parameters: true`, routing keeps only routes whose own
  `supported_parameters` include every parameter sent. Check per-route values
  first (`trustedrouter.endpoints[].capabilities` and `supported_parameters`),
  because some routes declare less than the model supports.
  It checks names, not effort values or image input;
  clients should pin a suitable provider when values differ. Each field is
  independent and does not promise arbitrary combinations of controls.
- Catalog construction applies reviewed tools/seed booleans and effort lists
  to each endpoint once: verified support adds the parameter; verified rejection
  removes it. Unknown effort leaves the declaration intact. Routing has no
  tools/tool_choice alias, so tool_choice retains its separate declaration.
  Native Mistral/Anthropic routes drop seed (unsupported gateway transport),
  and GPT-OSS 20B/120B drop image input. Reviewed vision declarations update
  route modalities at the same point. Model declarations lose a parameter or
  modality only when every route explicitly rejects it. Routing and both
  catalog responses then read the corrected endpoint fields without additional
  overrides. Reviewed routes now pass the require_parameters filter for verified
  parameters: OpenAI's GPT-5.5 routes keep tools requests; Mistral Large rejects
  reasoning_effort requests when that filter is enabled.

- Private proxies, such as the named decision models, are served through
  another model's routes. They keep their own declarations and inherit only
  the removals that normalization makes to their backing model; their
  architecture is not merged with the backing model's routes.

## Reviewed sources

Provider/model contracts and their source URLs are in
[`request_capabilities.json`](../../src/trusted_router/data/request_capabilities.json).
A row may carry only a `tools` contract taken from the provider's own
documentation. Omit `reasoning_effort` when its accepted values are unknown:
the endpoint publishes `null` and keeps any declared `reasoning_effort`
parameter. Do not write `null` or `[]` for unknown effort; `[]` is an explicit
rejection that removes the parameter. Every row requires a source, and each
provider/model pair occurs only once across all rows. Keep evidence
provider-scoped; native support does not verify every host.

Keep each row's `models` list sorted. During review, run
`uv run python scripts/check_request_capabilities.py` to list reviewed
provider/model pairs with no endpoint in the catalog built from repo data
(exit 1 for stale pairs, 0 when clean; no network). Check
`data/provider_models/*.json`, their `routable` flags, and `provider_lifecycle`
before removing stale pairs; keep contracts for routes that still exist today
even if a future cutover will retire them. To review past all scheduled
cutovers, set `TR_ENVIRONMENT=test` and `TR_LIFECYCLE_CLOCK_OVERRIDE` before
running the tool, using the clock calculation in CI's `test-post-cutover` job.
This is a reviewer tool, not a CI or hourly refresh gate: routine refreshes
and retirements can legitimately leave contracts with no route.

Gateway review: quill-cloud-proxy `2f3a86c1bfb6f105184d1bf3b4548fa6c9ad688c`:

- `internal/llm/byok.go` and `reasoning_control.go`: effort forwarding;
  Gemini 3.7+ `none`/`minimal` normalize to `low` on the documented routes.
- `internal/llm/anthropic_reasoning.go` and `internal/adapter/adapter.go`:
  native effort, disabled thinking, and older Claude effort-to-budget mapping.
- `internal/llm/vertex_gemini.go`: Vertex-specific budget/level translation.
- `internal/llm/privatemode.go`: GLM low/high/max and GPT-OSS low/medium/high;
  unsupported values return HTTP 400.
- `internal/llm/openai_responses.go`: newer OpenAI tool requests use Responses.
  Native Mistral forwards `seed` without renaming it to `random_seed`;
  native Anthropic does not transmit seed.

Tools contracts from provider documentation (2026-10-07, quill-cloud-proxy
`09f09254`): the gateway forwards `tools` for every provider with such a row.
Most go through an OpenAI-compatible client that passes `tools` unchanged:
`newOpenAICompatible` in `internal/llm/multi.go` (among them OpenAI, Mistral,
Grok, Parasail and Morph), `newOpenAICompatibleAt` for Cloudflare Workers AI and
Databricks, Azure's OpenAI-compatible client for non-Claude models,
`newTinfoilAttested`, NEAR AI's attested OpenAI-compatible stream, and the
shared streaming helper in `byok.go` for Kimi, Z.AI and the `directproviders`
table (SambaNova, Scaleway, NVIDIA NIM, Aion Labs, Arcee, Upstage, Reka,
Mancer). Google Vertex uses its native adapter (`vertex_gemini.go`), which
translates tools as it does for the existing Vertex rows. Google AI Studio's
native client handles only image generation.

The Python test in `tests/test_request_capabilities.py` uses
`tests/fixtures/gateway_effort_contract.json` to check advertised enums against
reviewed wire/acceptance vectors. This is an offline catalog consistency test,
not an execution check of the gateway. Gateway-side tests belong in
quill-cloud-proxy; no manual Go overlay check is shipped here.

## Round 3 catalog audit

Offline comparison against the uncommitted round 2 catalog on 2026-10-03:
663 registry models, 578 concrete capability objects, 1,822 routes (Credits
and BYOK counted separately). No upstream capability probes were made.

| Model-level audit | Round 2 | Round 3 |
|---|---:|---:|
| Tools true | 234 | 259 |
| Seed true | 80 | 150 |
| Vision true | 192 | 220 |
| Confidential true | 17 | 17 |
| Verified non-empty effort list | 45 | 45 |
| Verified empty effort list | 1 | 1 |
| Unknown effort (`null`) | 532 | 532 |
| Tools disagrees with supported_parameters | 25 | 0 |
| Seed disagrees with supported_parameters | 78 | 0 |
| Vision disagrees with input_modalities | 28 | 0 |

73 routes across 42 models changed `supported_parameters`: 41 Credits routes
and 32 BYOK routes. Tools changed on 47 routes, seed on 12, and reasoning_effort
on 69 (these sets overlap); tool_choice did not change.

For single-parameter requests with `require_parameters: true` and otherwise
default preferences, the candidate route set changed for 42 distinct models:
29 with tools, 9 with seed, and 40 with reasoning_effort (overlapping sets).
Of these, 26 tools, 9 seed, and 37 effort cases changed whether any route was
eligible. These are catalog eligibility outcomes, before credentials, health,
capacity, or other request filters.

| Example | Effort | Tools / seed / vision / confidential |
|---|---|---|
| GPT-5.5 model | none, low, medium, high, xhigh | true / true / true / false |
| GPT-5.5, OpenAI route | none, low, medium, high, xhigh | true / false / true / false |
| Mistral Large | `[]` | true / false / false / false |
| Gemini 3 Flash Preview | `null` | true / true / true / false |
| DeepSeek V4 Flash | high, max | true / true / false / false |
| DeepSeek V4 Flash 0731 | `null` | true / true / false / false |
| GPT-OSS 120B | low, medium, high | true / true / false / true |

GPT-5.5's model-level seed is now true because its legacy discovery union
contains seed. OpenAI's reviewed route rejects it, and the other routes do not
declare it; the model flag therefore does not promise a route will survive a
seed request with require_parameters enabled.
