"""Quarantine a provider whose fresh data fails the snapshot's exact-SHA CI.

The hourly refresh (.github/workflows/refresh-prices.yml) publishes a catalog
snapshot only after a successful ci.yml run of that exact commit. One
provider's bad data (a listing that breaks a catalog contract test) used to
fail that run and so block every provider's price update until a person
fixed it. This module isolates such a provider instead:

1. ``diagnose`` runs in the failed refresh job. It reads the failing CI run's
   per-shard failure records (scripts/ci/failure_record.py), reproduces the
   failing tests locally, and searches for the smallest set of changed
   providers whose committed state, restored exactly as a failed refresh
   restores it (scripts/pricing/refresh.py), makes those tests pass. It
   quarantines that set only when the attribution is clean: the tests fail on
   the snapshot as published, pass with every changed provider restored, and
   pass with only the chosen providers restored. Anything else (a flaky or
   environment-dependent test, a failure no provider explains, a collection
   error, too many culprits, an exhausted probe budget) quarantines nothing
   and the failure stays as loud as before.
2. The next refresh (``TR_PRICE_QUARANTINE``, read by refresh.py) treats each
   active quarantined provider exactly like a provider whose fetch failed: it
   keeps the provider's committed manifest, parser and snapshot endpoints and
   lists it under "Held for price review", which opens the held-price issue.
   That snapshot is new, so it goes through the complete exact-SHA CI gate
   like any other. Nothing here publishes anything.

A quarantine lasts QUARANTINE_HOURS, then the provider is refreshed again; if
its data still fails CI, the next failed run quarantines it again (and the
refresh-failed alert fires again). The state travels between runs as the
``price-quarantine`` workflow artifact, which only workflow runs can write.

Usage:
  python -m scripts.pricing.quarantine active STATE
  python -m scripts.pricing.quarantine prune STATE OUT
  python -m scripts.pricing.quarantine diagnose --baseline DIR --failures DIR \
      --state STATE --out STATE --ci-run ID
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
QUARANTINE_HOURS = 12
# A failure that needs more than this many providers restored is not one bad
# provider: quarantining a large part of the catalog would publish a degraded
# tree, so it stays a loud failure instead.
MAX_CULPRITS = 3
# Each probe is one local pytest run of the failing tests (~20-60 s).
MAX_PROBES = 40
PROBE_TIMEOUT_SECONDS = 600
# Environment the local probes keep. The refresh job's environment holds
# every provider credential; the probes need none of it.
PROBE_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR", "VIRTUAL_ENV")
PROBE_MARKERS = "not provider_health and not proof_oracle"


# --- quarantine state -------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    slug: str
    reason: str
    until: datetime

    def as_json(self) -> dict[str, str]:
        return {"slug": self.slug, "reason": self.reason, "until": self.until.isoformat()}


def load_state(path: Path) -> list[Entry]:
    """Entries from a state file; a missing or unreadable file is no state."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries: list[Entry] = []
    for item in raw.get("quarantined", []) if isinstance(raw, dict) else []:
        try:
            until = datetime.fromisoformat(item["until"])
            if until.tzinfo is None:
                continue
            entries.append(Entry(str(item["slug"]), str(item["reason"]), until))
        except (KeyError, TypeError, ValueError):
            continue
    return entries


def active(entries: Iterable[Entry], now: datetime) -> list[Entry]:
    """Unexpired entries, the latest per provider, sorted by slug."""
    latest: dict[str, Entry] = {}
    for entry in entries:
        if entry.until > now and (entry.slug not in latest or entry.until > latest[entry.slug].until):
            latest[entry.slug] = entry
    return [latest[slug] for slug in sorted(latest)]


def dump_state(path: Path, entries: Iterable[Entry]) -> None:
    path.write_text(json.dumps({"quarantined": [e.as_json() for e in entries]}, indent=2) + "\n",
                    encoding="utf-8")


def quarantined_providers(now: datetime | None = None) -> dict[str, str]:
    """Active quarantine named by TR_PRICE_QUARANTINE: slug -> reason."""
    path = os.environ.get("TR_PRICE_QUARANTINE", "")
    if not path:
        return {}
    return {e.slug: e.reason for e in active(load_state(Path(path)), now or datetime.now(UTC))}


# --- culprit search ---------------------------------------------------------

@dataclass
class Diagnosis:
    status: str  # culprits | not_reproduced | not_provider_data | too_many | budget | no_tests
    culprits: list[str] = field(default_factory=list)
    probes: int = 0


class _Budget(Exception):
    pass


