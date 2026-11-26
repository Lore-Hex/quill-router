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

    # each model the tests name vanishing from every host at once
    python3 scripts/tombstone_sweep.py models /tmp/models-out [--workers 6] [--only z-ai/glm-5.2 ...]
    python3 scripts/tombstone_sweep.py run - tests/test_x.py --vanish z-ai/glm-5.2

    # each provider changing its prices and limits, in groups, then alone
    python3 scripts/tombstone_sweep.py values /tmp/values-out [--group-size 8] [--workers 8]
    python3 scripts/tombstone_sweep.py run featherless tests/test_x.py --perturb

    # the refresh's own unserved-route hold on every routable row it can hold
    python3 scripts/tombstone_sweep.py holds /tmp/holds-out [--workers 8] [--files tests/test_x.py ...]
    python3 scripts/tombstone_sweep.py run - tests/test_x.py --hold nscale:z-ai/glm-5.3,wafer:moonshotai/kimi-k3

A provider delisting is one event; a model vanishing is another: providers sunset
a model one by one until none serves it (Fireworks retired GLM 5.2 on 2026-09-25),
and no single provider's delisting shows that. `models` tombstones one model's
rows in every manifest and drops it from the snapshot, then runs the test files
that name it, or name a catalog model that depends on it (a preset it is a member
of, a name it backs), since those break with it.

A provider can also change what it lists without delisting anything: a price, a
context window. A release test that pins today's value then blocks the refresh
the day the provider changes it. `values` scales every price in a provider's
manifest rows and price tiers, and in its snapshot endpoints and their price
tiers, by 1.07, and every context and output limit and tier threshold by 0.9, then
runs the release suite like `sweep`; `--down` lowers prices by 0.93 and raises
limits by 1.1, since a test pinning a floor breaks one way and a ceiling the other.
`--skew` moves values apart and far, to break a test that pins a relation between
today's values: input prices x2 and output prices x0.5 with cache prices held,
other prices and every limit x2 (with `--down`, input x0.5, output x2, other
prices and limits x0.5). (The refresh's price-spike gate may hold a real jump
that size; that gate is deliberate and not a test.)

The refresh also holds routes itself. Its step
scripts/pricing/unserved_route_holds.py marks a listed route the provider
refuses to serve routable=false with routable_reason="provider-route-unserved",
unserved_held_at and unserved_hold_count, on any routable row of any provider,
and the shared manifest writers carry the hold forward until that step lifts it.
The row stays listed, so this is not a delisting: a test that reads a committed
row and expects it to route, or lists the states a row may be in, fails on the
refresh's own snapshot. On 2026-11-26 the step held one Cloudflare row
(meta-llama/llama-3.2-11b-vision-instruct) and the refresh could not publish,
because a Cloudflare manifest test allowed every state but that one. `holds`
finds such tests. The step itself places each hold
(scripts/tombstone_sweep_holds.py calls it), so a row carries exactly what the
refresh writes, under the step's own limits: never a provider's last routable
row, never more than half of its rows. Each provider's routable rows are cut
into the fewest runs those limits allow (two, three for an odd count, more
where the step already holds rows), and slice k holds every provider's k-th cut
at once. Once every slice has run, a failing one is halved, over the files that
failed, until each failure is on one row (`rows` in OUT/holds.json) or needs
several rows held together (`needs_several_rows`); the step can hold those
together too, so they are findings, not noise. The step's limit of 25 held rows in total is lifted
for a run that holds more, since no sweep could cover every row inside it; such
a run is marked total_route_cap_lifted, and a single row is always held within
every limit. Only the manifests change, as in that step: the snapshot, and the
files later refresh steps derive from the manifests, stay as committed.
`--files` sweeps those test files instead of the release suite. It resumes from
OUT/holds.json, and only with the same files and routable rows.

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
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path.cwd()
DATA_PATHS = ["src/trusted_router/data/provider_models", "src/trusted_router/data/openrouter_snapshot.json"]
MANIFESTS = ROOT / DATA_PATHS[0]
SNAPSHOT = ROOT / DATA_PATHS[1]
PLUGIN_DIR = Path(__file__).resolve().parent
SESSION_CRASH = "SESSION-CRASH"
TEMP_ROOT = Path(tempfile.gettempdir()) / "tombstone-sweep" / str(os.getpid())


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


