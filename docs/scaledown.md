# ScaleDown Task Models

ScaleDown offers four specialized text tasks through TrustedRouter's
`POST https://api.trustedrouter.com/v1/chat/completions`:

| Model | Task configuration |
|---|---|
| `scaledown/compress` | `context`, `prompt`, optional `scaledown.rate` |
| `scaledown/summarize` | `text`, optional `instructions` and `max_tokens` |
| `scaledown/extract` | `text`, `entities` mapping names to descriptions |
| `scaledown/classify` | `text`, `labels` with `name` and `rubric` |

Send the task configuration as a JSON string in **one user message**. Summarize
also accepts plain text. This first adapter supports text, not image blocks,
tools, multi-turn conversations, or `response_format`. These are task APIs, not
general chat models. Streaming returns the completed task result together;
the upstream does not stream individual tokens.
Choose a task model explicitly; these tasks do not participate in the generic
auto/cheap/fast model-selection pools or output-throughput benchmarks.

```python
import json
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["TRUSTEDROUTER_API_KEY"],
    base_url="https://api.trustedrouter.com/v1",
)
result = client.chat.completions.create(
    model="scaledown/extract",
    messages=[{"role": "user", "content": json.dumps({
        "text": "Acme launched Monday.",
        "entities": {"company": "The company name"},
    })}],
)
entities = json.loads(result.choices[0].message.content)["entities"]
print(entities)
```

Each model's catalog documentation includes an example task and response.
The content is a JSON object containing the native result. Usage is the exact
provider-reported **billable input count**, including provider-side task
preprocessing. `completion_tokens` is intentionally zero: ScaleDown charges
no output tokens, including for summaries. TrustedRouter credits and the
standard routing markup apply; BYOK is not supported. Consult the model page
for the current customer price.

The hourly refresh verifies [public pricing](https://scaledown.ai/#pricing)
and runs a small synthetic task against each of the four endpoints. A missing
price commitment or failed canary is not silently accepted. Native task IDs
are explicit until ScaleDown supplies a working machine-readable catalog.

ScaleDown's [DPA](https://scaledown.ai/dpa) commits to no prompt/output storage,
no training, and US processing. Metadata-only operational logs may remain for
90 days. These routes are ZDR, **not confidential/E2EE**: there is no verified
hardware attestation or enclave-bound encrypted transport for this provider.

The provider logo is vendored unchanged from
`https://scaledown.ai/logo_actual.png`, the official homepage's favicon and
social-image asset, retrieved 2026-09-09.
