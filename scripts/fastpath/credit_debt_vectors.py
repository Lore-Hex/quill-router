"""Golden vectors for the Go port of the debt rules (fastpath/internal/creditdebt).

Runs `trusted_router.credit_debt` over a fixed set of credit rows and amounts,
edge cases and seeded random ones, and writes each result to
fastpath/testdata/credit_debt_vectors.json. The Go tests hold the port to the
file; tests/test_fastpath_credit_debt_vectors.py fails when the file is not
what this script writes now.

    uv run python scripts/fastpath/credit_debt_vectors.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trusted_router import credit_debt  # noqa: E402

OUTPUT = ROOT / "fastpath" / "testdata" / "credit_debt_vectors.json"

EDGE_ROWS: list[list[int]] = [
    [0], [1], [-1], [5, -3], [-3, 5], [-5, 3], [3, -3], [0, 0, 0], [-1, -1, 2], [2, -1, -1],
    [-2, -2, 3], [3, -5, 1, 1], [1, 1, -5, 3], [-4, 0, 0, 4], [0, -7, 7, 0], [-1, 2, -1, 0],
    [10**15, -(10**15)], [-(10**15), 10**15 - 1], [10**15] * 16, [-1] * 15 + [15],
    # At int64's bounds: sums that pass them on the way and end inside.
    [2**63 - 1, 1, -1], [2**63 - 1, -5, 3], [-(2**63) + 1, 2**63 - 1, 1], [1, 2**63 - 1, -(2**63) + 1],
]
EDGE_AMOUNTS = [0, 1, 2, 3, 5, 7, 10, 10**15]


def _result(call: Any) -> Any:
    try:
        return call()
    except ValueError:
        return {"error": True}


def case(headroom: list[int], amounts: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {"headroom": headroom}
    out["cover"] = _result(lambda: list(credit_debt.cover(headroom)))
    squared = credit_debt.square(headroom)
    out["square"] = {"headroom": list(squared.headroom), "marked": squared.marked}
    out["deltas"] = list(credit_debt.credit_deltas(headroom, squared.headroom))
    inflows = []
    for amount in amounts:
        rows, left = credit_debt.repay(headroom, amount)
        inflow = credit_debt.take_inflow(headroom, amount)
        inflows.append({
            "amount": amount,
            "repay": {"headroom": list(rows), "left": left},
            "take_inflow": {"headroom": list(inflow.headroom), "marked": inflow.marked, "left": inflow.left},
        })
    out["inflows"] = inflows
    return out


def vectors() -> dict[str, Any]:
    cases = [case(rows, EDGE_AMOUNTS) for rows in EDGE_ROWS]
    rng = random.Random(1586)  # noqa: S311 - reproducible test vectors, not secrets.
    for _ in range(200):
        shards = rng.randint(1, 8) if rng.random() < 0.9 else 16
        scale = rng.choice([3, 50, 10_000]) if rng.random() < 0.9 else 10**12
        rows = [rng.randint(-scale, scale) for _ in range(shards)]
        amounts = sorted({0, rng.randint(0, scale), rng.randint(0, 4 * scale), max(0, -sum(rows))})
        cases.append(case(rows, amounts))
    refused = [
        {"function": "square", "headroom": [], "amount": 0},
        {"function": "cover", "headroom": [], "amount": 0},
        {"function": "repay", "headroom": [], "amount": 1},
        {"function": "take_inflow", "headroom": [], "amount": 1},
        {"function": "repay", "headroom": [1, -1], "amount": -1},
        {"function": "take_inflow", "headroom": [1, -1], "amount": -1},
        {"function": "credit_deltas", "headroom": [1, 2], "after": [1]},
    ]
    for r in refused:
        call = {
            "square": lambda r=r: credit_debt.square(r["headroom"]),
            "cover": lambda r=r: credit_debt.cover(r["headroom"]),
            "repay": lambda r=r: credit_debt.repay(r["headroom"], r["amount"]),
            "take_inflow": lambda r=r: credit_debt.take_inflow(r["headroom"], r["amount"]),
            "credit_deltas": lambda r=r: credit_debt.credit_deltas(r["headroom"], r["after"]),
        }[r["function"]]
        try:
            call()
        except ValueError:
            continue
        raise AssertionError(f"credit_debt.{r['function']} takes {r}")
    return {"generator": "scripts/fastpath/credit_debt_vectors.py", "cases": cases, "refused": refused}


def render() -> str:
    """The file's text: one case a line, so a change shows as the cases it changes."""
    v = vectors()
    cases = [json.dumps(c, sort_keys=True, separators=(",", ":")) for c in v["cases"]]
    return (
        "{\"generator\": " + json.dumps(v["generator"]) + ",\n"
        + " \"refused\": " + json.dumps(v["refused"], sort_keys=True) + ",\n"
        + " \"cases\": [\n  " + ",\n  ".join(cases) + "\n ]}\n"
    )


if __name__ == "__main__":
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
