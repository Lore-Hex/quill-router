# Direct provider activation: 2026-09-20

Status: release authorized; implementation and local validation complete. AWS
secret copies and Azure's encrypted bundle are provisioned. GCP provisioning is
blocked by authentication/IAM. No new routes have been deployed yet.

## Verified services

| Provider | Native discovery | Routable local rows | Live checks |
| --- | --- | ---: | --- |
| Redpill | `https://api.redpill.ai/v1/models` | 55 | All 68 priced chat rows probed; GLM 5.1 and 5.2 failed canaries and remain held. Eleven more are excluded by existing routing policy. Streaming GLM 5.3 Flash passed. |
| Meta direct | `https://api.meta.ai/v1/models` | 3 | Muse Spark 1.1, 1.2, and 1.3 returned PONG. Streaming 1.3 passed. |
| General Compute | `https://api.generalcompute.com/v1/models` | 4 | Four priced chat models returned PONG. Streaming MiniMax M2.7 passed. Gemma 4 31B remains held without an official price. |
| Infomaniak | `https://api.infomaniak.com/1/ai/models` | 1 | Ministral 3 14B returned PONG in both modes. Seven other chat models are explicitly coming soon. |

These are provider-direct checks, not production TrustedRouter requests. Streaming
checks required content, termination, and integer usage. No customer prompts were
used. Standard privacy applies; none inherits another provider's ZDR/E2EE status.
There are 74 successful provider canaries but 63 final eligible routes. The nine
Redpill Claude models remain excluded by the existing first-party-only policy;
the Redpill DeepSeek V4 Pro 0813 route does not alter that frozen route set, and
the global GPT 5.4 hold remains unchanged.

Redpill's modern OpenAI models reject `max_tokens`. Initial canaries exposed
that contract mismatch. The shared Python runtime/discovery field selector and
the existing enclave selector now use `max_completion_tokens` for GPT 5+ and
o-series models on Redpill, preserving the caller's bound. Legacy/OpenWeight
models keep `max_tokens`. After this fix and a 2048-token probe bound, 16 of the
initially held 18 routes passed. Stream/non-stream serialization tests cover it.

Redpill has its own credential and identity, independent of Phala. Phala discovery
stays on `inference.phala.com`. The new `meta-direct` provider calls `api.meta.ai`;
the existing `meta` provider via OpenRouter is unchanged during this rollout.

## Automatic discovery and prices

The four ready providers use the shared direct-provider discovery, canary,
manifest, freshness, and coverage gates. They are registered in hourly refresh
and native discovery checks. Missing prices and failed canaries cannot route.

