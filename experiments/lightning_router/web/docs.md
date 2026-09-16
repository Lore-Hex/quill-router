# LightningRouter setup

LightningRouter is the Bitcoin over Lightning funding site for TrustedRouter.
Both are operated by Lore Hex Corp. The same API key, model catalog and inference
API documentation apply. You do not need an email address or a separate password.

## Fund a key

Open https://lightningrouter.ai/ with JavaScript enabled. Choose a USD credit amount
and optionally paste an existing key to top up its account. Pay the displayed
invoice with a Lightning wallet, not an on-chain Bitcoin transfer.

A new account is created only after a verified payment settles. BTC converts once
into USD credits at the invoice's locked quote. The 10% FX buffer means 90% of its
Coinbase spot value becomes USD credits, rounded to whole satoshis. Your wallet may
charge a separate routing fee. For $10 in credits, expect about $11.11 in BTC at
that spot rate. Balances and model charges stay in USD, not BTC.

Keep your key safe: it is a secret bearer credential with access to your balance
and inference. This browser tab remembers it until you sign out. Do not put it in
URLs, logs, public repositories or support messages. Ask the user before spending
money on inference or a Lightning payment.

## Inference API

Base URL: https://api.trustedrouter.com/v1

Authentication: `Authorization: Bearer <your API key>`

Store the key in `TRUSTEDROUTER_API_KEY` through your secret manager or private
environment. A small Chat Completions example, after the user authorizes a paid call:

```sh
curl --fail-with-body https://api.trustedrouter.com/v1/chat/completions \
  -H "Authorization: Bearer ${TRUSTEDROUTER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-flash","messages":[{"role":"user","content":"What is the capital of France?"}],"max_tokens":1024,"reasoning_effort":"low"}'
```

Use the [homepage setup tabs](https://lightningrouter.ai/#setup-title) for Trusted
Cowork, OpenCode, Crush and OMP. The generated configs show reviewed model-specific
reasoning settings. Do not assume every model supports the same effort values.

- [Current models, prices, limits and reasoning controls](https://lightningrouter.ai/api/models)
- [TrustedRouter API reference](https://trustedrouter.com/docs)
- [Pricing and conversion terms](https://lightningrouter.ai/pricing)
- [Your private balance and usage](https://lightningrouter.ai/usage)

The live catalog is a public GET with no key required. Prices are route-dependent.
Do not treat a missing price as free, or an unavailable balance as zero.

## Discovery and health

- [Read-only OpenAPI](https://lightningrouter.ai/openapi.json) describes this site's
  `/api/models` and `/health`. It does not expose payment tools. Inference calls
  belong at the separate API base above.
- [Funding health](https://lightningrouter.ai/health) returns HTTP 200 when ready,
  HTTP 503 when degraded or unavailable. Check `payments_ready` before relying on funding.
- [Agent index](https://lightningrouter.ai/llms.txt)

The homepage and `/docs` also return Markdown with `Accept: text/markdown` and
set `Vary: Accept`. Public guide reads never create an invoice or an account.

## Privacy and attestation

Funding happens on this website. Prompts go separately to TrustedRouter's attested
API. You or your agent can independently verify gateway attestation at
https://trust.trustedrouter.com before sending inference traffic. Selected model
providers still process requests. Their retention and confidential-computing
guarantees vary: read https://trustedrouter.com/providers and
https://lightningrouter.ai/privacy before sending sensitive data.

## Recover and get help

For an expired or canceled invoice, choose New invoice. If payment is in flight
or needs review, keep the tab open and wait for its final status. Do not pay it
again or assume a canceled browser display means a payment was not received.

Private payment help: support@trustedrouter.com. Never share your API key.
Public bugs: https://github.com/Lore-Hex/lightning-router/issues. Do not post private
payment information or prompts. Security reports: security@trustedrouter.com.

[Terms](https://lightningrouter.ai/terms) | [Privacy](https://lightningrouter.ai/privacy)
