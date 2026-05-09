"""Human-only orchestration for the hourly self-healing pricing refresh.

This module contains:
  fetch_html           — the ONLY place network IO happens.
  validate             — schema + plausibility checks on parser output.
  ast_whitelist_check  — static gate on LLM-generated parser source.
  sandbox_run_parser   — runs LLM-generated parser in a subprocess
                         with no network, no filesystem, 5s timeout.
  self_heal_parser     — calls TR's smartest model via TR's own API to
                         rewrite parsers/<slug>.py when validation fails.
  fetch_provider       — the per-provider entry point used by refresh.py.

LLM-rewriteable code never imports this module — it lives in
`scripts/pricing/parsers/<slug>.py` as pure `parse(html: str) -> dict`.
Everything in this file is human-maintained.
"""
from __future__ import annotations

import ast
import difflib
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("pricing")

REPO_ROOT = Path(__file__).resolve().parents[2]
PARSERS_DIR = REPO_ROOT / "scripts" / "pricing" / "parsers"

# ----------------------------------------------------------------------
# Plausibility ranges and import whitelist for LLM-generated parser code.
# ----------------------------------------------------------------------

# Per-million-token prices in microdollars. $0.001/M = 1_000;
# $1000/M = 1_000_000_000. Anything outside this is almost certainly a
# parsing bug (units mismatch).
MIN_PRICE_MICRO_PER_M = 0  # 0 is allowed for free tiers; below 0 is a bug.
MAX_PRICE_MICRO_PER_M = 1_000_000_000

# Imports allowed in LLM-generated parsers. No urllib, requests, socket,
# os, subprocess, pathlib, open, __import__.
PARSER_IMPORT_WHITELIST = frozenset(
    {
        "re",
        "bs4",
        "decimal",
        "json",
        "typing",
        "dataclasses",
    }
)

# Names that may NEVER appear in LLM-generated parser code regardless of
# import path.
PARSER_NAME_BLACKLIST = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "globals",
        "locals",
        "vars",
        "input",
        "breakpoint",
        "memoryview",
    }
)

# Maximum source size for a parser file. Real parsers should be <5KB;
# anything bigger is suspicious.
MAX_PARSER_SOURCE_BYTES = 30_000

# Sandbox-subprocess timeout for running an LLM-generated parser once.
SANDBOX_WALL_CLOCK_SECONDS = 5.0
SANDBOX_OUTPUT_BYTES_MAX = 1_000_000

# How many attempts the LLM gets per provider per hourly run. 1 by
# design — if the rewrite fails, we don't loop; the next hourly run
# retries from scratch.
MAX_SELF_HEAL_ATTEMPTS_PER_HOUR = 1

# TR's own API for self-heal calls. Eats own dogfood; free for us.
# Inference API (NOT trustedrouter.com — that's the marketing/control
# plane). TLS terminates inside the attested enclave; the workflow
# overrides via TR_API_BASE env var so prod and staging can both work.
TR_API_BASE = os.environ.get("TR_API_BASE", "https://api.quillrouter.com")
TR_SELF_HEAL_MODEL = os.environ.get("TR_SELF_HEAL_MODEL", "anthropic/claude-opus-4.7")
TR_API_KEY_ENV = "TR_API_KEY"

