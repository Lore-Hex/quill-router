"""Independent, metadata-only refresh monitoring. Uses only Python's stdlib and gh."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid4, uuid5

REPOSITORY = "Lore-Hex/quill-router"
WORKFLOW = "refresh-prices.yml"
MARKER_STEP = "Catalog refresh validated"
ISSUE_TITLE = "[bot] Price refresh monitoring"
STATE_MARKER = "<!-- price-refresh-monitor-v1 "
INTERVAL = timedelta(hours=6)
# GitHub drops scheduled runs (the :37 hourly cron started only twice on
# 2026-11-18), so the observer dispatches the refresh itself once no run has
# been created or started for this long. One dispatch per window at most,
# recorded in the ledger's "refresh_dispatch" cooldown.
DISPATCH_AFTER = timedelta(minutes=90)
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
FAILURES = {"failure", "timed_out", "action_required", "startup_failure"}
STAGES = {
    "Install dependencies": "dependency_install",
    "Authenticate to GCP via WIF": "authentication",
    "Refresh provider-direct prices (with self-heal)": "provider_refresh",
    "Sanity-check for price spikes + record deltas": "price_guard",
    "Price-source coverage audit": "coverage_audit",
    "Block unsafe coverage before publication": "coverage_gate",
    "Commit, verify, and push if changed": "publication",
    "Reconcile model-discovery coverage issue": "issue_reporting",
    "Reconcile held-price issue": "issue_reporting",
    "Deploy a price snapshot no rollout has carried": "deploy_dispatch",
}


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must have a timezone")
    return result.astimezone(UTC)


def gh(path: str, data: dict[str, Any] | None = None) -> Any:
    if os.environ.get("TR_PRICE_REFRESH_PUBLIC_API") == "1":
        if data is not None:
            raise ValueError("Public observer cannot write to GitHub")
        return public_gh(path)
    endpoint = path if path.startswith("search/") else f"repos/{REPOSITORY}/{path}"
    args = ["gh", "api", endpoint]
    if data is not None:
        args += ["--method", "PATCH" if re.fullmatch(r"issues/\d+", path) else "POST",
                 "--input", "-"]
    result = subprocess.run(  # noqa: S603 - fixed CLI, no shell or caller-supplied repository
        args, input=json.dumps(data) if data is not None else None,
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode:
        # Never echo CLI output: it can contain request headers or credentials.
        raise RuntimeError("GitHub monitoring API request failed")
    # A workflow dispatch answers 204 No Content.
    return json.loads(result.stdout) if result.stdout.strip() else None


class MonitoringReadError(RuntimeError):
    """A failed anonymous GitHub read.

    The message is a fixed phrase plus at most an HTTP status code, never a
    URL, header or response body, so the CLI may print it.
    """


# One anonymous read in a few hundred fails transiently (2026-11-19 18:23Z:
# one observer run errored, the next hour's run succeeded), and every failed
# run raises and resolves a Cron issue. Retry those reads a bounded number of
# times; a whole run stays far inside the ten-minute max runtime.
PUBLIC_READ_RETRY_DELAYS = (2.0, 8.0)
# Honor a short Retry-After from a rate limit. A longer one (the anonymous
# primary limit resets hourly) fails now instead of sleeping through the run.
MAX_RETRY_AFTER_SECONDS = 30


def _read_failure(exc: Exception) -> tuple[str, float | None]:
    """A safe failure classification and the delay before a retry (None: do not retry)."""
    if isinstance(exc, HTTPError):
        headers = exc.headers or {}
        limited = exc.code == 429 or (exc.code == 403 and (
            headers.get("x-ratelimit-remaining") == "0" or headers.get("retry-after") is not None))
        if limited:
            retry_after = str(headers.get("retry-after") or "")
            if retry_after.isdigit() and int(retry_after) <= MAX_RETRY_AFTER_SECONDS:
                return f"HTTP {exc.code}, rate limited", float(retry_after)
            return f"HTTP {exc.code}, rate limited", None
        if 500 <= exc.code < 600:
            return f"HTTP {exc.code}", 0.0
        return f"HTTP {exc.code}", None
    if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
        return "timeout", 0.0
    return "network error", 0.0


def public_gh(path: str) -> Any:
    """Anonymous metadata-only reads: no personal or repository write token."""
    if "://" in path or ".." in path or not path.startswith(("actions/", "search/issues?q=repo:" + REPOSITORY)):
        raise ValueError("Unexpected public monitoring API path")
    endpoint = path if path.startswith("search/") else f"repos/{REPOSITORY}/{path}"
    request = Request("https://api.github.com/" + endpoint, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "TrustedRouter-price-observer",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    for attempt in range(len(PUBLIC_READ_RETRY_DELAYS) + 1):
        try:
            with build_opener(NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(2_000_001)
            break
        except (HTTPError, URLError, TimeoutError) as exc:
            reason, retry_after = _read_failure(exc)
            if retry_after is None or attempt == len(PUBLIC_READ_RETRY_DELAYS):
                raise MonitoringReadError(f"GitHub monitoring API request failed ({reason})") from exc
            time.sleep(max(retry_after, PUBLIC_READ_RETRY_DELAYS[attempt]))
    if len(raw) > 2_000_000:
        raise ValueError("Monitoring API response is too large")
    return json.loads(raw)


def read_local_state(path: Path, remote: dict[str, Any]) -> dict[str, Any]:
    state = {**remote, "sent": dict(remote["sent"])}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return state
    with os.fdopen(descriptor) as handle:
        raw = handle.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("Monitoring state is too large")
    local = json.loads(raw)
    if not isinstance(local, dict) or not isinstance(local.get("sent"), dict):
        raise ValueError("Monitoring state is invalid")
    for value in [local.get("last_validated_at"), *local["sent"].values()]:
        if value is not None:
            timestamp(value)
    state.update(local)
    # Import the bot observer's cooldowns without modifying its issue ledger.
    for kind, value in remote["sent"].items():
        previous = state["sent"].get(kind)
        if previous is None or timestamp(value) > timestamp(previous):
            state["sent"][kind] = value
    validated = remote.get("last_validated_at")
    if validated and (not state.get("last_validated_at")
                      or timestamp(validated) > timestamp(state["last_validated_at"])):
        state["last_validated_at"] = validated
    return state


def write_local_state(path: Path, state: dict[str, Any]) -> None:
    raw = json.dumps(state, sort_keys=True).encode()
    if len(raw) > 65_536:
        raise ValueError("Monitoring state is too large")
    descriptor, temporary = tempfile.mkstemp(prefix=".price-observer-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def local_state_lock(path: Path) -> Iterator[bool]:
    if not path.is_absolute():
        raise ValueError("Monitoring state path must be absolute")
    descriptor = os.open(path.with_suffix(path.suffix + ".lock"),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def read_state() -> tuple[int | None, dict[str, Any]]:
    result = gh(f"search/issues?q=repo:{REPOSITORY}+is:issue+author:github-actions%5Bbot%5D+in:title+%22Price+refresh+monitoring%22&per_page=100")
    if result["total_count"] > 100 or result.get("incomplete_results"):
        raise ValueError("Incomplete monitoring ledger lookup")
    issues = result["items"]
    matches = [issue for issue in issues if issue.get("title") == ISSUE_TITLE
               and issue.get("user", {}).get("login") == "github-actions[bot]"]
    if len(matches) > 1:
        raise ValueError("Duplicate monitoring ledgers; reconcile before sending")
    if not matches:
        return None, {"last_validated_at": None, "sent": {}}
    issue = matches[0]
    body = issue.get("body") or ""
    if STATE_MARKER not in body:
        raise ValueError("Monitoring ledger state is missing")
    state = json.loads(body.split(STATE_MARKER, 1)[1].split(" -->", 1)[0])
    if not isinstance(state, dict) or not isinstance(state.get("sent"), dict):
        raise ValueError("Monitoring ledger state is invalid")
    # Fail closed on malformed/future cooldown timestamps, not unlimited sends.
    for value in [state.get("last_validated_at"), *state["sent"].values()]:
        if value is not None:
            timestamp(value)
    return int(issue["number"]), state


def run_metadata(run: dict[str, Any]) -> dict[str, Any]:
    if (run.get("path") != f".github/workflows/{WORKFLOW}"
            or run.get("head_branch") != "main"
            or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("event") not in {"schedule", "workflow_dispatch"}):
        raise ValueError("Unexpected refresh run identity")
    sha = run["head_sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Invalid refresh commit")
    run_id = int(run["id"])
    return {
        "run_id": run_id, "attempt": int(run.get("run_attempt", 1)), "commit": sha,
        "run_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
    }


def jobs_for(run: dict[str, Any]) -> list[dict[str, Any]]:
    meta = run_metadata(run)
    result = gh(f"actions/runs/{meta['run_id']}/attempts/{meta['attempt']}/jobs?per_page=100")
    if result["total_count"] > 100:
        raise ValueError("Unexpected refresh job count")
    return result["jobs"]


def validated_at(jobs: list[dict[str, Any]]) -> str | None:
    times = [step["completed_at"] for job in jobs for step in job.get("steps", [])
             if step.get("name") == MARKER_STEP and step.get("conclusion") == "success"]
    return max(times, key=timestamp) if times else None


def failed_run(run: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    conclusion = run.get("conclusion")
    started = any(step.get("started_at") for job in jobs for step in job.get("steps", []))
    # Concurrency routinely cancels superseded QUEUED runs. A cancelled active
    # run is an incident; an unstarted cancellation is covered by freshness.
    if conclusion not in FAILURES and not (conclusion == "cancelled" and started):
        return None
    failed = [step for job in jobs for step in job.get("steps", [])
              if step.get("conclusion") in FAILURES | {"cancelled"}]
    return {
        **run_metadata(run), "conclusion": conclusion,
        "stage": STAGES.get(failed[0].get("name", ""), "other") if failed else "runner",
        "step_number": int(failed[0]["number"]) if failed else 0,
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def send_sentry(kind: str, payload: dict[str, Any]) -> None:
    dsn = os.environ.get("TR_SENTRY_DSN", "")
    parsed = urlsplit(dsn)
    host = parsed.hostname or ""
    project = parsed.path.removeprefix("/")
    if (parsed.scheme != "https" or not host.endswith(".ingest.us.sentry.io")
            and not host.endswith(".ingest.sentry.io")
            and not host.endswith(".ingest.de.sentry.io")
            or not re.fullmatch(r"[a-f0-9]+", parsed.username or "")
            or not project.isdigit() or parsed.password or parsed.query or parsed.fragment
            or parsed.port not in {None, 443}):
        raise ValueError("Missing or invalid Sentry ingestion DSN")
    headers = {"dsn": dsn}
    if kind == "event":
        headers["event_id"] = payload["event_id"]
    envelope = (json.dumps(headers) + "\n" + json.dumps({"type": kind})
                + "\n" + json.dumps(payload) + "\n").encode()
    request = Request(
        f"https://{host}/api/{project}/envelope/", data=envelope,
        headers={"Content-Type": "application/x-sentry-envelope"}, method="POST",
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("Sentry did not accept monitoring telemetry")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError("Sentry monitoring delivery failed; cooldown not advanced") from exc


def heartbeat(status: str, check_in_id: str, duration: float = 0) -> None:
    # This monitor checks the observer itself from outside GitHub. If Actions
    # stops scheduling both workflows, Sentry can still detect the silence.
    payload: dict[str, Any] = {
        "check_in_id": check_in_id, "monitor_slug": "price-refresh-watchdog",
        "status": status, "environment": "production", "duration": duration,
    }
    if status == "in_progress":
        # Two consecutive failed or missed hourly check-ins open the Cron
        # issue: one transient failure no longer pages and then resolves an
        # hour later, while a broken or silent observer still pages within
        # about two hours.
        payload["monitor_config"] = {
            "schedule": {"type": "interval", "value": 1, "unit": "hour"},
            "checkin_margin": 60, "failure_issue_threshold": 2, "recovery_threshold": 1,
            "max_runtime": 10,
        }
    send_sentry("check_in", payload)


def report(kind: str, details: dict[str, Any], state: dict[str, Any], now: datetime) -> bool:
    last = state["sent"].get(kind)
    if last is not None and now - timestamp(last) < INTERVAL:
        return False
    # Stable across redelivery within one six-hour bucket, including a crash
    # after delivery but before the GitHub ledger update.
    event_id = uuid5(NAMESPACE_URL, f"{REPOSITORY}/{kind}/{int(now.timestamp()) // 21600}").hex
    send_sentry("event", {
        "event_id": event_id, "timestamp": now.isoformat(), "platform": "python",
        "level": "error", "logger": "price_refresh_monitor", "environment": "production",
        "message": {"refresh_failed": "Price/catalog refresh failed",
                    "catalog_stale": "No validated catalog refresh within six hours",
                    "refresh_dispatch_failed": "Could not dispatch a stalled price refresh"}[kind],
        "fingerprint": ["price-refresh", kind],
        "tags": {"component": "price-refresh", "failure_kind": kind},
        "extra": details,
    })
    state["sent"][kind] = now.isoformat()
    return True


def refresh_runs() -> list[dict[str, Any]]:
    """Recently created runs plus any queued or running rerun of an older one."""
    runs: dict[int, dict[str, Any]] = {}
    for query in ("", "&status=queued", "&status=in_progress"):
        for candidate in gh(f"actions/workflows/{WORKFLOW}/runs?branch=main&per_page=20{query}")["workflow_runs"]:
            if candidate.get("path") == f".github/workflows/{WORKFLOW}":
                runs[int(candidate["id"])] = candidate
    return list(runs.values())


def dispatch_decision(runs: list[dict[str, Any]], state: dict[str, Any],
                      now: datetime) -> str:
    """Why the observer would ("dispatch") or would not kick the refresh."""
    if any(candidate.get("status") in ACTIVE for candidate in runs):
        # refresh-prices keeps one pending run per concurrency group, so a
        # dispatch now could cancel the queued run instead of adding one.
        return "active"
    starts = [timestamp(value) for candidate in runs
              for value in (candidate.get("created_at"), candidate.get("run_started_at")) if value]
    if starts and now - max(starts) < DISPATCH_AFTER:
        return "recent"
    last = state["sent"].get("refresh_dispatch")
    if last is not None and now - timestamp(last) < DISPATCH_AFTER:
        return "cooldown"
    return "dispatch"


def dispatch_refresh(state: dict[str, Any], now: datetime) -> str:
    """Dispatch refresh-prices.yml on main when GitHub dropped its schedule.

    Returns the decision; a failed dispatch returns "failed" and never raises,
    so the freshness alert below still runs.
    """
    try:
        decision = dispatch_decision(refresh_runs(), state, now)
        if decision == "dispatch":
            gh(f"actions/workflows/{WORKFLOW}/dispatches", {"ref": "main"})
            state["sent"]["refresh_dispatch"] = now.isoformat()
    except Exception as exc:  # noqa: BLE001 - reported below; never blocks alerts
        print(f"::warning::Price refresh dispatch failed ({type(exc).__name__})", file=sys.stderr)
        return "failed"
    return decision


def observe(*, dry_run: bool = False, state_path: Path | None = None,
            dispatch: bool = False) -> None:
    number, state = read_state()
    if state_path is not None:
        state = read_local_state(state_path, state)
    now = datetime.now(UTC)
    if any(timestamp(value) > now for value in
           [state.get("last_validated_at"), *state["sent"].values()] if value is not None):
        raise ValueError("Monitoring state contains a future timestamp")
    # Bounded query, including successes whose final reporting step failed.
    runs = gh(f"actions/workflows/{WORKFLOW}/runs?branch=main&status=completed&per_page=20")["workflow_runs"]
    run_id = os.environ.get("TR_REFRESH_RUN_ID", "")
    if run_id:
        if not run_id.isdigit():
            raise ValueError("Invalid trigger run id")
        triggered = gh(f"actions/runs/{run_id}")
        if all(run["id"] != triggered["id"] for run in runs):
            runs.append(triggered)
    observed: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for run in runs:
        jobs = jobs_for(run)
        observed.append((run, jobs))
        validated = validated_at(jobs)
        if validated and (state["last_validated_at"] is None
                          or timestamp(validated) > timestamp(state["last_validated_at"])):
            state["last_validated_at"] = validated
    # Observe completions rather than creation order (reruns retain created_at).
    observed.sort(key=lambda item: timestamp(item[0]["updated_at"]), reverse=True)
    current_failure = failed_run(*observed[0]) if observed else None
    recent_failures = [failure for run, jobs in observed
                       if (failure := failed_run(run, jobs)) is not None]
    latest_failure = recent_failures[0] if recent_failures else None
    failure_key = (f"{latest_failure['run_id']}:{latest_failure['attempt']}"
                   if latest_failure else None)
    # A completion notification can wait behind another observer or be
    # coalesced by Actions concurrency. Still report a newly seen failure even
    # if a later run recovered before this observer started.
    failure = (latest_failure if failure_key != state.get("last_failure") else current_failure)
    stale = (state["last_validated_at"] is None
             or now - timestamp(state["last_validated_at"]) >= INTERVAL)
    if dry_run:
        preview: dict[str, Any] = {"failure": failure, "stale": stale,
                                   "last_validated_at": state["last_validated_at"]}
        if dispatch:
            preview["dispatch"] = dispatch_decision(refresh_runs(), state, now)
        print(json.dumps(preview, sort_keys=True))
        return
    # Kick a dropped schedule before alerting; the stale alert still fires
    # below if publication stays stale despite the dispatch.
    dispatched = dispatch_refresh(state, now) if dispatch else None
    if failure:
        report("refresh_failed", failure, state, now)
    if stale:
        report("catalog_stale", {
            "last_validated_at": state["last_validated_at"], "threshold_hours": 6,
            "workflow_url": f"https://github.com/{REPOSITORY}/actions/workflows/{WORKFLOW}",
        }, state, now)
    if dispatched == "failed":
        report("refresh_dispatch_failed", {
            "threshold_minutes": int(DISPATCH_AFTER.total_seconds() // 60),
            "workflow_url": f"https://github.com/{REPOSITORY}/actions/workflows/{WORKFLOW}",
        }, state, now)
    if failure_key:
        state["last_failure"] = failure_key
    unhealthy = bool(current_failure) or stale or dispatched == "failed"
    status = "Attention required" if unhealthy else "Recovered"
    if dispatched == "dispatch":
        print(f"Dispatched {WORKFLOW} on main: no refresh run for {DISPATCH_AFTER}")
    if state_path is not None:
        write_local_state(state_path, state)
        print(f"Price refresh monitoring: {status}; durable local ledger")
        return
    body = (f"{status}. This ledger bounds Sentry error delivery across workflow runs.\n\n"
            f"Last validated refresh: {state['last_validated_at'] or 'not yet observed'}.\n\n"
            "A validated refresh means publication (or no changes), not a superseded run. "
            "Deployment health is checked separately. Do not delete the cooldown ledger.\n\n"
            f"{STATE_MARKER}{json.dumps(state, sort_keys=True)} -->")
    data = {"body": body, "state": "open" if unhealthy else "closed"}
    if number is None:
        created = gh("issues", {"title": ISSUE_TITLE, "body": body})
        number = int(created["number"])
    gh(f"issues/{number}", data)
    print(f"Price refresh monitoring: {status}; ledger #{number}")


def main(*, dry_run: bool = False, state_path: Path | None = None,
         heartbeat_enabled: bool = True, dispatch: bool = False) -> None:
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("Monitoring is restricted to the production repository")
    if os.environ.get("TR_PRICE_REFRESH_PUBLIC_API") == "1" and state_path is None:
        raise ValueError("Public observer requires local state")
    if dispatch and os.environ.get("TR_PRICE_REFRESH_PUBLIC_API") == "1":
        raise ValueError("The anonymous observer cannot dispatch workflows")
    if dry_run:
        observe(dry_run=True, state_path=state_path, dispatch=dispatch)
        return
    with local_state_lock(state_path) if state_path else nullcontext(True) as acquired:
        if not acquired:
            print("Price refresh observer is already running")
            return
        check_in_id = uuid4().hex
        started = time.monotonic()
        if heartbeat_enabled:
            heartbeat("in_progress", check_in_id)
        try:
            observe(state_path=state_path, dispatch=dispatch)
        except Exception:
            if heartbeat_enabled:
                heartbeat("error", check_in_id, time.monotonic() - started)
            raise
        # Only a fully successful read, report and durable-state write earns OK.
        if heartbeat_enabled:
            heartbeat("ok", check_in_id, time.monotonic() - started)


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--state-file", type=Path)
        parser.add_argument("--dsn-file", type=Path)
        parser.add_argument("--no-heartbeat", action="store_true")
        parser.add_argument("--dispatch-stalled", action="store_true",
                            help="dispatch refresh-prices.yml when no run started recently")
        args = parser.parse_args()
        if args.dsn_file is not None:
            with args.dsn_file.open() as handle:
                dsn = handle.read(4097).strip()
            if len(dsn) > 4096:
                raise ValueError("Invalid ingestion credential")
            os.environ["TR_SENTRY_DSN"] = dsn
        main(dry_run=args.dry_run, state_path=args.state_file,
             heartbeat_enabled=not args.no_heartbeat, dispatch=args.dispatch_stalled)
    except MonitoringReadError as exc:
        # Safe to print: a fixed phrase and at most an HTTP status code.
        print(f"::error::Price refresh monitoring failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        # Suppress exception strings/tracebacks: URLs can contain the DSN.
        print(f"::error::Price refresh monitoring failed ({type(exc).__name__}); inspect configuration and delivery", file=sys.stderr)
        raise SystemExit(1) from None
