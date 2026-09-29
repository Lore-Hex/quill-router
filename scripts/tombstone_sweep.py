#!/usr/bin/env python3
"""Which release tests fail when a provider delists its models?

The hourly price refresh (.github/workflows/refresh-prices.yml) publishes only
if the release suite (pytest -m "not provider_health") passes, and providers
delist models without notice. A release test that assumes a provider still
serves a model freezes the refresh for every provider the day it is delisted.
This finds such tests. It is too slow for CI (a full suite run per group of
providers); tests/test_catalog_survives_any_delisting.py is the fast part that
runs in the release suite.

Delisting a provider means what the refresh publishes on a second miss (see
scripts/pricing/base.py reconcile_manifest_tombstones): every routable row of
src/trusted_router/data/provider_models/<provider>.json gets routable=false and
routable_reason="delisted-upstream", and the provider's endpoints are dropped
from src/trusted_router/data/openrouter_snapshot.json (a model left with none
is dropped too).

The data files are rewritten IN PLACE and restored with `git checkout`, so run
this in a disposable worktree with no uncommitted data edits:

    git worktree add --detach ../qr-sweep HEAD && cd ../qr-sweep

    # every provider, in groups, then each provider of a failing group alone
    python3 scripts/tombstone_sweep.py sweep /tmp/sweep-out [--group-size 8] [--workers 8]

    # one or more providers delisted, specific test files
    python3 scripts/tombstone_sweep.py run anthropic,baseten tests/test_billing.py [...]

A sweep starts from a passing release suite, so every failure under a
delisting is that delisting's. It resumes from OUT/state.json, and only with the
groups it was written with. Phase 2 runs whole files, not node ids: a delisting can shrink a
catalog-derived parametrization, and pytest runs nothing when a requested node
id is missing. A session that does not finish normally (a delisting that stops
the app from starting, workers that die at import, nothing run) is recorded as
SESSION-CRASH, never as zero failures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path.cwd()
DATA_PATHS = ["src/trusted_router/data/provider_models", "src/trusted_router/data/openrouter_snapshot.json"]
MANIFESTS = ROOT / DATA_PATHS[0]
SNAPSHOT = ROOT / DATA_PATHS[1]
PLUGIN_DIR = Path(__file__).resolve().parent
SESSION_CRASH = "SESSION-CRASH"


def restore() -> None:
    subprocess.run(  # noqa: S603 - fixed argv
        ["git", "checkout", "--", *DATA_PATHS],  # noqa: S607 - git from PATH
        cwd=ROOT,
        check=True,
    )
    dirty = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "status", "--porcelain", "--", *DATA_PATHS],  # noqa: S607 - git from PATH
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit(f"data files not restored: {dirty}")


def delist(providers: list[str]) -> dict[str, int]:
    counts = {"rows": 0, "endpoints": 0, "models_dropped": 0}
    for provider in providers:
        path = MANIFESTS / f"{provider}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for row in raw.get("models", []):
            if not isinstance(row, dict) or row.get("routable") is False:
                continue  # curated, held and already tombstoned rows stay as they are
            row.update(routable=False, routable_reason="delisted-upstream")
            row.setdefault("missing_since", time.strftime("%Y-%m-%d"))
            counts["rows"] += 1
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    gone = set(providers)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    kept = []
    for model in snapshot.get("models", []):
        endpoints = model.get("endpoints") or []
        remaining = [e for e in endpoints if e.get("tr_provider_slug") not in gone]
        counts["endpoints"] += len(endpoints) - len(remaining)
        if endpoints and not remaining:
            counts["models_dropped"] += 1
            continue
        model["endpoints"] = remaining
        kept.append(model)
    snapshot["models"] = kept
    SNAPSHOT.write_text(json.dumps(snapshot), encoding="utf-8")
    return counts


def run_pytest(files: list[str], workers: int, log: Path) -> tuple[set[str], str]:
    """Run the release suite (or these files); return exact failed node ids and the summary."""
    failures_file = Path(tempfile.mkstemp(prefix="tombstone-failures-")[1])
    cmd = [
        "uv", "run", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tombstone_sweep_plugin",
        "-m", "not provider_health", "--tb=line", "-W", "ignore::DeprecationWarning",
        "-n", str(workers), "--dist", "loadgroup", *files,
    ]
    env = dict(
        os.environ,
        PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{PLUGIN_DIR}",
        TOMBSTONE_SWEEP_FAILURES=str(failures_file),
    )
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(  # noqa: S603 - fixed pytest argv plus test file paths
        cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    output = proc.stdout + proc.stderr
    log.write_text(output, encoding="utf-8")
    failures = {line.strip() for line in failures_file.read_text(encoding="utf-8").splitlines() if line.strip()}
    failures_file.unlink(missing_ok=True)
    summary = next(
        (line.strip() for line in reversed(output.splitlines()) if re.search(r"\d+ (passed|failed)|no tests ran", line)),
        None,
    )
    # pytest exits 0 when every test passed and 1 when some failed. Anything
    # else (an internal error, an interrupted or empty session), and a failed
    # session with no failed test on record, means the run did not happen as
    # asked: xdist can print "no tests ran" after its workers die at import.
    if summary is None or proc.returncode not in (0, 1) or (proc.returncode == 1 and not failures):
        last = summary or (output.strip().splitlines() or ["(no output)"])[-1]
        return failures | {SESSION_CRASH}, f"session crashed (exit {proc.returncode}): {last}"
    return failures, summary


def sweep(out: Path, group_size: int, workers: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    providers = sorted(path.stem for path in MANIFESTS.glob("*.json"))
    # A saved group must be the group this run would test under its key;
    # another --group-size or provider list would skip providers on resume.
    for key, group_state in state.get("groups", {}).items():
        start = int(key.removeprefix("group")) * group_size
        if group_state["providers"] != providers[start:start + group_size]:
            raise SystemExit(
                f"{state_path}: {key} holds {group_state['providers']}, but this run's {key} would be "
                f"{providers[start:start + group_size]}. Resume with the same --group-size and "
                "providers, or sweep into a new OUT."
            )

    def save() -> None:
        state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    def log(message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    restore()
    # Every failure under a delisting is that delisting's only if the suite
    # passes without one. Subtracting a failing baseline instead would hide the
    # same test failing for another reason, or a crash, in every group.
    if "baseline" not in state:
        failures, summary = run_pytest([], workers, out / "baseline.log")
        if failures:
            raise SystemExit(f"the release suite must pass before a sweep: {summary}; see {out / 'baseline.log'}")
        state["baseline"] = {"failures": [], "summary": summary}
        save()
        log(f"baseline: {summary}")
    elif state["baseline"]["failures"]:
        raise SystemExit(f"{state_path}: its baseline did not pass ({state['baseline']['summary']}); sweep into a new OUT")

    state.setdefault("groups", {})
    for index in range(0, len(providers), group_size):
        key = f"group{index // group_size:02d}"
        if key in state["groups"]:
            continue
        group = providers[index:index + group_size]
        try:
            counts = delist(group)
            failures, summary = run_pytest([], workers, out / f"{key}.log")
        finally:
            restore()
        state["groups"][key] = {"providers": group, "counts": counts, "summary": summary,
                                "failures": sorted(failures)}
        save()
        log(f"{key} {group}: {summary}")

    state.setdefault("single", {})
    for group_state in state["groups"].values():
        suspects = group_state["failures"]
        if not suspects:
            continue
        crashed = SESSION_CRASH in suspects
        files = [] if crashed else sorted({test.split("::")[0] for test in suspects})
        for provider in group_state["providers"]:
            if provider in state["single"]:
                continue
            try:
                counts = delist([provider])
                failures, summary = run_pytest(files, workers, out / f"single-{provider}.log")
            finally:
                restore()
            state["single"][provider] = {"counts": counts, "summary": summary,
                                         "failures": sorted(failures)}
            save()
            log(f"  {provider}: {summary}")

    attributed = {test for single in state["single"].values() for test in single["failures"]}
    state["needs_several_providers"] = sorted(
        {test for group_state in state["groups"].values() for test in group_state["failures"]} - attributed
    )
    save()
    log(f"done: {len(attributed)} tests fail on a single provider's delisting; "
        f"{len(state['needs_several_providers'])} only when several are delisted together")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    sweep_parser = commands.add_parser("sweep")
    sweep_parser.add_argument("out", type=Path)
    sweep_parser.add_argument("--group-size", type=int, default=8)
    sweep_parser.add_argument("--workers", type=int, default=8)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("providers", help="comma-separated, or - for none")
    run_parser.add_argument("files", nargs="*")
    run_parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if not (ROOT / DATA_PATHS[1]).exists():
        raise SystemExit("run from the repository root of a disposable worktree")
    if args.command == "sweep":
        sweep(args.out, args.group_size, args.workers)
        return
    providers = [] if args.providers == "-" else args.providers.split(",")
    restore()
    try:
        if providers:
            print("delisted:", providers, delist(providers))
        failures, summary = run_pytest(args.files, args.workers, Path(tempfile.mkstemp(suffix=".log")[1]))
    finally:
        restore()
    print(summary)
    for failure in sorted(failures):
        print("FAILED", failure)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