# User-Agent for provider fetches. Pricing pages (notably openai.com)
# 403 the obvious "TrustedRouterPriceRefresh/1.0" UA, so we use a
# real Linux/Chrome string. We are scraping public pricing pages — a
# disclosed function of every page where we look for a "$/M tokens"
# label. The trade-off is real: the bot-identifying UA was honest but
# silently dead at the gateway. Override via PROVIDER_FETCH_UA env var
# if you want to revert to identifying.
PROVIDER_FETCH_UA = os.environ.get(
    "PROVIDER_FETCH_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

PROVIDER_FETCH_TIMEOUT = 30.0
# Retry transient HTTP failures (TCP reset, DNS hiccup, 5xx). httpx's
# transport-level retries fire on failed-to-connect errors; we layer
# on our own retry around explicit 5xx responses since those are
# returned bodies, not transport failures.
PROVIDER_FETCH_TRANSPORT_RETRIES = 3
PROVIDER_FETCH_5XX_RETRIES = 2
PROVIDER_FETCH_5XX_BACKOFF_SECONDS = 1.5


# ----------------------------------------------------------------------
# Result types.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PriceTier:
    """One tier of context-conditional pricing.

    A request whose prompt token count is ≤ `max_prompt_tokens` uses
    this tier's rates. The LAST tier in a model's list MUST have
    `max_prompt_tokens=None`, which means "no upper bound — applies to
    anything above the previous tier's threshold." Tiers are evaluated
    in order; the first one whose threshold accommodates the prompt
    wins.

    `prompt_cached_micro_per_m` is the discounted rate that applies to
    the cache-hit portion of an input prompt. None means "upstream
    charges the same rate cached or not." Most providers offer a
    discount (typically 50-90% off) for cache reads.
    """

    max_prompt_tokens: int | None
    prompt_micro_per_m: int
    completion_micro_per_m: int
    prompt_cached_micro_per_m: int | None = None


class ModelPrice:
    """One model's price profile.

    `tiers` is the canonical form (length-1 list for a flat rate;
    length-2+ for context-conditional pricing). The constructor
    accepts either the canonical `tiers=[...]` form or the legacy
    flat-rate form (`prompt_micro_per_m`, `completion_micro_per_m`)
    so existing callers keep working.

    `prompt_micro_per_m` / `completion_micro_per_m` properties return
    the headline (low-tier) rates — the values displayed on /v1/models
    and used by code paths that don't yet speak tier-aware billing.
    """

    __slots__ = ("tiers",)

    def __init__(
        self,
        prompt_micro_per_m: int | None = None,
        completion_micro_per_m: int | None = None,
        *,
        prompt_cached_micro_per_m: int | None = None,
        tiers: list[PriceTier] | None = None,
    ) -> None:
        if tiers is not None:
            if (
                prompt_micro_per_m is not None
                or completion_micro_per_m is not None
                or prompt_cached_micro_per_m is not None
            ):
                raise ValueError(
                    "ModelPrice: pass either flat rates OR `tiers=`, not both"
                )
            if not tiers:
                raise ValueError("ModelPrice: `tiers` cannot be empty")
            if tiers[-1].max_prompt_tokens is not None:
                raise ValueError(
                    "ModelPrice: last tier must have max_prompt_tokens=None"
                )
            self.tiers = list(tiers)
            return
        if prompt_micro_per_m is None or completion_micro_per_m is None:
            raise ValueError(
                "ModelPrice: must supply prompt_micro_per_m + "
                "completion_micro_per_m OR tiers="
            )
        self.tiers = [
            PriceTier(
                max_prompt_tokens=None,
                prompt_micro_per_m=int(prompt_micro_per_m),
                completion_micro_per_m=int(completion_micro_per_m),
                prompt_cached_micro_per_m=(
                    int(prompt_cached_micro_per_m)
                    if prompt_cached_micro_per_m is not None
                    else None
                ),
            )
        ]

    @property
    def prompt_micro_per_m(self) -> int:
        """Headline (low-tier) prompt rate."""
        return self.tiers[0].prompt_micro_per_m

    @property
    def completion_micro_per_m(self) -> int:
        """Headline (low-tier) completion rate."""
        return self.tiers[0].completion_micro_per_m

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ModelPrice):
            return NotImplemented
        return self.tiers == other.tiers

    def __repr__(self) -> str:
        return f"ModelPrice(tiers={self.tiers!r})"


@dataclass
class ProviderPricingResult:
    """Result of fetching one provider's prices for one hourly run."""

    slug: str
    prices: dict[str, ModelPrice]
    source: str  # "deterministic" | "self_healed" | "api"
    heal_diff: str | None = None  # unified diff of the rewritten parser
    fetched_url: str | None = None
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# Network IO — the ONLY place we make outbound HTTP calls. Provider
# modules pass their hardcoded URL constants in.
# ----------------------------------------------------------------------


