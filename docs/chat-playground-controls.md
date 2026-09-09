# Chat playground controls

Open a model's settings in `/chat` to select Reasoning: Default, Off, or On.
Off disables the provider's reasoning computation; collapsing the Thinking panel
only changes its display. The choices are offered only for verified direct
hybrid-model routes. Unsupported choices remain disabled. Default leaves the
provider's behavior unchanged.

Selecting a provider pins that provider with `provider.only` and
`provider.allow_fallbacks=false`. An unavailable pin or unsupported saved setting
fails explicitly instead of silently switching providers or discarding reasoning
controls. An explicit reasoning mode with Auto provider restricts routing to
providers that support that mode. Normal privacy and billing authorization still
apply.

Seed is optional and available only when a selected endpoint advertises support.
It is best-effort repeatability, not determinism. A lower temperature, stable model
and provider, fixed seed, and identical conversation can reduce variation, but do
not guarantee identical answers. Some models ignore or constrain sampling
parameters, including Kimi's hybrid models and DeepSeek in thinking mode.

## Editing assistant responses

The pencil action edits an assistant reply in browser-local conversation history.
Save edit does not call inference, change model weights, or change the original
request's billed cost and token counts. Future turns send the edited reply as an
assistant message. Edits persist in the same browser, are marked Edited, and stay
separate for each side-by-side model slot. Cancel and Escape discard an unsaved
edit. A new generation clears the edit marker for that response.

Generated reasoning and tool-call details from the original reply are removed
from an edited response because they no longer describe the edited text. Existing
later replies are not regenerated automatically. Branch the conversation at the
edited turn when existing later replies should not be included in the next request.

No server-side prompt/output cache or retention is added. Existing explicit
conversation sharing and opt-in content broadcast policies are unchanged.

## Native provider contracts

- [Z.ai thinking mode](https://docs.z.ai/guides/capabilities/thinking-mode)
- [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Kimi K2.5 API examples](https://github.com/MoonshotAI/Kimi-K2.5)

The gateway translates `reasoning.enabled` to native `thinking.type` for direct
Z.ai, DeepSeek and Kimi requests. This is shared by prepaid and BYOK invocation.
Other provider contracts remain unchanged. Forced-thinking models and unverified
third-party hosting routes do not advertise an On/Off switch.

## Release order and verification

Deploy the gateway's native reasoning translation before publishing the
control-plane endpoint metadata and chat controls. Local tests do not prove that
every production gateway is running the new code. After rollout, smoke test
`z-ai/glm-5.2` pinned to `zai` with reasoning Off and On using a bounded completion
budget, and verify the selected provider and reasoning behavior.

The deterministic regression suite uses fake endpoints, so it spends no provider
credits. It covers native wire JSON, explicit mode conflicts, strict pins,
unsupported routes, invalid/zero seeds, legacy routing settings, edit cancellation,
original billing preservation, subsequent request history, duplicate model slots,
streaming locks, XSS sanitization, and desktop/mobile layout.
