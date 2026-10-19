# Hixtube Pilot

Hixtube's planned service is a small real Qwen3 0.6B integration pilot using the Apache-2.0
[Neocloud-in-a-Box toolkit](https://github.com/Lore-Hex/token-exchange-neocloud-in-a-box).
It must run in a separate cloud project, not on TrustedRouter production resources.

- Intended API base: `https://api.hixtube.com/v1`.
- Catalog/pricing: public metadata `GET /models`, Provider Catalog v2;
  inference and discovery canaries require authentication.
- Model identity: `qwen/qwen3-0.6b`; 4,096-token context and 256 output tokens.
- Credential: `HIXTUBE_API_KEY`, secret name `trustedrouter-hixtube-api-key`.
- Standard TLS only. No ZDR, confidential-compute or E2EE eligibility.
- Exact prices come from the operator-approved catalog, not another provider.
- Credits only; measured integer input/output usage follows normal settlement.

## Activation Order

The integration code alone does not establish a live route. Do not merge the
refresh registration until the public server and discovery credential are ready:
otherwise the hourly refresh would acquire a new missing-credential failure.

1. Independently deploy the bounded Hixtube server, DNS and TLS. Verify public
   authentication, streaming, token usage, enforced limits and Catalog v2.
2. Place independent credential copies in the approved cloud secret stores.
   Never mount the operator inference key into the public control plane.
3. Deploy the gateway's reviewed `hixtube` registry/egress changes. AWS needs
   its explicit hostname tunnel; Azure needs a sealed bundle before opt-in.
4. Fetch and canary the real catalog, generate the manifest, then publish the
   control-plane integration through the normal CI/deployment gates.
5. Force `provider.only=["hixtube"]`, `allow_fallbacks=false` for the gateway
   smoke; verify usage and settlement without fallback masking a broken route.

No public-server or production-gateway success is claimed by these source changes.
The future confidential DeepSeek/GLM deployment needs its own hardware evidence,
verifier integration and approved measurements; it cannot inherit a badge from
this standard-TLS pilot.