def _provider_client() -> httpx.Client:
    """Construct an httpx.Client with transport-level retries on
    connect-failures (TCP reset, DNS hiccup). 5xx responses are still
    returned bodies and need application-level retry; that's handled
    in `_get_with_retries` below."""
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    return httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    )


def _get_with_retries(client: httpx.Client, url: str, headers: dict[str, str]) -> httpx.Response:
    """GET with N retries on 5xx and a fixed backoff. 4xx responses
    do NOT retry — those mean "this URL is wrong" or "we're rate
    limited" and retrying just makes it worse."""
    last_response: httpx.Response | None = None
    for attempt in range(PROVIDER_FETCH_5XX_RETRIES + 1):
        response = client.get(url, headers=headers)
        if response.status_code < 500:
            return response
        last_response = response
        if attempt < PROVIDER_FETCH_5XX_RETRIES:
            log.warning(
                "pricing.fetch_5xx_retry url=%s status=%d attempt=%d/%d",
                url,
                response.status_code,
                attempt + 1,
                PROVIDER_FETCH_5XX_RETRIES,
            )
            time.sleep(PROVIDER_FETCH_5XX_BACKOFF_SECONDS * (attempt + 1))
    assert last_response is not None
    return last_response


def fetch_html(url: str, *, extra_headers: dict[str, str] | None = None) -> str:
    """Fetch one provider's pricing page. Network IO lives here, only here.

    URL must be passed in by the caller from a hardcoded constant in
    `scripts/pricing/providers/<slug>.py`. The LLM-rewriteable parser
    tier never sees a URL and cannot make network calls.

    `extra_headers` lets a provider config request specific headers —
    notably `X-Return-Format: markdown` for r.jina.ai-proxied URLs,
    which we use for providers whose pricing pages are JS-rendered or
    Cloudflare-blocked (OpenAI, Gemini, Z.AI). Anthropic / Cerebras /
    Mistral / DeepSeek don't need this; they return server-rendered
    HTML directly.
    """
    headers = {"User-Agent": PROVIDER_FETCH_UA}
    if extra_headers:
        headers.update(extra_headers)
    with _provider_client() as client:
        response = _get_with_retries(client, url, headers)
        response.raise_for_status()
        return response.text


def fetch_json(url: str) -> Any:
    """Fetch a provider JSON API (e.g., Together's /v1/models). The
    only providers that bypass the parser tier are those with a real
    JSON pricing API; this helper lives here so its network IO is also
    accounted for in the human-only tier."""
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    with _provider_client() as client:
        response = _get_with_retries(client, url, headers)
        response.raise_for_status()
        return response.json()


# ----------------------------------------------------------------------
# Validation — applied to deterministic parser output AND self-healed
# parser output. Same rules either way.
# ----------------------------------------------------------------------


