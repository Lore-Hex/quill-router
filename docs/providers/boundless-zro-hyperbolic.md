# Boundless, Zro, Hyperbolic

Reviewed 2026-10-11. Boundless and Zro passed native streaming and non-streaming
GLM-5.3-Flash probes; all 20 Boundless and 5 Zro priced models passed PONG canaries
with integer token accounting. Routing activation also requires gateway rollout.
The serving catalog admits 16 Boundless routes and 5 Zro routes: existing
Claude first-party-only policy excludes three Boundless Claude reseller routes,
and the immutable DeepSeek Pro 0813 leaf retains its frozen provider set.
Native availability does not override either policy.

## Discovery and pricing

- Boundless: authenticated `https://api.inference.boundless.network/v1/models`,
  intersected with `https://inference.boundless.network/models` JSON-LD products.
  Visible prices are rounded: MiMo Pro's $0.435 input / $0.0036 cache-read per
  million appear as $0.44 / $0.00. Never scrape those rounded display labels.
  Cross-check exact input/output against the native API. Laya uses a separate,
  unpriced decision API and is not a chat route.
- Zro: authenticated `https://zro.moonmath.ai/v1/models`, intersected with the
  exact USD/Mtoken table at `https://zro.moonmath.ai/docs/models`. Bare and `zro/`
  aliases collapse. Exclude the dynamic-price `auto` router and private variants.
  Stale 0731 remains held, even when the native list advertises it. New unpriced
  models remain visible to the coverage gate, not automatically billable.
- Hourly refresh canaries newly discovered/previously failed models, preserves
  exact native IDs, gates price spikes, and expires stale manifests.
- Hyperbolic: current official OpenAPI identifies both `api.hyperbolic.ai` and
  `api.hyperbolic.xyz` as its hosts. The supplied key receives 401 on `/v1/models`
  at both. Keep the provider visible but disabled; do not invent prices/models
  from old serverless pages or rent GPUs. Need a valid inference credential and
  current exact price contract before implementing an executable refresh parser.

## Privacy and locations

Boundless stays Standard: terms sections 3.2/3.3 qualify its marketing ZDR claim
and do not warrant third-party retention. US company registration is not GPU
residency. Zro's public privacy documentation declares no content retention or
training, including by subprocessors; short-lived account caches and operational
metadata are distinct. Mark Zro provider-declared ZDR, not confidential/E2EE.
Zro supports Europe/US placement restrictions at its own key level; TR does not
expose a verified request-level region pin for this integration.

## Credentials and rollout

`BOUNDLESS_API_KEY` and `ZRO_API_KEY` map to `trustedrouter-boundless-api-key` and
`trustedrouter-zro-api-key`. Keep values out of logs, code and the public control
plane. Copy credentials to each cloud's native store and preserve existing
Azure sealed bundle entries. AWS egress uses dedicated ports 8111 and 8112.
Roll out verified gateways before releasing catalog routes; no fallback should
mask forced-provider production smokes. Hyperbolic's rejected key is not deployed.

Provider branding uses the official Boundless icon (`https://boundless.network/icon.svg`),
Zro mark (`https://zro.moonmath.ai/zro-mark-black.png`), and Hyperbolic favicon
(`https://hyperbolic.ai/favicon-light.ico`). Social cards use the existing renderer.
