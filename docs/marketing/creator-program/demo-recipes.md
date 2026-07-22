# Four TrustedRouter demo recipes

Use synthetic prompts. Keep each first run small and capped. Record the model,
selected provider, token count, cost, latency, request ID, and result without
publishing credentials.

## 1. One-line migration

Start from an existing OpenAI client. Change only the key and base URL.

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["TRUSTEDROUTER_API_KEY"],
    base_url="https://api.trustedrouter.com/v1",
)

response = client.chat.completions.create(
    model="trustedrouter/zdr",
    messages=[{"role": "user", "content": "Reply with PONG only."}],
    max_tokens=128,
)
print(response.choices[0].message.content)
```

Show the original and changed `base_url`. Do not show the key.

## 2. Three-prompt model bakeoff

Choose three representative prompts from the creator's normal workflow and
three models from the live catalog. Include one strong open-weight model, one
frontier model, and one cost-oriented route. Shuffle model labels before rating
the answers. Score correctness, usefulness, latency, and actual request cost.

Start here:

```text
GET https://trustedrouter.com/v1/models
```

Do not pick winners from one cherry-picked prompt. Publish the prompts, scoring
rubric, token caps, and model versions when the content makes a performance
claim.

## 3. Fallback-ready request

Send an ordered primary and fallback list. This demonstrates the request shape
without pretending an outage occurred.

```python
response = client.chat.completions.create(
    model="trustedrouter/zdr",
    messages=[{"role": "user", "content": "Reply with ROUTED only."}],
    max_tokens=128,
    extra_body={
        "models": ["trustedrouter/auto"],
        "provider": {"allow_fallbacks": True},
    },
)
```

Use response and activity metadata to identify the route that served the call.
A forced provider-failure demonstration must be coordinated with TrustedRouter
in a sandbox window. Do not intentionally disrupt a production provider.

## 4. Verify before sending

Generate a fresh nonce, request live evidence, and compare it with the trust
page instructions.

```bash
NONCE=$(openssl rand -hex 16)
curl -s "https://api.trustedrouter.com/attestation?nonce=$NONCE" | jq .
```

Confirm the evidence includes the supplied nonce and the published workload
identity. Explain the boundary accurately: the evidence identifies the running
gateway image. It does not prove that the source is bug free or that every
downstream provider is confidential.
