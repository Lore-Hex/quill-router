from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from trusted_router.catalog import MODELS
from trusted_router.money import (
    MICRODOLLARS_PER_DOLLAR,
    format_money_precise,
    microdollars_to_decimal,
    token_cost_microdollars,
)

MicroMode = Literal["micro-offline", "micro-search-smoke", "micro-hybrid"]
ConfigKind = Literal["fusion", "solo"]

EXA_SEARCH_WITH_CONTENT_MICRODOLLARS = 7_000
DEFAULT_MAX_COST_MICRODOLLARS = 1_250_000
DEFAULT_WARN_COST_MICRODOLLARS = MICRODOLLARS_PER_DOLLAR
DEFAULT_SEED = 20_260_614
DEFAULT_TASK_COUNT = 20
SEARCH_SMOKE_TASK_COUNT = 5
DRACO_FULL_TASK_COUNT = 100
DRACO_PILOT_TASK_COUNT = 10
DRACO_JUDGE_PASSES = 3
DRACO_JUDGE_MODEL = "google/gemini-3.1-pro-preview"

BLOCKED_MODEL_SUBSTRINGS: tuple[str, ...] = (
    "claude-fable",
    "claude-opus",
    "gpt-5.5",
)


@dataclass(frozen=True)
class TokenProfile:
    input_tokens: int
    output_tokens: int


PANEL_PROFILE = TokenProfile(input_tokens=1_200, output_tokens=500)
FINAL_PROFILE = TokenProfile(input_tokens=2_700, output_tokens=650)
JUDGE_PROFILE = TokenProfile(input_tokens=2_200, output_tokens=450)


@dataclass(frozen=True)
class MicroTask:
    id: str
    domain: str
    prompt: str


@dataclass(frozen=True)
class EvalConfig:
    id: str
    label: str
    kind: ConfigKind
    generation_models: tuple[str, ...]
    final_model: str | None
    judge_model: str = "mistralai/mistral-small-2603"


@dataclass(frozen=True)
class ModelCallEstimate:
    config_id: str
    stage: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_microdollars: int


@dataclass(frozen=True)
class ConfigEstimate:
    config_id: str
    label: str
    kind: ConfigKind
    task_count: int
    live_search: bool
    search_requests_per_task: int
    model_cost_microdollars: int
    search_cost_microdollars: int
    total_cost_microdollars: int
    calls: tuple[ModelCallEstimate, ...]


@dataclass(frozen=True)
class SegmentEstimate:
    id: str
    description: str
    task_count: int
    live_search: bool
    config_estimates: tuple[ConfigEstimate, ...]

    @property
    def model_cost_microdollars(self) -> int:
        return sum(item.model_cost_microdollars for item in self.config_estimates)

    @property
    def search_cost_microdollars(self) -> int:
        return sum(item.search_cost_microdollars for item in self.config_estimates)

    @property
    def total_cost_microdollars(self) -> int:
        return sum(item.total_cost_microdollars for item in self.config_estimates)


@dataclass(frozen=True)
class FullEvalPlan:
    id: str
    label: str
    task_count: int
    judge_passes: int
    search_requests_per_generation_call: int
    config_estimates: tuple[ConfigEstimate, ...]
    notes: tuple[str, ...]

    @property
    def model_cost_microdollars(self) -> int:
        return sum(item.model_cost_microdollars for item in self.config_estimates)

    @property
    def search_cost_microdollars(self) -> int:
        return sum(item.search_cost_microdollars for item in self.config_estimates)

    @property
    def total_cost_microdollars(self) -> int:
        return sum(item.total_cost_microdollars for item in self.config_estimates)


@dataclass(frozen=True)
class MicroRunPlan:
    mode: MicroMode
    seed: int
    max_cost_microdollars: int
    warn_cost_microdollars: int
    tasks: tuple[MicroTask, ...]
    configs: tuple[EvalConfig, ...]
    segments: tuple[SegmentEstimate, ...]

    @property
    def model_cost_microdollars(self) -> int:
        return sum(segment.model_cost_microdollars for segment in self.segments)

    @property
    def search_cost_microdollars(self) -> int:
        return sum(segment.search_cost_microdollars for segment in self.segments)

    @property
    def total_cost_microdollars(self) -> int:
        return sum(segment.total_cost_microdollars for segment in self.segments)

    @property
    def over_warning(self) -> bool:
        return self.total_cost_microdollars >= self.warn_cost_microdollars


