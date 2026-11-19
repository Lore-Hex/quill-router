"""ScaleDown's native task API: input-only prices and task-specific canaries."""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html
from scripts.pricing.manifest import apply_canary_results, write_discovered_chat_manifest

SLUG = "scaledown"
BASE_URL = "https://api.scaledown.xyz"
URL = "https://scaledown.ai/llms.txt"
PRICING_URL = "https://scaledown.ai/#pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/scaledown.json"
)
MANIFEST_STALE_FALLBACK = True

# Native paths are necessary: /v1/models and /v1/chat/completions in the docs
# returned API Gateway 403 on 2026-09-09 while these four task calls succeeded.
TASKS = {
    "compress": {
        "path": "/compress/raw/",
        "input": {
            "context": "Acme launched Monday. Feedback will be reviewed Friday.",
            "prompt": "When will feedback be reviewed?",
            "scaledown": {"rate": "auto"},
        },
        "output": {"results": {"compressed_prompt": "Feedback will be reviewed Friday."}},
        "description": "Compress source text for a specific query.",
    },
    "summarize": {
        "path": "/summarization/abstractive",
        "input": {
            "text": "Acme launched Monday. Feedback will be reviewed Friday.",
            "instructions": "One short sentence.",
            "max_tokens": 64,
        },
        "output": {"summary": "Acme launched Monday and will review feedback Friday."},
        "description": "Summarize documents, transcripts, or other source text.",
    },
    "extract": {
        "path": "/extract",
        "input": {"text": "Acme launched Monday.", "entities": {"company": "The company name"}},
        "output": {
            "entities": [
                {"text": "Acme", "type": "company", "confidence": 1.0, "start": 0, "end": 4}
            ]
        },
        "description": "Extract entities described in natural language from source text.",
    },
    "classify": {
        "path": "/classify",
        "input": {
            "text": "My invoice has an incorrect tax amount.",
            "labels": [
                {"name": "billing", "rubric": "Is the request about an invoice?"},
                {"name": "technical", "rubric": "Is the request about broken software?"},
            ],
        },
        "output": {"top_label": "billing", "scores": {"billing": 0.99, "technical": 0.01}},
        "description": "Classify source text against caller-defined labels and rubrics.",
    },
}
UPSTREAM_ID_MAP = {f"scaledown/{task}": task for task in TASKS}
EXPECTED_MODELS = list(UPSTREAM_ID_MAP)
_ROWS: dict[str, dict] = {}

