#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trusted_router.evals.fusion_micro import (
    DEFAULT_SEED,
    DEFAULT_TASK_COUNT,
    BudgetExceededError,
    MicroMode,
    build_micro_run_plan,
    write_micro_artifacts,
)
from trusted_router.money import dollars_to_microdollars, format_money_precise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estimate a bounded TrustedRouter Fusion micro-eval run."
    )
    parser.add_argument(
        "--mode",
        choices=("micro-offline", "micro-search-smoke", "micro-hybrid"),
        default="micro-hybrid",
        help="Cost mode to estimate. Defaults to the near-$1 hybrid plan.",
    )
    parser.add_argument("--task-count", type=int, default=DEFAULT_TASK_COUNT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--max-cost-usd", default="1.00")
    parser.add_argument("--warn-cost-usd", default="0.80")
    parser.add_argument("--include-kimi-2-6", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/fusion-micro"),
        help="Directory for costs.json, scores.json, frontier.svg, and README.md.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Only print the estimate; do not write artifact files.",
    )
    args = parser.parse_args(argv)

    try:
        max_cost = dollars_to_microdollars(args.max_cost_usd)
        warn_cost = dollars_to_microdollars(args.warn_cost_usd)
        plan = build_micro_run_plan(
            mode=_mode(args.mode),
            task_count=args.task_count,
            seed=args.seed,
            max_cost_microdollars=max_cost,
            warn_cost_microdollars=warn_cost,
            include_kimi_2_6=args.include_kimi_2_6,
        )
    except (BudgetExceededError, ValueError) as exc:
        print(f"fusion micro eval rejected: {exc}", file=sys.stderr)
        return 2

    if not args.no_write:
        write_micro_artifacts(plan, args.output_dir)

    print(f"mode: {plan.mode}")
    print(f"tasks: {len(plan.tasks)}")
    print(f"model cost: {format_money_precise(plan.model_cost_microdollars)}")
    print(f"search cost: {format_money_precise(plan.search_cost_microdollars)}")
    print(f"estimated total: {format_money_precise(plan.total_cost_microdollars)}")
    print(f"cap: {format_money_precise(plan.max_cost_microdollars)}")
    if plan.over_warning:
        print(f"warning: estimate is above {format_money_precise(plan.warn_cost_microdollars)}")
    if not args.no_write:
        print(f"artifacts: {args.output_dir}")
    return 0


def _mode(value: str) -> MicroMode:
    if value in {"micro-offline", "micro-search-smoke", "micro-hybrid"}:
        return value  # type: ignore[return-value]
    raise ValueError(f"unsupported mode: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
