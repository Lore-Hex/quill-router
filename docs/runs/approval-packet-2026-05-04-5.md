# Outreach approval packet — 2026-05-04 (run 5)

Status for all items below: `proposed` (do not send/post without explicit human approval for the exact action + exact text).

## 1) MedRAX-2 (bowang-lab) — GitHub issue/discussion (manual)

- Evidence: https://github.com/bowang-lab/MedRAX2
- Score: 85
- Channel: GitHub issue/discussion comment (public) — manual
- Context link: https://trustedrouter.com/security
- Relevant quote (approved):
  - "We never log your prompt or the output."
- Proposed action (requires approval): Ask who owns provider/routing choices for MedRAX deployments and suggest adding TrustedRouter as an optional OpenAI-compatible base URL for higher-sensitivity/clinical prompts.

```text
Noticed MedRAX-2 supports OpenRouter (`OPENROUTER_BASE_URL=https://openrouter.ai/api/v1`) alongside multiple LLM providers. For higher-sensitivity/clinical prompts, TrustedRouter is a drop-in OpenAI-compatible base URL option designed for sensitive prompts.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```

## 2) ArchEHR-QA / LAMAR (biodatlab) — GitHub issue/discussion (manual)

- Evidence: https://github.com/biodatlab/archehr-qa-lamar
- Score: 75
- Channel: GitHub issue/discussion comment (public) — manual
- Context link: https://trustedrouter.com/security
- Relevant quote (approved):
  - "We never log your prompt or the output."
- Proposed action (requires approval): Suggest documenting TrustedRouter as an alternative OpenAI-compatible base URL for users who want a higher-sensitivity routing path than OpenRouter for EHR-related prompts.

```text
Saw the docs mention using OpenRouter via `OPENAI_BASE_URL="https://openrouter.ai/api/v1"`. For EHR-related workflows where prompt sensitivity is higher, TrustedRouter is an OpenAI-compatible base URL option designed for sensitive prompts.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```

## 3) scientific-agent-skills (K-Dense-AI) — GitHub issue/discussion (manual)

- Evidence: https://github.com/K-Dense-AI/scientific-agent-skills/security
- Score: 75
- Channel: GitHub issue/discussion comment (public) — manual
- Context link: https://trustedrouter.com/security
- Relevant quote (approved):
  - "Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash."
- Proposed action (requires approval): Offer TrustedRouter as a “verifiable hosted router” option for teams who want to keep using an OpenRouter-shaped API but want stronger verifiability for sensitive workflows.

```text
Noticed the project references sending requests to OpenRouter (`https://openrouter.ai/api/v1`) and flags exfiltration risk concerns around env vars and external calls. For teams that still want an OpenRouter-shaped, OpenAI-compatible router but want more verifiability, TrustedRouter is an option designed to be verifiable end-to-end.

"Customers can verify that the code that they're reaching out to and talking to is in fact what they're seeing in the open source repo and that stated commit hash."

Context: https://trustedrouter.com/security
```

## 4) Hermes Agent (NousResearch) — GitHub issue/discussion (manual)

- Evidence: https://github.com/NousResearch/hermes-agent/blob/main/cli-config.yaml.example
- Score: 60
- Channel: GitHub issue/discussion comment (public) — manual
- Context link: https://trustedrouter.com/compare/openrouter
- Relevant quote (approved):
  - "TrustedRouter is one API, all the LLMs, provably private."
- Proposed action (requires approval): Suggest adding TrustedRouter as a documented alternative `base_url` option for higher-sensitivity agent workloads that currently point at OpenRouter.

```text
Saw Hermes’ example config defaults to `base_url: "https://openrouter.ai/api/v1"`. If users need a higher-sensitivity routing option for agent context, TrustedRouter is a drop-in OpenAI-compatible alternative.

"TrustedRouter is one API, all the LLMs, provably private."

Context: https://trustedrouter.com/compare/openrouter
```

## 5) OpenClaw — GitHub issue comment (manual)

- Evidence: https://github.com/openclaw/openclaw/issues/51056
- Score: 60
- Channel: GitHub issue comment (public) — manual
- Context link: https://trustedrouter.com/docs/migrate-from-openrouter
- Relevant quote (approved):
  - "It is as simple as changing one URL, so one line of code."
- Proposed action (requires approval): Leave a short, non-spammy comment offering a sensitive-prompt alternative endpoint (TrustedRouter) that’s OpenRouter-compatible for users evaluating router choices.

```text
If you’re using OpenRouter as your OpenAI-compatible gateway, TrustedRouter is an OpenRouter-compatible alternative for higher-sensitivity prompts.

"It is as simple as changing one URL, so one line of code."

Context: https://trustedrouter.com/docs/migrate-from-openrouter
```

## 6) DeepTutor (HKUDS) — GitHub issue/discussion (manual)

- Evidence: https://github.com/HKUDS/DeepTutor
- Score: 60
- Channel: GitHub issue/discussion comment (public) — manual
- Context link: https://trustedrouter.com/compare/openrouter
- Relevant quote (approved):
  - "TrustedRouter is one API, all the LLMs, provably private."
- Proposed action (requires approval): Suggest adding TrustedRouter as another OpenAI-compatible provider option next to the existing OpenRouter provider (positioned for sensitive tutoring/user data contexts).

```text
Saw DeepTutor lists OpenRouter as an OpenAI-compatible provider (`https://openrouter.ai/api/v1`). For higher-sensitivity tutoring/user data contexts, TrustedRouter is a drop-in OpenAI-compatible router option.

"TrustedRouter is one API, all the LLMs, provably private."

Context: https://trustedrouter.com/compare/openrouter
```

## 7) CodexBar OpenRouter provider docs — GitHub issue/discussion (manual)

- Evidence: https://github.com/steipete/CodexBar/blob/main/docs/openrouter.md
- Score: 60
- Channel: GitHub issue/discussion comment (public) — manual
- Context link: https://trustedrouter.com/security
- Relevant quote (approved):
  - "We never log your prompt or the output."
- Proposed action (requires approval): Suggest a small docs note: “for sensitive prompts, consider a non-logging/verifiable router option (TrustedRouter)”, without changing defaults.

```text
Noticed CodexBar documents OpenRouter as a provider. If some users want a higher-sensitivity routing path for sensitive prompts, TrustedRouter is an OpenAI-compatible router option designed for sensitive prompts.

"We never log your prompt or the output."

Context: https://trustedrouter.com/security
```

## 8) cliProxyAPI-Dashboard OpenRouter config example — GitHub issue/discussion (manual)

- Evidence: https://github.com/0xAstroAlpha/cliProxyAPI-Dashboard/blob/main/config.example.yaml
- Score: 60
- Channel: GitHub issue/discussion comment (public) — manual
- Context link: https://trustedrouter.com/docs/migrate-from-openrouter
- Relevant quote (approved):
  - "It is as simple as changing one URL, so one line of code."
- Proposed action (requires approval): Suggest adding TrustedRouter as an example OpenAI-compatible `base-url` option for users who want a higher-sensitivity router.

```text
Saw the config example includes OpenRouter (`base-url: https://openrouter.ai/api/v1`). For higher-sensitivity prompts, TrustedRouter is an OpenAI-compatible router alternative.

"It is as simple as changing one URL, so one line of code."

Context: https://trustedrouter.com/docs/migrate-from-openrouter
```

