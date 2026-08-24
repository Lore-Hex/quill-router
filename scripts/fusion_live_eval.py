#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import TextIO, cast

import httpx

from trusted_router.evals.draco import (
    DRACO_TASK_COUNT,
    DRACO_TASK_FILTERS,
    DracoTask,
    DracoTaskFilter,
    filter_draco_tasks,
    load_or_fetch_draco_tasks,
)
from trusted_router.evals.exa import ExaSearchClient
from trusted_router.evals.fusion_live import (
    DEFAULT_DRACO_SEARCH_QUERY_COUNT,
    DEFAULT_FETCH_SEARCH_RESULTS,
    DEFAULT_LENGTH_RETRY_MAX_TOKENS,
    DEFAULT_PANEL_STREAM_TIMEOUT_SECONDS,
    DEFAULT_SEARCH_CONTEXT_CHARS_PER_RESULT,
    DEFAULT_TR_API_BASE_URL,
    DEFAULT_TR_CRITERION_JUDGE_CHUNK_SIZE,
    DEFAULT_TR_CRITERION_JUDGE_MAX_OUTPUT_TOKENS,
    FusionLiveRunner,
    FusionRunResult,
    ScoringMode,
    TrustedRouterChatClient,
    load_eval_key,
)
from trusted_router.evals.fusion_micro import (
    DRACO_JUDGE_PASSES,
    EvalConfig,
    call_estimates_for_config,
    default_draco_configs,
    estimate_config,
    frontier_draco_configs,
)
from trusted_router.money import dollars_to_microdollars, format_money_precise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded live TrustedRouter Fusion reproduction over DRACO."
    )
    parser.add_argument("--task-count", type=int, default=3)
    parser.add_argument("--task-offset", type=int, default=0)
    parser.add_argument(
        "--task-filter",
        choices=DRACO_TASK_FILTERS,
        default="all",
        help="Benchmark subset to run. non-financial excludes finance/PDF-heavy DRACO tasks.",
    )
    parser.add_argument("--config", default="fusion_tr_budget")
    parser.add_argument("--judge-passes", type=int, default=DRACO_JUDGE_PASSES)
    parser.add_argument(
        "--judge-model",
        help="Override the config judge model for estimates and live runs.",
    )
    parser.add_argument("--budget-usd", default="5.00")
    parser.add_argument("--base-url", default=DEFAULT_TR_API_BASE_URL)
    parser.add_argument("--cache-path", type=Path, default=Path("artifacts/draco/tasks.json"))
    parser.add_argument("--refresh-draco", action="store_true")
    parser.add_argument("--no-search", action="store_true")
    parser.add_argument(
        "--shared-search",
        action="store_true",
        help="Reuse one search context per task. Default is per-generation search to match the Fusion post.",
    )
    parser.add_argument(
        "--no-fetch-results",
        action="store_true",
        help="Do not fetch top search result pages. Default approximates web_fetch with transient page fetches.",
    )
    parser.add_argument(
        "--single-step-synthesis",
        action="store_true",
        help="Skip the separate Fusion analysis stage. Default matches the post more closely.",
    )
    parser.add_argument(
        "--no-length-retry",
        action="store_true",
        help="Do not retry truncated generations with larger token caps. Useful for bounded smoke runs.",
    )
    parser.add_argument("--include-content", action="store_true")
    parser.add_argument(
        "--execute", action="store_true", help="Actually call Exa and TrustedRouter."
    )
    parser.add_argument("--panel-max-tokens", type=int, default=1_800)
    parser.add_argument("--final-max-tokens", type=int, default=3_500)
    parser.add_argument(
        "--judge-max-tokens", type=int, default=DEFAULT_TR_CRITERION_JUDGE_MAX_OUTPUT_TOKENS
    )
    parser.add_argument(
        "--criterion-chunk-size", type=int, default=DEFAULT_TR_CRITERION_JUDGE_CHUNK_SIZE
    )
    parser.add_argument(
        "--search-context-chars-per-result",
        type=int,
        default=DEFAULT_SEARCH_CONTEXT_CHARS_PER_RESULT,
    )
    parser.add_argument(
        "--fetch-search-result-count",
        type=int,
        default=DEFAULT_FETCH_SEARCH_RESULTS,
        help="Number of search result pages to fetch per search. Defaults to OpenRouter-style five-result context.",
    )
    parser.add_argument(
        "--search-query-count",
        type=int,
        default=DEFAULT_DRACO_SEARCH_QUERY_COUNT,
        help="Number of query passes per generation. Finance tasks add SEC-biased passes.",
    )
    parser.add_argument(
        "--scoring-mode",
        choices=("criteria", "holistic"),
        default="criteria",
        help="Use DRACO criterion scoring by default. Holistic is legacy and not comparable to OpenRouter.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Append to an existing JSONL file and skip completed task IDs for the selected config.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of DRACO tasks to run in parallel. Defaults to 1.",
    )
    parser.add_argument(
        "--panel-concurrency",
        type=int,
        default=7,
        help="Number of panel model calls to run concurrently inside each Fusion task.",
    )
    parser.add_argument(
        "--panel-timeout-seconds",
        type=float,
        default=DEFAULT_PANEL_STREAM_TIMEOUT_SECONDS,
        help="Per-panel-member stream timeout. Slow panel models are recorded as failures.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/fusion-draco/live-results.jsonl"),
    )
    args = parser.parse_args(argv)

    try:
        budget = dollars_to_microdollars(args.budget_usd)
    except ValueError as exc:
        print(f"invalid budget: {exc}", file=sys.stderr)
        return 2
    if args.task_count < 1 or args.task_count > DRACO_TASK_COUNT:
        print(f"task-count must be between 1 and {DRACO_TASK_COUNT}", file=sys.stderr)
        return 2
    if args.task_offset < 0 or args.task_offset >= DRACO_TASK_COUNT:
        print(f"task-offset must be between 0 and {DRACO_TASK_COUNT - 1}", file=sys.stderr)
        return 2
    if args.judge_passes < 1:
        print("judge-passes must be positive", file=sys.stderr)
        return 2
    if args.concurrency < 1 or args.concurrency > 20:
        print("concurrency must be between 1 and 20", file=sys.stderr)
        return 2
    if args.panel_concurrency < 1 or args.panel_concurrency > 12:
        print("panel-concurrency must be between 1 and 12", file=sys.stderr)
        return 2
    if args.panel_timeout_seconds <= 0:
        print("panel-timeout-seconds must be positive", file=sys.stderr)
        return 2
    if args.timeout_seconds <= 0:
        print("timeout-seconds must be positive", file=sys.stderr)
        return 2
    if args.criterion_chunk_size < 1:
        print("criterion-chunk-size must be positive", file=sys.stderr)
        return 2
    if args.search_context_chars_per_result < 1:
        print("search-context-chars-per-result must be positive", file=sys.stderr)
        return 2
    if args.fetch_search_result_count < 1:
        print("fetch-search-result-count must be positive", file=sys.stderr)
        return 2
    if args.search_query_count < 1:
        print("search-query-count must be positive", file=sys.stderr)
        return 2
    for name in ("panel_max_tokens", "final_max_tokens", "judge_max_tokens"):
        if getattr(args, name) < 1:
            print(f"{name.replace('_', '-')} must be positive", file=sys.stderr)
            return 2

    frontier_config_ids: set[str] = set()
    configs = {config.id: config for config in default_draco_configs()}
    for frontier_config in frontier_draco_configs():
        configs[frontier_config.id] = frontier_config
        frontier_config_ids.add(frontier_config.id)
    config = configs.get(args.config)
    if config is None:
        print(
            f"unknown config {args.config!r}; choose one of {', '.join(sorted(configs))}",
            file=sys.stderr,
        )
        return 2
    if args.judge_model:
        config = replace(config, judge_model=args.judge_model)

    estimate = estimate_config(
        config,
        task_count=args.task_count,
        live_search=not args.no_search,
        judge_passes=args.judge_passes,
        search_requests_per_generation_call=args.search_query_count,
        allow_blocked_models=config.id in frontier_config_ids,
    )
    estimated_total = estimate.total_cost_microdollars
    if not args.single_step_synthesis and config.kind == "fusion":
        final_costs = [
            item.cost_microdollars
            for item in call_estimates_for_config(
                config,
                judge_passes=args.judge_passes,
                allow_blocked_models=config.id in frontier_config_ids,
            )
            if item.stage == "final"
        ]
        if final_costs:
            estimated_total += final_costs[0] * args.task_count
    print(f"config: {config.id}")
    print(f"tasks: {args.task_count}")
    print(f"task offset: {args.task_offset}")
    print(f"task filter: {args.task_filter}")
    print(f"estimated total for selected config: {format_money_precise(estimated_total)}")
    print(f"budget: {format_money_precise(budget)}")
    if estimated_total > budget:
        print("refusing to run: estimated selected config exceeds budget", file=sys.stderr)
        return 2
    if not args.execute:
        print("dry run only; add --execute to call Exa and TrustedRouter.")
        return 0

    tr_key = _first_key(
        (
            "TR_FUSION_EVAL_API_KEY",
            "TR_API_KEY",
            "TRUSTEDROUTER_API_KEY",
            "TR_SMOKE_API_KEY",
            "TR_API_KEY_FOR_SELF_HEAL",
        )
    )
    if not tr_key:
        print(
            "missing TR_FUSION_EVAL_API_KEY, TR_API_KEY, TRUSTEDROUTER_API_KEY, or TR_SMOKE_API_KEY in env/key file",
            file=sys.stderr,
        )
        return 2
    exa_key = None if args.no_search else load_eval_key("EXA_API_KEY")
    if not args.no_search and not exa_key:
        print("missing EXA_API_KEY in env/key file", file=sys.stderr)
        return 2

    task_filter = cast(DracoTaskFilter, args.task_filter)
    cache_length = DRACO_TASK_COUNT if task_filter != "all" else args.task_offset + args.task_count
    tasks = load_or_fetch_draco_tasks(
        args.cache_path,
        refresh=args.refresh_draco,
        length=cache_length,
    )
    eligible_tasks = filter_draco_tasks(tasks, task_filter=task_filter)
    if args.task_offset + args.task_count > len(eligible_tasks):
        print(
            f"task-offset + task-count exceeds eligible task count ({len(eligible_tasks)})",
            file=sys.stderr,
        )
        return 2
    print(f"eligible tasks after filter: {len(eligible_tasks)}")
    selected_tasks = eligible_tasks[args.task_offset : args.task_offset + args.task_count]
    results = _run_tasks(
        selected_tasks,
        task_filter=task_filter,
        config=config,
        tr_key=tr_key,
        exa_key=exa_key,
        base_url=args.base_url,
        judge_passes=args.judge_passes,
        live_search=not args.no_search,
        output=args.output,
        include_content=args.include_content,
        concurrency=args.concurrency,
        panel_max_tokens=args.panel_max_tokens,
        panel_concurrency=args.panel_concurrency,
        panel_timeout_seconds=args.panel_timeout_seconds,
        final_max_tokens=args.final_max_tokens,
        judge_max_tokens=args.judge_max_tokens,
        scoring_mode=args.scoring_mode,
        criterion_chunk_size=args.criterion_chunk_size,
        timeout_seconds=args.timeout_seconds,
        resume=args.resume,
        per_generation_search=not args.shared_search,
        fetch_search_results=not args.no_fetch_results,
        separate_fusion_analysis=not args.single_step_synthesis,
        length_retry_max_tokens=()
        if args.no_length_retry
        else DEFAULT_LENGTH_RETRY_MAX_TOKENS,
        search_context_chars_per_result=args.search_context_chars_per_result,
        fetch_search_result_count=args.fetch_search_result_count,
        search_query_count=args.search_query_count,
    )
    scores = [result.score for result in results if result.score is not None]
    if scores:
        print(f"mean score: {sum(scores) / len(scores):0.2f}")
    else:
        print("mean score: unavailable")
    print(f"results: {args.output}")
    return 0


