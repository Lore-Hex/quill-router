# TrustedRouter revenue loop — 2026-05-04 (run 10)

Run time (UTC): 2026-05-04T20:00:00Z

## 1) Public surfaces check (web)

- ✅ `https://trustedrouter.com/` reachable; homepage highlights OpenAI-SDK `base_url="https://api.quillrouter.com/v1"` and “cryptographically attested non-logging” messaging.
- ⚠️ Web tool still blocked these required subpaths (safe-browsing restriction):
  - `https://trustedrouter.com/compare/openrouter`
  - `https://trustedrouter.com/docs/migrate-from-openrouter`
  - `https://trustedrouter.com/security`
  - `https://trustedrouter.com/models`

## 2) New qualified leads/opportunities (score >= 60)

Scoring rubric applied:
- +30 uses OpenRouter/LiteLLM/Vercel AI SDK
- +25 handles sensitive data
- +20 agent/coding/devtools/security/legal/finance/healthcare
- +15 hiring AI infra/platform
- +10 public multi-model usage
- -50 no clear AI usage
- -100 opted out

### A) `pentagi` (vxcontrol) — score 85

- Evidence: `pentagi` docs include OpenRouter provider config and `LLM_SERVER_URL=https://openrouter.ai/api/v1`.
- Source: `https://github.com/vxcontrol/pentagi`
- Segment: security / pentesting agents
- Suspected pain: pentest traces and target context are sensitive; strong need for provable non-logging / verifiable gateway behavior.
- Selected quote: `We never log your prompt or the output.`
- Context link: `https://trustedrouter.com/security`

### B) `markpdfdown` (MarkPDFdown) — score 75

- Evidence: `.env` supports `OPENROUTER_API_KEY` and OpenRouter model slugs for PDF->Markdown processing.
- Source: `https://github.com/MarkPDFdown/markpdfdown`
- Segment: document processing / devtools
- Suspected pain: PDFs frequently contain sensitive customer/legal/financial data; need a higher-sensitivity routing option.
- Selected quote: `We never log your prompt or the output.`
- Context link: `https://trustedrouter.com/security`

### C) `bloom` (safety-research) — score 60

- Evidence: `.env.example` includes `OPENROUTER_API_KEY` (LiteLLM-backed multi-provider workflow).
- Source: `https://github.com/safety-research/bloom/blob/main/.env.example`
- Segment: security / safety evaluation tooling
- Suspected pain: evaluation suites can contain confidential behaviors or internal prompts; want a router path with minimal trust surface.
- Selected quote: `Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash.`
- Context link: `https://trustedrouter.com/security`

### D) `deepwiki-open` (AsyncFuncAI) — score 60

- Evidence: README documents “OpenRouter Integration” with `OPENROUTER_API_KEY`.
- Source: `https://github.com/AsyncFuncAI/deepwiki-open`
- Segment: devtools / docs automation
- Suspected pain: repo contents can include sensitive proprietary code; need a safer routing option when using cloud routers.
- Selected quote: `TrustedRouter is one API, all the LLMs, provably private.`
- Context link: `https://trustedrouter.com/compare/openrouter`

### E) `claw-code-agent` (HarnessLab) — score 60

- Evidence: README “Optional: Use OpenRouter” with `OPENAI_BASE_URL=https://openrouter.ai/api/v1`.
- Source: `https://github.com/HarnessLab/claw-code-agent`
- Segment: coding agent / devtools
- Suspected pain: coding agents routinely ship file contents and shell output; need a router path intended for sensitive repos.
- Selected quote: `The most common feedback I hear from engineers is that they don't use OpenRouter at all because of their security and privacy concerns, especially for sensitive prompts, or they use it only for low-sensitivity prompts.`
- Context link: `https://trustedrouter.com/compare/openrouter`

### F) Open WebUI docs — score 60

- Evidence: docs page instructs connecting Open WebUI to `https://openrouter.ai/api/v1`.
- Source: `https://github.com/open-webui/docs/blob/main/docs/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible.mdx`
- Segment: devtools / infra UI
- Suspected pain: Open WebUI is commonly used for internal chat over proprietary data; a verifiable non-logging router option is relevant.
- Selected quote: `We never log your prompt or the output.`
- Context link: `https://trustedrouter.com/security`

### G) `deerflow2.0-enhanced` (stophobia) — score 60

- Evidence: README shows `base_url: https://openrouter.ai/api/v1` for an OpenRouter model.
- Source: `https://github.com/stophobia/deerflow2.0-enhanced/blob/main/README.md`
- Segment: agents / devtools
- Suspected pain: agent workflows can embed secrets or proprietary context; need safer routing for sensitive runs.
- Selected quote: `It is as simple as changing one URL, so one line of code.`
- Context link: `https://trustedrouter.com/docs/migrate-from-openrouter`

## 3) Notes

- No direct Google Sheets write path available in this run; CSV rows generated in `docs/runs/sheet1-rows-2026-05-04-10.csv`.
- Approval-only outreach drafts are in `docs/runs/approval-packet-2026-05-04-10.md`.

