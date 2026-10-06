# Tencent Cloud TokenHub

Provider slug: `tencent`.

TrustedRouter calls the Tencent Cloud Singapore/global OpenAI-compatible API
directly at `https://tokenhub-intl.tencentcloudmaas.com/v1`. It does not call
OpenRouter or the unrelated tokenhub.com service.

## Routing

Use a model ID from `GET /v1/models`, then pin Tencent explicitly:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.trustedrouter.com/v1",
    api_key="YOUR_TRUSTEDROUTER_API_KEY",
)
stream = client.chat.completions.create(
    model="xiaomi/mimo-v2.6-flash",
    messages=[{"role": "user", "content": "Reply exactly PONG"}],
    max_tokens=128,
    stream=True,
    extra_body={"provider": {"only": ["tencent"], "allow_fallbacks": False}},
)
for event in stream:
    if event.choices:
        print(event.choices[0].delta.content or "", end="", flush=True)
```

Tencent availability is model-specific. A model appearing in Tencent's global
catalog does not mean our account's inference service is activated. The refresh
intersects the authenticated online catalog, documented chat capabilities, exact
Singapore USD prices and a successful PONG canary. Failed models stay disabled.
Models marked for retirement and image/video/audio/embedding services are not
added to the chat catalog. Vendor Direct aliases are excluded rather than
collapsed into Tencent-hosted routes with different prices.

The release-specific `deepseek/deepseek-v4-pro-0813` leaf also stays disabled
for Tencent: its already-published combo presets pin a fixed provider set.
Do not change those immutable presets as part of provider discovery.

## Pricing and privacy

Prices refresh through the regular catalog job. MiniMax M3 has a 512K input
threshold. Tencent's scheduled DeepSeek routes use weekday peak windows of
09:00 to 12:00 and 14:00 to 18:00 in Asia/Shanghai. Peak prices are twice off-peak,
including cached inputs. The applicable customer rate is locked at authorization,
so crossing a boundary during streaming does not reprice settlement.

The Singapore ingress has global resource scheduling. It is not a Singapore-only
inference guarantee. Tencent upstream ZDR, confidential compute and E2EE are not
claimed. TrustedRouter's attested router is a separate boundary.

## Operations

The prepaid key is stored in Secret Manager as
`trustedrouter-tencent-tokenhub-api-key` and supplied to the enclave as
the secret pointer `QUILL_TENCENT_SECRET`. The operator keyfile and pricing
refresh use `TENCENT_API_KEY`. Do not place it in a public manifest or
application log.
The shared direct-provider registry controls bootstrap and cloud deployment
wiring. Deploy gateway support before publishing a newly enabled provider.

A `402` with code `401006` means the default online inference service has not
been activated or does not match the model, even if the account has funds.
Enable only the desired pay-as-you-go services in the matching Singapore site.
Do not purchase reserved throughput or dedicated instances for catalog canaries.

The small discovery canaries use the models' native thinking defaults. In the
September 29 checks, GLM 5.3 and Kimi 2.7 Code routes rejected explicit
`thinking: {"type": "disabled"}` while their ordinary calls succeeded.

## Sources

* [API hosts and authenticated catalog](https://www.tencentcloud.com/document/product/1300/78941)
* [Model capabilities and context windows](https://www.tencentcloud.com/document/product/1300/78934)
* [Regional pricing](https://www.tencentcloud.com/document/product/1300/78937)
* [Service activation](https://www.tencentcloud.com/document/product/1300/83717)
* [OpenAI protocol and errors](https://www.tencentcloud.com/document/product/1300/80632)
