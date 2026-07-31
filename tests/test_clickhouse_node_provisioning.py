from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_clickhouse_node_preserves_disk_and_blocks_accidental_vm_deletion() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_node.sh").read_text()

    assert "--no-boot-disk-auto-delete" in script
    assert "set-disk-auto-delete" in script
    assert "--no-auto-delete" in script
    assert script.count("--deletion-protection") >= 2
