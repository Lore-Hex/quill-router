from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manual_credit_scripts_do_not_set_retired_counter_mirror_env() -> None:
    retired_env = "_".join(("TR", "TYPED", "COUNTER", "MIRROR"))
    for relpath in ("scripts/credit_makeup.py", "scripts/credit_grant_joseph.py"):
        source = (ROOT / relpath).read_text(encoding="utf-8")
        assert retired_env not in source
