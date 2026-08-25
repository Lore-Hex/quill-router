"""Parse Fireworks serverless pricing docs."""

from __future__ import annotations

import re
from decimal import Decimal

MODEL_LABELS = {
    # Match Fast labels before their standard prefixes. These are distinct
    # Fireworks routers with different prices and lifecycle dates.
    "Kimi K3 Fast": "moonshotai/kimi-k3-fast",
    "Kimi K2.7 Code Fast": "moonshotai/kimi-k2.7-code-fast",
    "Kimi K2.6 Fast": "moonshotai/kimi-k2.6-fast",
    "GLM 5.2 Fast": "z-ai/glm-5.2-fast",
    "Kimi K3": "moonshotai/kimi-k3",
    "Kimi K2.7 Code": "moonshotai/kimi-k2.7-code",
    "Kimi K2.6": "moonshotai/kimi-k2.6",
    "Kimi K2.5": "moonshotai/kimi-k2.5",
    "DeepSeek V4 Pro": "deepseek/deepseek-v4-pro",
    "DeepSeek V4 Pro 0813": "deepseek/deepseek-v4-pro-0813",
    "DeepSeek V4 Flash (0731)": "deepseek/deepseek-v4-flash-0731",
    "DeepSeek V4 Flash": "deepseek/deepseek-v4-flash",
    "GLM 5.2": "z-ai/glm-5.2",
    "GLM 5.1": "z-ai/glm-5.1",
    "Qwen 3.7 Plus": "qwen/qwen3.7-plus",
    "Qwen 3.8 Max": "qwen/qwen3.8-max",
    "OpenAI GPT OSS 120B": "openai/gpt-oss-120b",
    "OpenAI GPT OSS 20B": "openai/gpt-oss-20b",
    "Muse Glimmer 30B": "meta-models/muse-glimmer-30b",
    "NVIDIA Nemotron 3.5 Lightning 30B A3B": "nvidia/nemotron-3.5-lightning",
    "NVIDIA Nemotron 3 Ultra (Preview)": "nvidia/nemotron-3-ultra-550b-a55b",
    "MiniMax M3": "minimax/minimax-m3",
    "MiniMax M2.7": "minimax/minimax-m2.7",
    "MiniMax 2.7": "minimax/minimax-m2.7",
    "MiniMax 2.5": "minimax/minimax-m2.5",
}

_FAMILY_AUTHORS = (
    ("deepseek-", "deepseek"),
    ("glm-", "z-ai"),
    ("gpt-oss-", "openai"),
    ("kimi-", "moonshotai"),
    ("minimax-", "minimax"),
    ("qwen", "qwen"),
)
_LINKED_PRICE_ROW = re.compile(
    r"\[(?P<label>[^]]+)\]\((?P<href>[^)]+)\)\s*\|\s*"
    r"\$(?P<prompt>[0-9]+(?:\.[0-9]+)?)\s*/\s*"
    r"\$(?P<cached>[0-9]+(?:\.[0-9]+)?)\s*/\s*"
    r"\$(?P<completion>[0-9]+(?:\.[0-9]+)?)",
    flags=re.I,
)


def _money_to_micro(raw: str) -> int:
    return int((Decimal(raw) * Decimal(1_000_000)).to_integral_value())


def _canonical_linked_model(label: str, href: str) -> str | None:
    slug = href.rstrip("/").rsplit("/", 1)[-1].casefold().replace("_", "-")
    slug = re.sub(r"-(\d+)p(\d+)(?=$|-)", r"-\1.\2", slug)
    slug = re.sub(r"([km])(\d+)p(\d+)(?=$|-)", r"\1\2.\3", slug)
    slug = re.sub(r"qwen(\d+)p(\d+)(?=$|-)", r"qwen\1.\2", slug)
    label_words = set(re.findall(r"[a-z0-9]+", label.casefold()))
    slug_words = set(slug.split("-"))
    # Fireworks sometimes gives a Fast or US row the standard model's link.
    # Auto-aliasing that row would overwrite the standard price, so require
    # those modifiers to be present in the provider-native slug as well.
    if any(
        modifier in label_words and modifier not in slug_words
        for modifier in ("fast", "us")
    ):
        return None
    for prefix, author in _FAMILY_AUTHORS:
        if slug.startswith(prefix):
            return f"{author}/{slug}"
    return None


def parse(html: str) -> dict:
    text = re.sub(r"\s+", " ", html)
    out: dict[str, dict[str, int]] = {}
    for label, model_id in MODEL_LABELS.items():
        if model_id in out:
            continue
        label_pattern = rf"(?:\[{re.escape(label)}\]\([^)]*\)|{re.escape(label)})"
        pattern = (
            label_pattern
            + r"\s*(?:\||\])?\s*\$([0-9]+(?:\.[0-9]+)?)\s*/\s*"
            + r"\$([0-9]+(?:\.[0-9]+)?)\s*/\s*"
            + r"\$([0-9]+(?:\.[0-9]+)?)"
        )
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        prompt, cached, completion = match.groups()
        out[model_id] = {
            "prompt_micro_per_m": _money_to_micro(prompt),
            "prompt_cached_micro_per_m": _money_to_micro(cached),
            "completion_micro_per_m": _money_to_micro(completion),
        }
    for match in _LINKED_PRICE_ROW.finditer(text):
        label = match.group("label")
        if label in MODEL_LABELS:
            continue
        model_id = _canonical_linked_model(label, match.group("href"))
        if model_id is None or model_id in out:
            continue
        out[model_id] = {
            "prompt_micro_per_m": _money_to_micro(match.group("prompt")),
            "prompt_cached_micro_per_m": _money_to_micro(match.group("cached")),
            "completion_micro_per_m": _money_to_micro(match.group("completion")),
        }
    return out
