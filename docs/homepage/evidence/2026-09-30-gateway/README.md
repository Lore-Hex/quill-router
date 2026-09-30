# Public gateway attestation capture

Captured September 30, 2026 at 00:16 UTC (September 29, 8:16 PM EDT). This is a dated review artifact, not a live operational claim and not an inference receipt. It is not served by the homepage.

The strict Lore-Hex verifier passed for `api.trustedrouter.com`: Google JWT signature, issuer and audience; measured image in the published accepted set; live TLS certificate binding; fresh caller nonce; TLS exporter binding; debug-disabled guest; and a second fresh attestation over the same TLS connection. No API key, prompt, paid inference, deployment or production mutation was used. Existing installed verification dependencies were reused.

The release record was explicitly `rolling`, with two accepted image digests. The initial check against only its primary target digest failed because the sampled instance still ran the other explicitly accepted build. The second capture used the independently published acceptance set; no observed digest was added by us. `initial-check.json` records that initial outcome. Both public release URL variants and the control-plane release endpoint agreed on the accepted set. The record's target source commit must not be interpreted as the sampled instance's source commit.

`capture.json` records the verifier's pinned source URL and matching SHA-256. `sha256.json` records the captured artifact hashes. The two JWTs and certificate are public gateway evidence; `verification.txt` records the successful checks at capture time. The live socket is now closed; this artifact does not authorize a later connection. No reproducible-build check or provider-specific inference was performed.

To demonstrate a real model response, capture a harmless authenticated inference with receipt opt-in, exact request/response bytes and matching receipt-key attestation. Verify that bundle with the existing receipt verifier. A gateway attestation alone does not establish the selected model/provider, output tokens, billed cost or skipped routes. The repository's synthetic receipt fixtures are not substitutes for this capture.