class Change(NamedTuple):
    """One value change: a factor per kind of price, and one for every limit."""

    input: Decimal
    output: Decimal
    cache: Decimal
    other: Decimal
    limit: Decimal


# A value change raises prices and shrinks limits, or with --down lowers prices
# and grows limits. --skew moves them apart and far: input and output prices in
# opposite directions with cache prices held, so a relation between today's
# values (a cache discount, an output price above the input price, a window
# under a cap) breaks too.
UP = Change(*[Decimal("1.07")] * 4, limit=Decimal("0.9"))
DOWN = Change(*[Decimal("0.93")] * 4, limit=Decimal("1.1"))
SKEW_UP = Change(input=Decimal(2), output=Decimal("0.5"), cache=Decimal(1), other=Decimal(2), limit=Decimal(2))
SKEW_DOWN = Change(input=Decimal("0.5"), output=Decimal(2), cache=Decimal(1), other=Decimal("0.5"),
                   limit=Decimal("0.5"))
_LIMIT_KEYS = frozenset({"context_length", "max_output_tokens", "max_completion_tokens", "max_prompt_tokens"})


def _is_price_key(key: str) -> bool:
    return key.endswith(("_price_per_m", "_microdollars", "_microdollars_by_resolution"))


def _price_factor(key: str, change: Change) -> Decimal:
    """The factor for a manifest price key or a snapshot pricing key."""
    if "cache" in key:
        return change.cache
    if key.startswith("fixed_"):
        return change.other
    if "input" in key or key == "prompt":
        return change.input
    if "output" in key or key == "completion":
        return change.output
    return change.other


def _scaled(value: Any, factor: Decimal) -> Any:
    """A positive integer times factor, rounded, and changed even when rounding
    would not; a factor of 1 leaves it as it is."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or factor == 1:
        return value
    scaled = int((Decimal(value) * factor).to_integral_value(rounding=ROUND_HALF_UP))
    if scaled == value:
        scaled = value + 1 if factor > 1 else max(1, value - 1)
    return scaled


def _perturb_fields(fields: dict[str, Any], change: Change) -> int:
    changed = 0
    for key, value in list(fields.items()):
        if _is_price_key(key):
            factor = _price_factor(key, change)
            new = {k: _scaled(v, factor) for k, v in value.items()} if isinstance(value, dict) \
                else _scaled(value, factor)
        elif key in _LIMIT_KEYS:
            new = _scaled(value, change.limit)
        else:
            continue
        if new != value:
            fields[key] = new
            changed += 1
    return changed


def _perturb_snapshot_pricing(pricing: dict[str, Any], change: Change) -> int:
    """OpenRouter's decimal-string prices, and each price tier's prices and threshold."""
    changed = 0
    for key, value in list(pricing.items()):
        if isinstance(value, list):
            changed += sum(_perturb_snapshot_pricing(tier, change) for tier in value if isinstance(tier, dict))
            continue
        if key == "max_prompt_tokens":
            new = _scaled(value, change.limit)
        else:
            try:
                amount = Decimal(value) if isinstance(value, str) else None
            except InvalidOperation:
                amount = None
            factor = _price_factor(key, change)
            if amount is None or amount <= 0 or factor == 1:
                continue
            new = f"{(amount * factor).normalize():f}"
        if new != value:
            pricing[key] = new
            changed += 1
    return changed


def perturb(providers: list[str], *, down: bool = False, skew: bool = False) -> dict[str, int]:
    """Change each provider's prices and limits, in its manifest rows and price
    tiers and in its snapshot endpoints and their price tiers: prices x1.07 and
    limits x0.9, or with down, prices x0.93 and limits x1.1; skew moves them
    apart and far (SKEW_UP, SKEW_DOWN)."""
    change = (SKEW_DOWN if down else SKEW_UP) if skew else (DOWN if down else UP)
    counts = {"rows": 0, "endpoints": 0, "fields": 0}
    for provider in providers:
        path = MANIFESTS / f"{provider}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for row in raw.get("models", []):
            if not isinstance(row, dict):
                continue
            changed = _perturb_fields(row, change)
            for tier in row.get("price_tiers") or []:
                if isinstance(tier, dict):
                    changed += _perturb_fields(tier, change)
            if changed:
                counts["rows"] += 1
                counts["fields"] += changed
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    chosen = set(providers)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for model in snapshot.get("models", []):
        for endpoint in model.get("endpoints") or []:
            if endpoint.get("tr_provider_slug") not in chosen:
                continue
            changed = _perturb_snapshot_pricing(endpoint.get("pricing") or {}, change)
            for key in ("context_length", "max_completion_tokens", "max_prompt_tokens"):
                new = _scaled(endpoint.get(key), change.limit)
                if new != endpoint.get(key):
                    endpoint[key] = new
                    changed += 1
            if changed:
                counts["endpoints"] += 1
                counts["fields"] += changed
    SNAPSHOT.write_text(json.dumps(snapshot), encoding="utf-8")
    return counts


def _holds_step(command: str, **request: Any) -> dict[str, Any]:
    """Ask scripts/tombstone_sweep_holds.py, under the worktree's interpreter:
    it plans and places holds with the refresh's own step."""
    proc = subprocess.run(  # noqa: S603 - fixed argv
        ["uv", "run", "python", str(PLUGIN_DIR / "tombstone_sweep_holds.py"), command],  # noqa: S607 - uv from PATH
        cwd=ROOT, env=dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT}"),
        input=json.dumps({"manifest_dir": str(MANIFESTS), **request}),
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"tombstone_sweep_holds.py {command} failed: {(proc.stderr or proc.stdout).strip()[-2000:]}")
    result: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