class BudgetExceededError(ValueError):
    """Raised when a planned micro eval would exceed its hard cost cap."""


DEFAULT_MICRO_TASKS: tuple[MicroTask, ...] = (
    MicroTask(
        id="music-001",
        domain="music",
        prompt="Recommend five real albums for a listener who loves Talk Talk's Spirit of Eden and explain the connection in one sentence each.",
    ),
    MicroTask(
        id="music-002",
        domain="music",
        prompt="A user likes Sade, Prefab Sprout, and The Blue Nile. Suggest three lesser-known real artists and one starting track for each.",
    ),
    MicroTask(
        id="law-001",
        domain="legal",
        prompt="Draft a short checklist a lawyer could use before sending privileged documents to an LLM API. Keep it practical and non-legal-advice.",
    ),
    MicroTask(
        id="law-002",
        domain="legal",
        prompt="Compare zero data retention, no training, and end-to-end encryption in terms a procurement lawyer can verify in a vendor contract.",
    ),
    MicroTask(
        id="finance-001",
        domain="finance",
        prompt="Explain the difference between gross margin and operating margin for a SaaS company with a simple numeric example.",
    ),
    MicroTask(
        id="finance-002",
        domain="finance",
        prompt="Identify three risks in using a floating point ledger for token billing and propose integer-safe alternatives.",
    ),
    MicroTask(
        id="code-001",
        domain="code",
        prompt="Given a FastAPI endpoint that accepts API keys, list the security tests needed before public launch.",
    ),
    MicroTask(
        id="code-002",
        domain="code",
        prompt="Design a small retry policy for an SDK calling a multi-region API. Include idempotency and billing concerns.",
    ),
    MicroTask(
        id="science-001",
        domain="science",
        prompt="Explain why compressed image bytes are not enough to bound memory usage when decoding images.",
    ),
    MicroTask(
        id="science-002",
        domain="science",
        prompt="Summarize the difference between latency p50 and p95 for a status page, with an example where p50 hides a problem.",
    ),
    MicroTask(
        id="medicine-001",
        domain="medicine",
        prompt="Create a cautious patient-friendly explanation of why an AI summary is not a substitute for a clinician's diagnosis.",
    ),
    MicroTask(
        id="medicine-002",
        domain="medicine",
        prompt="List privacy and audit controls a healthcare team should require before sending PHI to an AI service.",
    ),
    MicroTask(
        id="research-001",
        domain="research",
        prompt="Plan a source-grounded research answer about provider privacy claims without relying on vendor marketing headlines alone.",
    ),
    MicroTask(
        id="research-002",
        domain="research",
        prompt="Write a rubric for judging whether an answer cites primary sources, distinguishes facts from inference, and avoids overclaiming.",
    ),
    MicroTask(
        id="math-001",
        domain="math",
        prompt="A service has 99.9% uptime for 30 days. Compute the allowed downtime in minutes and show the arithmetic.",
    ),
    MicroTask(
        id="math-002",
        domain="math",
        prompt="Estimate the cost of 20 tasks when each task uses 14 model calls and one external search costs $0.007.",
    ),
    MicroTask(
        id="product-001",
        domain="product",
        prompt="Write concise developer-facing copy for an OpenAI-compatible router that emphasizes verifiable trust and low switching cost.",
    ),
    MicroTask(
        id="product-002",
        domain="product",
        prompt="Prioritize five features for a public alpha LLM router where reliability and privacy matter more than dashboard polish.",
    ),
    MicroTask(
        id="ops-001",
        domain="operations",
        prompt="Create an incident-response checklist for a regional API outage with synthetic monitoring and a public status page.",
    ),
    MicroTask(
        id="ops-002",
        domain="operations",
        prompt="Explain why a provider outage should be measured separately from router-core availability when fallback remains healthy.",
    ),
)


