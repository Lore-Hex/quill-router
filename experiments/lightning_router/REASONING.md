# Reasoning controls

Reviewed September 15, 2026. No global low/medium/high assumption is used.
Select a model to see its documented controls and generated CLI config.

| Family | Named effort control | Gateway behavior |
| --- | --- | --- |
| Claude Opus 4.6+, Sonnet 4.6+, Fable 5 | Native `output_config.effort`, per-model values | Chat `reasoning_effort` is mapped after selecting the actual provider model; adaptive thinking is used. |
| Claude Opus 4.5 | low, medium, high | Native effort plus the separate legacy thinking budget. |
| Claude Haiku 4.5, Sonnet 4.5 | No native named effort | Token-budget control; no invented native levels in snippets. |
| Gemini 2.5 | Compatibility API effort | low/minimal=1024, medium=8192, high=24576 thinking tokens. none=0 only on models that support disabling. |
| Gemini 3.x | Model-specific thinking levels | Native level on Vertex; compatibility effort on AI Studio. 3.7/3.8 Flash do not offer minimal. |
| OpenAI GPT / o-series / GPT-OSS | Model-specific `reasoning_effort` | Top-level provider field; no Anthropic thinking budget. |
| DeepSeek V4.1 Flash / current V4 Pro | low, high, max | Top-level provider effort. Other hosts can differ. |
| Kimi K3 | low, high, max | Top-level effort without the incompatible K2.x thinking object. |
| Kimi K2.5 / K2.6 | Thinking switch | No named effort; direct tool calls currently use non-thinking mode. |
| Kimi K2.7 Code / MiniMax M2.x | Always-on thinking | No named effort is sent. |
| MiniMax M3 / Gemma 4 | Thinking switch | Native switch is shown; no generic effort is invented. |
| GLM 5.2 / 5.3 | high,max / low,high,max | Top-level effort; 5.3 cannot disable thinking. |
| QwenCloud Qwen3.8 Max / Flash | low, medium, xhigh | Top-level effort; hosted implementations can expose different controls. |
| Mistral Medium 3.5 | none, high | none controls visible thinking, not a guarantee of no internal reasoning. |

## Verification boundaries

The gateway regression tests first failed on the old code. They capture actual
outgoing request bodies, including nested Responses-style effort, and verify
that fallback and native Messages requests are not mutated. Numeric budgets
remain separate from native levels. No prompt or reasoning trace is logged.

Lightning client tests consume the production registry and serialize every
enabled effort through `@ai-sdk/openai-compatible`. Crush and OMP configs use
their explicit model effort lists, not a generic model capability boolean.
OMP is also exercised through its actual request serializer for every supported
model/effort pair. Its custom-provider default otherwise maps off to the lowest
effort, so models supporting none explicitly configure `none-effort`.
Unknown model IDs remain unverified; an alias is reviewed explicitly.

An HTTP 200 alone does not prove a provider honored a parameter. These tests
prove request propagation. They do not certify hidden model computation or
every hosted provider's implementation. Use provider-pinned live canaries and
the provider's own documentation when a particular route's behavior matters.

## Sources

* [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)
* [Google compatibility mapping](https://ai.google.dev/gemini-api/docs/openai)
* [Gemini model-specific levels](https://ai.google.dev/gemini-api/docs/thinking)
* [OpenAI model catalog](https://developers.openai.com/api/docs/models)
* [Cerebras GPT-OSS effort](https://inference-docs.cerebras.ai/capabilities/reasoning)
* [DeepSeek thinking](https://api-docs.deepseek.com/guides/thinking_mode/)
* [Kimi K3 effort](https://platform.kimi.ai/docs/guide/use-reasoning-effort)
* [MiniMax controls](https://platform.minimax.io/docs/api-reference/text-openai-api)
* [Z.ai API](https://docs.z.ai/api-reference/llm/chat-completion)
* [QwenCloud controls](https://docs.qwencloud.com/api-reference/chat/openai-chat)
* [Gemma thinking](https://ai.google.dev/gemma/docs/capabilities/thinking)
* [Mistral reasoning](https://docs.mistral.ai/studio/conversations/reasoning)
