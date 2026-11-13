#!/usr/bin/env python3
"""Per-region post-deploy watchdog.

Polls https://trustedrouter.com/status.json every minute and decides,
per `target_region`, whether to roll that region back. Writes the list
of regions-to-rollback to `$GITHUB_OUTPUT` (key `rollback_regions`,
comma-separated) so the workflow's rollback step can target only the
failing regions.

Per-region status, `--slo-class router_core` (the control-plane canary):
only probes that pass through the control plane count: authorize, settle
and fallback. The synthetic monitor sends them to https://trustedrouter.com,
and each is attributed to the control-plane region whose Cloud Run serves the
path it hit (its `target_url`, whose host must be one the load balancer
serves), following the load balancer's routes:
  * `/v1/internal/gateway/authorize` goes to the multi-region control
    backend, which serves a monitor from its own region: `monitor_region`.
  * `/v1/internal/gateway/settle`, `/heartbeat` and `/refund` go to the
    terminal backend, whose only NEG is in us-central1: so every monitor's
    settle is us-central1's evidence. The route and hosts are read from the
    live URL map (`gcloud compute url-maps describe`), falling back with a
    warning to infra/; if the two disagree, settle is inconclusive.
Only fresh samples count: a row whose effective status differs from what it
recorded, or that is older than CURRENT_SAMPLE_TTL_SECONDS, is a dead or
late monitor, not evidence. A settle row counts only while the same
monitor's authorize row is fresh and up: when a monitor's authorize fails it
writes no settle, and its last settle row must not be blamed on the region
serving settle. A sample that depends on two stages served by different
regions counts for neither. A check that names a `target_region` is
attributed to it.
The enclave-only probes (`tls_health`, `attestation_nonce`) never call the
control plane, so for this verdict they are inconclusive, never failing: a
gateway drain must not roll back a control-plane release, and must not blind
its baseline. Nor is the published `router_core` SLO aggregate used, because
it blends those enclave probes in. A region with no attributable probe reads
`unknown`, which does not block (as before) but is announced with a warning
annotation. The synthetic monitors sample every three minutes and this
script does not require a sample to be newer than a traffic shift, so a
short watch can read a sample from before the shift.

`--slo-class control_plane` keeps the original reading: the SLO class's
`current_by_region`, falling back per missing region to `current.checks[]`.

Logic:
  - Poll once per minute for `--duration-min` minutes (default 10).
  - For each region, track a consecutive-down counter.
  - When ANY region reads "down" for `--rollback-after` consecutive
    minutes (default 3), that region is added to the rollback set,
    its counter resets, and the watchdog continues monitoring the
    other regions.
  - Exit 1 if at least one region is in the rollback set, else 0.

Why "down" and "trust_degraded" (not "degraded"): synthetics naturally
flap during a rolling update, so plain degraded is normal churn. But
trust_degraded means the attestation no longer verifies — for an
attested gateway that is the product being broken, not churn, so it
counts toward rollback exactly like down.

Why per-region: a deploy can break us-central1 but leave
europe-west4 healthy (e.g., bad regional secret rotation). Rolling
back globally over a single-region failure is overkill.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

# This script also runs directly from a checkout during deploys.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from trusted_router.enclave_regions import ENCLAVE_REGIONS  # noqa: E402

# Worst-of: the per-region effective_status is the worst over its checks.
# `unknown` is treated as severity 0 for ranking but reported separately
# so the operator can tell "no signal" apart from "all healthy."
# trust_degraded ranks with down, not with degraded. For an ATTESTED
# gateway, "cannot prove what code is running" is the product failing —
# a deploy that ships an enclave whose attestation no longer verifies
# must roll back. Previously it folded into "degraded", and rollback
# fires only on "down", so an attestation regression could never
# trigger one.
SEVERITY = {"up": 0, "degraded": 1, "trust_degraded": 2, "down": 2}
# Statuses that count toward the consecutive-failure rollback counter.
ROLLBACK_STATUSES = frozenset({"down", "trust_degraded"})
# Keep the legacy payload fallback scoped just like the SLO-aware path.
# This script runs with stdlib Python in CI, without application dependencies.
SLO_PROBES = {
    "router_core": frozenset(
        {
            "tls_health",
            "attestation_nonce",
            "gateway_authorize",
            "gateway_settle",
            "gateway_authorize_settle",
            "provider_fallback",
        }
    ),
    "control_plane": frozenset({"control_plane_health"}),
}
# router_core probes that never reach the control plane: the enclave answers
# /health and /attestation itself. Inconclusive for a control-plane verdict.
GATEWAY_ONLY_PROBES = frozenset({"tls_health", "attestation_nonce"})
# router_core probes that pass through the control plane being canaried.
CONTROL_PLANE_ROUTER_CORE_PROBES = SLO_PROBES["router_core"] - GATEWAY_ONLY_PROBES
CONTROL_PLANE_TARGET = "control-plane"
REPO_ROOT = Path(__file__).resolve().parents[2]
TERMINAL_BACKEND_TF = REPO_ROOT / "infra" / "control_terminal_backend.tf"
# The terminal paths the load balancer sends to that backend (with or without
# the /v1 prefix); tests pin this list to infra/control_lb_url_map.tf.
TERMINAL_PATHS = ("/internal/gateway/settle", "/internal/gateway/heartbeat", "/internal/gateway/refund")
AUTHORIZE_PATH = "/internal/gateway/authorize"
# Stages a sample depends on when its target_url does not say.
DEFAULT_STAGES = {
    "gateway_authorize": frozenset({"authorize"}),
    "gateway_settle": frozenset({"settle"}),
    "gateway_authorize_settle": frozenset({"authorize", "settle"}),
    "provider_fallback": frozenset({"authorize", "settle"}),
}
UNREADABLE = "unreadable"
URL_MAP_NAME = "trusted-router-control-map"
SURFACE_MATCHER = "trusted-router-service-surfaces"
CONTROL_LB_URL_MAP_TF = REPO_ROOT / "infra" / "control_lb_url_map.tf"
# status.py: two monitor cadences (180 s) plus a minute.
CURRENT_SAMPLE_TTL_SECONDS = 2 * 180 + 60
_warned_unknown: set[str] = set()
_routes_cache: list[Routes] = []


class Routes(NamedTuple):
    """Where the load balancer sends the control-plane probe paths."""

    terminal: str | None  # region serving settle; None = served by the monitor's region
    hosts: frozenset[str]  # hosts whose requests follow these routes


def terraform_lb_hosts(path: Path = CONTROL_LB_URL_MAP_TF) -> frozenset[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    match = re.search(r"hosts\s*=\s*\[([^\]]*)\]\s*path_matcher\s*=\s*\"" + SURFACE_MATCHER + "\"", text)
    return frozenset(re.findall(r'"([a-z0-9.-]+)"', match[1])) if match else frozenset()


def live_routes(run: Any = subprocess.run, project: str | None = None) -> Routes:
    """Read the terminal route and its hosts from the live URL map."""
    project = project or os.environ.get("PROJECT_ID") or "quill-cloud-proxy"

    def describe(*args: str) -> dict:
        result = run(["gcloud", "compute", *args, "--global", f"--project={project}", "--format=json"],
                     capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"gcloud compute {args[0]} describe failed: {result.stderr.strip()[:200]}")
        return json.loads(result.stdout)

    url_map = describe("url-maps", "describe", URL_MAP_NAME)
    matcher = next(m for m in url_map["pathMatchers"] if m["name"] == SURFACE_MATCHER)
    hosts = frozenset(h for rule in url_map.get("hostRules", []) if rule.get("pathMatcher") == SURFACE_MATCHER
                      for h in rule.get("hosts", []))
    rule = next((r for r in matcher.get("pathRules", [])
                 if f"/v1{TERMINAL_PATHS[0]}" in r.get("paths", [])), None)
    if rule is None or not rule.get("service"):
        return Routes(None, hosts)
    backend = describe("backend-services", "describe", rule["service"].rsplit("/", 1)[1])
    regions = {re.search(r"/regions/([a-z0-9-]+)/", b.get("group", "")).group(1)
               for b in backend.get("backends", []) if re.search(r"/regions/([a-z0-9-]+)/", b.get("group", ""))}
    # A multi-region backend serves each monitor from its own region.
    return Routes(regions.pop() if len(regions) == 1 else None, hosts)


def load_routes(run: Any = subprocess.run) -> Routes:
    """Live URL map, cross-checked against infra/; cached for the process."""
    if _routes_cache:
        return _routes_cache[0]
    tf_terminal, tf_hosts = terminal_route_region(), terraform_lb_hosts()
    if os.environ.get("TR_WATCHDOG_LIVE_ROUTES") == "0":
        routes = Routes(tf_terminal, tf_hosts)
    else:
        try:
            live = live_routes(run)
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the reviewed Terraform
            print(f"::warning::watchdog: live URL map unreadable ({type(exc).__name__}: {exc}); "
                  "using the routes in infra/", flush=True)
            routes = Routes(tf_terminal, tf_hosts)
        else:
            terminal = live.terminal
            if tf_terminal != UNREADABLE and tf_terminal != live.terminal:
                print(f"::warning::watchdog: the live URL map serves settle from {live.terminal} but infra/ says "
                      f"{tf_terminal}; settle evidence is inconclusive until they agree", flush=True)
                terminal = UNREADABLE
            routes = Routes(terminal, live.hosts)
    _routes_cache.append(routes)
    return routes


def terminal_route_region(path: Path = TERMINAL_BACKEND_TF) -> str | None:
    """us-central1 (the terminal NEG's region) when the terminal route is on,
    None when it is off, UNREADABLE when the Terraform cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return UNREADABLE
    enabled = re.findall(r"^\s*terminal_c1_route_enabled\s*=\s*(true|false)\s*$", text, re.MULTILINE)
    regions = set(re.findall(r"/regions/([a-z0-9-]+)/networkEndpointGroups/", text))
    if len(enabled) != 1 or len(regions) != 1:
        return UNREADABLE
    return regions.pop() if enabled[0] == "true" else None


def normalize_watchdog_status(status: object) -> str:
    text = str(status or "").lower()
    if text == "up":
        return "up"
    if text == "down":
        return "down"
    if text == "trust_degraded":
        return "trust_degraded"
    if text in {"degraded", "routing_degraded"}:
        return "degraded"
    return "unknown"


def fetch_per_region(
    url: str,
    regions: Iterable[str],
    timeout: int = 10,
    *,
    slo_class: str = "router_core",
) -> dict[str, str]:
    """Return {region: 'up'|'degraded'|'down'|'unknown'} for each requested region.

    A region maps to `unknown` when status.json had no checks targeting
    it. That distinguishes "monitor blackout" (status fetch ok, region
    has no recent probes) from a real "up" reading.
    """
    regions = tuple(regions)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - deploy script uses operator-provided HTTPS status URL.
            payload = json.load(response)
        data = payload.get("data", {})
        slo_region_status = (
            data.get("slo_classes", {}).get(slo_class, {}).get("current_by_region", {})
        )
        checks = data.get("current", {}).get("checks", []) or []
    except Exception as exc:
        print(f"  watchdog: status fetch error: {exc}", flush=True)
        # Fetch error -> unknown for every region (doesn't increment
        # the down counter; deploys aren't the suspect for an LB blip).
        return {region: "unknown" for region in regions}

    if slo_class == "router_core":
        return control_plane_per_region(checks, regions, load_routes())

    out: dict[str, str] = {}
    if isinstance(slo_region_status, dict):
        for region in regions:
            row = slo_region_status.get(region)
            if isinstance(row, dict):
                out[region] = normalize_watchdog_status(row.get("status"))

    # Keep the worst STATUS, not the worst severity int. down and
    # trust_degraded share severity 2, so ranking by int and mapping back
    # collapses trust_degraded into down — and the caller needs the
    # distinction to report WHY it rolled back (an attestation regression
    # reads very differently from an unreachable region).
    worst: dict[str, str] = {}
    for check in checks:
        if not isinstance(check, dict) or check.get("probe_type") not in SLO_PROBES.get(
            slo_class, ()
        ):
            continue
        target = (check or {}).get("target_region")
        status = normalize_watchdog_status(
            (check or {}).get("effective_status") or (check or {}).get("status")
        )
        if not target or not status:
            continue
        if target not in regions:
            continue
        sev = SEVERITY.get(status)
        if sev is None:
            continue
        if sev > SEVERITY.get(worst.get(target, ""), -1):
            worst[target] = status
    return {region: out.get(region, worst.get(region, "unknown")) for region in regions}


def sample_stages(check: dict) -> frozenset[str]:
    """Which control-plane stages a sample's status depends on."""
    path = urllib.parse.urlsplit(str(check.get("target_url") or "")).path.removeprefix("/v1")
    if path in TERMINAL_PATHS:
        # A settle sample is written only after its authorize succeeded, so
        # its status is the terminal call's.
        return frozenset({"settle"})
    if path == AUTHORIZE_PATH:
        return frozenset({"authorize"})
    return DEFAULT_STAGES.get(str(check.get("probe_type")), frozenset())


def fresh(check: dict) -> bool:
    """A sample the monitor wrote recently, not one the status page has aged."""
    recorded = normalize_watchdog_status(check.get("status"))
    effective = normalize_watchdog_status(check.get("effective_status") or check.get("status"))
    age = check.get("age_seconds")
    young = age is None or (isinstance(age, (int, float)) and age <= CURRENT_SAMPLE_TTL_SECONDS)
    return recorded == effective and young


def control_plane_region(check: dict, routes: Routes, authorize_up: set[str] | None = None) -> str | None:
    """The control-plane region that served a check, or None if unattributable.

    `authorize_up` holds the monitors whose authorize row is fresh and up; a
    settle-stage sample from any other monitor is not attributed.
    """
    target_region = check.get("target_region")
    if target_region:
        return str(target_region)
    monitor = check.get("monitor_region")
    if check.get("target") != CONTROL_PLANE_TARGET or not monitor:
        return None
    if urllib.parse.urlsplit(str(check.get("target_url") or "")).hostname not in routes.hosts:
        return None
    stages = sample_stages(check)
    if "settle" in stages and authorize_up is not None and monitor not in authorize_up:
        return None
    served = set()
    for stage in stages:
        if stage == "settle" and routes.terminal == UNREADABLE:
            return None
        served.add(routes.terminal if stage == "settle" and routes.terminal else str(monitor))
    # A sample that crossed two regions cannot be blamed on either.
    return served.pop() if len(served) == 1 else None


def control_plane_per_region(checks: Iterable[object], regions: Iterable[str], routes: Routes) -> dict[str, str]:
    """Worst fresh control-plane probe status per region; enclave-only probes never count."""
    regions = tuple(regions)
    rows = [check for check in checks if isinstance(check, dict)]
    authorize_up = {
        str(check["monitor_region"]) for check in rows
        if check.get("probe_type") == "gateway_authorize" and check.get("target") == CONTROL_PLANE_TARGET
        and check.get("monitor_region") and fresh(check)
        and normalize_watchdog_status(check.get("status")) == "up"
    }
    worst: dict[str, str] = {}
    inconclusive: dict[str, set[str]] = {}
    stale = 0
    for check in rows:
        probe_type = check.get("probe_type")
        if probe_type in GATEWAY_ONLY_PROBES:
            region = check.get("target_region")
            if region in regions:
                inconclusive.setdefault(str(region), set()).add(str(probe_type))
            continue
        if probe_type not in CONTROL_PLANE_ROUTER_CORE_PROBES:
            continue
        if not fresh(check):
            stale += 1
            continue
        region = control_plane_region(check, routes, authorize_up)
        status = normalize_watchdog_status(check.get("effective_status") or check.get("status"))
        if region not in regions or status == "unknown":
            continue
        if SEVERITY[status] > SEVERITY.get(worst.get(region, ""), -1):
            worst[region] = status
    if stale:
        print(f"  watchdog: {stale} stale control-plane row(s) not counted (dead or late monitor)", flush=True)
    if routes.terminal == UNREADABLE and "terminal" not in _warned_unknown:
        _warned_unknown.add("terminal")
        print("::warning::watchdog: the terminal route is unknown; settle evidence is inconclusive", flush=True)
    for region in regions:
        if region in inconclusive:
            print(f"  watchdog: {region}: {', '.join(sorted(inconclusive[region]))} are enclave-only; "
                  "not counted for a control-plane verdict", flush=True)
        if region not in worst and region not in _warned_unknown:
            _warned_unknown.add(region)
            print(f"::warning::watchdog: {region}: no control-plane probe is attributable to this region; "
                  "its canary verdict is unknown and does not block", flush=True)
    return {region: worst.get(region, "unknown") for region in regions}


def write_output(key: str, value: str) -> None:
    """Append a key=value line to $GITHUB_OUTPUT if defined.

    Falls back to stdout marker line for local runs.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")
    else:
        print(f"::output::{key}={value}", flush=True)


def wait_for_start_gate(path: str, timeout_sec: int) -> None:
    """Wait until the traffic-shift caller releases the polling window.

    Baseline grace and capture happen before this wait, so callers can overlap
    that fixed startup cost with a Cloud Run traffic update. The minute loop
    remains behind the gate, preserving the complete configured watch window
    after the traffic update has finished.
    """
    gate = Path(path)
    deadline = time.monotonic() + timeout_sec
    while not gate.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"watchdog start gate was not released within {timeout_sec}s: {gate}"
            )
        time.sleep(0.1)


def read_baseline(path: str, regions: Iterable[str]) -> dict[str, str]:
    """Read a baseline snapshot captured by an earlier watchdog process."""
    with Path(path).open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("baseline document must be an object")
    baseline: dict[str, str] = {}
    for region in regions:
        status = document.get(region, "unknown")
        normalized = normalize_watchdog_status(status)
        if normalized == "unknown" and status != "unknown":
            raise ValueError(f"invalid baseline status for {region}: {status!r}")
        baseline[region] = normalized
    return baseline


def write_baseline(path: str, baseline: dict[str, str]) -> None:
    """Atomically persist a baseline for a post-shift watchdog invocation."""
    destination = Path(path)
    temporary = destination.with_name(destination.name + f".tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(baseline, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-min", type=int, default=10)
    parser.add_argument("--rollback-after", type=int, default=3)
    parser.add_argument(
        "--regions",
        default=",".join(ENCLAVE_REGIONS),
        help="Comma-separated list of target regions to monitor.",
    )
    parser.add_argument(
        "--status-url",
        default="https://trustedrouter.com/status.json",
    )
    parser.add_argument(
        "--slo-class",
        default="router_core",
        choices=("router_core", "control_plane"),
        help="SLO class to evaluate for rollback. Default: router_core.",
    )
    parser.add_argument(
        "--baseline-grace-sec",
        type=int,
        default=30,
        help=(
            "Wait this long before the first poll, then capture a "
            "baseline of regions that are ALREADY 'down' before the "
            "deploy is the suspect. Pre-existing flap (e.g. a flaky "
            "upstream provider that's broken globally) is held "
            "constant: a region whose status was 'down' in the "
            "baseline doesn't accrue 'consecutive_down' counts. "
            "Only NEW downs (regions that flipped from up/degraded "
            "to down after the canary) count toward rollback. This "
            "lets us keep an aggressive --rollback-after of 1 (block "
            "any deploy-introduced regression) without false-positive "
            "rollbacks when the underlying probe is already failing."
        ),
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help=(
            "Hotfix flag: skip baseline and use raw 'down' counting. "
            "Used by the emergency-fix workflow path that must ship "
            "regardless of pre-existing badness."
        ),
    )
    parser.add_argument(
        "--start-gate-file",
        default="",
        help=(
            "Optional path that must exist before the minute polling window starts. "
            "Baseline grace/capture still runs immediately, allowing staged traffic "
            "updates to overlap the 30-second startup cost."
        ),
    )
    parser.add_argument(
        "--start-gate-timeout-sec",
        type=int,
        default=600,
        help="Maximum wait for --start-gate-file before failing closed.",
    )
    parser.add_argument(
        "--baseline-input",
        default="",
        help=(
            "Read a baseline captured before the traffic shift instead of paying "
            "the baseline grace in this process. Ignored with --skip-baseline."
        ),
    )
    parser.add_argument(
        "--baseline-output",
        default="",
        help=(
            "Atomically write the captured baseline for a later watchdog process. "
            "Use --duration-min 0 for a baseline-only invocation."
        ),
    )
    args = parser.parse_args()
    if args.baseline_input and args.baseline_output:
        parser.error("--baseline-input and --baseline-output are mutually exclusive")

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    consecutive_down = {region: 0 for region in regions}
    rollback_set: set[str] = set()

    # Baseline: the set of regions that were ALREADY 'down' before the
    # deploy. The canary watchdog is interested in whether the new
    # revision INTRODUCED a regression — not whether some unrelated
    # upstream is currently broken. Regions in the baseline-down set
    # are excluded from rollback accounting unless their status
    # transitions further (today: still treat as down — but we won't
    # blame the deploy for them).
    baseline_down: set[str] = set()
    if not args.skip_baseline:
        if args.baseline_input:
            try:
                baseline_snapshot = read_baseline(args.baseline_input, regions)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                print(f"watchdog: invalid baseline input: {exc}", file=sys.stderr)
                write_output("rollback_regions", ",".join(sorted(regions)))
                return 1
        else:
            if args.baseline_grace_sec > 0:
                time.sleep(args.baseline_grace_sec)
            baseline_snapshot = fetch_per_region(
                args.status_url,
                regions,
                slo_class=args.slo_class,
            )
        baseline_down = {r for r, s in baseline_snapshot.items() if s == "down"}
        if baseline_down:
            print(
                f"watchdog: baseline 'down' regions (not blamed on deploy): "
                f"{sorted(baseline_down)}",
                flush=True,
            )
    else:
        baseline_snapshot = {region: "unknown" for region in regions}

    if args.baseline_output:
        try:
            write_baseline(args.baseline_output, baseline_snapshot)
        except OSError as exc:
            print(f"watchdog: could not write baseline output: {exc}", file=sys.stderr)
            write_output("rollback_regions", ",".join(sorted(regions)))
            return 1

    if args.start_gate_file:
        if args.start_gate_timeout_sec <= 0:
            parser.error("--start-gate-timeout-sec must be positive")
        print(
            "watchdog: baseline ready; waiting for traffic shift to complete",
            flush=True,
        )
        try:
            wait_for_start_gate(args.start_gate_file, args.start_gate_timeout_sec)
        except TimeoutError as exc:
            print(f"watchdog: {exc}", file=sys.stderr, flush=True)
            write_output("rollback_regions", ",".join(sorted(regions)))
            return 1

    print(
        f"watchdog: polling {args.status_url} every 60s for {args.duration_min} min; "
        f"per-region {args.slo_class} rollback if 'down' for {args.rollback_after} consecutive minutes "
        f"AND not already 'down' before the deploy; regions={regions}",
        flush=True,
    )
    for minute in range(1, args.duration_min + 1):
        time.sleep(60)
        per_region = fetch_per_region(args.status_url, regions, slo_class=args.slo_class)
        line = f"  minute {minute}:"
        for region in regions:
            status = per_region.get(region, "unknown")
            if region in rollback_set:
                line += f"  {region}=ROLLED_BACK"
                continue
            # Only count "down" as deploy-caused if the region was
            # healthy in the baseline. If it was already down, the
            # deploy isn't the suspect; leave the counter at 0 so
            # we don't roll back over a pre-existing condition.
            if status in ROLLBACK_STATUSES and region not in baseline_down:
                consecutive_down[region] += 1
            else:
                consecutive_down[region] = 0
            tag = f"{status}({consecutive_down[region]})"
            if region in baseline_down:
                tag += "[baseline]"
            line += f"  {region}={tag}"
            if consecutive_down[region] >= args.rollback_after:
                rollback_set.add(region)
                print(
                    f"  watchdog: ROLLBACK {region} — 'down' for {consecutive_down[region]} consecutive minutes "
                    f"(was {'down' if region in baseline_down else 'healthy'} pre-deploy)",
                    flush=True,
                )
        print(line, flush=True)
        # If every region is rolled back, no point continuing the watch.
        if rollback_set == set(regions):
            break

    rollback_list = ",".join(sorted(rollback_set))
    write_output("rollback_regions", rollback_list)
    if rollback_set:
        print(f"watchdog: rolling back regions: {rollback_list}", flush=True)
        return 1
    print("watchdog: deploy healthy across all regions", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