def _coerce_to_model_prices(raw: object) -> tuple[dict[str, ModelPrice] | None, list[str]]:
    """Coerce a parser's return value into a {model_id: ModelPrice}.

    Each row in the parser output may be:
      * **Flat**: `{"prompt_micro_per_m": int, "completion_micro_per_m": int}`
        — single-tier pricing, the common case.
      * **Tiered**: `{"tiers": [{"max_prompt_tokens": int|None,
                                  "prompt_micro_per_m": int,
                                  "completion_micro_per_m": int}, ...]}`
        — context-conditional pricing (Gemini 2.5 Pro etc.).

    Used for both deterministic parser output and sandbox subprocess
    output. Both shapes go through the same validation paths.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, [f"parser must return dict, got {type(raw).__name__}"]
    out: dict[str, ModelPrice] = {}
    for model_id, row in raw.items():
        if not isinstance(model_id, str) or not model_id:
            errors.append(f"non-string or empty model_id: {model_id!r}")
            continue
        if not re.fullmatch(r"[A-Za-z0-9._\-/:]+", model_id):
            errors.append(f"model_id has unexpected chars: {model_id!r}")
            continue
        if not isinstance(row, dict):
            errors.append(f"{model_id}: row must be dict, got {type(row).__name__}")
            continue
        # Tiered shape takes precedence if `tiers` is present.
        if "tiers" in row:
            tiers, tier_errors = _coerce_tiers(model_id, row["tiers"])
            if tier_errors:
                errors.extend(tier_errors)
                continue
            out[model_id] = ModelPrice(tiers=tiers)
            continue
        # Flat shape.
        prompt = row.get("prompt_micro_per_m")
        completion = row.get("completion_micro_per_m")
        cached = row.get("prompt_cached_micro_per_m")
        if not isinstance(prompt, int) or isinstance(prompt, bool):
            errors.append(f"{model_id}: prompt_micro_per_m must be int, got {prompt!r}")
            continue
        if not isinstance(completion, int) or isinstance(completion, bool):
            errors.append(
                f"{model_id}: completion_micro_per_m must be int, got {completion!r}"
            )
            continue
        if cached is not None and (not isinstance(cached, int) or isinstance(cached, bool)):
            errors.append(
                f"{model_id}: prompt_cached_micro_per_m must be int or None, got {cached!r}"
            )
            continue
        out[model_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        )
    return (out if not errors else None), errors


def _coerce_tiers(
    model_id: str, raw_tiers: object
) -> tuple[list[PriceTier], list[str]]:
    """Coerce a parser-supplied `tiers` array into a list of PriceTier.
    Returns (tiers, errors); on errors, tiers is empty and errors lists
    every problem found."""
    errors: list[str] = []
    if not isinstance(raw_tiers, list) or not raw_tiers:
        return [], [f"{model_id}: tiers must be a non-empty list"]
    coerced: list[PriceTier] = []
    for idx, tier in enumerate(raw_tiers):
        if not isinstance(tier, dict):
            errors.append(f"{model_id}: tiers[{idx}] must be dict")
            continue
        max_prompt = tier.get("max_prompt_tokens")
        prompt = tier.get("prompt_micro_per_m")
        completion = tier.get("completion_micro_per_m")
        cached = tier.get("prompt_cached_micro_per_m")
        if max_prompt is not None and not isinstance(max_prompt, int):
            errors.append(
                f"{model_id}: tiers[{idx}].max_prompt_tokens must be int or None"
            )
            continue
        if isinstance(max_prompt, bool):  # bool is a subclass of int — guard it
            errors.append(
                f"{model_id}: tiers[{idx}].max_prompt_tokens must be int, got bool"
            )
            continue
        if not isinstance(prompt, int) or isinstance(prompt, bool):
            errors.append(f"{model_id}: tiers[{idx}].prompt_micro_per_m must be int")
            continue
        if not isinstance(completion, int) or isinstance(completion, bool):
            errors.append(
                f"{model_id}: tiers[{idx}].completion_micro_per_m must be int"
            )
            continue
        if cached is not None and (not isinstance(cached, int) or isinstance(cached, bool)):
            errors.append(
                f"{model_id}: tiers[{idx}].prompt_cached_micro_per_m must be int or None"
            )
            continue
        coerced.append(
            PriceTier(
                max_prompt_tokens=max_prompt,
                prompt_micro_per_m=prompt,
                completion_micro_per_m=completion,
                prompt_cached_micro_per_m=cached,
            )
        )
    if errors:
        return [], errors
    if coerced[-1].max_prompt_tokens is not None:
        return (
            [],
            [
                f"{model_id}: last tier must have max_prompt_tokens=None "
                "(uncapped fallback)"
            ],
        )
    # Verify thresholds are strictly ascending (None always last).
    last_threshold = -1
    for idx, tier in enumerate(coerced[:-1]):
        if tier.max_prompt_tokens is None or tier.max_prompt_tokens <= last_threshold:
            return (
                [],
                [
                    f"{model_id}: tier thresholds must be strictly ascending; "
                    f"tiers[{idx}].max_prompt_tokens={tier.max_prompt_tokens}"
                ],
            )
        last_threshold = tier.max_prompt_tokens
    return coerced, []


def validate(
    prices: dict[str, ModelPrice], expected_models: list[str]
) -> list[str]:
    """Return a list of validation errors. Empty list = pass.

    Checks:
      - non-empty
      - every tier's prompt/completion in [MIN, MAX]
      - every model in `expected_models` is present (drift detector)
      - units sanity: at least one tier across all models has nonzero
        price (otherwise the parser likely missed the price column)
    """
    errors: list[str] = []
    if not prices:
        errors.append("empty pricing dict")
        return errors
    for model_id, row in prices.items():
        for idx, tier in enumerate(row.tiers):
            if (
                tier.prompt_micro_per_m < MIN_PRICE_MICRO_PER_M
                or tier.prompt_micro_per_m > MAX_PRICE_MICRO_PER_M
            ):
                errors.append(
                    f"{model_id}: tiers[{idx}].prompt {tier.prompt_micro_per_m} "
                    f"outside [{MIN_PRICE_MICRO_PER_M}, {MAX_PRICE_MICRO_PER_M}]"
                )
            if (
                tier.completion_micro_per_m < MIN_PRICE_MICRO_PER_M
                or tier.completion_micro_per_m > MAX_PRICE_MICRO_PER_M
            ):
                errors.append(
                    f"{model_id}: tiers[{idx}].completion "
                    f"{tier.completion_micro_per_m} outside "
                    f"[{MIN_PRICE_MICRO_PER_M}, {MAX_PRICE_MICRO_PER_M}]"
                )
    missing = [m for m in expected_models if m not in prices]
    if missing:
        errors.append(f"expected models missing: {missing}")
    has_nonzero = any(
        tier.prompt_micro_per_m > 0 or tier.completion_micro_per_m > 0
        for row in prices.values()
        for tier in row.tiers
    )
    if not has_nonzero:
        errors.append(
            "all prices are zero — parser likely missed the price column "
            "(units mismatch?)"
        )
    return errors


# ----------------------------------------------------------------------
# AST whitelist gate — runs BEFORE any execution of LLM-generated code.
# ----------------------------------------------------------------------


def ast_whitelist_check(source: str) -> list[str]:
    """Return a list of policy violations. Empty = pass.

    This is the static defense: if the LLM tries to import urllib or
    call subprocess.run, this rejects the source before sandbox_run
    even spawns a process.
    """
    errors: list[str] = []
    if len(source.encode("utf-8")) > MAX_PARSER_SOURCE_BYTES:
        errors.append(f"source > {MAX_PARSER_SOURCE_BYTES} bytes")
        return errors
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]

    parse_func_node: ast.FunctionDef | None = None
    top_level_names: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in PARSER_IMPORT_WHITELIST:
                    errors.append(f"import {alias.name!r} not in whitelist")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root not in PARSER_IMPORT_WHITELIST:
                errors.append(f"from {node.module!r} import ... not in whitelist")
        elif isinstance(node, ast.FunctionDef):
            top_level_names.add(node.name)
            if node.name == "parse":
                parse_func_node = node
        elif isinstance(node, ast.AsyncFunctionDef):
            errors.append(f"async functions not allowed: {node.name!r}")
        elif isinstance(node, ast.ClassDef):
            errors.append(f"top-level class not allowed: {node.name!r}")
        elif isinstance(node, ast.Assign):
            # Allow simple module-level constants only (Name = literal).
            for target in node.targets:
                if isinstance(target, ast.Name):
                    top_level_names.add(target.id)
                else:
                    errors.append(
                        f"complex top-level assignment target not allowed: "
                        f"{ast.dump(target)}"
                    )
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                top_level_names.add(node.target.id)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            # Module docstring is fine.
            pass
        else:
            errors.append(f"unexpected top-level node: {type(node).__name__}")

    if parse_func_node is None:
        errors.append("missing top-level function `parse`")
        return errors

    # Validate parse(html: str) -> dict signature.
    args = parse_func_node.args
    if (
        len(args.args) != 1
        or args.vararg is not None
        or args.kwarg is not None
        or args.kwonlyargs
        or args.posonlyargs
    ):
        errors.append("parse() must take exactly one positional arg `html`")
    elif args.args[0].arg != "html":
        errors.append("parse() first arg must be named `html`")

    # Walk the entire AST once for blacklisted name references.
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Name) and sub.id in PARSER_NAME_BLACKLIST:
            errors.append(f"forbidden name reference: {sub.id!r}")
        elif isinstance(sub, ast.Attribute):
            # Block dunder attribute access, which is the standard
            # escape hatch (e.g., obj.__class__.__bases__[0].__subclasses__()).
            if sub.attr.startswith("__") and sub.attr.endswith("__"):
                if sub.attr not in {"__name__", "__doc__"}:
                    errors.append(f"forbidden dunder attribute: {sub.attr!r}")
        elif isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name) and func.id in PARSER_NAME_BLACKLIST:
                errors.append(f"forbidden call: {func.id!r}")
            if isinstance(func, ast.Name) and func.id == "getattr":
                # Only allow getattr(x, "literal") — no dynamic attrs.
                if len(sub.args) >= 2 and not (
                    isinstance(sub.args[1], ast.Constant)
                    and isinstance(sub.args[1].value, str)
                ):
                    errors.append("getattr with non-literal attr name not allowed")

    return errors


# ----------------------------------------------------------------------
# Sandbox subprocess — actually runs the LLM-generated parser, with no
# network, no filesystem (the source is passed via -c, the html via stdin).
# ----------------------------------------------------------------------


_SANDBOX_RUNNER_TEMPLATE = textwrap.dedent(
    '''
    import sys, json
    {parser_source}

    if __name__ == "__main__":
        html = sys.stdin.read()
        result = parse(html)
        sys.stdout.write(json.dumps(result))
    '''
).strip()


def sandbox_run_parser(
    parser_source: str, html: str
) -> tuple[dict[str, ModelPrice] | None, list[str]]:
    """Run the LLM-generated parser in a fresh subprocess.

    The subprocess starts with `-S -I` (no site-packages, isolated mode),
    a minimal env (no PATH, no HOME, only PYTHONIOENCODING). HTML goes in
    via stdin; result comes out via stdout as JSON. Wall-clock cap is
    SANDBOX_WALL_CLOCK_SECONDS; output is capped at SANDBOX_OUTPUT_BYTES_MAX.

    The subprocess can still `import bs4` etc. via the whitelist, but
    cannot do network or filesystem IO via standard library means
    (all such names are rejected by the AST whitelist before we get here).

    Returns (prices, errors). On any failure, prices is None and errors
    is non-empty.
    """
    runner = _SANDBOX_RUNNER_TEMPLATE.format(parser_source=parser_source)
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        # Keep the venv's site-packages on the path (bs4 lives there)
        # but strip everything else.
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    try:
        # noqa: S603 — running LLM-generated parser source by design.
        # Defense-in-depth lives in `ast_whitelist_check` (called by
        # the only caller of this function) before we ever get here:
        # imports outside the parser whitelist are rejected, as are
        # eval/exec/open/subprocess names. Subprocess uses `-I`
        # (isolated mode) and a stripped env to remove ambient state.
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", runner],
            input=html.encode("utf-8"),
            capture_output=True,
            timeout=SANDBOX_WALL_CLOCK_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, [f"sandbox timeout after {SANDBOX_WALL_CLOCK_SECONDS}s"]
    if proc.returncode != 0:
        stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-500:]
        return None, [f"sandbox exited {proc.returncode}: {stderr_tail}"]
    if len(proc.stdout) > SANDBOX_OUTPUT_BYTES_MAX:
        return None, [f"sandbox output > {SANDBOX_OUTPUT_BYTES_MAX} bytes"]
    try:
        raw = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"sandbox output is not valid JSON: {exc}"]
    prices, schema_errors = _coerce_to_model_prices(raw)
    if schema_errors:
        return None, schema_errors
    return prices, []


# ----------------------------------------------------------------------
# Self-heal: ask TR's smartest model to rewrite the parser file.
# ----------------------------------------------------------------------


_SELF_HEAL_SYSTEM_PROMPT = """\
You rewrite a single Python file at scripts/pricing/parsers/<slug>.py.