def plan_holds() -> dict[str, Any]:
    """The slices of routable rows to hold together, and the rows the step never holds."""
    return _holds_step("plan")


def hold_routes(routes: list[list[str]]) -> dict[str, Any]:
    """Hold these [provider, model] rows as the refresh's unserved-route step does."""
    return _holds_step("hold", routes=routes)


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
        # Every pytest session of a user shares one temp root, and each deletes
        # the oldest trees there at start: under load, one session spent 24
        # minutes deleting another's multi-gigabyte deploy-harness tree before
        # running a test. This sweep's sessions clean up only their own.
        PYTEST_DEBUG_TEMPROOT=str(TEMP_ROOT),
    )
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
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


def sweep(
    out: Path, group_size: int, workers: int, *, values: bool = False, thorough: bool = False,
    down: bool = False, skew: bool = False,
) -> None:
    """Change each group of providers' data, then each provider of a failing group
    alone: delist everything, or (values=True) change every price and limit.

    A value change runs first for every provider at once, and the groups then run
    only the files that failed then: two full-suite runs instead of one per group.
    That can miss a test that only fails when providers' values move apart (a
    pinned "cheapest route"); `thorough` runs every group over the full suite.
    A values state resumes only in the mode it was written in."""
    alter = (lambda group: perturb(group, down=down, skew=skew)) if values else delist
    event = "value change" if values else "delisting"
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / ("values.json" if values else "state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if values:
        mode = {"down": down, "skew": skew, "thorough": thorough}
        if state and state.get("mode") != mode:
            raise SystemExit(f"{state_path}: written in mode {state.get('mode')}, not {mode}; sweep into a new OUT")
        state["mode"] = mode
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

    group_files: list[str] = []  # the full suite
    if values and not thorough:
        if "all" not in state:
            try:
                counts = alter(providers)
                failures, summary = run_pytest([], workers, out / "all.log")
            finally:
                restore()
            state["all"] = {"counts": counts, "summary": summary, "failures": sorted(failures)}
            save()
            log(f"every provider at once: {summary}")
        suspects = state["all"]["failures"]
        if not suspects:
            log("done: no release test fails with every provider's values changed")
            return
        if SESSION_CRASH not in suspects:
            group_files = sorted({test.split("::")[0] for test in suspects})

    state.setdefault("groups", {})
    for index in range(0, len(providers), group_size):
        key = f"group{index // group_size:02d}"
        if key in state["groups"]:
            continue
        group = providers[index:index + group_size]
        try:
            counts = alter(group)
            failures, summary = run_pytest(group_files, workers, out / f"{key}.log")
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
                counts = alter([provider])
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
    log(f"done: {len(attributed)} tests fail on a single provider's {event}; "
        f"{len(state['needs_several_providers'])} only when several providers' change together")


# Printed by the worktree's own catalog: every model some catalog model depends on
# (the chat model behind a private name, a member of a meta model or preset),
# with the ids that depend on it.
_DEPENDENTS_SCRIPT = """
import json
from collections import defaultdict
from trusted_router import catalog
dependents = defaultdict(set)
for name, backing in catalog.PRIVATE_PROXY_MODEL_TARGETS.items():
    dependents[backing].add(name)
for meta in catalog.META_MODEL_IDS:
    for member in catalog.meta_candidate_models(meta):
        dependents[member.id].add(meta)
print(json.dumps({model: sorted(names) for model, names in dependents.items()}))
"""


def data_model_ids() -> set[str]:
    """Every model id the data files carry. The refresh removes a model only through them."""
    ids = {model["id"] for model in json.loads(SNAPSHOT.read_text(encoding="utf-8")).get("models", [])
           if isinstance(model.get("id"), str)}
    for path in MANIFESTS.glob("*.json"):
        ids |= {row["id"] for row in json.loads(path.read_text(encoding="utf-8")).get("models", [])
                if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return ids


def catalog_dependents() -> dict[str, list[str]]:
    proc = subprocess.run(  # noqa: S603 - fixed argv
        ["uv", "run", "python", "-c", _DEPENDENTS_SCRIPT],  # noqa: S607 - uv from PATH
        cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def files_naming(ids: set[str]) -> dict[str, set[str]]:
    """Which test files name each id in a string literal: the id itself, an
    endpoint id starting with it, or a page path containing it."""
    pattern = re.compile(
        r"(?<![\w.-])(" + "|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True)) + r")(?![\w.-])"
    )
    named: dict[str, set[str]] = defaultdict(set)
    # The release suite's own files. tests/conformance is a separate suite with
    # its own collection hooks, which reject the node ids of an xdist loadgroup run.
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for model_id in pattern.findall(node.value):
                    named[model_id].add(str(path.relative_to(ROOT)))
    return named


def models_to_sweep() -> dict[str, list[str]]:
    """Each data model a test names, directly or through a dependent, with the files to run."""
    dependents = catalog_dependents()
    ids = data_model_ids()
    named = files_naming(ids | {name for names in dependents.values() for name in names})
    files: dict[str, list[str]] = {}
    for model_id in ids:
        wanted = set(named.get(model_id, ()))
        for dependent in dependents.get(model_id, ()):
            wanted |= named.get(dependent, set())
        if wanted:
            files[model_id] = sorted(wanted)
    return files


def delist_model(model_id: str) -> dict[str, int]:
    """Tombstone the model's rows in every manifest and drop it from the snapshot:
    what the refresh leaves once the last provider serving it has delisted it."""
    counts = {"rows": 0, "snapshot_models": 0}
    for path in MANIFESTS.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = [row for row in raw.get("models", [])
                if isinstance(row, dict) and row.get("id") == model_id and row.get("routable") is not False]
        for row in rows:
            row.update(routable=False, routable_reason="delisted-upstream")
            row.setdefault("missing_since", time.strftime("%Y-%m-%d"))
        if rows:
            counts["rows"] += len(rows)
            path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    kept = [model for model in snapshot.get("models", []) if model.get("id") != model_id]
    counts["snapshot_models"] = len(snapshot.get("models", [])) - len(kept)
    if counts["snapshot_models"]:
        snapshot["models"] = kept
        SNAPSHOT.write_text(json.dumps(snapshot), encoding="utf-8")
    return counts


def models(out: Path, workers: int, only: list[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(exist_ok=True)
    state_path = out / "models.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    def save() -> None:
        state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    def log(message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    restore()
    to_sweep = models_to_sweep()
    if only:
        to_sweep = {model_id: files for model_id, files in to_sweep.items() if model_id in only}
    union = sorted({file for files in to_sweep.values() for file in files})
    # Every failure below is that model's only if these files pass without it.
    if "baseline" not in state:
        failures, summary = run_pytest(union, workers, out / "baseline.log")
        if failures:
            raise SystemExit(f"these test files must pass before a sweep: {summary}; see {out / 'baseline.log'}")
        state["baseline"] = {"files": union, "summary": summary}
        save()
        log(f"baseline over {len(union)} files: {summary}")
    elif not set(union) <= set(state["baseline"]["files"]):
        missing = sorted(set(union) - set(state["baseline"]["files"]))
        raise SystemExit(f"{state_path}: its baseline did not run {missing}; sweep this selection into a new OUT")
    state.setdefault("models", {})
    for model_id, files in sorted(to_sweep.items(), key=lambda item: (-len(item[1]), item[0])):
        if model_id in state["models"]:
            continue
        try:
            counts = delist_model(model_id)
            if not any(counts.values()):
                result = {"files": files, "counts": counts, "summary": "not served: nothing to delist",
                          "failures": []}
            else:
                failures, summary = run_pytest(
                    files, max(1, min(workers, len(files))),
                    out / "logs" / (model_id.replace("/", "__") + ".log"),
                )
                result = {"files": files, "counts": counts, "summary": summary, "failures": sorted(failures)}
        finally:
            restore()
        state["models"][model_id] = result
        save()
        log(f"{model_id} ({len(files)} files): {result['summary']}")
    failing = sorted(model_id for model_id, result in state["models"].items() if result["failures"])
    log(f"done: {len(failing)} of {len(state['models'])} models break a release test when every host delists them")


def holds(out: Path, workers: int, files: list[str]) -> None:
    """Hold each slice of routable rows as the refresh's unserved-route step
    does, then halve a failing slice until each failure is on one row.

    A half runs only the files that failed with the whole held. A failure that
    neither half reproduces needs rows from both, and is reported as such."""
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "holds.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    files = sorted(files)

    def save() -> None:
        state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    def log(message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    restore()
    plan = plan_holds()
    # A saved run is the rows this sweep would hold under its key, over the
    # files it would run; anything else would report rows that were never held.
    if state and (state.get("files") != files or state.get("slices") != plan["slices"]):
        raise SystemExit(
            f"{state_path}: written for other test files, or the routable rows have changed since "
            "(a hold placed or lifted, a new manifest); sweep into a new OUT"
        )
    state.update(files=files, slices=plan["slices"], never_held=plan["never_held"])
    # Every failure under a hold is that hold's only if these files pass without one.
    if "baseline" not in state:
        failures, summary = run_pytest(files, workers, out / "baseline.log")
        if failures:
            raise SystemExit(f"the release suite must pass before a sweep: {summary}; see {out / 'baseline.log'}")
        state["baseline"] = {"failures": [], "summary": summary}
        save()
        log(f"baseline: {summary}")
    elif state["baseline"]["failures"]:
        raise SystemExit(f"{state_path}: its baseline did not pass ({state['baseline']['summary']}); sweep into a new OUT")
    state.setdefault("runs", {})

    def held_run(key: str, routes: list[list[str]], run_files: list[str]) -> list[str]:
        if key not in state["runs"]:
            try:
                held = hold_routes(routes)
                failures, summary = run_pytest(
                    run_files, max(1, min(workers, len(run_files))) if run_files else workers,
                    out / f"{key}.log",
                )
            finally:
                restore()
            state["runs"][key] = {
                "routes": routes, "total_route_cap_lifted": held["total_route_cap_lifted"],
                "files": run_files, "summary": summary, "failures": sorted(failures),
            }
            save()
            held_rows = f"{routes[0][0]}:{routes[0][1]}" if len(routes) == 1 else f"{len(routes)} rows"
            log(f"{key} ({held_rows}): {summary}")
        failed: list[str] = state["runs"][key]["failures"]
        return failed

    def narrow(key: str, routes: list[list[str]], failures: list[str]) -> None:
        if not failures or len(routes) == 1:
            return
        # A crash names no file: the halves run everything the slice ran.
        run_files = files if SESSION_CRASH in failures else sorted({test.split("::")[0] for test in failures})
        middle = len(routes) // 2
        for half_key, half in ((f"{key}a", routes[:middle]), (f"{key}b", routes[middle:])):
            narrow(half_key, half, held_run(half_key, half, run_files))

    # Every slice first: which tests fail under a hold is known after one run
    # per slice, and the halving, which is most of a sweep, can be left to resume.
    whole = [(f"slice{index}", routes) for index, routes in enumerate(plan["slices"])]
    for key, routes in whole:
        held_run(key, routes, files)
    for key, routes in whole:
        narrow(key, routes, held_run(key, routes, files))

    state["rows"] = {
        f"{run['routes'][0][0]}:{run['routes'][0][1]}": run["failures"]
        for run in state["runs"].values() if len(run["routes"]) == 1 and run["failures"]
    }
    attributed = {test for failures in state["rows"].values() for test in failures}
    in_a_slice = {
        test for index in range(len(plan["slices"])) for test in state["runs"][f"slice{index}"]["failures"]
    }
    state["needs_several_rows"] = sorted(in_a_slice - attributed)
    save()
    log(f"done: {len(attributed)} tests fail with one row held ({len(state['rows'])} rows); "
        f"{len(state['needs_several_rows'])} only with several rows held together; "
        f"{len(plan['never_held'])} rows the step never holds")


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
    run_parser.add_argument("--vanish", default="", help="comma-separated model ids to remove from every host")
    run_parser.add_argument("--perturb", action="store_true", help="change prices and limits, not delist")
    run_parser.add_argument("--down", action="store_true", help="with --perturb, lower prices and raise limits")
    run_parser.add_argument("--skew", action="store_true", help="with --perturb, move prices and limits apart and far")
    run_parser.add_argument("--hold", default="",
                            help="comma-separated provider:model rows to hold as the refresh's unserved-route step does")
    values_parser = commands.add_parser("values")
    values_parser.add_argument("out", type=Path)
    values_parser.add_argument("--group-size", type=int, default=8)
    values_parser.add_argument("--workers", type=int, default=8)
    values_parser.add_argument("--thorough", action="store_true",
                               help="run every group over the full suite, not just the files that failed at once")
    values_parser.add_argument("--down", action="store_true", help="lower prices and raise limits")
    values_parser.add_argument("--skew", action="store_true", help="move prices and limits apart and far")
    models_parser = commands.add_parser("models")
    models_parser.add_argument("out", type=Path)
    models_parser.add_argument("--workers", type=int, default=6)
    models_parser.add_argument("--only", nargs="*", default=[], help="sweep just these model ids")
    holds_parser = commands.add_parser("holds")
    holds_parser.add_argument("out", type=Path)
    holds_parser.add_argument("--workers", type=int, default=8)
    holds_parser.add_argument("--files", nargs="*", default=[], help="sweep these test files, not the release suite")
    args = parser.parse_args()
    if not (ROOT / DATA_PATHS[1]).exists():
        raise SystemExit("run from the repository root of a disposable worktree")
    if args.command == "sweep":
        sweep(args.out, args.group_size, args.workers)
        return
    if args.command == "models":
        models(args.out, args.workers, args.only)
        return
    if args.command == "values":
        sweep(args.out, args.group_size, args.workers, values=True, thorough=args.thorough, down=args.down,
              skew=args.skew)
        return
    if args.command == "holds":
        holds(args.out, args.workers, args.files)
        return
    providers = [] if args.providers == "-" else args.providers.split(",")
    restore()
    try:
        if providers:
            print("perturbed:" if args.perturb else "delisted:", providers,
                  perturb(providers, down=args.down, skew=args.skew) if args.perturb else delist(providers))
        for model_id in filter(None, args.vanish.split(",")):
            print("vanished:", model_id, delist_model(model_id))
        if args.hold:
            print("held:", hold_routes([route.split(":", 1) for route in args.hold.split(",")])["notes"])
        failures, summary = run_pytest(args.files, args.workers, Path(tempfile.mkstemp(suffix=".log")[1]))
    finally:
        restore()
    print(summary)
    for failure in sorted(failures):
        print("FAILED", failure)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
