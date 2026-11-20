"""ScaleDown's native task API: input-only prices and task-specific canaries."""

from __future__ import annotations

import html
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
# Only an ATX heading at column 0 of the Pricing heading's level or higher
# ends the section. A setext underline, a blockquote line or a deeper heading
# inside it is not an allowlisted line, so it fails closed instead of
# silently ending the section.
_SECTION_END = re.compile(r"#{1,2} ")
_SETEXT_UNDERLINE = re.compile(r"\s*(?:=+|-+)\s*")


def _pricing_headings(lines: list[str]) -> list[int]:
    """Every heading or setext candidate whose letters contain "pricing".

    Deliberately broad, so a formatted, linked, blockquoted or setext heading
    cannot hide a second Pricing section.
    """
    found = []
    for i, line in enumerate(lines):
        atx = re.sub(r"^[\s>]*", "", line).startswith("#")
        setext = bool(line.strip()) and i + 1 < len(lines) and bool(_SETEXT_UNDERLINE.fullmatch(lines[i + 1]))
        if (atx or setext) and "pricing" in re.sub(r"[^a-z]", "", line.lower()):
            found.append(i)
    return found


def _pricing_section(markdown: str) -> list[str]:
    """Lines under the one "## Pricing" heading, up to the next "# " or "## " heading."""
    lines = markdown.splitlines()
    pricing = _pricing_headings(lines)
    if len(pricing) > 1:
        raise RuntimeError("scaledown: ambiguous public pricing sections")
    if not pricing or lines[pricing[0]].rstrip() != "## Pricing":
        raise RuntimeError("scaledown: public pricing section missing")
    start = pricing[0]
    end = next((i for i in range(start + 1, len(lines)) if _SECTION_END.match(lines[i])), len(lines))
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


# Whole-document rules, independent of headings, so a second price block
# hidden behind heading syntax the section scanner does not recognise still
# fails closed. Lines are read after decoding HTML entities and dropping any
# leading whitespace, blockquote and list markers.
_CONTAINER_PREFIX = re.compile(r"^(?:\s*(?:>|[-*+](?=\s)|[0-9]{1,9}[.)](?=\s)))*\s*")
# Emphasis and code markers are dropped too, before the key-line counts and
# both unit scans, so "- **Text:** $0.20" is still a Text rate and
# "per **1M** input tokens" is still a unit.
_EMPHASIS = re.compile(r"[*_`]")
# Key lines and how often each must appear in the per-modality and legacy
# layouts. The legacy layout has none of them, so one there is a second,
# conflicting tariff. None means the layout does not constrain it.
_KEY_LINES = {
    "the Public API heading": (lambda line: line == _PUBLIC_API_HEADING.casefold(), 1, 0),
    "a Text rate": (lambda line: line.startswith("text:"), 1, 0),
    "an Image rate": (lambda line: line.startswith("image:"), 1, 0),
    "the flat pricing bullet": (lambda line: line == "flat pricing, no tiers or seats", 1, None),
}
# A per-token unit is:
# - "per" or "/" and a K, M, thousand, million or billion quantity (optionally
#   digits, "one" or "a" first), whatever word follows: "per 1M", "/ 1K",
#   "per one million image input tokens", "per a million tokens";
# - "per" or "/" and up to three words before "token(s)": "per 1,000 input
#   tokens", "per-token";
# - "per" and a bare number ending a phrase: "per 1,000.".
# A bare number before another word, such as "per 1,000 calls" or "$10/day",
# is not one. Accepted trade-off: a shorthand edit such as "per 1K calls" now
# counts as a unit, so it holds ScaleDown for review rather than risk letting
# "per 1M user supplied input tokens" through.
_NUM = r"[0-9]+(?:[.,][0-9]+)*(?![.,]?[0-9])"
_PER_TOKEN_UNIT = re.compile(
    rf"(?:\bper\b|/)[\s-]*(?:"
    rf"(?:(?:a|one)\s+)?(?:{_NUM}\s*)?(?:k|m|thousand|million|billion)\b"
    rf"|(?:{_NUM}\s+)?(?:[a-z0-9,.-]+\s+){{0,3}}tokens?\b)"
    rf"|\bper\s+{_NUM}(?=\s*(?:$|[^\w\s]))",
    re.IGNORECASE,
)


def _single_rate_line(text: str, *, legacy: bool) -> bool:
    return bool(_LEGACY_RATE.fullmatch(text)) if legacy else text == _PUBLIC_API_HEADING


def _paragraph_units(lines: list[str]) -> list[str | None]:
    """Per-token units found with each paragraph joined into one line.

    A paragraph is a run of non-blank lines with container, emphasis and code
    markers stripped, so a tariff wrapped across lines is still one match.
    Each match yields the line that holds it, or None when it spans lines.
    """
    found: list[str | None] = []
    paragraph: list[str] = []
    for line in [*lines, ""]:
        if stripped := _EMPHASIS.sub("", _CONTAINER_PREFIX.sub("", line)).strip():
            paragraph.append(stripped)
            continue
        spans, at = [], 0
        for part in paragraph:
            spans.append((at, at + len(part), part))
            at += len(part) + 1
        for match in _PER_TOKEN_UNIT.finditer(" ".join(paragraph)):
            holders = [part for start, end, part in spans if start <= match.start() and match.end() <= end]
            found.append(holders[0] if holders else None)
        paragraph = []
    return found


def _check_whole_document(markdown: str, *, legacy: bool) -> None:
    lines = html.unescape(markdown).splitlines()
    normalized = [_EMPHASIS.sub("", _CONTAINER_PREFIX.sub("", line)).strip().casefold() for line in lines]
    for name, (test, modal, legacy_count) in _KEY_LINES.items():
        expected = legacy_count if legacy else modal
        if expected is not None and sum(1 for line in normalized if test(line)) != expected:
            rule = "must appear exactly once" if expected else "must not appear in the legacy layout"
            raise RuntimeError(f"scaledown: {name} {rule} in llms.txt")
    # The one rate line of the layout is the only place a per-token unit may
    # appear, read both line by line and with each paragraph joined, after
    # entities are decoded and emphasis and code markers are dropped.
    units = [plain for plain in (_EMPHASIS.sub("", line) for line in lines) if _PER_TOKEN_UNIT.search(plain)]
    wrapped = _paragraph_units(lines)
    if (
        len(units) != 1
        or not _single_rate_line(units[0], legacy=legacy)
        or len(wrapped) != 1
        or wrapped[0] is None
        or not _single_rate_line(wrapped[0], legacy=legacy)
    ):
        raise RuntimeError("scaledown: per-token unit outside the single public rate line")


def _input_price(markdown: str, website_code: str) -> ModelPrice:
    section = _pricing_section(markdown)
    legacy = _PUBLIC_API_HEADING not in section
    rate = _legacy_rate(markdown) if legacy else _public_api_rate(section)
    _check_whole_document(markdown, legacy=legacy)
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