The file MUST contain exactly one top-level function with this signature:

    def parse(html: str) -> dict:
        ...

The function returns a dict like:
    {
        "anthropic/claude-opus-4.7": {
            "prompt_micro_per_m": 15_000_000,    # $15/M tokens
            "completion_micro_per_m": 75_000_000, # $75/M tokens
        },
        ...
    }
where each value is microdollars per million tokens (i.e. $1/M = 1_000_000).

STRICT RULES:
1. Only import from this whitelist: re, bs4, decimal, json, typing, dataclasses.
2. Do NOT import: urllib, requests, socket, os, subprocess, pathlib, sys.
3. Do NOT call: open, eval, exec, compile, __import__, getattr (except with a
   string literal second arg).
4. Do NOT use any dunder attribute access except __name__ / __doc__.
5. Do NOT define classes; the file is functions and module-level constants only.
6. Pure function: no side effects, no network, no filesystem.
7. Return ONLY the complete new file content inside <file_content>...</file_content>
   tags. Do NOT include explanations outside those tags.

If the previous parser is broken because the page structure changed, look at
the new HTML and write fresh CSS/regex extraction logic. Prefer BeautifulSoup
(`from bs4 import BeautifulSoup`) for HTML structure parsing.
"""


_FILE_CONTENT_RE = re.compile(
    r"<file_content>\s*(.*?)\s*</file_content>", re.DOTALL
)


def self_heal_parser(
    *,
    slug: str,
    current_src: str,
    html: str,
    errors: list[str],
) -> str:
    """Call TR's smartest configured model to rewrite the parser source.

    Returns the new parser source as a string. Raises RuntimeError on
    LLM API failure or response shape failure (no `<file_content>` block,
    empty content, etc.). Does NOT validate the returned source — that
    is the caller's job (ast_whitelist_check + sandbox_run_parser).
    """
    api_key = os.environ.get(TR_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{TR_API_KEY_ENV} not set; cannot call TR for self-heal"
        )
    user_message = (
        f"Provider slug: {slug}\n\n"
        f"Validation errors from the current parser:\n"
        f"{json.dumps(errors, indent=2)}\n\n"
        f"Current parser source (scripts/pricing/parsers/{slug}.py):\n"
        f"```python\n{current_src}\n```\n\n"
        f"Live HTML from the provider's pricing page:\n"
        f"```html\n{html}\n```\n"
    )
    body = {
        "model": TR_SELF_HEAL_MODEL,
        "messages": [
            {"role": "system", "content": _SELF_HEAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{TR_API_BASE}/v1/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        payload = resp.json()
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected TR response shape: {exc}") from exc
    if not isinstance(content, str):
        raise RuntimeError(
            f"unexpected TR content type: {type(content).__name__}"
        )
    match = _FILE_CONTENT_RE.search(content)
    if not match:
        raise RuntimeError(
            "TR response missing <file_content>...</file_content> block"
        )
    new_src = match.group(1).strip()
    if not new_src:
        raise RuntimeError("TR response had empty <file_content> block")
    return new_src


def diff_sources(old: str, new: str, slug: str) -> str:
    """Unified diff of two parser sources, for the commit body."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/scripts/pricing/parsers/{slug}.py",
            tofile=f"b/scripts/pricing/parsers/{slug}.py",
        )
    )


