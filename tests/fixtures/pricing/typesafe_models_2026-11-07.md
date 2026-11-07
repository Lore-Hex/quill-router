> ## Documentation Index
> Fetch the complete documentation index at: https://docs.typesafe.ai/llms.txt
> Use this file to discover all available pages before exploring further.

# Models

Jev is TypeSafe's flagship model and the first [System One model](https://docs.typesafe.ai/concepts/system-one.md). Every model on this page is served by the same endpoint, `POST /v1/systemone`. The request's `model` field selects which one handles it; see the [API reference](https://docs.typesafe.ai/api) for the full request shape.

## Current models

| | Jev 1.14 | Jev 1.13 |
| :- | :- | :- |
| Model ID | `jev-1.14.0` | `jev-1.13.0` |
| Price (per Btok / per Mtok) | \$42 / \$0.042 | \$42 / \$0.042 |
| Context length | 64k per-request / 32k per-question | 64k per-request / 32k per-question |
| Input | Text, images | Text, images |
| Question types | Choice, Score, Noul, Box | Choice, Score, Noul |

Jev 1.14 is the newest Jev release, available from October 14, 2026.

* **Price:** You pay per input token. Output tokens are free. A Btok is a billion tokens and an Mtok is a million tokens.
* **Context length:** Two limits, the per-request context and the per-question context. See [Context length](#context-length). To raise both, see [Long context](#long-context).
* **Input:** Jev reads natural-language text, sent as a string, a JSON object, or an array of text values. It also reads PNG and JPEG [images](https://docs.typesafe.ai/images.md) anywhere in the request. It does not natively accept audio or video (yet). See [State](https://docs.typesafe.ai/concepts/state.md) for the shapes `state` accepts.
* **Question types:** [Box](https://docs.typesafe.ai/primitives/box.md) questions need Jev 1.14.

## Context length

Jev ingests the `state` once and evaluates every question against it in parallel. A request has to fit two context limits:

* **Per-request context** (`per_request_context`): the `state` plus all questions combined.
* **Per-question context** (`per_question_context`): the `state` plus any one question.

There is no separate limit on the number of questions in a request. Any number of questions is allowed as long as the request fits both limits. See [Speculative fan-out](https://docs.typesafe.ai/patterns/fan-out.md) for packing many questions into one request, and [Model jaggedness](https://docs.typesafe.ai/model-jaggedness.md) for how accuracy shifts as the state grows.

To raise both limits, see [Long context](#long-context).

### Long context

Long context is off by default and works on both Jev 1.14 and Jev 1.13. To turn it on, send the `TypeSafe-Danger-Zone-Long-Context: true` header with each request that needs it. With the header, the per-request context and the per-question context are both 262k tokens. Requests without the header keep the default limits in the table above.

```bash theme={null}
curl https://api.typesafe.ai/v1/systemone \
  -H "Authorization: Bearer $TYPESAFE_API_KEY" \
  -H "Content-Type: application/json" \
  -H "TypeSafe-Danger-Zone-Long-Context: true" \
  -d '{
    "model": "jev-latest",
    "state": "<a long contract, transcript, or record>",
    "questions": {
      "has_termination_clause": {
        "type": "noul",
        "instructions": "Does the `state` include a clause that lets either party end the agreement early?"
      }
    }
  }'
```

Long context costs the same per token as any other request. It is slower and less accurate than staying within the default limits, so turn it on only for requests that need it.

## Rate limits

Rate limits are set per organization. Your organization's current limits are in the [developer console](https://console.typesafe.ai/rate-limits).

Limits are measured in tokens per second and requests per second. A request over either limit returns `429 Too Many Requests`. Our [client SDKs](https://docs.typesafe.ai/sdk.md) retry these requests by default, waiting longer between each attempt, and follow the `retry-after` header when the response includes one. If you call the HTTP API directly, see [Handling rate limits](https://docs.typesafe.ai/api/errors.md#handling-rate-limits).

Higher limits are available on custom and enterprise plans. To request a higher limit, fill out the [rate limit increase request form](https://support.typesafe.ai/forms/rate-limit-increase-request).

## Aliases

An alias is a model name that points to a versioned model ID. Send it in the `model` field the same way you send a model ID.

| Alias | Points to | Meaning |
| :- | :- | :- |
| `jev-latest` | `jev-1.14.0` | The most recent stable, official release. The default in our client SDKs, and the name the examples in these docs use. |
| `jev-preview` | `jev-1.14.0` | The most recent release, official or not. Moves ahead of `jev-latest` when a preview build is available. |

> [!WARNING]
>
> `jev-preview` currently points to the same model as `jev-latest`. There is no preview build available right now.

An alias moves when a new release ships, so its answers can change without any change to your code. Each response's `model` field gives the versioned ID that answered, so you can log which model produced each result. If you have tuned confidence thresholds against one version, send that version's ID instead of the alias, and switch to the new version when you are ready.

## Customizing Jev

Jev is not fine-tuned or LoRA-adapted with customer data. It is trained with [RLCD](https://docs.typesafe.ai/introduction/machine-learning-primer.md) to return calibrated decisions, and the same weights serve every account. You shape its answers to your domain through the request rather than through per-account weights:

* Put your proprietary content, records, and reference material in the `state` field. See [State](https://docs.typesafe.ai/concepts/state.md).
* Encode your domain rules and boundary cases in the `instructions` and `criteria` of each question. See [How to build with TypeSafe](https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md) and [Advanced: structure](https://docs.typesafe.ai/primitives/advanced.md).
* Decompose broad judgments into atomic questions and combine the outputs in code. See [Composite scoring](https://docs.typesafe.ai/patterns/composite-scoring.md) and the [AutoResearch cookbook](https://docs.typesafe.ai/cookbooks/autoresearch_feature_discovery.md) for training a downstream classical model on Jev's probabilities.

## Data handling

Jev is not trained on customer requests or responses. See [Legal](https://docs.typesafe.ai/legal.md) for the Data Processing Agreement, the Privacy Policy, and details on zero data retention (ZDR) for enterprise customers.

## Listing models

`GET /v1/models` returns the names your account can send in the `model` field, each with a description and a release date. It currently lists the aliases. Versioned IDs such as `jev-1.14.0` are accepted by the `model` field whether or not they appear in the list.

```bash cURL theme={null}
curl https://api.typesafe.ai/v1/models \
  -H "Authorization: Bearer $TYPESAFE_API_KEY"
```

```python Python theme={null}
from typesafe_sdk import TypeSafeClient

with TypeSafeClient() as client:
    for model in client.models.list().models:
        print(model.name, model.release_date, model.description)
```

```typescript JavaScript theme={null}
import { TypeSafeClient } from "@typesafe-ai/sdk";

const client = new TypeSafeClient();
const models = await client.models.list();
for (const model of models) {
  console.log(model.name, model.release_date, model.description);
}
```

**models** `array` *required*

One entry per model or alias.

**properties**

**name** `string` *required*

The model ID or alias, as accepted by the `model` field.

**description** `string` *required*

What the model is for.

**release\_date** `string` *required*

When the model or alias was released.

See the [Python](https://docs.typesafe.ai/sdk/python/api/clients/sync.md#typesafe_sdk.Models.list) and [JavaScript](https://docs.typesafe.ai/sdk/javascript/api/interfaces/Models.md) SDK references for the full method signatures.


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.