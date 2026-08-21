"""Local serialization contract for pre-promotion rollout mutations."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "scripts/deploy/rollout_local_lock.py"


def _command(lock_path: Path, delay: str) -> list[str]:
    return [
        sys.executable,
        str(LOCK),
        str(lock_path),
        "--",
        sys.executable,
        "-c",
        f"import time; time.sleep({delay})",
    ]


def test_local_rollout_lock_rejects_concurrent_mutation_and_releases_on_exit(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "rollout.lock"
    first = subprocess.Popen(  # noqa: S603
        _command(lock_path, "0.6"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 2
    while not lock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    second = subprocess.run(  # noqa: S603
        _command(lock_path, "0"), capture_output=True, text=True, check=False
    )
    assert second.returncode != 0
    assert "another local rollout operation" in second.stderr
    assert first.wait(timeout=2) == 0

    third = subprocess.run(  # noqa: S603
        _command(lock_path, "0"), capture_output=True, text=True, check=False
    )
    assert third.returncode == 0, third.stderr
    assert lock_path.stat().st_mode & 0o777 == 0o600