def default_micro_configs(*, include_kimi_2_6: bool = False) -> tuple[EvalConfig, ...]:
    configs = [
        EvalConfig(
            id="fusion_main_micro",
            label="Fusion main micro",
            kind="fusion",
            generation_models=(
                "google/gemini-3-flash-preview",
                "moonshotai/kimi-k2.7-code",
                "deepseek/deepseek-v4-pro",
            ),
            final_model="z-ai/glm-4.7",
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="fusion_ultra_micro",
            label="Fusion ultra-cheap micro",
            kind="fusion",
            generation_models=(
                "deepseek/deepseek-v4-flash",
                "minimax/minimax-m3",
                "mistralai/mistral-small-2603",
            ),
            final_model="mistralai/mistral-small-2603",
        ),
        EvalConfig(
            id="solo_kimi_k2_7",
            label="Solo Kimi K2.7 Code",
            kind="solo",
            generation_models=("moonshotai/kimi-k2.7-code",),
            final_model=None,
        ),
        EvalConfig(
            id="solo_deepseek_v4_pro",
            label="Solo DeepSeek V4 Pro",
            kind="solo",
            generation_models=("deepseek/deepseek-v4-pro",),
            final_model=None,
        ),
        EvalConfig(
            id="solo_gemini_3_flash",
            label="Solo Gemini 3 Flash",
            kind="solo",
            generation_models=("google/gemini-3-flash-preview",),
            final_model=None,
        ),
        EvalConfig(
            id="solo_mistral_small",
            label="Solo Mistral Small",
            kind="solo",
            generation_models=("mistralai/mistral-small-2603",),
            final_model=None,
        ),
    ]
    if include_kimi_2_6:
        configs.append(
            EvalConfig(
                id="solo_kimi_k2_6",
                label="Solo Kimi K2.6",
                kind="solo",
                generation_models=("moonshotai/kimi-k2.6",),
                final_model=None,
            )
        )
    for config in configs:
        validate_config(config)
    return tuple(configs)


def default_draco_configs() -> tuple[EvalConfig, ...]:
    """Configurations for the open DRACO-style reproduction plan.

    This deliberately avoids Fable, Opus, and GPT-5.5. OpenRouter used Opus
    4.8 as one synthesizer in its blog post; our first public reproduction
    should use a cheaper judge/final model and report that difference plainly.
    """
    configs = (
        EvalConfig(
            id="fusion_tr_budget",
            label="Fusion budget panel",
            kind="fusion",
            generation_models=(
                "google/gemini-3-flash-preview",
                "moonshotai/kimi-k2.6",
                "deepseek/deepseek-v4-pro",
            ),
            final_model="z-ai/glm-4.7",
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="fusion_tr_current",
            label="Fusion current TR panel",
            kind="fusion",
            generation_models=(
                "google/gemini-3-flash-preview",
                "moonshotai/kimi-k2.7-code",
                "deepseek/deepseek-v4-pro",
            ),
            final_model="z-ai/glm-4.7",
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="fusion_tr_ultra_cheap",
            label="Fusion ultra-cheap panel",
            kind="fusion",
            generation_models=(
                "deepseek/deepseek-v4-flash",
                "minimax/minimax-m3",
                "mistralai/mistral-small-2603",
            ),
            final_model="mistralai/mistral-small-2603",
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="solo_deepseek_v4_pro",
            label="Solo DeepSeek V4 Pro",
            kind="solo",
            generation_models=("deepseek/deepseek-v4-pro",),
            final_model=None,
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="solo_kimi_k2_6",
            label="Solo Kimi K2.6",
            kind="solo",
            generation_models=("moonshotai/kimi-k2.6",),
            final_model=None,
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="solo_kimi_k2_7",
            label="Solo Kimi K2.7 Code",
            kind="solo",
            generation_models=("moonshotai/kimi-k2.7-code",),
            final_model=None,
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="solo_gemini_3_flash",
            label="Solo Gemini 3 Flash",
            kind="solo",
            generation_models=("google/gemini-3-flash-preview",),
            final_model=None,
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="solo_mistral_small",
            label="Solo Mistral Small",
            kind="solo",
            generation_models=("mistralai/mistral-small-2603",),
            final_model=None,
            judge_model=DRACO_JUDGE_MODEL,
        ),
    )
    for config in configs:
        validate_config(config)
    return configs


def frontier_solo_draco_configs() -> tuple[EvalConfig, ...]:
    """Expensive solo baselines for explicit OpenRouter comparison runs.

    These are intentionally kept out of `default_draco_configs()` so the normal
    reproduction plan stays cheap. The live runner opts into them by id.
    """
    configs = (
        EvalConfig(
            id="solo_gemini_3_1_pro",
            label="Solo Gemini 3.1 Pro",
            kind="solo",
            generation_models=("google/gemini-3.1-pro-preview",),
            final_model=None,
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="solo_opus_4_8",
            label="Solo Claude Opus 4.8",
            kind="solo",
            generation_models=("anthropic/claude-opus-4.8",),
            final_model=None,
            judge_model=DRACO_JUDGE_MODEL,
        ),
    )
    for config in configs:
        validate_config(config, allow_blocked_models=True)
    return configs


