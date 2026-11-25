"""Parse SiliconFlow's official model library, pricing cards and legacy Markdown.

https://www.siliconflow.com/models lists every serverless model as a Framer CMS
card whose fields carry ``data-framer-name`` labels (``model_name``,
``sub_type``, ``input_pricing``, ``output_pricing``, ``price_unit``). The
/pricing page shows only a curated subset of those cards, so it cannot price
most of the authenticated /v1/models catalog.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

_DETAILS_RE = re.compile(
    r"\[Details\]\(https://(?:www\.)?siliconflow\.com/models/([\w.\-]+)\)"
)
_PRICE_RE = re.compile(r"\$\s*\n?\s*([\d.]+)")
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _normalize(native_slug: str) -> str:
    if "/" in native_slug:
        return native_slug
    lower = native_slug.casefold()
    if lower.startswith("deepseek"):
        return f"deepseek/{lower}"
    if lower.startswith("qwen"):
        return f"qwen/{lower}"
    if lower.startswith("glm"):
        return f"z-ai/{lower}"
    if lower.startswith("kimi") or lower.startswith("moonshot"):
        return f"moonshotai/{lower}"
    if lower.startswith("llama") or lower.startswith("meta"):
        return f"meta-llama/{lower}"
    if re.match(r"hy\d", lower) or lower.startswith("hunyuan"):
        return f"tencent/{lower}"
    if lower.startswith("ling-") or lower.startswith("ring-"):
        return f"inclusionai/{lower}"
    if lower.startswith("seed-"):
        return f"bytedance-seed/{lower}"
    if lower.startswith("longcat"):
        return f"meituan-longcat/{lower}"
    if lower.startswith("step-"):
        return f"stepfun-ai/{lower}"
    if lower.startswith("minimax"):
        return f"minimax/{lower}"
    if lower.startswith("gpt-oss"):
        return f"openai/{lower}"
    if lower.startswith("gemma"):
        return f"google/{lower}"
    return lower


def _skip_non_text(slug: str) -> bool:
    return any(
        tag in slug.casefold()
        for tag in ("flux", "z-image", "wan2", "vidu", "sora", "kling")
    )


def _micro(value: str) -> int | None:
    # Framer renders a missing CMS number as "nan" or an empty string.
    if not _NUMBER_RE.match(value):
        return None
    return int(round(float(value) * 1_000_000))


def _parse_model_library(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    output: dict = {}
    for card in soup.find_all("a"):
        names = card.select('[data-framer-name="model_name"]')
        if len(names) != 1:
            continue
        sub_types = card.select('[data-framer-name="sub_type"]')
        # Only token-billed chat models. Image, video, speech, embedding and
        # rerank cards are billed per image, second, byte or input token.
        if not sub_types or sub_types[0].get_text(strip=True).casefold() != "chat":
            continue
        label = ""
        values: dict = {}
        units: set = set()
        for field in card.select("[data-framer-name]"):
            kind = field["data-framer-name"]
            text = field.get_text(" ", strip=True)
            if kind == "currency" and text.endswith(":"):
                label = text[:-1].casefold()
            elif kind == "currency" and text != "$":
                label = ""
            elif kind == "price_unit":
                units.add(text.casefold())
            elif kind in ("input_pricing", "output_pricing") and label:
                values.setdefault(label, text)
        if units != {"/ m tokens"}:
            continue
        prompt = _micro(values.get("input", ""))
        completion = _micro(values.get("output", ""))
        if prompt is None or completion is None:
            continue
        row = {"prompt_micro_per_m": prompt, "completion_micro_per_m": completion}
        cached = _micro(values.get("cached input", ""))
        if cached is not None:
            row["prompt_cached_micro_per_m"] = cached
        output.setdefault(_normalize(names[0].get_text(" ", strip=True)), row)
    return output


def _parse_cards(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    output: dict = {}
    for card in soup.select('[data-framer-name="LLM - Desktop"]'):
        strings = list(card.stripped_strings)
        if len(strings) < 6:
            continue
        native_name = strings[0]
        if _skip_non_text(native_name):
            continue
        values = _PRICE_RE.findall(card.get_text(" ", strip=True))
        if len(values) < 2:
            continue
        try:
            prompt = int(round(float(values[0]) * 1_000_000))
            completion = int(round(float(values[-1]) * 1_000_000))
            cached = int(round(float(values[1]) * 1_000_000)) if len(values) >= 3 else None
        except ValueError:
            continue
        row = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
        if cached is not None:
            row["prompt_cached_micro_per_m"] = cached
        output.setdefault(_normalize(native_name), row)
    return output


def _parse_legacy_markdown(markdown: str) -> dict:
    output: dict = {}
    for match in _DETAILS_RE.finditer(markdown):
        slug = match.group(1)
        if _skip_non_text(slug):
            continue
        window = markdown[max(0, match.start() - 600) : match.start()]
        context_matches = list(re.finditer(r"\b(\d+[KMkm])\s*\n+\s*\$", window))
        if not context_matches:
            continue
        row_window = window[context_matches[-1].end() - 1 :]
        values = _PRICE_RE.findall(row_window)
        if len(values) < 2:
            continue
        row = {
            "prompt_micro_per_m": int(round(float(values[0]) * 1_000_000)),
            "completion_micro_per_m": int(round(float(values[-1]) * 1_000_000)),
        }
        if len(values) >= 3:
            row["prompt_cached_micro_per_m"] = int(round(float(values[1]) * 1_000_000))
        output.setdefault(_normalize(slug), row)
    return output


def parse(source: str) -> dict:
    return (
        _parse_model_library(source)
        or _parse_cards(source)
        or _parse_legacy_markdown(source.replace(r"\$", "$"))
    )
