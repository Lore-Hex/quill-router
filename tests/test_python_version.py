import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_running_python_matches_repository_pin() -> None:
    pinned = tuple(map(int, (ROOT / ".python-version").read_text().strip().split(".")))
    assert sys.version_info[:2] == pinned