def find_culprits(
    units: list[str], fails: Callable[[frozenset[str]], bool], *,
    max_probes: int = MAX_PROBES, max_culprits: int = MAX_CULPRITS,
) -> Diagnosis:
    """Smallest-found set of units whose restoration makes the tests pass.

    ``fails(held)`` runs the failing tests with ``held`` restored and returns
    whether they still fail. The search assumes that restoring more units
    never breaks a passing run (each restored unit is back at a state that
    passed CI); the caller's exact-SHA CI of the next snapshot is the proof.
    Each culprit is found by binary search over a prefix of the remaining
    units: the smallest prefix whose restoration passes ends in a unit that
    is needed.
    """
    cache: dict[frozenset[str], bool] = {}

    def probe(held: Iterable[str]) -> bool:
        key = frozenset(held)
        if key not in cache:
            if len(cache) >= max_probes:
                raise _Budget
            cache[key] = fails(key)
        return cache[key]

    try:
        if not probe(()):
            return Diagnosis("not_reproduced", probes=len(cache))
        if not units or probe(units):
            return Diagnosis("not_provider_data", probes=len(cache))
        chosen: list[str] = []
        remaining = list(units)
        # Invariant: probe(chosen + remaining) passes.
        while probe(chosen):
            if len(chosen) >= max_culprits:
                return Diagnosis("too_many", sorted(chosen), len(cache))
            low, high = 0, len(remaining)  # fails at low, passes at high
            while high - low > 1:
                mid = (low + high) // 2
                if probe(chosen + remaining[:mid]):
                    low = mid
                else:
                    high = mid
            chosen.append(remaining[high - 1])
            remaining = remaining[: high - 1]
        return Diagnosis("culprits", sorted(chosen), len(cache))
    except _Budget:
        return Diagnosis("budget", probes=len(cache))


# --- the repository side ----------------------------------------------------

def failed_tests(failures_dir: Path) -> tuple[list[str], list[str]]:
    """(failed node IDs, collection errors) over every shard's record."""
    failed: set[str] = set()
    collection: set[str] = set()
    for record in sorted(failures_dir.rglob("failures.json")):
        try:
            body = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        failed.update(str(test) for test in body.get("failed", []))
        collection.update(str(item) for item in body.get("collection_errors", []))
    return sorted(failed), sorted(collection)


def _endpoint_rows(snapshot: dict[str, Any], slug_of: Callable[[str], str]) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for model in snapshot.get("models") or []:
        if not isinstance(model, dict):
            continue
        for endpoint in model.get("endpoints") or []:
            provider = endpoint.get("tr_provider_slug") if isinstance(endpoint, dict) else None
            if isinstance(provider, str):
                rows.setdefault(slug_of(provider), []).append(
                    json.dumps([model.get("id"), endpoint], sort_keys=True))
    return {slug: sorted(values) for slug, values in rows.items()}


def changed_providers(baseline: Path) -> list[str]:
    """Refreshed providers whose manifest, parser or snapshot endpoints changed."""
    from scripts.pricing import refresh

    changed: set[str] = set()
    for slug in refresh.PROVIDER_SLUGS:
        manifest = getattr(refresh._import_provider(slug), "MANIFEST_PATH", None)
        files = [refresh.PARSERS_DIR / refresh.parser_path(slug).name]
        if manifest is not None:
            files.append(refresh.PROVIDER_MANIFEST_DIR / Path(manifest).name)
        for path in files:
            published = baseline / path.parent.name / path.name
            now = path.read_bytes() if path.exists() else None
            before = published.read_bytes() if published.exists() else None
            if now != before:
                changed.add(slug)
    current = json.loads(refresh.SNAPSHOT_PATH.read_text(encoding="utf-8"))
    published = json.loads((baseline / refresh.SNAPSHOT_PATH.name).read_text(encoding="utf-8"))
    now_rows = _endpoint_rows(current, refresh._result_slug_for_provider)
    before_rows = _endpoint_rows(published, refresh._result_slug_for_provider)
    for slug in set(now_rows) | set(before_rows):
        if slug in refresh.PROVIDER_SLUGS and now_rows.get(slug) != before_rows.get(slug):
            changed.add(slug)
    return sorted(changed)


