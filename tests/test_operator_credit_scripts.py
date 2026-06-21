from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manual_credit_scripts_enable_typed_counter_mirror_before_store_creation() -> None:
    for relpath in ("scripts/credit_makeup.py", "scripts/credit_grant_joseph.py"):
        source = (ROOT / relpath).read_text(encoding="utf-8")
        assert 'setdefault("TR_TYPED_COUNTER_MIRROR"' not in source
        mirror_pos = source.index('os.environ["TR_TYPED_COUNTER_MIRROR"] = "1"')
        store_pos = source.index("create_store(Settings())")
        assert mirror_pos < store_pos
