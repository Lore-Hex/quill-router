# Confidential AI Attestation Review, October 22, 2026

## Decision

Keep Confidential AI routes at ZDR. Do not enable `provider_e2ee`, the strict
confidential filter, or the E2E alias for them yet. This is an integration and
serving-policy review result, not evidence of a compromise. No customer prompts,
API keys, or inference requests were sent during these attestation checks.

The previous September audit is historical. The provider has made substantial
changes: public release source, signed release bundles, fresh TLS-bound proof,
and a measured GPU boot gate. The current remaining policy issue is specific;
it should not be described as simply "no attestation".

## What Passed

- Verified the v0.15.15 release bundle with Cosign, GitHub OIDC issuer
  `https://token.actions.githubusercontent.com`, and exact release workflow
  identity `https://github.com/confidential-dot-ai/confidential-inference/.github/workflows/release-bundle.yml@refs/tags/v0.15.15`.
- Built the public TEErminator verifier from source commit
  `327ff74d318a9e650a0cb60285cdc6e76c9a1ecf`.
- Verified the actual `api.confidential.ai` TLS peer using a fresh challenge,
  Intel collateral/revocation checks with only `UpToDate` accepted, and the
  release's MRTD, RTMR1, RTMR2 and exact allowlist bytes.
- Verified the front-door workload `c8s-router-fb21f5cac23c` and operator-signed
  route manifest. Wrong workload and wrong route-digest tests failed closed.

The operator key used in this diagnostic came from live public metadata. It is
a candidate key, not an independently approved production trust anchor.
`/attestation` schema 3 is discovery metadata, not itself fresh nonce-bound proof.
Do not substitute a successful metadata fetch for TLS-bound verification.

## Remaining Serving-Policy Blocker

The signed/live allowlist includes workload `busybox-fd8d9aa63ba2-7859552d`,
image `busybox@sha256:fd8d9aa63ba2f0982b5304e1ee8d3b90a210bc1ffb5314d980eb6962f1a9715d`,
with `/bin/sh /script/setup` or `/bin/sh /script/teardown`. Its arguments,
environment and mounts allow `any`. Pinning the shell image does not pin the
script supplied through a mounted ConfigMap.

The pinned [C8s v0.59.1 policy documentation](https://github.com/confidential-dot-ai/c8s/blob/4c916ecea7221e506988ff9e37725a314a64b562/docs/allowlist-and-capabilities.md)
explicitly describes this residual in issue
[#872](https://github.com/confidential-dot-ai/c8s/issues/872). It also documents
generated RKE2 system entries that allow unrestricted commands, arguments and
mounts for shell-bearing images. This prevents approval of a policy intended
to exclude operator-injected code. No exploitation was attempted.

GPU CC mode and NVIDIA verification are now a measured node boot gate. Source
requires the gate before RKE2 startup and powers off on failure. PCI hotplug is
disabled in the kernel, but the source documents a remaining guest-root manual
PCI rescan concern. This is not independent per-request GPU verification, and
its trust depends on the measured guest enforcing runtime isolation.

## Required Before Promotion

1. Publish a new signed release with helper script contents and mounts pinned,
   or bake them into the measured image; close the unrestricted system-image
   command/mount path. Provide the exact allowlist and source commits.
2. Explain and test the GPU boot-gate/runtime boundary, including the documented
   guest-root PCI rescan residual, and show how the serving workload cannot
   replace a verified GPU or bypass the gate.
3. Independently approve release/workload/operator-key pins and implement the
   attested transport inside TrustedRouter's enclave. Reject partial verdicts,
   stale/revoked evidence, wrong routes, and wrong workloads before sending
   credentials or prompts. Ordinary HTTPS must not be a fallback.
4. Run encrypted inference and negative-path regression tests, deploy the
   reviewed adapter, then promote only routes covered by that verified path.

## Evidence Coordinates

- Release source: [v0.15.15, f555f44f01be5940cad99c81c7c18ecb4edb4c59](https://github.com/confidential-dot-ai/confidential-inference/tree/f555f44f01be5940cad99c81c7c18ecb4edb4c59).
- Measured node source: [C8s v0.59.1, 4c916ecea7221e506988ff9e37725a314a64b562](https://github.com/confidential-dot-ai/c8s/tree/4c916ecea7221e506988ff9e37725a314a64b562).
- Release-bundle SHA-256: `3c875412b8fddda55dfe84e79ec206bc12c22e7acb3c3330afd0ddc31c629905`.
- Allowlist SHA-256: `8de45cdb8f1242d714bb0de35b2f4aceb7eba93b3537219cb20fa029d10eb58d`.
- Observed signed route-manifest revision: `1792615389`; digest
  `f518edf31d5271e7ab4e525f1366bceef2583d83b458d3f23786d3958d98054d`.

These coordinates document a point-in-time test, not perpetual approval of
future releases or every model behind the provider.
