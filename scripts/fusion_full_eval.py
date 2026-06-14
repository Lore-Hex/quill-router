#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from trusted_router.evals.draco import load_or_fetch_draco_tasks
from trusted_router.evals.fusion_micro import (
    DRACO_FULL_TASK_COUNT,
    DRACO_JUDGE_PASSES,
    DRACO_PILOT_TASK_COUNT,
    build_draco_eval_plan,
    write_draco_eval_artifacts,
)
from trusted_router.money import format_money_precise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estimate the full TrustedRouter Fusion DRACO reproduction run."
    )
    parser.add_argument(
        "--task-count",
        type=int,
        default=DRACO_FULL_TASK_COUNT,
        help="DRACO task count to estimate. Defaults to 100.",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help=f"Estimate the {DRACO_PILOT_TASK_COUNT}-task pilot instead of the full run.",
    )
    parser.add_argument(
        "--judge-passes",
        type=int,
        default=DRACO_JUDGE_PASSES,
        help="Independent judge passes per response. Defaults to 3.",
    )
    parser.add_argument(
        "--search-requests-per-generation-call",
        type=int,
        default=1,
        help="External search-with-content calls to budget per generation/final call.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/fusion-draco"),
        help="Directory for draco-costs.json, draco-frontier.svg, and README.md.",
    )
    parser.add_argument(
        "--fetch-draco",
        action="store_true",
        help="Fetch/cache the public DRACO test split before writing estimate artifacts.",
    )
    parser.add_argument(
        "--draco-cache-path",
        type=Path,
        default=Path("artifacts/draco/tasks.json"),
        help="Cache path for the public DRACO task/rubric rows.",
    )
    parser.add_argument("--refresh-draco", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    task_count = DRACO_PILOT_TASK_COUNT if args.pilot else args.task_count
    plan = build_draco_eval_plan(
        task_count=task_count,
        judge_passes=args.judge_passes,
        search_requests_per_generation_call=args.search_requests_per_generation_call,
    )
    if not args.no_write:
        write_draco_eval_artifacts(plan, args.output_dir)
    if args.fetch_draco:
        tasks = load_or_fetch_draco_tasks(
            args.draco_cache_path,
            refresh=args.refresh_draco,
            length=task_count,
        )
        print(f"draco cache: {args.draco_cache_path} ({len(tasks)} tasks)")

    print(f"tasks: {plan.task_count}")
    print(f"judge passes: {plan.judge_passes}")
    print(f"model cost: {format_money_precise(plan.model_cost_microdollars)}")
    print(f"search cost: {format_money_precise(plan.search_cost_microdollars)}")
    print(f"estimated total: {format_money_precise(plan.total_cost_microdollars)}")
    if not args.no_write:
        print(f"artifacts: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
