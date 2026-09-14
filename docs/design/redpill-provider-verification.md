# RedPill Provider Verification

Assessment: September 14, 2026 UTC. Provider slug `redpill`, separate from
`phala`, using `REDPILL_API_KEY` and `trustedrouter-redpill-api-key`.

## Catalog And Billing

`https://api.redpill.ai/v1/models` returned 70 priced models with this account.
Prices are exact decimal USD/token strings. The shared discovery adapter
converts them to integer microdollars per million tokens and retains exact
upstream IDs. New and previously failed routes must pass a bounded chat canary.
The standard catalog markup, authorization, reservation, settlement, refund,
and streaming code is reused. No second ledger or billing implementation.

Discovery does not override existing global model-retirement or first-party
Claude routing policy. A live catalog row is not sufficient to bypass either.
Hourly refreshes use the independent RedPill secret and preserve the existing
price-spike, expiry, and repeated-miss removal safeguards.

The local paid-path pass accepted 67 of 70 models, yielding 56 eligible
credits routes after existing catalog policy. Gemini 2.5 Pro, GPT-5 Nano,
and Qwen3 VL 30B A3B Instruct remain held by failed canaries. Newer OpenAI
models initially failed because RedPill requires `max_completion_tokens`;
both discovery and the shared enclave request builder now honor that contract.
Regression tests demonstrated the failure before the fix. Open-weight models
retain `max_tokens`. Streamed GPT-OSS, GLM Flash, and GPT-5.6 Luna were also tested
through the actual Go adapter, separately from the discovery probes.

## Attestation Evidence

The live service supports ACI at `/v1/aci/attestation?nonce=<fresh-32-byte-hex>`.
The independently built public ACI verifier at commit
`19daf2b7152eeaf1f8be3fd66d261b8c1ce8eac5` completed these checks:

- Intel TDX/DCAP certificate and collateral verification: `UpToDate`.
- Fresh nonce, canonical workload keyset digest, and quote report-data binding.
- Keyset expiry and client-facing TLS SPKI binding.
- App-compose hash replayed into RTMR3:
  `c1602d10cad628529e9fc16389301e9f13206b1d460cb616fb6c42e34a24b039`.
- RTMR3 OS-image hash in the verifier's production allowlist.

These are not a complete confidentiality proof. The CLI explicitly skipped
private-key custody. Its OS policy checks the RTMR3 claim, not independent
reconstruction of MRTD and RTMR0-2. Source provenance reported a repository
commit but `image_digest=null` and `image_provenance=null`; that label alone
does not establish reviewed source to the executing workload. The measured
compose pins a launcher image, not a directly inspectable inference binary.

The published compose also allows `DSTACK_ROOT_PUBLIC_KEY`; its pre-launch
script installs that value into root's SSH authorized keys. This does not prove
that an operator is currently logged in or that SSH is externally reachable,
but exclusion of operator workload/key access remains unproven.

## Release Decision

Do not set `provider_e2ee`, confidential compute, or blanket ZDR from `is_tee`.
The catalog mixes TEE and ordinary upstreams; even a verified gateway channel
does not prove the selected model's entire downstream execution path.

The initial integration is HTTPS/Standard. It is excluded from both privacy
filters. E2EE requires a separately reviewed enclave transport that verifies
approved workload and custody/boot policy before releasing prompts, pins the
attested channel or encrypts to its verified key, requires approved downstream
sessions, and validates receipts against the actual request/response bytes.
Negative tests must reject stale nonce, changed key/measurement, unverified
fallback, absent receipt, and mismatched payload hashes. Never silently fall
back to the ordinary client on attestation failure.

References:

- [ACI quickstart and verification limits](https://github.com/Dstack-TEE/private-ai-gateway/blob/19daf2b7152eeaf1f8be3fd66d261b8c1ce8eac5/docs/quickstart.md)
- [RedPill trust boundary](https://docs.redpill.ai/confidential-ai/trust-boundary)
- [RedPill verification overview](https://redpill.ai/verify)
- [Official logo source](https://redpill.ai/apple-touch-icon.png)