def frontier_fusion_draco_configs() -> tuple[EvalConfig, ...]:
    """Expensive multi-frontier Fusion configs for explicit opt-in runs.

    These are for trying to close the gap to the strongest published systems on
    DRACO-style deep research. They intentionally include blocked-by-default
    frontier models and should only be run with a hard budget and explicit
    `--task-filter non-financial` when comparing the non-financial slice.
    """
    configs = (
        EvalConfig(
            id="fusion_mythos_candidate_6",
            label="Fusion Mythos candidate, 6-model panel",
            kind="fusion",
            generation_models=(
                "openai/gpt-5.5",
                "anthropic/claude-opus-4.8",
                "moonshotai/kimi-k2.7-code",
                "z-ai/glm-5.1",
                "minimax/minimax-m3",
                "google/gemini-3-flash-preview",
            ),
            final_model="anthropic/claude-opus-4.8",
            judge_model=DRACO_JUDGE_MODEL,
        ),
        EvalConfig(
            id="fusion_mythos_candidate_7",
            label="Fusion Mythos candidate, 7-model panel",
            kind="fusion",
            generation_models=(
                "openai/gpt-5.5",
                "anthropic/claude-opus-4.8",
                "moonshotai/kimi-k2.7-code",
                "z-ai/glm-5.1",
                "minimax/minimax-m3",
                "google/gemini-3-flash-preview",
                "google/gemini-3.1-pro-preview",
            ),
            final_model="anthropic/claude-opus-4.8",
            judge_model=DRACO_JUDGE_MODEL,
        ),
    )
    for config in configs:
        validate_config(config, allow_blocked_models=True)
    return configs


def frontier_draco_configs() -> tuple[EvalConfig, ...]:
    """All explicit opt-in expensive DRACO configs."""
    return frontier_fusion_draco_configs() + frontier_solo_draco_configs()


def select_micro_tasks(
    task_count: int = DEFAULT_TASK_COUNT,
    *,
    seed: int = DEFAULT_SEED,
    tasks: tuple[MicroTask, ...] = DEFAULT_MICRO_TASKS,
) -> tuple[MicroTask, ...]:
    if task_count < 1:
        raise ValueError("task_count must be positive")
    if task_count > len(tasks):
        raise ValueError(f"task_count cannot exceed {len(tasks)}")

    domains = sorted({task.domain for task in tasks})
    grouped: dict[str, list[MicroTask]] = {
        domain: [task for task in tasks if task.domain == domain] for domain in domains
    }
    for domain_tasks in grouped.values():
        domain_tasks.sort(key=lambda task: _stable_sort_key(seed, task.domain, task.id))
    domains.sort(key=lambda domain: _stable_sort_key(seed, domain))

    selected: list[MicroTask] = []
    while len(selected) < task_count:
        made_progress = False
        for domain in domains:
            domain_tasks = grouped[domain]
            if not domain_tasks:
                continue
            selected.append(domain_tasks.pop(0))
            made_progress = True
            if len(selected) >= task_count:
                break
        if not made_progress:
            break
    return tuple(selected)


def validate_config(config: EvalConfig, *, allow_blocked_models: bool = False) -> None:
    model_ids = list(config.generation_models)
    if config.final_model is not None:
        model_ids.append(config.final_model)
    model_ids.append(config.judge_model)
    for model_id in model_ids:
        if not allow_blocked_models:
            assert_model_allowed(model_id)
        if model_id not in MODELS:
            raise ValueError(f"model is not in the TrustedRouter catalog: {model_id}")


def assert_model_allowed(model_id: str) -> None:
    lowered = model_id.lower()
    for blocked in BLOCKED_MODEL_SUBSTRINGS:
        if blocked in lowered:
            raise ValueError(f"blocked expensive or unavailable model in micro eval: {model_id}")


