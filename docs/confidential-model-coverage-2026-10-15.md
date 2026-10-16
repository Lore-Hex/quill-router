# Confidential model coverage, October 15, 2026

## Scope and release gates

Qwen 3.8 27B has one public catalog/search/SEO listing, `qwen/qwen3.8-27b`.
Reviewed legacy spellings retain their API identities and original provider
routes. The canonical listing includes those routes without altering native
provider model names, tariffs, or admission holds. Unknown variants are not
merged by fuzzy matching.

Tinfoil discovery now includes every explicitly mapped live chat model,
GPT-OSS Safeguard 120B, and Nomic Embed Text. Safety is not synonymous with an
unsupported API: Safeguard supports the existing chat contract. The embedding
route needs the companion enclave dispatch change and an encrypted canary
before its rollout hold is removed. Non-token tariffs cannot enter the token
price index as zero-cost routes.

Privatemode discovery covers its seven published models. OCR and embeddings
have companion encrypted adapters, but remain held pending the gateway release
and encrypted canaries. Existing chat models keep their existing release policy.

Audio transcription, realtime, and Tinfoil TTS rows are discovery inventory,
not working routes. They remain disabled until endpoint-specific transport,
bounded input/session handling, billing, and encrypted live tests are complete.
Do not describe this change as all audio models being live.

## Privatemode trust approval

The live v1.59 manifest differs from the deployed immutable pin. Its separate
source and reproducible-build review is complete; activation still requires
approval of manifest SHA256
`9f5ac6bf38431fb073c2185bd9b344140e776b4f7c32eee595716e9456bdec2a`
and the matching reviewed proxy image. This catalog change does not change the
trusted pin or substitute plaintext HTTPS.

## NEAR diagnosis

Fresh nonce-bound reports were obtained from:

- `qwen3-8-27b.completions.near.ai`
- `qwen3-6-35b.completions.near.ai`
- `qwen3-vl-30b.completions.near.ai`

All three quote TDX module `TDX_01`, security version 7, FMSPC `90c06f000000`.
Intel PCS collateral issued `2026-10-15T20:07:20Z`, expiring
`2026-11-14T20:07:20Z`, marks minimum SVN 11 UpToDate and the level matching
SVN 7 OutOfDate. The strict quote verifier rejects all three. This check
retrieves fresh collateral; it is not a stale TR cache. The dedicated Intel-only
diagnostic submits no evidence to NVIDIA and sends no inference.

NEAR must update the platform TDX module and relaunch the affected workloads,
then provide fresh evidence. A platform pass is only the first gate: workload
policy, GPU evidence, nonce and live TLS binding must also pass. Updating a
verifier merely to accept OutOfDate is not a safe availability fix.

## Sources

- [Tinfoil live catalog](https://inference.tinfoil.sh/v1/models)
- [Tinfoil audio protocol](https://docs.tinfoil.sh/guides/processing-audio)
- [Privatemode models](https://docs.privatemode.ai/models/overview/)
- [Privatemode pricing](https://docs.privatemode.ai/pricing/)

Provider prices were fetched directly. Existing committed tariffs were left
unchanged in this coverage patch; the regular guarded price refresh owns them.
