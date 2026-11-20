# Workspace model and provider origin

Under **Workspace settings > Model and provider origin**, an owner or admin can restrict new inference requests for all existing and future keys in that workspace. Existing workspaces start unrestricted. The public catalog remains a directory, not a list of routes a particular key may call.

| Policy | Model developer and serving company must both be based in |
| --- | --- |
| Unrestricted | Any country |
| U.S. only | United States |
| Western countries | United States, Canada, EU/EEA, United Kingdom, Switzerland, Australia or New Zealand |

The two companies need not be in the same country. A U.S. host serving Qwen is ineligible under both restrictions. A French Mistral model served by a verified U.S. operator can qualify under Western, but not U.S. only. This is about company origin, not the datacenter or base-model ancestry. An unverified developer, opaque broker or unverified operator is excluded. Eligible candidates still have to satisfy other routing, privacy and credit rules. Pins and fallback arrays cannot expand the policy. Requests already admitted can finish.

## Rollout

1. Keep console writes disabled. Deploy the enforcement code to every serving control plane and the Go authorization edge using their normal release pipelines. Verify deployed revisions (including active traffic splits) and the edge's maximum workspace cache age (default 15 seconds).
2. Enable `TR_WORKSPACE_ORIGIN_PEER_CHECKS_ENABLED` in managed configuration on every federated peer. Verify both capability negotiation and home-plane revalidation on a stale unrestricted shadow; during home-plane outages only the intersection of the presets is safe. Check cross-plane latency/error rates.
3. Only after step 2 is verified, enable `TR_WORKSPACE_ORIGIN_CONTROLS_ENABLED` on the console. Use a disposable test workspace to check an allowed route, a denied pin and a denied replay after tightening. Recheck all planes before notifying customers. Do not toggle production customer workspaces.
4. To roll back the UI, turn off new writes first. Leave peer checks and enforcement running while any restricted workspace exists; rolling back enforcement alone would remove the protection. New grants must not bypass the authoritative candidate checks.
