# Direct provider activation: 2026-09-20

Status: implementation and local validation only. Not merged or deployed.

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

No Swisscom inference credential was identified in the operator keyfile. Need the
key variable, entitled Swiss AI Platform project/model endpoint, and current
pricing. Infomaniak hosting Apertus is not a Swisscom route. Keep the provider
visible but unroutable until those inputs are verified.

## Release gate

Cloud-wiring edits were denied by the tool safety review. No cloud credentials or
production configuration have been changed. Explicit approval is required for
the four keys below to be distributed to each standalone GCP, AWS, and Azure
deployment. Do not retry the denied operation until that approval is received.

| Local key variable | Cloud-local secret name |
| --- | --- |
| `REDPILL_API_KEY` | `trustedrouter-redpill-api-key` |
| `META_API_KEY` | `trustedrouter-meta-direct-api-key` |
| `GENERAL_COMPUTE_KEY` | `trustedrouter-general-compute-api-key` |
| `INFOMANIAK_API_KEY` | `trustedrouter-infomaniak-api-key` |

The enclave registry and focused transport tests are implemented, but cloud
parity is intentionally incomplete until the approved bootstrap, secret, and
egress wiring is present. The existing parity test must stay enabled and fail
until all deployments cover the new registry entries.

After approval:

1. Complete the standard per-cloud credential/bootstrap/allowlist plumbing.
   AWS additionally needs native Meta, General Compute, and Infomaniak egress
   destinations. Never load one cloud's credentials from another cloud.
2. Run full router lint, types, tests and coverage; run the complete enclave
   cloud build/test matrix including registry-to-cloud parity.
3. Roll out enclave transports first through reviewed regional health and
   attestation gates. Publish router catalog/config only after ready transports
   and secrets are available. Do not merge the catalog ahead of its transports.
4. Smoke authenticated discovery, chat, streaming and usage through each cloud;
   check provider identity and that privacy filters exclude these Standard
   routes. Verify settlement through existing billing paths, without ad hoc DML.
5. Confirm the scheduled price refresh succeeds with the new credentials and
   maintains failed-canary/unpriced holds.
