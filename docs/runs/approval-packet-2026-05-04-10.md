# Outreach / Social Approval Packet — 2026-05-04 (run 10)

Status: all items `proposed` (DO NOT SEND/POST)

## A) Lead outreach drafts (quote-based)

### 1) vxcontrol/pentagi — proposed GitHub issue/discussion

Exact proposed action:
- Create a GitHub issue (or discussion) on `https://github.com/vxcontrol/pentagi` offering TrustedRouter as an optional OpenAI-compatible `base_url` for higher-sensitivity pentest traces.

Approved message draft:

```text
Noticed `pentagi` ships an OpenRouter provider config (`LLM_SERVER_URL=https://openrouter.ai/api/v1`). If you ever need a higher-sensitivity router option for pentest traces / target context, TrustedRouter is OpenAI-compatible and designed to be verifiable.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```

### 2) MarkPDFdown/markpdfdown — proposed GitHub issue/discussion

Exact proposed action:
- Create a GitHub issue (or discussion) on `https://github.com/MarkPDFdown/markpdfdown` suggesting TrustedRouter as an optional OpenAI-compatible router path for sensitive PDFs.

Approved message draft:

```text
Saw `markpdfdown` supports OpenRouter (`OPENROUTER_API_KEY` + `MODEL_NAME=openrouter/...`). For teams converting sensitive PDFs (legal/finance/customer docs), TrustedRouter is a drop-in OpenAI-compatible base_url option meant for higher-sensitivity prompts.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```

### 3) safety-research/bloom — proposed GitHub issue/discussion

Exact proposed action:
- Create a GitHub issue (or discussion) on `https://github.com/safety-research/bloom` suggesting TrustedRouter as a verifiable OpenAI-compatible routing option (where users care about auditing what’s running).

Approved message draft:

```text
Noticed Bloom includes `OPENROUTER_API_KEY` in `.env.example`. For teams running safety evals with sensitive seeds/prompts, TrustedRouter is an OpenAI-compatible router option focused on verifiability.

"Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash."

Context: https://trustedrouter.com/security
```

### 4) AsyncFuncAI/deepwiki-open — proposed GitHub issue/discussion

Exact proposed action:
- Create a GitHub issue (or discussion) on `https://github.com/AsyncFuncAI/deepwiki-open` offering TrustedRouter as an OpenAI-compatible alternative base_url for sensitive repos.

Approved message draft:

```text
Saw DeepWiki’s README includes “OpenRouter Integration”. If you’re generating docs over proprietary repos and want a higher-sensitivity router option, TrustedRouter is OpenAI-compatible and designed to be verifiable.

"TrustedRouter is one API, all the LLMs, provably private."

Context: https://trustedrouter.com/compare/openrouter
```

### 5) HarnessLab/claw-code-agent — proposed GitHub issue/discussion

Exact proposed action:
- Create a GitHub issue (or discussion) on `https://github.com/HarnessLab/claw-code-agent` suggesting TrustedRouter as an alternative base_url for sensitive coding-agent sessions (files + shell output).

Approved message draft:

```text
Noticed `claw-code-agent` documents OpenRouter (`OPENAI_BASE_URL=https://openrouter.ai/api/v1`). For coding-agent runs that include sensitive repo contents and shell output, TrustedRouter is an OpenAI-compatible router option aimed at higher-sensitivity prompts.

"The most common feedback I hear from engineers is that they don't use OpenRouter at all because of their security and privacy concerns, especially for sensitive prompts, or they use it only for low-sensitivity prompts."

Context: https://trustedrouter.com/compare/openrouter
```

### 6) open-webui/docs — proposed PR or issue

Exact proposed action:
- Open an issue (or PR) on `https://github.com/open-webui/docs` adding TrustedRouter alongside OpenRouter in the “OpenAI-compatible providers” doc, positioned specifically for higher-sensitivity prompts.

Approved message draft:

```text
Saw the Open WebUI docs include an OpenRouter connection example (`https://openrouter.ai/api/v1`). If it’s useful, TrustedRouter is also an OpenAI-compatible base_url option designed to be verifiable for higher-sensitivity prompts.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```

### 7) stophobia/deerflow2.0-enhanced — proposed GitHub issue/discussion

Exact proposed action:
- Create a GitHub issue (or discussion) on `https://github.com/stophobia/deerflow2.0-enhanced` suggesting TrustedRouter as a 1-line base_url swap option for sensitive agent runs.

Approved message draft:

```text
Noticed the README shows OpenRouter via `base_url: https://openrouter.ai/api/v1`. If you ever need a higher-sensitivity routing option for agent workflows that include sensitive context, TrustedRouter is OpenAI-compatible.

"It is as simple as changing one URL, so one line of code."

Context: https://trustedrouter.com/docs/migrate-from-openrouter
```

## B) Social approval queue items (quote-based; DO NOT POST)

### 1) X post — positioning (OpenRouter privacy wedge)

- Channel: X
- Target URL: https://trustedrouter.com/compare/openrouter
- Action type: post
- Status: proposed

Approved message draft:

```text
"The most common feedback I hear from engineers is that they don't use OpenRouter at all because of their security and privacy concerns, especially for sensitive prompts, or they use it only for low-sensitivity prompts."

Context: https://trustedrouter.com/compare/openrouter
```

### 2) LinkedIn post — trust claim (non-logging)

- Channel: LinkedIn
- Target URL: https://trustedrouter.com/security
- Action type: post
- Status: proposed

Approved message draft:

```text
"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```