def _run_tasks(
    tasks: tuple[DracoTask, ...],
    *,
    task_filter: DracoTaskFilter,
    config: EvalConfig,
    tr_key: str,
    exa_key: str | None,
    base_url: str,
    judge_passes: int,
    live_search: bool,
    output: Path,
    include_content: bool,
    concurrency: int,
    panel_max_tokens: int,
    panel_concurrency: int,
    panel_timeout_seconds: float,
    final_max_tokens: int,
    judge_max_tokens: int,
    scoring_mode: ScoringMode,
    criterion_chunk_size: int,
    timeout_seconds: float,
    resume: bool,
    per_generation_search: bool,
    fetch_search_results: bool,
    separate_fusion_analysis: bool,
    length_retry_max_tokens: tuple[int, ...],
    search_context_chars_per_result: int,
    fetch_search_result_count: int,
    search_query_count: int,
) -> tuple[FusionRunResult, ...]:
    output.parent.mkdir(parents=True, exist_ok=True)
    results: list[FusionRunResult] = []
    completed_task_ids = _completed_task_ids(output, config_id=config.id) if resume else set()
    pending_tasks = tuple(task for task in tasks if task.id not in completed_task_ids)
    if completed_task_ids:
        print(f"resuming: skipped {len(completed_task_ids)} completed rows for config={config.id}")
    mode = "a" if resume else "w"
    with output.open(mode, encoding="utf-8") as raw_fh:
        fh = cast(TextIO, raw_fh)
        if concurrency == 1:
            for index, task in enumerate(pending_tasks, start=1):
                try:
                    result = _run_one_task(
                        task,
                        config=config,
                        tr_key=tr_key,
                        exa_key=exa_key,
                        base_url=base_url,
                        judge_passes=judge_passes,
                        live_search=live_search,
                        panel_max_tokens=panel_max_tokens,
                        panel_concurrency=panel_concurrency,
                        panel_timeout_seconds=panel_timeout_seconds,
                        final_max_tokens=final_max_tokens,
                        judge_max_tokens=judge_max_tokens,
                        scoring_mode=scoring_mode,
                        criterion_chunk_size=criterion_chunk_size,
                        timeout_seconds=timeout_seconds,
                        per_generation_search=per_generation_search,
                        fetch_search_results=fetch_search_results,
                        separate_fusion_analysis=separate_fusion_analysis,
                        length_retry_max_tokens=length_retry_max_tokens,
                        search_context_chars_per_result=search_context_chars_per_result,
                        fetch_search_result_count=fetch_search_result_count,
                        search_query_count=search_query_count,
                    )
                except Exception as exc:  # noqa: BLE001 - eval runner records task-level failures.
                    _write_failure_line(fh, task, config_id=config.id, error=exc)
                    print(
                        f"failed {index}/{len(pending_tasks)} task_id={task.id} err={type(exc).__name__}"
                    )
                    continue
                results.append(result)
                _write_result_line(
                    fh,
                    result,
                    include_content=include_content,
                    task_filter=task_filter,
                )
                print(
                    f"completed {index}/{len(pending_tasks)} task_id={task.id} score={result.score}"
                )
            return tuple(results)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_by_task = {
                executor.submit(
                    _run_one_task,
                    task,
                    config=config,
                    tr_key=tr_key,
                    exa_key=exa_key,
                    base_url=base_url,
                    judge_passes=judge_passes,
                    live_search=live_search,
                    panel_max_tokens=panel_max_tokens,
                    panel_concurrency=panel_concurrency,
                    panel_timeout_seconds=panel_timeout_seconds,
                    final_max_tokens=final_max_tokens,
                    judge_max_tokens=judge_max_tokens,
                    scoring_mode=scoring_mode,
                    criterion_chunk_size=criterion_chunk_size,
                    timeout_seconds=timeout_seconds,
                    per_generation_search=per_generation_search,
                    fetch_search_results=fetch_search_results,
                    separate_fusion_analysis=separate_fusion_analysis,
                    length_retry_max_tokens=length_retry_max_tokens,
                    search_context_chars_per_result=search_context_chars_per_result,
                    fetch_search_result_count=fetch_search_result_count,
                    search_query_count=search_query_count,
                ): task
                for task in pending_tasks
            }
            completed = 0
            for future in as_completed(future_by_task):
                task = future_by_task[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - eval runner records task-level failures.
                    completed += 1
                    _write_failure_line(fh, task, config_id=config.id, error=exc)
                    print(
                        f"failed {completed}/{len(pending_tasks)} task_id={task.id} err={type(exc).__name__}"
                    )
                    continue
                results.append(result)
                completed += 1
                _write_result_line(
                    fh,
                    result,
                    include_content=include_content,
                    task_filter=task_filter,
                )
                print(
                    f"completed {completed}/{len(pending_tasks)} task_id={task.id} score={result.score}"
                )
    return tuple(results)


def _run_one_task(
    task: DracoTask,
    *,
    config: EvalConfig,
    tr_key: str,
    exa_key: str | None,
    base_url: str,
    judge_passes: int,
    live_search: bool,
    panel_max_tokens: int,
    panel_concurrency: int,
    panel_timeout_seconds: float,
    final_max_tokens: int,
    judge_max_tokens: int,
    scoring_mode: ScoringMode,
    criterion_chunk_size: int,
    timeout_seconds: float,
    per_generation_search: bool,
    fetch_search_results: bool,
    separate_fusion_analysis: bool,
    length_retry_max_tokens: tuple[int, ...],
    search_context_chars_per_result: int,
    fetch_search_result_count: int,
    search_query_count: int,
) -> FusionRunResult:
    tr_client = TrustedRouterChatClient(
        tr_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        stream_timeout_seconds=timeout_seconds,
    )
    exa_client = ExaSearchClient(exa_key, timeout_seconds=timeout_seconds) if exa_key else None
    try:
        runner = FusionLiveRunner(
            tr_client=tr_client,
            exa_client=exa_client,
            judge_passes=judge_passes,
            panel_max_tokens=panel_max_tokens,
            panel_concurrency=panel_concurrency,
            panel_stream_timeout_seconds=panel_timeout_seconds,
            final_max_tokens=final_max_tokens,
            judge_max_tokens=judge_max_tokens,
            scoring_mode=scoring_mode,
            criterion_chunk_size=criterion_chunk_size,
            per_generation_search=per_generation_search,
            fetch_search_results=fetch_search_results,
            separate_fusion_analysis=separate_fusion_analysis,
            length_retry_max_tokens=length_retry_max_tokens,
            search_context_chars_per_result=search_context_chars_per_result,
            fetch_search_result_count=fetch_search_result_count,
            search_query_count=search_query_count,
        )
        return runner.run_task_config(task, config, live_search=live_search)
    finally:
        tr_client.close()
        if exa_client is not None:
            exa_client.close()


def _write_result_line(
    fh: TextIO,
    result: FusionRunResult,
    *,
    include_content: bool,
    task_filter: DracoTaskFilter,
) -> None:
    payload = result.public_dict(include_content=include_content)
    payload["task_filter"] = task_filter
    fh.write(json.dumps(payload, sort_keys=True) + "\n")
    fh.flush()


def _write_failure_line(fh: TextIO, task: DracoTask, *, config_id: str, error: Exception) -> None:
    payload: dict[str, object] = {
        "config_id": config_id,
        "task_id": task.id,
        "domain": task.domain,
        "status": "failed",
        "error_type": type(error).__name__,
        "error": str(error)[:500],
    }
    if isinstance(error, httpx.HTTPStatusError):
        payload["http_status"] = error.response.status_code
        payload["response_body"] = error.response.text[:1_000]
    fh.write(
        json.dumps(
            payload,
            sort_keys=True,
        )
        + "\n"
    )
    fh.flush()


def _completed_task_ids(path: Path, *, config_id: str) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("config_id") != config_id or row.get("status") == "failed":
            continue
        task_id = row.get("task_id")
        if isinstance(task_id, str):
            completed.add(task_id)
    return completed


def _first_key(names: tuple[str, ...]) -> str | None:
    for name in names:
        if value := load_eval_key(name):
            return value
    return None


if __name__ == "__main__":
    raise SystemExit(main())