_LEGACY_RATE = re.compile(r"(?m)^Public API: \$(\d+(?:\.\d+)?) per 1M tokens\s*$")
# Since 2026-11-19 the llms.txt Pricing section is a Public API block of
# per-modality input rates, then an Enterprise block. Every line of the
# section must be blank or one exact line below, under its own block heading
# with no blank line between them, each line at most once. Anything else,
# including indented bullets, fails closed.
_PUBLIC_API_HEADING = "Public API, per 1M input tokens:"
_ENTERPRISE_HEADING = "Enterprise: dedicated or licensed deployments, custom pricing"
# ScaleDown's first-party statement, inside the Public API block, that the
# public rate covers all four task models: it names Summarize, Extract,
# Classify and Compress. It also backs the 1M context_length above.
_ALL_FOUR_MODELS_LINE = (
    "- 1M-token context window on Summarize, Extract, and Classify; Compress has no limit"
)
_USD = r"\$([0-9]+(?:\.[0-9]+)?)"
_BLOCK_LINES = {
    _PUBLIC_API_HEADING: {
        # The routes are text only, so the Text rate is the tariff; the
        # Image rate is validated but never prices a route.
        "text": re.compile(rf"- Text: {_USD}"),
        "image": re.compile(rf"- Image: {_USD}"),
        "output": re.compile(re.escape("- Output tokens are never billed")),
        "free": re.compile(r"- New accounts get [0-9]+(?:\.[0-9]+)?[MK]? tokens free"),
        "all-four-models": re.compile(re.escape(_ALL_FOUR_MODELS_LINE)),
        "flat": re.compile(re.escape("- Flat pricing, no tiers or seats")),
    },
    _ENTERPRISE_HEADING: {
        "self-hosted": re.compile(re.escape("- Self-hosted in your VPC, including air-gapped")),
        "fine-tuning": re.compile(re.escape("- Fine-tuning on your data")),
        "support": re.compile(re.escape("- Dedicated support and SLAs")),
    },
}
_ATX_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*")
_SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
_LIST_ITEM = re.compile(r" {0,3}(?:[-*+]|[0-9]{1,9}[.)])(?:[ \t]|$)")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) of every ATX and setext Markdown heading."""
    found = []
    for i, line in enumerate(lines):
        if atx := _ATX_HEADING.fullmatch(line):
            found.append((i, len(atx[1]), atx[2] or ""))
        elif (
            line.strip()
            and i + 1 < len(lines)
            and _SETEXT_UNDERLINE.fullmatch(lines[i + 1])
            and not _SETEXT_UNDERLINE.fullmatch(line)
            and not _LIST_ITEM.match(line)
        ):
            found.append((i, 1 if "=" in lines[i + 1] else 2, line))
    return found


def _pricing_section(markdown: str) -> list[str]:
    """Lines under the one "## Pricing" heading, up to the next heading of its level or higher."""
    lines = markdown.splitlines()
    headings = _headings(lines)
    pricing = [(i, level) for i, level, text in headings if " ".join(text.split()).casefold() == "pricing"]
    if len(pricing) > 1:
        raise RuntimeError("scaledown: ambiguous public pricing sections")
    if not pricing or lines[pricing[0][0]].rstrip() != "## Pricing":
        raise RuntimeError("scaledown: public pricing section missing")
    start, level = pricing[0]
    end = next((i for i, other, _ in headings if i > start and other <= level), len(lines))
    return lines[start + 1:end]


def _public_api_rate(section: list[str]) -> str:
    """The Text rate of a section made only of exact allowlisted lines."""
    rules: dict[str, re.Pattern[str]] = {}
    blocks: list[str] = []
    seen: dict[str, re.Match[str]] = {}
    for line in section:
        if not line.strip():
            rules = {}
        elif line in _BLOCK_LINES:
            blocks.append(line)
            rules = _BLOCK_LINES[line]
        else:
            matched = [(kind, m) for kind, rule in rules.items() if (m := rule.fullmatch(line))]
            if len(matched) != 1 or matched[0][0] in seen:
                raise RuntimeError("scaledown: unknown or repeated line in the Pricing section")
            seen[matched[0][0]] = matched[0][1]
    if (
        blocks not in ([_PUBLIC_API_HEADING], [_PUBLIC_API_HEADING, _ENTERPRISE_HEADING])
        or not {"text", "all-four-models", "flat"} <= seen.keys()
    ):
        raise RuntimeError("scaledown: missing or ambiguous flat task pricing")
    return seen["text"][1]


def _legacy_rate(markdown: str) -> str:
    """The pre-2026-11-19 layout, checked exactly as before."""
    section = re.search(r"(?ms)^## Pricing\s*\n(.*?)(?=^## |\Z)", markdown)
    if section is None:
        raise RuntimeError("scaledown: public pricing section missing")
    rates = _LEGACY_RATE.findall(section[1])
    if (
        len(rates) != 1
        or "All four models included" not in section[1]
        or "Flat pricing, no tiers" not in section[1]
    ):
        raise RuntimeError("scaledown: missing or ambiguous flat task pricing")
    return rates[0]


def _input_price(markdown: str, website_code: str) -> ModelPrice:
    section = _pricing_section(markdown)
    rate = _public_api_rate(section) if _PUBLIC_API_HEADING in section else _legacy_rate(markdown)
    # Read the live FAQ, rather than assuming an omitted output rate is free.
    if (
        "ScaleDown does not charge for output tokens. We exclusively charge for input tokens"
        not in website_code
    ):
        raise RuntimeError("scaledown: input-only billing commitment missing")
    amount = Decimal(rate) * 1_000_000
    if amount != amount.to_integral_value() or not 0 < amount <= 1_000_000_000:
        raise RuntimeError("scaledown: invalid input price or precision")
    return ModelPrice(int(amount), 0)


def _website_code() -> str:
    html = fetch_html("https://scaledown.ai/")
    urls = set()
    for tag in BeautifulSoup(html, "html.parser").find_all("script", src=True):
        url = urljoin("https://scaledown.ai/", str(tag["src"]))
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "scaledown.ai"
            and parsed.path.startswith("/assets/")
            and parsed.path.endswith(".js")
        ):
            urls.add(url)
    if not urls or len(urls) > 8:
        raise RuntimeError("scaledown: public application bundles missing or ambiguous")
    return "\n".join(fetch_html(url) for url in sorted(urls))


def _valid_result(task: str, payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    tokens = payload.get("input_tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 < tokens <= 1 << 31:
        return False
    if task == "compress":
        result = payload.get("results")
        return (
            payload.get("successful") is True
            and isinstance(result, dict)
            and result.get("success") is True
            and isinstance(result.get("compressed_prompt"), str)
            and bool(result["compressed_prompt"].strip())
        )
    if task == "summarize":
        return isinstance(payload.get("summary"), str) and bool(payload["summary"].strip())
    if task == "extract":
        return isinstance(payload.get("entities"), list)
    return (
        task == "classify"
        and isinstance(payload.get("top_label"), str)
        and bool(payload["top_label"])
    )


def fetch() -> ProviderPricingResult:
    global _ROWS  # noqa: PLW0603
    key = os.environ.get("SCALEDOWN_API_KEY")
    if not key:
        raise RuntimeError("scaledown: SCALEDOWN_API_KEY required for native task canaries")
    price = _input_price(fetch_html(URL), _website_code())
    rows = {}
    healthy = set()
    with httpx.Client(timeout=45, follow_redirects=False) as client:
        for task, spec in TASKS.items():
            model_id = f"scaledown/{task}"
            rows[model_id] = {
                "id": model_id,
                "upstream_id": task,
                "display_name": f"ScaleDown {task.title()}",
                "model_type": "chat",
                "context_length": 1_000_000,
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "endpoints": ["chat/completions"],
                "supported_features": ["chat", "completion"],
                "documentation": {
                    "description": str(spec["description"])
                    + " Input-only billing; no output-token charge.",
                    "input_format": "Send the native task JSON object as the content of one user message. Summarize also accepts plain text. Text only; tools, chat history, response_format, and image blocks are not supported by this adapter.",
                    "output_format": "A JSON object in choices[0].message.content, including native task results. Stream responses deliver the completed result together, not incremental upstream tokens. Usage reports the provider's billable input tokens; completion_tokens is zero.",
                    "example_input": json.dumps(spec["input"], indent=2),
                    "example_output": json.dumps(spec["output"], indent=2),
                },
            }
            try:
                response = client.post(
                    BASE_URL + str(spec["path"]), headers={"x-api-key": key}, json=spec["input"]
                )
                if response.status_code == 200 and _valid_result(task, response.json()):
                    healthy.add(model_id)
            except (httpx.HTTPError, ValueError):
                # Never log upstream bodies, keys, or task content.
                pass
    apply_canary_results(rows, checked_model_ids=set(rows), healthy_model_ids=healthy)
    _ROWS = rows
    return ProviderPricingResult(
        slug=SLUG,
        prices={model_id: price for model_id in rows},
        source="api",
        fetched_url=URL,
        notes=[f"native task canaries: {len(healthy)}/{len(rows)} passed; input-only prices"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_ROWS,
        source_url="https://docs.scaledown.ai/quickstart",
        pricing_source_url=PRICING_URL,
    )
