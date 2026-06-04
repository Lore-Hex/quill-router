"""Sourced external benchmark scores for the public model pages.

OpenRouter's benchmark tab is powered by Artificial Analysis (ToS-restricted)
and shows no per-benchmark citations. We do the opposite: a table where EVERY
score carries a real source URL, drawn from first-party vendor model cards /
papers (class A) and open leaderboards with permissive data licenses (class B).

Hard rules (enforced in `scores_for_model`):
  * a score is rendered ONLY if it has a non-empty `source_url` and
    `source_class` in {"A", "B"}.
  * class "C" sources (Artificial Analysis, LMArena Elo, LiveBench) are
    ToS-restricted — we never store their numbers here; link out instead.
  * numbers are NEVER fabricated. Each row in benchmark_scores.json was
    verified against its cited primary source, and is attached only to the
    EXACT model checkpoint it was measured on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).parent / "data" / "benchmark_scores.json"
_RENDERABLE_CLASSES = {"A", "B"}


@dataclass(frozen=True)
class BenchmarkDef:
    key: str
    label: str
    category: str
    unit: str
    about_url: str


# Controlled vocabulary of benchmarks we cite. Adding a key here makes it
# renderable; a score row whose benchmark_key is not in this map is dropped.
BENCHMARK_DEFS: dict[str, BenchmarkDef] = {
    "swe_bench_verified": BenchmarkDef(
        "swe_bench_verified", "SWE-bench Verified", "Coding", "percent",
        "https://www.swebench.com/",
    ),
    "swe_bench_multimodal": BenchmarkDef(
        "swe_bench_multimodal", "SWE-bench Multimodal", "Coding", "percent",
        "https://www.swebench.com/multimodal.html",
    ),
    "osworld": BenchmarkDef(
        "osworld", "OSWorld", "Agentic", "percent", "https://os-world.github.io/",
    ),
    "livecodebench": BenchmarkDef(
        "livecodebench", "LiveCodeBench", "Coding", "percent",
        "https://livecodebench.github.io/",
    ),
    "aider_polyglot": BenchmarkDef(
        "aider_polyglot", "Aider Polyglot", "Coding", "percent",
        "https://aider.chat/docs/leaderboards/",
    ),
    "hle": BenchmarkDef(
        "hle", "Humanity's Last Exam", "Reasoning", "percent", "https://lastexam.ai/",
    ),
    "mmlu": BenchmarkDef(
        "mmlu", "MMLU", "Knowledge", "percent", "https://github.com/hendrycks/test",
    ),
    "mmlu_pro": BenchmarkDef(
        "mmlu_pro", "MMLU-Pro", "Knowledge", "percent",
        "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro",
    ),
    "gpqa_diamond": BenchmarkDef(
        "gpqa_diamond", "GPQA Diamond", "Science", "percent",
        "https://github.com/idavidrein/gpqa",
    ),
    "aime_2024": BenchmarkDef(
        "aime_2024", "AIME 2024", "Math", "percent",
        "https://maa.org/maa-invitational-competitions/",
    ),
    "aime_2025": BenchmarkDef(
        "aime_2025", "AIME 2025", "Math", "percent",
        "https://maa.org/maa-invitational-competitions/",
    ),
    "math": BenchmarkDef(
        "math", "MATH", "Math", "percent", "https://github.com/hendrycks/math",
    ),
    "math_500": BenchmarkDef(
        "math_500", "MATH-500", "Math", "percent", "https://github.com/hendrycks/math",
    ),
    "humaneval": BenchmarkDef(
        "humaneval", "HumanEval", "Coding", "percent",
        "https://github.com/openai/human-eval",
    ),
    "ifeval": BenchmarkDef(
        "ifeval", "IFEval", "Instruction following", "percent",
        "https://github.com/google-research/google-research/tree/master/instruction_following_eval",
    ),
    "mmmu": BenchmarkDef(
        "mmmu", "MMMU", "Vision", "percent", "https://mmmu-benchmark.github.io/",
    ),
    "bfcl": BenchmarkDef(
        "bfcl", "Berkeley Function Calling", "Tool use", "percent",
        "https://gorilla.cs.berkeley.edu/leaderboard.html",
    ),
}


def _display(score: Any, unit: str) -> str:
    if unit in {"percent", "pass@1"}:
        return f"{score}%"
    if unit in {"elo", "rating"}:
        return str(int(score))
    return str(score)


@lru_cache(maxsize=1)
def _raw_scores() -> list[dict[str, Any]]:
    if not _DATA_PATH.exists():
        return []
    data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    rows = data.get("scores", [])
    return rows if isinstance(rows, list) else []


def scores_for_model(model_id: str) -> list[dict[str, Any]]:
    """Renderable, sourced benchmark rows for one model, grouped by category.

    Drops any row without a citation URL, with a non-renderable source class,
    or whose benchmark_key isn't in BENCHMARK_DEFS.
    """
    out: list[dict[str, Any]] = []
    for row in _raw_scores():
        if row.get("model_id") != model_id:
            continue
        if row.get("source_class") not in _RENDERABLE_CLASSES:
            continue
        if not row.get("source_url"):
            continue
        defn = BENCHMARK_DEFS.get(str(row.get("benchmark_key")))
        if defn is None:
            continue
        unit = str(row.get("unit") or defn.unit)
        out.append(
            {
                "label": defn.label,
                "category": defn.category,
                "about_url": defn.about_url,
                "score": row["score"],
                "unit": unit,
                "display": _display(row["score"], unit),
                "source_name": row["source_name"],
                "source_url": row["source_url"],
                "as_of_date": row.get("as_of_date"),
                "config_note": row.get("config_note"),
            }
        )
    out.sort(key=lambda r: (r["category"], r["label"]))
    return out


def models_with_scores() -> set[str]:
    return {str(row.get("model_id")) for row in _raw_scores() if row.get("model_id")}