- Redpill: prices and cache rates embedded in its authenticated models response.
- Meta: [official Standard tier](https://dev.meta.ai/docs/pricing-rate-limits).
  Contributor tiers use customer data for training and are deliberately excluded.
  Image, audio, and segmentation APIs need separate metering/adapters.
- General Compute: intersect authenticated availability with its
  [published USD price table](https://docs.generalcompute.com/models.md).
- Infomaniak: intersect ready chat models with
  [published CHF prices](https://www.infomaniak.com/en/hosting/ai-services/prices).
  Convert with the current ECB USD/CHF cross-rate; reject stale/invalid FX.
  Product ID `111565` is a non-secret routing coordinate, verified against the
  account's active AI product. Do not replace it with a caller-controlled URL.

## Providers still blocked

### Privatemode / Edgeless Systems

Local official `privatemode-ai@1.56.0` SDK verification succeeded. Encrypted
`gpt-oss-120b` inference returned PONG with 76 prompt and 47 completion tokens.
The verified manifest SHA-256 was
`f53011576c782d61912c884158a781ae5b55f3a49948e577c6292befc2535ab7`.
This is diagnostic evidence, not an independently reviewed permanent trust pin.

The provider is visible but unroutable. An attesting, encrypting transport must
run inside the enclave, with reviewed measurement policy, negative verification
tests, and price discovery. A successful local SDK test does not establish that
TrustedRouter's deployed transport verifies Privatemode. Never fall back to
plaintext or mark ordinary HTTPS as confidential.

### Swisscom

The operator has contacted Swisscom. No Swisscom inference credential was identified
in the operator keyfile. Need the
key variable, entitled Swiss AI Platform project/model endpoint, and current
pricing. Infomaniak hosting Apertus is not a Swisscom route. Keep the provider
visible but unroutable until those inputs are verified.

## Release gate

Local validation:

- Router `ruff check .` and `mypy` pass (385 source files).
- Focused provider and route-scoped privacy tests: 28 passed.
- Full router coverage reached 84.59%, above the 70% requirement. That run
  exposed an obsolete page-wide assertion that all GPT 5.5 routes were ZDR.
  The corrected test scopes OpenAI's claim to its own routes and verifies
  Redpill has no inherited privacy badge. Full-suite confirmation passes:
  11,523 passed, 472 skipped, 11 expected failures in 248.46 seconds after
  rebasing onto main `58f96af8`.
- Full enclave suites pass for `cloud_gcp,llm_multi`, `cloud_aws,llm_multi`,
  `cloud_azure,llm_multi`, `cloud_gcp,llm_vertex`, and `cloud_aws,llm_bedrock`.
- Registry, parent, sealer, and egress parity now include all four providers.
  The exact-match assertions remain enabled. AWS has 89 matched vsock tunnels.
- Parent: 39 tests, lint, format check, and strict types pass.
- Azure deployment script: 97 tests pass; sealed-bundle manifest: 8 tests pass.
- Enclave PR #348 CI passes, including build, vet, lint, and race tests for all
  cloud variants, both sidecars, and deployment-script validation.
- All 112 local browser tests pass. The search test now verifies that a Phala
  search also matches Redpill's policy note, without inheriting Phala's badges.

The operator explicitly authorized deployment and cloud-local distribution of
the four keys below on September 20. No secret values are stored in this report.

| Local key variable | Cloud-local secret name |
| --- | --- |
| `REDPILL_API_KEY` | `trustedrouter-redpill-api-key` |
| `META_API_KEY` | `trustedrouter-meta-direct-api-key` |
| `GENERAL_COMPUTE_KEY` | `trustedrouter-general-compute-api-key` |
| `INFOMANIAK_API_KEY` | `trustedrouter-infomaniak-api-key` |

Credential provisioning:

- AWS: four independent Secrets Manager values verified in `eu-west-1` and
  `eu-west-3`. No enclave has been rolled yet.
- Azure: additive sealed bundle `tr-bootstrap-bundle-direct-20260920`, version
  `4e0e7838167c4318bba19cb3d0246548`, contains 69 entries (existing 65 plus four).
  The generated manifest is committed with the enclave wiring. Running ACI
  containers still use the previous bundle and release policy.
- GCP: `tr-deploy` lacks `secretmanager.versions.add`; the interactive operator
  session requires reauthentication. An attempted create may have created an
  empty Redpill secret, but no provider value was uploaded. After login, inspect
  this explicitly and provision only the four authorized values. Do not grant
  broader IAM privileges or use another cloud as a secret source.

Remaining release sequence:

1. Finish GCP secret provisioning and verify least-privilege runtime access.
   Never load one cloud's credentials from another cloud.
   Re-run native discovery, prices, FX, and canaries before activation; do not
   publish an expired September 20 manifest if rollout is delayed.
2. Require green release-head CI in both repositories, including the enclave
   cloud build/test matrix and registry-to-cloud parity.
3. Roll out enclave transports first through reviewed regional health and
   attestation gates. Publish router catalog/config only after ready transports
   and secrets are available. Do not merge the catalog ahead of its transports.
4. Smoke authenticated discovery, chat, streaming and usage through each cloud;
   check provider identity and that privacy filters exclude these Standard
   routes. Verify settlement through existing billing paths, without ad hoc DML.
5. Confirm the scheduled price refresh succeeds with the new credentials and
   maintains failed-canary/unpriced holds.
