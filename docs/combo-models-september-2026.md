# September 2026 combo models

These are new versioned graphs. Earlier versioned models keep their published
components. The rolling names move after the attested gateway rollout.

| Model | Worker or parallel panel | Optional advisor |
| --- | --- | --- |
| `trustedrouter/prometheus-4.0` | MiMo 2.6 Pro, GLM 5.3, Kimi K3, DeepSeek V4.1 Flash, MiniMax M3, Qwen3.8 2.4T-A95B | None |
| `trustedrouter/zeus-3.0` | GPT-6 Astra, Claude Fable 5.1, Gemini 3.8 Flash, MiMo 2.6 Pro, GLM 5.3, Kimi K3, DeepSeek V4.1 Flash | None |
| `trustedrouter/plato-4.0` | MiMo 2.6 Pro | Prometheus 4.0 |
| `trustedrouter/socrates-3.0` | MiMo 2.6 Pro UltraSpeed | Zeus 3.0 |

Both synths use MiMo 2.6 Pro as judge and synthesizer. Judge fallbacks are
DeepSeek V4.1 Flash, GLM 5.3, then Kimi K3. Synthesizer fallbacks are Kimi K3,
GLM 5.3, then DeepSeek V4.1 Flash. Plato falls back to DeepSeek V4.1 Flash and
GLM 5.3. Socrates falls back to regular MiMo 2.6 Pro, DeepSeek V4.1 Flash, then
GLM 5.3. Advisors are called only when the worker asks for advice.

## Context and provider scope

The catalog advertises a 1,000,000-token **total context class**, not 1,048,576
usable input tokens. Instructions, tool definitions, panel answers, reasoning,
and output all consume context. GPT-6 Astra separately limits input to 922,000
tokens. Leave room for orchestration; a 1M-token user prompt is not supported
merely because every component has a 1M-class window. Catalog verification and
short inference smoke tests are not a full-window stress test.

The enclave applies the following hard provider allowlists to every leaf call,
including nested advisors, judge/synthesizer calls and fallbacks. Caller privacy,
jurisdiction, ignore, price and provider-only constraints remain in force.
An incompatible provider-only pin returns an error rather than widening access.

| Component | Eligible providers |
| --- | --- |
| MiMo 2.6 Pro | Xiaomi |
| MiMo 2.6 Pro UltraSpeed | Xiaomi |
| GLM 5.3 | Z.AI, Novita |
| Kimi K3 | Moonshot, Novita, Together |
| DeepSeek V4.1 Flash | Novita, Together |
| MiniMax M3 | MiniMax, Novita |
| Qwen3.8 2.4T-A95B | Novita, Together |
| GPT-6 Astra | OpenAI |
| Claude Fable 5.1 | Anthropic |
| Gemini 3.8 Flash | Google AI Studio, Vertex AI |

Qwen and MiniMax each have two independently operated 1M-capable routes. This
is not a claim that every reseller supports that window. Together's authenticated
models API reports Qwen at 1,010,000 but MiniMax M3 at 524,288, so Together is
eligible for Qwen and deliberately excluded for MiniMax in these graphs.
DeepInfra is excluded from every route in these new graphs, including fallbacks.

## Sources reviewed September 22, 2026

* [MiMo 2.6 Pro](https://mimo.mi.com/models/en-US/mimo-v2.6-pro)
* [MiMo UltraSpeed](https://mimo.mi.com/models/en-US/mimo-v2.6-pro-ultraspeed):
  10x regular Pro token pricing; only Socrates chooses it first.
* [DeepSeek V4.1 Flash announcement](https://api-docs.deepseek.com/news/news260910/):
  V4.1 Flash is released. V4.1 Pro is not substituted into these graphs.
* [GLM 5.3](https://docs.z.ai/guides/llm/glm-5.3)
* [Kimi K3 model card](https://huggingface.co/moonshotai/Kimi-K3)
* [MiniMax context documentation](https://platform.minimax.io/docs/api-reference/text-anthropic-api)
* [MiniMax M3 on Novita](https://novita.ai/models/model-detail/minimax-minimax-m3)
* [Qwen3.8 model card](https://huggingface.co/Qwen/Qwen3.8-2.4T-A95B)
* [Qwen3.8 on Novita](https://blogs.novita.ai/qwen3-8-2-4t-a95b-on-novita-ai/)
* [Together models API](https://docs.together.ai/reference/models-1)
* [GPT-6 Astra limits](https://developers.openai.com/api/docs/models/gpt-6-astra)
* [Claude context windows](https://platform.claude.com/docs/en/build-with-claude/context-windows)
* [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)

## Rollout

1. Pass local control-plane lint, types and full tests; gateway race tests and CI.
2. Deploy the gateway through the reviewed attested release workflows. Preserve
   the published digest and regional rollout checks.
3. Publish catalog entries and rolling aliases after gateways understand them.
4. Smoke both streaming and non-streaming and optional nested advice. Record
   actual providers, successful completion, cost and any provider-specific failures.
5. Keep versioned predecessor models available for callers who pin them.
