"""Hourly upstream-price refresh — provider-direct, self-healing scrapers.

Architecture:
  scripts/pricing/base.py         — human-only orchestration (fetch, validate,
                                    self-heal, AST whitelist, sandbox).
  scripts/pricing/refresh.py      — top-level orchestrator run hourly.
  scripts/pricing/providers/<X>.py — human-only per-provider config (URL,
                                     EXPECTED_MODELS). LLM cannot touch.
  scripts/pricing/parsers/<X>.py  — LLM-rewriteable per-provider parser.
                                     Pure `parse(html: str) -> dict`.
                                     No imports outside whitelist.

The LLM-rewriteable surface is sandboxed to `str -> dict` transforms:
network IO, URL choice, and filesystem access live entirely outside
the rewriteable code, so prompt-injected HTML cannot exfiltrate data
or follow injected URLs.
"""
