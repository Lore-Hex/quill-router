These fixtures retain selected rows and fields from provider API payloads
fetched on 2026-10-07. Capability values and native model IDs are unchanged.

| Fixture | Source |
| --- | --- |
| `deepinfra_models_list.json` | `GET https://api.deepinfra.com/models/list` (public) |
| `featherless_models_subset.json` | `GET https://api.featherless.ai/v1/models` |
| `atlas-cloud_models.json` | `GET https://api.atlascloud.ai/v1/models` |
| `fireworks_models.json` | `GET https://api.fireworks.ai/inference/v1/models` |
| `friendli_models.json` | `GET https://api.friendli.ai/serverless/v1/models` |
| `wafer_models.json` | `GET https://pass.wafer.ai/v1/models` |
| `kimi_models.json` | `GET https://api.moonshot.ai/v1/models` |

Tests also remove or negate declarations on these same model IDs to check that
model names, unrelated APIs, and malformed truthy values do not imply support.
DeepInfra's separate priced OpenAI listing and Fireworks's separately sourced
prices are synthetic transport stubs, not capability evidence.
