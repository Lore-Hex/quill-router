# Confidential AI E2E Verification, September 8, 2026

## Verdict

**Partial verification passed; do not enable the E2E/confidential filter.**

The public API supplied real, independently checkable hardware evidence. This
is stronger than merely reading a provider-reported `verified` field, but is
not yet a complete proof that reviewed software and the intended model handle
the entire request inside the approved confidential boundary.

No customer prompt or API key was sent to attestation endpoints or NVIDIA.
The provider integration remains ordinary HTTPS; these audit tools are not a
deployed transport adapter.

## Checks Performed

| Check | Result |
| --- | --- |
| Fresh 32-byte challenge and public report | HTTP 200, matching nonce echo |
| Seven Intel TDX quotes: front door, gateway, two workers, two metrics workloads, router | Signatures and pinned Intel root chain passed |
| Fresh Intel revocation/TCB/QE collateral | Passed for those seven quotes; debug guests disallowed |
| CPU boot event logs | Replayed against signed RTMRs |
| Altered CPU quotes | Rejected for all seven targets |
| Live front-door nonce and exact TLS certificate binding | TEErminator check passed |
| Static allowlist, CA evidence and front-door workload stamp | Passed under the observed image tuple |
| Wrong front-door workload | Rejected live |
| Changed allowlist bytes with unchanged JSON meaning | Rejected live |
| NVIDIA worker-0 evidence | Eight signed passing GB110/Blackwell device verdicts |
| Reviewed source-to-live image/weights and complete worker path | Not established |

The report contains eight device entries in each of two worker receipts, but
only eight distinct device identities. They are not sixteen distinct GPUs.
NVIDIA verification was performed on the worker-0 set; do not describe the
worker-1 bundle as independently checked by NVIDIA.

## What The Passing Results Mean

TEErminator revision `8d302348a7f66a2bfd74b24535c6f2f791b2839e` was built from
public source. Its verifier and proxy test packages passed locally. Two config
tests failed on this macOS environment; the audit called the verifier directly
and did not use its persistent user configuration.

The live TLS check enforced the server name `api.confidential.ai`, workload
`c8s-tls-lb`, the sealed allowlist, and the complete MRTD/RTMR1/RTMR2 tuple.
**That tuple was discovered from verified hardware evidence, not independently
approved from a reviewed build.** The resulting `Verified` diagnostic proves
binding consistency under those candidate pins, not application trust. Never
copy those candidate pins directly into a production trust policy.

TEErminator's `verifyEndpointEvidence` uses the offline `teeverify.Verify`
default. This audit separately used `VerifyWithOptions` with
`GetCollateral=true` and `CheckRevocations=true`. A production adapter must
enforce fresh collateral for every relevant CPU/CA proof; the diagnostic's
embedded CA proof was not separately subjected to that online audit.

NVIDIA's signed overall and all eight detached device JWTs were verified
against its official JWKS, including issuer, validity and nonce. Required
device claims passed: secure boot, disabled debugging, successful measurements,
report signature/certificate validation, and driver/VBIOS RIM checks.

The GPU challenge is **not** the raw API nonce. The collector passes the CPU
report-data transcript into `attest_with_nvidia_gpu`, which derives
`SHA256(transcript || "NVIDIA-GPU-EAT-v1")`. With the signed 48-byte CPU
transcript, NVIDIA verification passed. Tests using the raw API nonce, or a
hash of that raw nonce alone, correctly failed; those were diagnostic input
mismatches, not evidence of defective GPUs.

## Remaining Requirements

1. Publish/access the reviewed serving source and release bundle, and verify
   its immutable images and full OS measurements against the live evidence.
   The documented `confidential-dot-ai/confidential-inference` GitHub link
   returned 404 through both git and authenticated GitHub API access during
   this audit. The OS builder is public, but the live image was not rebuilt
   or matched to an independently reviewed measurement manifest.
2. Verify every worker receipt's transcript against the caller's fresh nonce,
   session key and identity. The audit verified worker CPU signatures and
   GPU-to-CPU-transcript consistency, not the complete worker transcript
   construction or enforced gateway-to-worker execution path.
3. Establish the exact model-weight and downstream serving-software policy.
   Admitted launch arguments name pinned images and model revisions, but those
   claims alone do not establish the full runtime path. The captured worker
   configuration names DeepSeek Flash 0731, not every catalog model.
4. Integrate those approved policies into an enclave-side attested transport,
   fail closed before releasing prompts, and test streaming/non-streaming
   inference plus every reconnect and policy mismatch. A standalone passing
   diagnostic does not make the existing HTTPS adapter confidential.

## Evidence

Local artifacts are in `/private/tmp/confidential-ai-e2e-evidence/`; no secret
values or prompt data are in them. Stable identifiers from this audit:

- Public report SHA-256: `40bf5e9e79ae732822a9c13bbef2d934779eb822b6604eb27ebc7dd43b4d7335`
- Sealed policy SHA-256: `05235c60dd61b239f3ac6a367b4fd6469008d766270002a235df4f54b02e217c`
- Declared release: `v0.13.26-static-attestation-20260905`

These identify captured evidence; they are not approved production image pins.

## Sources

- [Provider attestation documentation](https://confidential.ai/docs/inference-api/attestation)
- [TEErminator source](https://github.com/confidential-dot-ai/TEErminator/tree/8d302348a7f66a2bfd74b24535c6f2f791b2839e)
- [CPU verification library](https://github.com/confidential-dot-ai/attestation-go/tree/v0.4.1)
- [GPU challenge derivation](https://github.com/confidential-dot-ai/attestation-rs/blob/main/crates/attestation/src/platforms/nvidia_gpu/mod.rs)
- [NVIDIA GPU verification API](https://docs.api.nvidia.com/attestation/reference/attestmultigpu_1)