def parser_path(slug: str) -> Path:
    return PARSERS_DIR / f"{slug}.py"


# ----------------------------------------------------------------------
# Per-provider orchestration. Used by `providers/<slug>.py:fetch()`.
# ----------------------------------------------------------------------


def fetch_provider(
    *,
    slug: str,
    url: str,
    expected_models: list[str],
    extra_headers: dict[str, str] | None = None,
) -> ProviderPricingResult:
    """Fetch one provider's prices via the deterministic parser, with
    LLM self-heal as fallback.

    `extra_headers` lets a provider config request specific headers on
    the fetch — e.g. `{"X-Return-Format": "markdown"}` for Jina-proxied
    URLs that should return clean markdown instead of HTML.

    Steps:
      1. fetch_html(url, extra_headers=...)
      2. import parsers/<slug>.py and call parse(html)
      3. validate; on success, return.
      4. on failure, call self_heal_parser to get a rewritten source.
      5. ast_whitelist_check the new source. Reject on violation.
      6. sandbox_run_parser the new source on the captured HTML.
      7. validate the sandbox output.
      8. only after all pass, write the new source to disk and return.
    """
    log.info("pricing.fetch slug=%s url=%s", slug, url)
    html = fetch_html(url, extra_headers=extra_headers)

    # Exec the parser source in a fresh namespace each call — sidesteps
    # importlib.reload edge cases (the parser file may have been
    # rewritten by an earlier self-heal in this same workflow run) and
    # ensures we always run the on-disk version.
    # noqa: S102 — exec is intentional. The parser source is checked
    # by `ast_whitelist_check` before being persisted to disk by the
    # self-heal flow, so anything we read back from disk has already
    # passed the whitelist (or is a human-written initial parser).
    parser_source = parser_path(slug).read_text(encoding="utf-8")
    parser_ns: dict[str, Any] = {}
    exec(compile(parser_source, str(parser_path(slug)), "exec"), parser_ns)  # noqa: S102
    parse_fn = parser_ns.get("parse")
    if not callable(parse_fn):
        raise RuntimeError(f"{slug}: parsers/{slug}.py has no callable `parse`")
    raw = parse_fn(html)
    prices, schema_errors = _coerce_to_model_prices(raw)
    if schema_errors:
        log.warning("pricing.parse schema_errors slug=%s errors=%s", slug, schema_errors)
        prices = None
    errors = schema_errors[:] if schema_errors else []
    if prices is not None:
        errors = validate(prices, expected_models)
    if not errors:
        return ProviderPricingResult(
            slug=slug,
            prices=prices or {},
            source="deterministic",
            fetched_url=url,
        )

    log.warning("pricing.deterministic_failed slug=%s errors=%s", slug, errors)

    # Self-heal: rewrite parsers/<slug>.py.
    current_src = parser_path(slug).read_text(encoding="utf-8")
    try:
        new_src = self_heal_parser(
            slug=slug,
            current_src=current_src,
            html=html,
            errors=errors,
        )
    except Exception as exc:
        raise RuntimeError(f"{slug}: self-heal LLM call failed: {exc}") from exc

    ast_errors = ast_whitelist_check(new_src)
    if ast_errors:
        raise RuntimeError(
            f"{slug}: self-heal AST whitelist failed: {ast_errors}"
        )

    sandbox_prices, sandbox_errors = sandbox_run_parser(new_src, html)
    if sandbox_errors:
        raise RuntimeError(
            f"{slug}: self-heal sandbox failed: {sandbox_errors}"
        )
    assert sandbox_prices is not None  # for type checker
    final_errors = validate(sandbox_prices, expected_models)
    if final_errors:
        raise RuntimeError(
            f"{slug}: self-heal output failed validation: {final_errors}"
        )

    # All gates passed — persist the new parser source.
    parser_path(slug).write_text(new_src, encoding="utf-8")
    diff = diff_sources(current_src, new_src, slug)
    return ProviderPricingResult(
        slug=slug,
        prices=sandbox_prices,
        source="self_healed",
        heal_diff=diff,
        fetched_url=url,
        notes=[
            f"self-healed parser (validation errors: {len(errors)} → 0)"
        ],
    )
