"""LLM-rewriteable parser modules.

Each module MUST expose a single top-level function
`parse(html: str) -> dict` returning
{model_id: {"prompt_micro_per_m": int, "completion_micro_per_m": int}}.

No imports outside the whitelist enforced in `scripts/pricing/base.py`
(re, bs4, decimal, json, typing, dataclasses). No network IO, no
filesystem, no subprocess. The base framework re-validates these
constraints via AST gate + sandbox subprocess every time a parser
file is rewritten by the self-heal flow.
"""
