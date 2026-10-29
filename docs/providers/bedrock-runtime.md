# Managed Claude on Bedrock Runtime

Prepared, not deployed. Gateway PR [#630](https://github.com/Lore-Hex/quill-cloud-proxy/pull/630)
adds the native Runtime adapter. The five exact US profiles in
`BEDROCK_RUNTIME_PROFILES` are independent of the existing Mantle catalog.
All five passed live streaming through the new adapter on October 29, 2026.

## Privacy boundary

The managed Runtime account has retention `none` in us-east-1, us-east-2 and
us-west-2. The gateway verifies all three before every request and refuses
missing, unknown or unavailable policy evidence. The reviewed canonical/profile
pairs alone qualify for ZDR; BYOK, Mantle, global profiles and future models do
not inherit that claim. This is not confidential compute or E2EE.

## Pricing and discovery

The existing Bedrock refresh now joins the five reviewed Runtime profiles to
the public AWS **Geo and In-region Cross-region Inference** standard token
table, explicitly excluding global, batch and provisioned-capacity tables.
Decimal prices come from the AWS pricing widget's regional USD artifacts.
Missing, duplicate or changed price units fail publication closed.
Each refresh checks the regional retention controls and runs one bounded PONG
canary per Runtime profile. Inference is never retried automatically.

Haiku 5.5 uses the standard tier through exactly 100,000 total prompt tokens,
including cache reads and writes; above that the entire request uses the high
tier. All five support the reviewed five-minute cache-write rate at 1.25 times
input. One-hour writes remain rejected by the gateway. Boundary tests compare
the frozen ledger calculation to an independent decimal oracle without changing
the shared ledger's existing half-up rounding.

## Release gate

`BEDROCK_RUNTIME_GATEWAY_REGIONS` is empty until serving gateways are qualified.
Manifest rows stay `runtime-enclave-rollout-pending`, catalog admission removes
them, and authorization/snapshots require an explicitly qualified region.
An ordinary control-plane catch-up must not make an older Mantle-only gateway
receive Runtime traffic. Preserve the existing Mantle region restrictions.

After the gateway release, add only verified serving regions, refresh the five
rows, run strict-ZDR and non-streaming/streaming/cache billing probes, and deploy
through the normal multi-cloud coordinator. Keep one healthy cloud unchanged.
Local Claude CLI Opus is signed out; no private API key substitute was used.

Sources: [AWS pricing](https://aws.amazon.com/bedrock/pricing/),
[retention](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html),
[Haiku limits and price boundary](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html).
