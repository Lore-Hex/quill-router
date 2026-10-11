"""The deployment harness must not lose evidence from parallel regions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .deploy_script_harness import _STUB


def test_parallel_long_commands_are_recorded_without_interleaving(tmp_path: Path) -> None:
    stub = tmp_path / "gcloud"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    log = tmp_path / "argv.log"
    env = {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}",
        "HARNESS_ARGV_LOG": str(log),
    }
    # Exceed stdio's buffer: one shell printf can perform several writes.
    arguments = [f"region-{index}:" + str(index % 10) * 65536 for index in range(16)]
    processes = []
    try:
        for argument in arguments:
            processes.append(subprocess.Popen(  # noqa: S603 - repo stub and synthetic argv only
                [str(stub), "record-only", argument],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            ))
        for process in processes:
            _, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr.decode()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()

    assert sorted(log.read_text().splitlines()) == sorted(
        f"gcloud\trecord-only\t{argument}" for argument in arguments
    )
