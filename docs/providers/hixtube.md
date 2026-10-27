# Hixtube Pilot

Hixtube's planned service uses the Apache-2.0
[Neocloud-in-a-Box toolkit](https://github.com/Lore-Hex/token-exchange-neocloud-in-a-box).
It must run in a separate cloud project, not on TrustedRouter production resources.

- Intended API base: `https://api.hixtube.com/v1`.
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
- Standard TLS only. No ZDR, confidential-compute or E2EE eligibility.
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

Ordinary inference through a pinned test certificate has passed; this is not
trusted public DNS/TLS, 24-hour availability or production-gateway evidence.
No generated manifest or upstream credential is included in this integration.
The inactive parser and versioned schema may deploy before the endpoint is
ready; publishing a live manifest still requires every activation gate above.

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