def estimate_model_call_microdollars(model_id: str, profile: TokenProfile) -> int:
    model = MODELS.get(model_id)
    if model is None:
        raise ValueError(f"model is not in the TrustedRouter catalog: {model_id}")
    return token_cost_microdollars(
        profile.input_tokens,
        model.prompt_price_microdollars_per_million_tokens,
    ) + token_cost_microdollars(
        profile.output_tokens,
        model.completion_price_microdollars_per_million_tokens,
    )


def call_estimates_for_config(
    config: EvalConfig,
    *,
    judge_passes: int = 1,
    allow_blocked_models: bool = False,
) -> tuple[ModelCallEstimate, ...]:
    validate_config(config, allow_blocked_models=allow_blocked_models)
    if judge_passes < 1:
        raise ValueError("judge_passes must be positive")
    calls: list[ModelCallEstimate] = []
    if config.kind == "fusion":
        for model_id in config.generation_models:
            calls.append(_call_estimate(config.id, "panel", model_id, PANEL_PROFILE))
        if config.final_model is None:
            raise ValueError(f"fusion config {config.id} needs a final_model")
        calls.append(_call_estimate(config.id, "final", config.final_model, FINAL_PROFILE))
    else:
        if len(config.generation_models) != 1:
            raise ValueError(f"solo config {config.id} must have exactly one generation model")
        calls.append(_call_estimate(config.id, "solo", config.generation_models[0], PANEL_PROFILE))
    calls.append(
        _call_estimate(
            config.id,
            "judge",
            config.judge_model,
            JUDGE_PROFILE,
            calls=judge_passes,
        )
    )
    return tuple(calls)


def estimate_config(
    config: EvalConfig,
    *,
    task_count: int,
    live_search: bool,
    judge_passes: int = 1,
    search_requests_per_generation_call: int = 1,
    allow_blocked_models: bool = False,
) -> ConfigEstimate:
    calls = call_estimates_for_config(
        config,
        judge_passes=judge_passes,
        allow_blocked_models=allow_blocked_models,
    )
    per_task_model_cost = sum(call.cost_microdollars for call in calls)
    search_requests_per_task = 0
    if live_search:
        generation_call_count = sum(1 for call in calls if call.stage != "judge")
        search_requests_per_task = generation_call_count * search_requests_per_generation_call
    model_cost = per_task_model_cost * task_count
    search_cost = search_requests_per_task * EXA_SEARCH_WITH_CONTENT_MICRODOLLARS * task_count
    return ConfigEstimate(
        config_id=config.id,
        label=config.label,
        kind=config.kind,
        task_count=task_count,
        live_search=live_search,
        search_requests_per_task=search_requests_per_task,
        model_cost_microdollars=model_cost,
        search_cost_microdollars=search_cost,
        total_cost_microdollars=model_cost + search_cost,
        calls=calls,
    )


def build_micro_run_plan(
    *,
    mode: MicroMode,
    task_count: int = DEFAULT_TASK_COUNT,
    seed: int = DEFAULT_SEED,
    max_cost_microdollars: int = DEFAULT_MAX_COST_MICRODOLLARS,
    warn_cost_microdollars: int = DEFAULT_WARN_COST_MICRODOLLARS,
    include_kimi_2_6: bool = False,
) -> MicroRunPlan:
    tasks = select_micro_tasks(task_count, seed=seed)
    configs = default_micro_configs(include_kimi_2_6=include_kimi_2_6)
    segments = _segments_for_mode(mode, configs=configs, task_count=len(tasks))
    plan = MicroRunPlan(
        mode=mode,
        seed=seed,
        max_cost_microdollars=max_cost_microdollars,
        warn_cost_microdollars=warn_cost_microdollars,
        tasks=tasks,
        configs=configs,
        segments=segments,
    )
    if plan.total_cost_microdollars > max_cost_microdollars:
        raise BudgetExceededError(
            "micro eval estimate exceeds cap: "
            f"{format_money_precise(plan.total_cost_microdollars)} > "
            f"{format_money_precise(max_cost_microdollars)}"
        )
    return plan


