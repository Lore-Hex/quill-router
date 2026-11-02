# Hixtube

Hixtube's live service uses the Apache-2.0
[Neocloud-in-a-Box toolkit](https://github.com/Lore-Hex/token-exchange-neocloud-in-a-box).
It must run in a separate cloud project, not on TrustedRouter production resources.

- API base: `https://api.hixtube.com/v1`.
- Catalog/pricing: public metadata `GET /models`, Provider Catalog v2;
  inference and discovery canaries require authentication.
- Selected checkpoint: `dealignai/deepseek-v4.1-flash-1017-uncensored-fp8`.
  Preserve this identity; it is not the original DeepSeek checkpoint.
- The later finite graph-profile qualification measured 1,048,576-token context
  and 393,216 output tokens. These require an explicit Catalog v2.1 declaration
  and the gateway's long-request support; the earlier eager profile remains
  8,192/256. Never substitute checkpoint limits for tested serving limits.
- The parser remains model-neutral and also accepts the earlier Qwen CPU pilot.
  Only the live, authenticated catalog and successful canaries establish routes.
- Credential: `HIXTUBE_API_KEY`, secret name `trustedrouter-hixtube-api-key`.
- ZDR under the [Hixtube data-handling policy](https://hixtube.com/privacy.html).
  No persistent prompt/output/tool-content storage or training. Content-free
  billing and operational metadata remain. Prefix/KV caches can persist in
  volatile memory until eviction or shutdown, not on disk.
- Standard TLS only. No confidential-compute or E2EE eligibility; the host
  operator can access workload memory. Never infer attestation from ZDR.
- Current inference location: Verda Finland, rented through Spheron. No
  provider-side geographic failover or request-level region selector. This
  does not pin gateway, client, transit or billing-system geography.
- Exact prices come from the operator-approved catalog, not another provider.
- Credits only; measured integer input/output usage follows normal settlement.

## Activation Order

The integration code alone does not establish a live route. Keep activation held
until the public server and discovery credential are ready. Missing or expired
manifests disable this provider's routes and raise an operator warning; they do
not block unrelated providers' price publication.

1. Independently deploy the bounded Hixtube server, DNS and trusted public TLS. Verify public
   authentication, streaming, token usage, enforced limits and Catalog v2.
2. Place independent credential copies in the approved cloud secret stores.
   Never mount the operator inference key into the public control plane.
3. Deploy the gateway's reviewed `hixtube` registry/egress changes. AWS needs
   its explicit hostname tunnel; Azure needs a sealed bundle before opt-in.
4. Fetch and canary the real catalog, generate the manifest, then publish the
   control-plane integration through the normal CI/deployment gates.
5. Force `provider.only=["hixtube"]`, `allow_fallbacks=false` for the gateway
   smoke; verify usage and settlement without fallback masking a broken route.
6. Before the finite service retires, withdraw the route through a reviewed
   release or qualify a separately reviewed replacement. Its published deadline
   must never outlive the server's admission cutoff. Runtime expiry prevents
   routing after that deadline; it does not provide a replacement or continuous
   availability. Keep provider-local refresh failures visible to operators.

Discovery permits an explicit future retirement no more than seven days away,
matching the node's opt-in weekly serving lease. It never invents or extends
that cutoff. The generated manifest expires at the earliest declared retirement;
catalog refresh is not lease renewal, and a missing cutoff is still rejected.

Public system-root TLS, authenticated JSON/SSE inference and exact usage/charge
reconciliation passed through the canonical, AWS, Azure and Azure Sydney entry
points on October 27, 2026. The published provider checker passed 18 required
checks. These are launch checks, not a completed 24-hour availability or
full multi-domain tau2 evaluation. The generated manifest is public; credentials
remain in independent cloud secret stores.

## Privacy Release Gate

Before publishing the ZDR label, verify the exact serving image has content and
access logging disabled, no swap or disk-backed inference cache, and disabled
crash dumps. A zero core-size limit alone is insufficient when the host pipes
crashes to a collector. Keep the operator audit outside this reusable integration
documentation and re-check these settings after each serving lifecycle change.
The public policy must be live before the catalog flags are enabled.

## Long Completions

Catalog v2.0 retains its 900-second maximum completion deadline. v2.1 explicitly
permits up to 3,600 seconds, while first-token and stream-idle limits remain
unchanged. Synthetic probes still have their own bounded policy; a catalog
declaration does not by itself extend the gateway's request lifetime. Hixtube's
gateway adapter must be deployed before publishing the 3,540-second profile.
Pricing, cache discounts, capabilities and retirement remain explicit catalog
fields and require the same validation and authenticated canaries as before.

The future confidential DeepSeek/GLM deployment needs its own hardware evidence,
verifier integration and approved measurements; it cannot inherit a badge from
this standard-TLS pilot.
