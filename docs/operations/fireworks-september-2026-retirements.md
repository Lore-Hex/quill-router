# Fireworks serverless retirement, September 25, 2026

Source: [Fireworks September 12 changelog](https://docs.fireworks.ai/updates/changelog#2026-09-12),
confirmed by the customer reminder received September 21.

The notice specifies a date but no hour or timezone. TrustedRouter uses
September 25 at 00:00 UTC as a conservative routing cutoff.

| Retiring model | Provider's suggested replacement |
| --- | --- |
| DeepSeek V4 Flash 0731 | DeepSeek V4.1 Flash |
| DeepSeek V4 Pro 0813 | DeepSeek V4.1 Flash |
| DeepSeek V4 Flash Vision Exp | DeepSeek V4.1 Flash |
| GLM 5.2, including Fast | GLM 5.3 |
| Muse Glimmer 30B | Nemotron 3.5 Lightning |
| Kimi K2.6 | GLM 5.3 or Kimi K3 |
| Kimi K2.7 Code | GLM 5.3 or Kimi K3 |

The cutoff includes US-only and Fast serverless variants. The earlier August
retirement of Kimi Fast routes remains in force. Dedicated deployments are
unaffected by the notice and are not these shared catalog routes.

Only Fireworks endpoints are removed. Other providers may still serve the
same checkpoint. Replacement models retain distinct IDs; requesting an old
model never silently substitutes one of the recommendations above. Immutable
DeepSeek 0813 routes can start without Fireworks after its announced retirement.

The lifecycle check runs at request time and during discovery and refresh, so
a stale upstream feed cannot restore these routes. Tests cover the exact
boundary, warm and cold processes, Credits and BYOK, provider scope, and stale
price/model feeds. No new inference request is necessary to verify retirement.
