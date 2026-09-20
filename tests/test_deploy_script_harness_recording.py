"""Concurrent stub invocations must remain separate, complete argv records."""

import subprocess
from collections import Counter
from pathlib import Path

import pytest

from .deploy_script_harness import DeployScriptHarness, summarise


def test_concurrent_stub_records_are_complete(tmp_path: Path) -> None:
    isolated = DeployScriptHarness(tmp_path)
    # Keep each record below macOS Bash's 1 KiB output buffer: a single printf
    # can still use multiple writes for oversized records. Concurrency exposes
    # the per-argument writes even with this bounded, representative argv.
    script = isolated.write_script(
        "record-concurrently.sh",
        r"""#!/usr/bin/env bash
set -euo pipefail
arguments=(run services update-traffic trusted-router '--to-revisions=new=100')
for ((i=0; i<80; i++)); do arguments+=("arg-$i"); done
arguments+=('' 'space value' $'line\nbreak' $'tab\tvalue' 'literal\n\t')
pids=()
for ((worker=0; worker<8; worker++)); do
  (
    for ((invocation=0; invocation<25; invocation++)); do
      gcloud "${arguments[@]}" >/dev/null
    done
  ) &
  pids+=("$!")
done
(
  for ((i=0; i<10000; i++)); do
    printf '%s\n' $'regional_quota_reconciler.sh\t--once' >> "$HARNESS_ARGV_LOG"
  done
) &
pids+=("$!")
for pid in "${pids[@]}"; do wait "$pid"; done
""",
    )
    run = isolated.run(script)
    assert run.returncode == 0, summarise(run)
    expected_gcloud = (
        "gcloud",
        "run",
        "services",
        "update-traffic",
        "trusted-router",
        "--to-revisions=new=100",
        *(f"arg-{i}" for i in range(80)),
        "",
        "space value",
        r"line\nbreak",
        r"tab\tvalue",
        r"literal\n\t",
    )
    expected = Counter({expected_gcloud: 200, ("regional_quota_reconciler.sh", "--once"): 10000})
    # Use run.calls: these are parsed by the production harness's tab splitter.
    # Exact record equality catches missing fields, merged names, and torn tails.
    actual = Counter(tuple(call) for call in run.calls)
    malformed = sum(count for call, count in actual.items() if call not in expected)
    assert malformed == 0, f"{malformed} malformed records out of {sum(actual.values())}"
    assert actual == expected


@pytest.mark.parametrize("scale", [None, "2.5"])
@pytest.mark.parametrize("budget", [None, 30])
def test_timeout_scale_applies_to_default_and_explicit_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scale: str | None, budget: int | None
) -> None:
    isolated = DeployScriptHarness(tmp_path)
    if scale is None:
        monkeypatch.delenv("HARNESS_TIMEOUT_SCALE", raising=False)
    else:
        monkeypatch.setenv("HARNESS_TIMEOUT_SCALE", scale)
    timeouts: list[object] = []

    def record_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", record_run)
    if budget is None:
        isolated.run("unused.sh")
    else:
        isolated.run("unused.sh", timeout=budget)
    assert timeouts == [(120 if budget is None else budget) * float(scale or "1")]


@pytest.mark.parametrize("scale", ["0", "-1", "nan", "inf", "invalid"])
def test_invalid_timeout_scale_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scale: str
) -> None:
    isolated = DeployScriptHarness(tmp_path)
    monkeypatch.setenv("HARNESS_TIMEOUT_SCALE", scale)
    with pytest.raises(ValueError):
        isolated.run("unused.sh")
