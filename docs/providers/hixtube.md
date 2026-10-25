# Hixtube Pilot

Hixtube's planned service uses the Apache-2.0
[Neocloud-in-a-Box toolkit](https://github.com/Lore-Hex/token-exchange-neocloud-in-a-box).
It must run in a separate cloud project, not on TrustedRouter production resources.

- Intended API base: `https://api.hixtube.com/v1`.
- Catalog/pricing: public metadata `GET /models`, Provider Catalog v2;
  inference and discovery canaries require authentication.
- Selected checkpoint: `dealignai/deepseek-v4.1-flash-1017-uncensored-fp8`.
  Preserve this identity; it is not the original DeepSeek checkpoint.
- The tested eager service profile declares 8,192-token context and 256 output
  tokens. A graph profile needs its own verified limits; never copy the
  checkpoint's theoretical context or another provider's capabilities.
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

Ordinary inference through a pinned test certificate has passed; this is not
trusted public DNS/TLS, 24-hour availability or production-gateway evidence.
No generated manifest or upstream credential is included in this PR. Keep it
unmerged until all activation gates above are satisfied.
The future confidential DeepSeek/GLM deployment needs its own hardware evidence,
verifier integration and approved measurements; it cannot inherit a badge from
this standard-TLS pilot.