def restore_providers(baseline: Path, slugs: Iterable[str]) -> None:
    """Put the providers back as a failed refresh keeps them (refresh.py)."""
    from scripts.pricing import refresh

    held = {slug: ["quarantine probe"] for slug in slugs}
    if not held:
        return
    for slug in held:
        refresh._restore_published_files(baseline, slug)
    merged = json.loads(refresh.SNAPSHOT_PATH.read_text(encoding="utf-8"))
    published = json.loads((baseline / refresh.SNAPSHOT_PATH.name).read_text(encoding="utf-8"))
    disabled = refresh._published_disabled_held_routes(baseline, published, held)
    refresh._keep_failed_snapshot_routes(merged, published, held, disabled_routes=disabled)
    refresh._write_snapshot(merged)
    if refresh._held_routes_changed(baseline, held):
        published = json.loads((baseline / refresh.SNAPSHOT_PATH.name).read_text(encoding="utf-8"))
        refresh._hold_inexact_snapshot_models(
            merged, published, held, refresh._published_disabled_held_routes(baseline, published, held))
        refresh._write_snapshot(merged)


def _run_tests(tests: list[str]) -> bool:
    """Whether the tests fail on the working tree, run offline without secrets."""
    env = {name: os.environ[name] for name in PROBE_ENV_KEEP if name in os.environ}
    env["TR_STORAGE_BACKEND"] = "memory"
    command = ["uv", "run", "--frozen", "pytest", "-q", "-p", "no:cacheprovider",
               "-m", PROBE_MARKERS, "--", *tests]
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,  # noqa: S603
                                timeout=PROBE_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return True
    return result.returncode != 0


def diagnose(baseline: Path, tests: list[str],
             run_tests: Callable[[list[str]], bool] = _run_tests) -> Diagnosis:
    """Find the providers to quarantine; leaves the working tree as it found it."""
    from scripts.pricing import refresh

    if not tests:
        return Diagnosis("no_tests")
    current = Path(tempfile.mkdtemp(prefix="quarantine-current-"))
    try:
        shutil.copyfile(refresh.SNAPSHOT_PATH, current / refresh.SNAPSHOT_PATH.name)
        shutil.copytree(refresh.PROVIDER_MANIFEST_DIR, current / refresh.PROVIDER_MANIFEST_DIR.name)
        shutil.copytree(refresh.PARSERS_DIR, current / refresh.PARSERS_DIR.name)
        units = changed_providers(baseline)

        def fails(held: frozenset[str]) -> bool:
            refresh._restore_all_published_files(current)
            restore_providers(baseline, sorted(held))
            outcome = run_tests(tests)
            print(f"  probe restoring {sorted(held) if len(held) < 6 else f'{len(held)} providers'}: "
                  f"{'fails' if outcome else 'passes'}", flush=True)
            return outcome

        return find_culprits(units, fails)
    finally:
        refresh._restore_all_published_files(current)
        shutil.rmtree(current, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("active", help="print the active quarantine, one 'slug: reason' per line")
    show.add_argument("state", type=Path)
    prune = sub.add_parser("prune", help="write only the active entries of a state, for the next run")
    prune.add_argument("state", type=Path)
    prune.add_argument("out", type=Path)
    run = sub.add_parser("diagnose", help="quarantine the providers that failed snapshot CI")
    run.add_argument("--baseline", type=Path, required=True,
                     help="directory with the published snapshot, provider_models/ and parsers/")
    run.add_argument("--failures", type=Path, required=True, help="downloaded test-failures-* records")
    run.add_argument("--state", type=Path, required=True, help="this run's loaded quarantine state")
    run.add_argument("--out", type=Path, required=True, help="state to save for the next run")
    run.add_argument("--ci-run", required=True)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)

    if args.command == "active":
        for entry in active(load_state(args.state), now):
            print(f"{entry.slug}: {entry.reason} (until {entry.until.isoformat()})")
        return 0
    if args.command == "prune":
        dump_state(args.out, active(load_state(args.state), now))
        return 0

    tests, collection_errors = failed_tests(args.failures)
    if collection_errors:
        print(f"Collection errors {collection_errors[:5]}: not attributable to provider data; nothing quarantined.")
        return 3
    print(f"Reproducing {len(tests)} failed test(s) from ci run {args.ci_run}:")
    for test in tests[:20]:
        print(f"  {test}")
    result = diagnose(args.baseline, tests)
    print(f"Diagnosis: {result.status} after {result.probes} probe(s)")
    if result.status != "culprits":
        print("Nothing quarantined; the snapshot CI failure stands.")
        return 3
    reason = (f"its fresh data failed snapshot CI run {args.ci_run} "
              f"({', '.join(tests[:3])}{', ...' if len(tests) > 3 else ''})")
    until = now + timedelta(hours=QUARANTINE_HOURS)
    entries = active([*load_state(args.state), *(Entry(slug, reason, until) for slug in result.culprits)], now)
    dump_state(args.out, entries)
    for slug in result.culprits:
        print(f"::error title=Provider quarantined from price refresh::{slug}: {reason}. "
              f"The next refreshes keep its committed prices until {until.isoformat()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
