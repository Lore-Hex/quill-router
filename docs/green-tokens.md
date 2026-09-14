# Green Tokens

Use `trustedrouter/green` with the existing Chat Completions or Responses API,
including streaming. The control plane expands it into directly hosted models
and applies a hard provider filter. Fallback remains inside the eligible pool.
An unavailable pool or incompatible privacy/provider requirement returns an
error rather than routing to an ineligible provider.

```python
from openai import OpenAI
import os

client = OpenAI(
    base_url="https://api.trustedrouter.com/v1",
    api_key=os.environ["TRUSTEDROUTER_API_KEY"],
)
for chunk in client.chat.completions.create(
    model="trustedrouter/green",
    messages=[{"role": "user", "content": "Explain solar power in one sentence."}],
    max_tokens=256,
    stream=True,
):
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
```

For a specific model, use its canonical ID and `provider.only: ["regolo"]`.
The pool alias advertises a conservative 30,000-token context; the provider's
individual model limits are available in the source catalog. Green does not
select external search, hosted tools or composite model orchestration.

## Evidence And Scope

Initial provider: [Regolo](https://regolo.ai/), operated by Seeweb S.r.l. in Italy.
Its [sustainability policy](https://regolo.ai/sustainable-ai/) states that its
inference GPU servers use 100% renewable electricity. This is a provider
declaration, not a per-request energy measurement or a whole-lifecycle emissions
claim. It does not cover client devices, network transit, model training or
TrustedRouter's gateway electricity. Reviewed September 14, 2026.

Regolo separately publishes a [zero-retention policy](https://regolo.ai/zero-data-retention/).
That is not provider-side confidential computing or verifiable E2EE. The public
catalog keeps these properties separate. Prompts terminate in TrustedRouter's
attested gateway and are sent directly to Regolo over HTTPS.

## Catalog And Operations

The hourly refresh reads the authenticated Regolo `model_group/info` catalog.
Only concrete, positively priced chat models with known limits are eligible;
Regolo's nested Brick routers are excluded. Newly discovered models must pass a
paid-path canary. Native IDs preserve case. Inference pricing is EUR per token,
converted to USD using a dated ECB reference rate no older than seven days.
The normal TrustedRouter service fee applies, with no additional green surcharge.
Invalid feeds retain the last-known-good manifest, which expires after 14 days.

`REGOLO_API_KEY` is provisioned as `trustedrouter-regolo-api-key`. The attested
runtime receives it through its cloud-native bootstrap secret channel. Only the
price-refresh job receives the catalog credential outside inference; the normal
control-plane service does not receive the raw provider key.

Add future providers only after reviewing inference-electricity scope and adding
a source URL alongside `renewable_energy_inference`. Do not infer renewable
power from a carbon-neutral pledge, ZDR policy, model publisher or cloud region.

Public page: [Green Tokens](https://trustedrouter.com/green-tokens).
