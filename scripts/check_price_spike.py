#!/usr/bin/env python3
"""Compare two openrouter_snapshot.json files; fail on suspicious price spikes.

Used in `.github/workflows/refresh-prices.yml` between the
provider-direct refresh step and the auto-commit step. The hourly
auto-rollback already catches catalog-shape regressions, so this only
needs to defend against literal-2x parsing-bug spikes.

Fails (exit 1) when:
  * any single model's prompt OR completion cost goes ≥ 2× the previous
    value (>=100% increase); OR
  * an existing model's both prompt and completion go to literal 0.

Removed models are noted in --summary output but do not fail.

Usage:
    python scripts/check_price_spike.py BEFORE.json AFTER.json
    python scripts/check_price_spike.py BEFORE.json AFTER.json --summary
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

DEFAULT_SPIKE_RATIO = 2.0  # 100% increase = ≥2× the previous value


def _load(path: Path) -> dict[str, dict[str, str]]:
    """Return {model_id: {"prompt": str, "completion": str}} from a snapshot file."""
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for model in snapshot.get("models", []):
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        pricing = model.get("pricing") or {}
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        out[model_id] = {
            "prompt": str(pricing.get("prompt") or "0"),
            "completion": str(pricing.get("completion") or "0"),
        }
    return out


def _to_decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except Exception:  # noqa: BLE001
        return Decimal("0")


def check(
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
    spike_ratio: float = DEFAULT_SPIKE_RATIO,
) -> tuple[list[str], list[str], list[str]]:
    """Return (failures, changes, removed).

    failures: list of human-readable reasons why the workflow should fail
    changes: list of price-change lines (for --summary)
    removed: list of model ids present in before but not after
    """
    failures: list[str] = []
    changes: list[str] = []
    removed: list[str] = []
    spike = Decimal(str(spike_ratio))

    for model_id, prev in before.items():
        if model_id not in after:
            removed.append(model_id)
            continue
        cur = after[model_id]
        prev_p = _to_decimal(prev["prompt"])
        cur_p = _to_decimal(cur["prompt"])
        prev_c = _to_decimal(prev["completion"])
        cur_c = _to_decimal(cur["completion"])

        # Literal 2× spike on either dimension.
        for dim, prv, curv in (
            ("prompt", prev_p, cur_p),
            ("completion", prev_c, cur_c),
        ):
            if prv > 0 and curv >= prv * spike:
                ratio = curv / prv
                failures.append(
                    f"{model_id} {dim}: {prv} → {curv} (×{ratio:.2f} ≥ ×{spike})"
                )

        # Both dimensions zeroed out.
        if prev_p > 0 and prev_c > 0 and cur_p == 0 and cur_c == 0:
            failures.append(
                f"{model_id}: both prompt and completion went to 0 "
                f"(was prompt={prev_p}, completion={prev_c})"
            )

        if prev_p != cur_p or prev_c != cur_c:
            direction_p = (
                "+" if cur_p > prev_p else "-" if cur_p < prev_p else "="
            )
            direction_c = (
                "+" if cur_c > prev_c else "-" if cur_c < prev_c else "="
            )
            changes.append(
                f"{model_id}: prompt {prev_p}{direction_p}{cur_p}, "
                f"completion {prev_c}{direction_c}{cur_c}"
            )

    return failures, changes, removed


def _summary_line(changes: list[str], removed: list[str]) -> str:
    n_changed = len(changes)
    if n_changed == 0 and not removed:
        return "no price changes"
    parts = [f"{n_changed} prices changed"]
    if removed:
        parts.append(f"{len(removed)} models removed")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="emit a one-line summary suitable for the commit body",
    )
    parser.add_argument(
        "--spike-ratio",
        type=float,
        default=DEFAULT_SPIKE_RATIO,
        help=f"fail when after/before >= this (default {DEFAULT_SPIKE_RATIO})",
    )
    args = parser.parse_args(argv)

    before = _load(args.before)
    after = _load(args.after)
    failures, changes, removed = check(before, after, args.spike_ratio)

    if args.summary:
        print(_summary_line(changes, removed))
        if removed:
            print(f"removed: {', '.join(removed[:10])}" + (
                f" (+{len(removed) - 10} more)" if len(removed) > 10 else ""
            ))
        return 1 if failures else 0

    for line in changes:
        print(line)
    if removed:
        print(f"removed ({len(removed)}): {', '.join(removed[:10])}" + (
            f" ... and {len(removed) - 10} more" if len(removed) > 10 else ""
        ))
    if failures:
        print("", file=sys.stderr)
        print("PRICE SPIKE FAILURES:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
