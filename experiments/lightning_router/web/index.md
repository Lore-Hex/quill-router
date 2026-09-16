# LightningRouter

Pay with Bitcoin. Start building. No email, password or card.

LightningRouter funds a TrustedRouter API key with Bitcoin over Lightning.
It is for people using AI apps or coding agents who prefer to pay with Bitcoin.
Use your key with Trusted Cowork, OpenCode, Crush, OMP or your own application.

## How it works

1. Open https://lightningrouter.ai/ with JavaScript enabled and choose a USD credit amount.
2. Optionally connect an existing API key to top up that account.
3. Review and pay the Lightning invoice in your wallet. This is not an on-chain payment address.
4. After verified settlement, a new funded key is revealed or the existing account is credited.

No new account is created before payment. BTC converts once into USD credits at
the invoice quote. The 10% FX buffer means 90% of the quoted BTC spot value becomes
credits, rounded to whole satoshis. Your wallet may charge a separate routing fee.
Your balance stays in USD.

## Connect an agent

Use https://api.trustedrouter.com/v1 as the API base and your key as a Bearer token.
Keys are secrets. Do not put them in URLs, logs, public posts or support messages.
Confirm the user's spending intent before any paid call.

- [Setup guide](https://lightningrouter.ai/docs.md)
- [Current models, prices and reasoning controls](https://lightningrouter.ai/api/models)
- [Pricing](https://lightningrouter.ai/pricing)
- [Agent index](https://lightningrouter.ai/llms.txt)
- [Read-only OpenAPI](https://lightningrouter.ai/openapi.json)
- [Funding health](https://lightningrouter.ai/health)

## Privacy boundary

The funding site does not handle inference prompts. Inference goes to TrustedRouter's
attested gateway. You or your agent can verify it at https://trust.trustedrouter.com.
The selected downstream model provider still processes your request. Consult
https://trustedrouter.com/providers for its retention and confidential-computing policy.

[Terms](https://lightningrouter.ai/terms) | [Privacy](https://lightningrouter.ai/privacy)

Operated by Lore Hex Corp. Payment help: support@trustedrouter.com.
