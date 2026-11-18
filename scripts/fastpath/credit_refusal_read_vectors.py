"""Golden vectors for the edge's reading of X-TR-Credit-Refusal-Read.

Writes, for a fixed set of reads, the header Python's internal authorize 402
carries (trusted_router.credit_refusal_read) to
fastpath/testdata/credit_refusal_read_vectors_v1.json. The Go shadow's parser
(fastpath/internal/edge/funding.go, parseCreditRefusalRead) is held to the
file; tests/test_credit_refusal_read.py fails when the file is not what this
script writes now.

    uv run python scripts/fastpath/credit_refusal_read_vectors.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trusted_router.credit_refusal_read import (  # noqa: E402
    CREDIT_REFUSAL_READ_HEADER,
    HEADROOM,
    HEAL,
    REBALANCE,
    REMEMBERED,
    CreditRefusalRead,
    credit_refusal_read_headers,
)

OUTPUT = ROOT / "fastpath" / "testdata" / "credit_refusal_read_vectors_v1.json"

INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)

READS = [
    # The 2026-11-18 07:42:14Z page's figures: 32 rows and the hold of
    # 82,074, with the sum the shadow read on a refusal two seconds before.
    CreditRefusalRead(REMEMBERED, 82_074, 32, 75_869, 40_896, 1_794_987_734_889_123_456),
    CreditRefusalRead(HEAL, 82_960, 1, 50_000, 50_000, 1_794_987_734_000_000_000),
    CreditRefusalRead(HEADROOM, 82_960, 3, 80_000, 30_000, 1_794_987_734_999_999_999),
    CreditRefusalRead(HEADROOM, 1, 16, -12, 0, 1_792_000_000_000_000_007),
    CreditRefusalRead(REBALANCE, 82_960, 2, 70_000, 50_000),
    CreditRefusalRead(REBALANCE, 0, 4, -40, -10),
    CreditRefusalRead(REMEMBERED, INT64_MAX, 1, INT64_MIN, INT64_MAX, 1),
]


def vectors() -> dict[str, Any]:
    cases = []
    for read in READS:
        [(name, value)] = credit_refusal_read_headers(read).items()
        assert name == CREDIT_REFUSAL_READ_HEADER
        cases.append({
            "read": read.read,
            "read_timestamp_ns": read.read_timestamp_ns,
            "hold": read.hold,
            "rows": read.rows,
            "sum": read.available,
            "most": read.most,
            "header": value,
        })
    return {
        "generator": "scripts/fastpath/credit_refusal_read_vectors.py",
        "header": CREDIT_REFUSAL_READ_HEADER,
        "cases": cases,
    }


def render() -> str:
    return json.dumps(vectors(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUTPUT.write_text(render())


if __name__ == "__main__":
    main()
