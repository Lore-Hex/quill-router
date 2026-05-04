# Revenue loop run (2026-05-04 #7)

## Public surfaces check (via web tool)

- OK `https://trustedrouter.com/`
- OK `https://trust.trustedrouter.com/`
- BLOCKED by safe-browsing (web tool could not fetch these required subpaths today):
  - `https://trustedrouter.com/compare/openrouter`
  - `https://trustedrouter.com/docs/migrate-from-openrouter`
  - `https://trustedrouter.com/security`
  - `https://trustedrouter.com/models`

## New qualified leads / opportunities (score >= 60)

Scoring rubric: +30 OpenRouter/LiteLLM/Vercel AI SDK, +25 sensitive data, +20 agent/coding/devtools/security/legal/finance/healthcare, +15 hiring AI infra/platform, +10 multi-model usage.

1) Open Cowork (OSS agent desktop app; supports OpenRouter)
   - Evidence: `https://github.com/OpenCoworkAI/open-cowork`
   - Score: 60 (+30 OpenRouter, +20 devtools/agent, +10 multi-provider table)
   - Quote/context: trust / `https://trustedrouter.com/security`

2) Open-Sable (local-first agent framework; OpenRouter is a supported provider)
   - Evidence: `https://github.com/IdeoaLabs/Open-Sable`
   - Score: 60 (+30 OpenRouter, +20 agent framework, +10 multi-provider support)
   - Quote/context: trust / `https://trustedrouter.com/security`

3) ValueCell (multi-agent platform for financial applications; supports OpenRouter)
   - Evidence: `https://github.com/ValueCell-ai/valuecell`
   - Score: 85 (+30 OpenRouter, +25 finance sensitivity, +20 finance, +10 multi-provider)
   - Quote/context: trust / `https://trustedrouter.com/security`

4) MedRAX-2 (medical RAG tooling; supports OpenRouter)
   - Evidence: `https://github.com/bowang-lab/MedRAX2`
   - Score: 85 (+30 OpenRouter, +25 healthcare sensitivity, +20 healthcare, +10 multi-provider)
   - Quote/context: trust / `https://trustedrouter.com/security`

5) OpenSail (agentic coding platform; uses LiteLLM + BYOK incl OpenRouter)
   - Evidence: `https://github.com/TesslateAI/OpenSail`
   - Score: 60 (+30 LiteLLM, +20 devtools/agentic coding, +10 multi-provider)
   - Quote/context: open_source / `https://trustedrouter.com/security`

6) cliProxyAPI-Dashboard (OpenAI-compat gateway config includes OpenRouter base-url)
   - Evidence: `https://github.com/0xAstroAlpha/cliProxyAPI-Dashboard/blob/main/config.example.yaml`
   - Score: 60 (+30 OpenRouter, +20 gateway/devtools, +10 multi-provider pattern)
   - Quote/context: trust (verifiable running code) / `https://trustedrouter.com/security`

7) CLIProxyAPIPlus (OpenAI-compat providers config includes OpenRouter base-url)
   - Evidence: `https://github.com/router-for-me/CLIProxyAPIPlus/blob/main/config.example.yaml`
   - Score: 60 (+30 OpenRouter, +20 gateway/devtools, +10 multi-provider pattern)
   - Quote/context: trust (verifiable running code) / `https://trustedrouter.com/security`

8) OpenClaw OpenRouter auth issue (GitHub issue; OpenRouter baseUrl + API-key auth)
   - Evidence: `https://github.com/openclaw/openclaw/issues/51056`
   - Score: 60 (+30 OpenRouter, +20 devtools, +10 multi-provider-ish via provider config)
   - Quote/context: openrouter (privacy concerns) / `https://trustedrouter.com/compare/openrouter`

## Outputs

- CSV rows for `Sheet1`: `docs/runs/sheet1-rows-2026-05-04-7.csv`
- Approval packet: `docs/runs/approval-packet-2026-05-04-7.md`