def write_micro_artifacts(plan: MicroRunPlan, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "costs.json").write_text(
        json.dumps(cost_artifact(plan), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "scores.json").write_text(
        json.dumps(score_placeholder_artifact(plan), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "frontier.svg").write_text(cost_frontier_svg(plan), encoding="utf-8")
    (output_dir / "README.md").write_text(readme_artifact(plan), encoding="utf-8")


def build_draco_eval_plan(
    *,
    task_count: int = DRACO_FULL_TASK_COUNT,
    judge_passes: int = DRACO_JUDGE_PASSES,
    search_requests_per_generation_call: int = 1,
) -> FullEvalPlan:
    configs = default_draco_configs()
    estimates = tuple(
        estimate_config(
            config,
            task_count=task_count,
            live_search=True,
            judge_passes=judge_passes,
            search_requests_per_generation_call=search_requests_per_generation_call,
        )
        for config in configs
    )
    return FullEvalPlan(
        id="draco-fusion-reproduction",
        label="DRACO Fusion reproduction plan",
        task_count=task_count,
        judge_passes=judge_passes,
        search_requests_per_generation_call=search_requests_per_generation_call,
        config_estimates=estimates,
        notes=(
            "Estimate-only plan. It does not run live providers or judges.",
            "The plan avoids Fable, Opus, and GPT-5.5 by policy.",
            "Scores from this plan are not directly comparable to OpenRouter until the live executor runs the same task set and publishes judge details.",
        ),
    )


def write_draco_eval_artifacts(plan: FullEvalPlan, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "draco-costs.json").write_text(
        json.dumps(draco_cost_artifact(plan), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "draco-frontier.svg").write_text(draco_cost_frontier_svg(plan), encoding="utf-8")
    (output_dir / "README.md").write_text(draco_readme_artifact(plan), encoding="utf-8")


def cost_artifact(plan: MicroRunPlan) -> dict[str, Any]:
    return {
        "schema": "trustedrouter.fusion_micro.costs.v1",
        "mode": plan.mode,
        "seed": plan.seed,
        "task_count": len(plan.tasks),
        "max_cost_microdollars": plan.max_cost_microdollars,
        "warn_cost_microdollars": plan.warn_cost_microdollars,
        "total_cost_microdollars": plan.total_cost_microdollars,
        "total_cost_usd": microdollars_to_decimal(plan.total_cost_microdollars),
        "model_cost_microdollars": plan.model_cost_microdollars,
        "search_cost_microdollars": plan.search_cost_microdollars,
        "notes": [
            "Estimate-only artifact. It does not contain model outputs.",
            "Micro scores are tuning signals only and must not be used as public benchmark claims.",
        ],
        "tasks": [{"id": task.id, "domain": task.domain} for task in plan.tasks],
        "segments": [_segment_to_dict(segment) for segment in plan.segments],
    }


def draco_cost_artifact(plan: FullEvalPlan) -> dict[str, Any]:
    return {
        "schema": "trustedrouter.fusion_draco.costs.v1",
        "id": plan.id,
        "label": plan.label,
        "task_count": plan.task_count,
        "judge_passes": plan.judge_passes,
        "search_requests_per_generation_call": plan.search_requests_per_generation_call,
        "model_cost_microdollars": plan.model_cost_microdollars,
        "search_cost_microdollars": plan.search_cost_microdollars,
        "total_cost_microdollars": plan.total_cost_microdollars,
        "total_cost_usd": microdollars_to_decimal(plan.total_cost_microdollars),
        "notes": list(plan.notes),
        "configs": [
            {
                "id": estimate.config_id,
                "label": estimate.label,
                "kind": estimate.kind,
                "task_count": estimate.task_count,
                "search_requests_per_task": estimate.search_requests_per_task,
                "model_cost_microdollars": estimate.model_cost_microdollars,
                "search_cost_microdollars": estimate.search_cost_microdollars,
                "total_cost_microdollars": estimate.total_cost_microdollars,
                "calls": [
                    {
                        "stage": call.stage,
                        "model": call.model,
                        "calls": call.calls,
                        "input_tokens": call.input_tokens,
                        "output_tokens": call.output_tokens,
                        "cost_microdollars": call.cost_microdollars,
                    }
                    for call in estimate.calls
                ],
            }
            for estimate in plan.config_estimates
        ],
    }


def score_placeholder_artifact(plan: MicroRunPlan) -> dict[str, Any]:
    return {
        "schema": "trustedrouter.fusion_micro.scores.v1",
        "mode": plan.mode,
        "seed": plan.seed,
        "status": "not_run",
        "reason": "This local micro runner estimates bounded cost. Live judging is intentionally separate.",
        "scores": [
            {
                "config_id": config.id,
                "label": config.label,
                "score": None,
                "cost_microdollars": _config_total_in_plan(plan, config.id),
            }
            for config in plan.configs
        ],
    }


def cost_frontier_svg(plan: MicroRunPlan) -> str:
    totals = [(config.id, _config_total_in_plan(plan, config.id)) for config in plan.configs]
    max_total = max((total for _config_id, total in totals), default=1)
    width = 840
    row_h = 42
    chart_h = 80 + row_h * len(totals)
    rows: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{chart_h}" viewBox="0 0 {width} {chart_h}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="32" y="36" font-family="Inter, Arial, sans-serif" font-size="22" font-weight="700" fill="#172033">Fusion micro cost frontier</text>',
        '<text x="32" y="60" font-family="Inter, Arial, sans-serif" font-size="13" fill="#5b6472">Estimate-only. Quality scores are populated by the live eval runner.</text>',
    ]
    for index, (config_id, total) in enumerate(totals):
        y = 92 + index * row_h
        bar_w = max(2, int(520 * total / max_total))
        rows.extend(
            [
                f'<text x="32" y="{y + 17}" font-family="Inter, Arial, sans-serif" font-size="13" fill="#172033">{_xml_escape(config_id)}</text>',
                f'<rect x="260" y="{y}" width="520" height="20" rx="4" fill="#eef2f7"/>',
                f'<rect x="260" y="{y}" width="{bar_w}" height="20" rx="4" fill="#2d5db3"/>',
                f'<text x="790" y="{y + 16}" font-family="Inter, Arial, sans-serif" font-size="12" fill="#172033">{_xml_escape(format_money_precise(total))}</text>',
            ]
        )
    rows.append("</svg>\n")
    return "\n".join(rows)


def draco_cost_frontier_svg(plan: FullEvalPlan) -> str:
    totals = [
        (estimate.config_id, estimate.total_cost_microdollars) for estimate in plan.config_estimates
    ]
    max_total = max((total for _config_id, total in totals), default=1)
    width = 920
    row_h = 42
    chart_h = 92 + row_h * len(totals)
    rows: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{chart_h}" viewBox="0 0 {width} {chart_h}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="32" y="36" font-family="Inter, Arial, sans-serif" font-size="22" font-weight="700" fill="#172033">DRACO Fusion reproduction cost plan</text>',
        f'<text x="32" y="62" font-family="Inter, Arial, sans-serif" font-size="13" fill="#5b6472">{plan.task_count} tasks, {plan.judge_passes} judge passes, live-search cost estimated separately.</text>',
    ]
    for index, (config_id, total) in enumerate(totals):
        y = 100 + index * row_h
        bar_w = max(2, int(540 * total / max_total))
        rows.extend(
            [
                f'<text x="32" y="{y + 17}" font-family="Inter, Arial, sans-serif" font-size="13" fill="#172033">{_xml_escape(config_id)}</text>',
                f'<rect x="300" y="{y}" width="540" height="20" rx="4" fill="#eef2f7"/>',
                f'<rect x="300" y="{y}" width="{bar_w}" height="20" rx="4" fill="#2d5db3"/>',
                f'<text x="852" y="{y + 16}" font-family="Inter, Arial, sans-serif" font-size="12" fill="#172033">{_xml_escape(format_money_precise(total))}</text>',
            ]
        )
    rows.append("</svg>\n")
    return "\n".join(rows)


def readme_artifact(plan: MicroRunPlan) -> str:
    return (
        "# TrustedRouter Fusion micro eval\n\n"
        "This directory was generated by `scripts/fusion_micro_eval.py`.\n\n"
        f"- Mode: `{plan.mode}`\n"
        f"- Tasks: `{len(plan.tasks)}`\n"
        f"- Estimated model cost: `{format_money_precise(plan.model_cost_microdollars)}`\n"
        f"- Estimated search cost: `{format_money_precise(plan.search_cost_microdollars)}`\n"
        f"- Estimated total cost: `{format_money_precise(plan.total_cost_microdollars)}`\n\n"
        "Estimate-only: live generation and judging should be run by a separate executor with "
        "the same budget guard.\n\n"
        "The artifacts are safe to inspect locally. They do not contain generated model outputs.\n"
    )


def draco_readme_artifact(plan: FullEvalPlan) -> str:
    return (
        "# TrustedRouter Fusion DRACO reproduction plan\n\n"
        "This directory was generated by `scripts/fusion_full_eval.py`.\n\n"
        f"- Tasks: `{plan.task_count}`\n"
        f"- Judge passes: `{plan.judge_passes}`\n"
        f"- Search requests per generation call: `{plan.search_requests_per_generation_call}`\n"
        f"- Estimated model cost: `{format_money_precise(plan.model_cost_microdollars)}`\n"
        f"- Estimated search cost: `{format_money_precise(plan.search_cost_microdollars)}`\n"
        f"- Estimated total cost: `{format_money_precise(plan.total_cost_microdollars)}`\n\n"
        "Estimate-only: this is the open reproducibility budget and run matrix, not a scored result.\n"
    )


def _segments_for_mode(
    mode: MicroMode,
    *,
    configs: tuple[EvalConfig, ...],
    task_count: int,
) -> tuple[SegmentEstimate, ...]:
    fusion_configs = tuple(config for config in configs if config.kind == "fusion")
    smoke_count = min(SEARCH_SMOKE_TASK_COUNT, task_count)
    if mode == "micro-offline":
        return (
            _segment(
                "offline",
                "All configs across the deterministic micro slice, no live search.",
                task_count=task_count,
                configs=configs,
                live_search=False,
            ),
        )
    if mode == "micro-search-smoke":
        return (
            _segment(
                "search-smoke",
                "Fusion configs only on a tiny live-search smoke slice.",
                task_count=smoke_count,
                configs=fusion_configs,
                live_search=True,
            ),
        )
    if mode == "micro-hybrid":
        return (
            _segment(
                "offline",
                "All configs across the deterministic micro slice, no live search.",
                task_count=task_count,
                configs=configs,
                live_search=False,
            ),
            _segment(
                "search-smoke",
                "Fusion configs only on a tiny live-search smoke slice.",
                task_count=smoke_count,
                configs=fusion_configs,
                live_search=True,
            ),
        )
    raise ValueError(f"unsupported micro mode: {mode}")


def _segment(
    segment_id: str,
    description: str,
    *,
    task_count: int,
    configs: tuple[EvalConfig, ...],
    live_search: bool,
) -> SegmentEstimate:
    return SegmentEstimate(
        id=segment_id,
        description=description,
        task_count=task_count,
        live_search=live_search,
        config_estimates=tuple(
            estimate_config(config, task_count=task_count, live_search=live_search)
            for config in configs
        ),
    )


def _call_estimate(
    config_id: str,
    stage: str,
    model_id: str,
    profile: TokenProfile,
    *,
    calls: int = 1,
) -> ModelCallEstimate:
    if calls < 1:
        raise ValueError("calls must be positive")
    return ModelCallEstimate(
        config_id=config_id,
        stage=stage,
        model=model_id,
        calls=calls,
        input_tokens=profile.input_tokens,
        output_tokens=profile.output_tokens,
        cost_microdollars=estimate_model_call_microdollars(model_id, profile) * calls,
    )


def _segment_to_dict(segment: SegmentEstimate) -> dict[str, Any]:
    return {
        "id": segment.id,
        "description": segment.description,
        "task_count": segment.task_count,
        "live_search": segment.live_search,
        "model_cost_microdollars": segment.model_cost_microdollars,
        "search_cost_microdollars": segment.search_cost_microdollars,
        "total_cost_microdollars": segment.total_cost_microdollars,
        "configs": [
            {
                "id": item.config_id,
                "label": item.label,
                "kind": item.kind,
                "task_count": item.task_count,
                "live_search": item.live_search,
                "search_requests_per_task": item.search_requests_per_task,
                "model_cost_microdollars": item.model_cost_microdollars,
                "search_cost_microdollars": item.search_cost_microdollars,
                "total_cost_microdollars": item.total_cost_microdollars,
                "calls": [
                    {
                        "stage": call.stage,
                        "model": call.model,
                        "input_tokens": call.input_tokens,
                        "output_tokens": call.output_tokens,
                        "cost_microdollars": call.cost_microdollars,
                    }
                    for call in item.calls
                ],
            }
            for item in segment.config_estimates
        ],
    }


def _config_total_in_plan(plan: MicroRunPlan, config_id: str) -> int:
    total = 0
    for segment in plan.segments:
        for estimate in segment.config_estimates:
            if estimate.config_id == config_id:
                total += estimate.total_cost_microdollars
    return total


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _stable_sort_key(seed: int, *parts: str) -> str:
    payload = ":".join((str(seed), *parts)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
