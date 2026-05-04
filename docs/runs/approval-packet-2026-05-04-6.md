# Outreach approval packet — run 6 (2026-05-04)

Notes:
- Do not send anything from this automation. All items below are `status=proposed`.
- All quotes are copied verbatim from `docs/outreach-quote-bank.csv`.

## 1) sentient-agi/OpenDeepSearch (GitHub)

- Evidence: https://github.com/sentient-agi/OpenDeepSearch
- Qualification: uses LiteLLM + OpenRouter (see README section “Integrating with SmolAgents & LiteLLM” and OpenRouter API key guidance)
- Score: 60
- Channel: GitHub (issue or discussion)
- Context link: https://trustedrouter.com/compare/openrouter
- Relevant quote:
  - "This is all the benefits of OpenRouter with none of the risks."
- Proposed action (needs human approval): Create a GitHub Discussion (preferred) or Issue asking if they want a verifiable non-logging OpenRouter-shaped route for higher-sensitivity agent use cases.
- Approved message draft:
```text
Noticed OpenDeepSearch supports OpenRouter via LiteLLM for agent workflows.

"This is all the benefits of OpenRouter with none of the risks."

Context: https://trustedrouter.com/compare/openrouter
```
- Status: proposed
- Next action: Approve exact GitHub target + exact text, then manually post.

## 2) ENTERPILOT/GOModel (GitHub)

- Evidence: https://github.com/ENTERPILOT/GOModel
- Qualification: explicitly supports `OPENROUTER_API_KEY` as a provider credential
- Score: 85
- Channel: GitHub (discussion or issue)
- Context link: https://trustedrouter.com/security
- Relevant quote:
  - "We never log your prompt or the output."
- Proposed action (needs human approval): Open a GitHub Discussion asking if they’d accept a “TrustedRouter upstream/provider” example for sensitive-prompt teams who want verifiable non-logging.
- Approved message draft:
```text
Saw GoModel supports OpenRouter as a provider; for teams routing sensitive prompts, a verifiable non-logging upstream can matter.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Status: proposed
- Next action: Approve exact GitHub target + exact text, then manually post.

## 3) MadAppGang/claudish (GitHub)

- Evidence: https://github.com/MadAppGang/claudish
- Qualification: positions itself as “Any OpenRouter model”; suggests checking OpenRouter status/keys
- Score: 85
- Channel: GitHub (discussion or issue)
- Context link: https://trustedrouter.com/security
- Relevant quote:
  - "We never log your prompt or the output."
- Proposed action (needs human approval): Open a discussion suggesting a TrustedRouter base_url example as an alternative for users with proprietary code prompts.
- Approved message draft:
```text
Noticed Claudish runs on OpenRouter models; some users will want a verifiable non-logging path for proprietary code prompts.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Status: proposed
- Next action: Approve exact GitHub target + exact text, then manually post.

## 4) bytedance/trae-agent (GitHub)

- Evidence: https://github.com/bytedance/trae-agent
- Qualification: documents `OPENROUTER_BASE_URL=https://openrouter.ai/api/v1` and running with `--provider openrouter`
- Score: 85
- Channel: GitHub (issue)
- Context link: https://trustedrouter.com/security
- Relevant quote:
  - "We never log your prompt or the output."
- Proposed action (needs human approval): Open an issue proposing they add TrustedRouter as an additional `base_url` example next to OpenRouter.
- Approved message draft:
```text
Saw trae-agent supports OpenRouter base_url for multi-provider access; for sensitive repo/code prompts, a verifiable non-logging base_url can help.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Status: proposed
- Next action: Approve exact GitHub target + exact text, then manually post.

## 5) Diatonic-AI/codex-cli-docker-mcp (GitHub)

- Evidence: https://github.com/Diatonic-AI/codex-cli-docker-mcp
- Qualification: includes a `[model_providers.openrouter]` example with `base_url="https://openrouter.ai/api/v1"`
- Score: 85
- Channel: GitHub (issue or PR suggestion)
- Context link: https://trustedrouter.com/compare/openrouter
- Relevant quote:
  - "TrustedRouter is one API, all the LLMs, provably private."
- Proposed action (needs human approval): Open an issue asking if they’d accept an optional “TrustedRouter provider” snippet alongside OpenRouter in docs.
- Approved message draft:
```text
Noticed your Codex provider config example uses OpenRouter; if you want the same OpenAI-shaped UX with verifiable non-logging, this may help.

"TrustedRouter is one API, all the LLMs, provably private."

Context: https://trustedrouter.com/compare/openrouter
```
- Status: proposed
- Next action: Approve exact GitHub target + exact text, then manually post.

## 6) ResearAI/AutoFigure-Edit (GitHub)

- Evidence: https://github.com/ResearAI/AutoFigure-Edit
- Qualification: lists OpenRouter as a supported provider (`openrouter.ai/api/v1`)
- Score: 60
- Channel: GitHub (issue)
- Context link: https://trustedrouter.com/compare/openrouter
- Relevant quote:
  - "This is all the benefits of OpenRouter with none of the risks."
- Proposed action (needs human approval): Open an issue offering TrustedRouter as an OpenAI-compatible alternative base_url for users who care about non-logging.
- Approved message draft:
```text
Saw AutoFigure-Edit supports OpenRouter as a provider; for users who want a verifiable non-logging option with the same interface:

"This is all the benefits of OpenRouter with none of the risks."

Context: https://trustedrouter.com/compare/openrouter
```
- Status: proposed
- Next action: Approve exact GitHub target + exact text, then manually post.

## 7) monperrus “Running Codex on top of OpenRouter” gist (GitHub Gist)

- Evidence: https://gist.github.com/monperrus/8e79efefb3d149857df1f388bc14361f
- Qualification: shows Codex configured with `model_provider="openrouter"` and `base_url="https://openrouter.ai/api/v1"`
- Score: 60
- Channel: GitHub Gist comment
- Context link: https://trustedrouter.com/compare/openrouter
- Relevant quote:
  - "The most common feedback I hear from engineers is that they don't use OpenRouter at all because of their security and privacy concerns, especially for sensitive prompts, or they use it only for low-sensitivity prompts."
- Proposed action (needs human approval): Leave a short gist comment suggesting TrustedRouter as an OpenRouter-shaped alternative for higher-sensitivity prompts.
- Approved message draft:
```text
Saw your note about running Codex on OpenRouter; a common blocker is using routers only for low-sensitivity prompts.

"The most common feedback I hear from engineers is that they don't use OpenRouter at all because of their security and privacy concerns, especially for sensitive prompts, or they use it only for low-sensitivity prompts."

Context: https://trustedrouter.com/compare/openrouter
```
- Status: proposed
- Next action: Approve exact comment text, then manually post.

