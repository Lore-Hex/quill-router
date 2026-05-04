# Outreach approval packet (2026-05-04 #8)

Rules recap: do not send. All items are `status=proposed`. Drafts below are quote-based and link to TrustedRouter context pages.

## Public surfaces check

- OK `https://trustedrouter.com/` (web tool)
- Verified these routes via repo templates (web tool + shell network could not retrieve them in this environment):
  - `https://trustedrouter.com/compare/openrouter`
  - `https://trustedrouter.com/docs/migrate-from-openrouter`
  - `https://trustedrouter.com/security`
  - `https://trustedrouter.com/models`

## Proposed outreach items (do not send)

### 1) steipete/CodexBar

- Evidence: `https://github.com/steipete/CodexBar/blob/main/docs/openrouter.md`
- Score: 60
- Channel: GitHub issue/discussion (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash.`
- Approved message (draft):
```text
Noticed CodexBar documents an OpenRouter provider (OPENROUTER_API_KEY + base URL).

"Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Open a GitHub issue/discussion with the message above.
- Status: proposed
- Opt out: false
- Next action: Human approval of exact GitHub text + target thread.

### 2) rnd-pro/agent-aggregator

- Evidence: `https://github.com/rnd-pro/agent-aggregator`
- Score: 85
- Channel: GitHub issue/discussion (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `We never log your prompt or the output.`
- Approved message (draft):
```text
Saw agent-aggregator supports OpenRouter as a provider for agent execution.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Open a GitHub issue/discussion with the message above.
- Status: proposed
- Opt out: false
- Next action: Human approval of exact GitHub text + target thread.

### 3) OpenClaw (OpenRouter provider docs)

- Evidence: `https://docs.openclaw.ai/providers/openrouter`
- Score: 85
- Channel: GitHub issue/discussion (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `We never log your prompt or the output.`
- Approved message (draft):
```text
Noticed OpenClaw supports OpenRouter as an OpenAI-compatible base_url for agent workflows.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Open a GitHub issue/discussion with the message above (or a docs PR note) suggesting TrustedRouter as an OpenAI-compatible base_url option.
- Status: proposed
- Opt out: false
- Next action: Human approval of exact text + where to post (docs vs repo).

### 4) tensakulabs/openclaw-mem0

- Evidence: `https://github.com/tensakulabs/openclaw-mem0`
- Score: 85
- Channel: GitHub issue/discussion (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `We never log your prompt or the output.`
- Approved message (draft):
```text
Saw openclaw-mem0 shows configs that route embedder + LLM calls through OpenRouter.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Open a GitHub issue/discussion with the message above.
- Status: proposed
- Opt out: false
- Next action: Human approval of exact GitHub text + target thread.

### 5) anomalyco/opencode (OpenRouter config issue)

- Evidence: `https://github.com/anomalyco/opencode/issues/749`
- Score: 85
- Channel: GitHub issue reply (manual)
- Context link: `https://trustedrouter.com/docs/migrate-from-openrouter`
- Relevant quote:
  - `It is as simple as changing one URL, so one line of code.`
- Approved message (draft):
```text
Saw opencode users configuring OpenRouter baseURL + OPENROUTER_API_KEY for coding agent models.

"It is as simple as changing one URL, so one line of code."

Context: https://trustedrouter.com/docs/migrate-from-openrouter
```
- Exact proposed action: Reply in the issue thread with the message above (positioning TrustedRouter as an OpenAI-compatible base_url option for higher-sensitivity prompts).
- Status: proposed
- Opt out: false
- Next action: Human approval of exact GitHub reply text.

### 6) renatogalera/ai-commit

- Evidence: `https://github.com/renatogalera/ai-commit`
- Score: 85
- Channel: GitHub issue/discussion (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `We never log your prompt or the output.`
- Approved message (draft):
```text
Noticed ai-commit supports OpenRouter as a provider for commit message generation and code review.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Open a GitHub issue/discussion with the message above.
- Status: proposed
- Opt out: false
- Next action: Human approval of exact GitHub text + target thread.

### 7) ARMES (product site)

- Evidence: `https://armes.ai/`
- Score: 65
- Channel: LinkedIn DM or email (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `We never log your prompt or the output.`
- Approved message (draft):
```text
Saw ARMES positions itself as a private multi-model chat app powered by OpenRouter.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Send a short LinkedIn DM (or email) with the message above.
- Status: proposed
- Opt out: false
- Next action: Human approval + find correct founder contact.

### 8) bytedance/trae-agent

- Evidence: `https://github.com/bytedance/trae-agent`
- Score: 85
- Channel: GitHub issue/discussion (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash.`
- Approved message (draft):
```text
Noticed trae-agent supports OpenRouter (and other providers) for software engineering agents.

"Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Open a GitHub issue/discussion with the message above.
- Status: proposed
- Opt out: false
- Next action: Human approval of exact GitHub text + target thread.

### 9) elizaos-plugins/plugin-openrouter

- Evidence: `https://github.com/elizaos-plugins/plugin-openrouter`
- Score: 80
- Channel: GitHub issue/discussion (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `We never log your prompt or the output.`
- Approved message (draft):
```text
Saw plugin-openrouter documents routing agent model calls through OpenRouter (OPENROUTER_API_KEY + base URL).

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Open a GitHub issue/discussion with the message above.
- Status: proposed
- Opt out: false
- Next action: Human approval of exact GitHub text + target thread.

### 10) xrip/ollama-api-proxy

- Evidence: `https://github.com/xrip/ollama-api-proxy`
- Score: 80
- Channel: GitHub issue/discussion (manual)
- Context link: `https://trustedrouter.com/security`
- Relevant quote:
  - `We never log your prompt or the output.`
- Approved message (draft):
```text
Noticed ollama-api-proxy supports OpenRouter models (and other providers) behind an Ollama-compatible proxy for JetBrains AI Assistant.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```
- Exact proposed action: Open a GitHub issue/discussion with the message above.
- Status: proposed
- Opt out: false
- Next action: Human approval of exact GitHub text + target thread.

## Blockers / needs human approval

1. Approve exact text + where to post for each GitHub item (issue vs discussion vs existing thread).
2. For ARMES: identify the correct founder contact + approve exact DM/email text.

