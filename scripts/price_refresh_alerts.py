"""Independent, metadata-only refresh monitoring. Uses only Python's stdlib and gh."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
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
}


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must have a timezone")
    return result.astimezone(UTC)


def gh(path: str, data: dict[str, Any] | None = None) -> Any:
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
    return json.loads(result.stdout)


def read_state() -> tuple[int | None, dict[str, Any]]:
    result = gh(f"search/issues?q=repo:{REPOSITORY}+is:issue+in:title+%22Price+refresh+monitoring%22&per_page=100")
    if result["total_count"] > 100 or result.get("incomplete_results"):
        raise ValueError("Incomplete monitoring ledger lookup")
    issues = result["items"]
    matches = [issue for issue in issues if issue.get("title") == ISSUE_TITLE]
    if len(matches) > 1:
        raise ValueError("Duplicate monitoring ledgers; reconcile before sending")
    if not matches:
        return None, {"last_validated_at": None, "sent": {}}
    issue = matches[0]
    if issue.get("user", {}).get("login") != "github-actions[bot]":
        raise ValueError("Monitoring ledger must be owned by GitHub Actions")
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
    envelope = (json.dumps({"dsn": dsn}) + "\n" + json.dumps({"type": kind})
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


def heartbeat() -> None:
    # This monitor checks the observer itself from outside GitHub. If Actions
    # stops scheduling both workflows, Sentry can still detect the silence.
    send_sentry("check_in", {
        "check_in_id": uuid4().hex, "monitor_slug": "price-refresh-watchdog",
        "status": "ok", "environment": "production",
        "monitor_config": {
            "schedule": {"type": "interval", "value": 1, "unit": "hour"},
            "checkin_margin": 60, "failure_issue_threshold": 1, "recovery_threshold": 1,
        },
    })


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
                    "catalog_stale": "No validated catalog refresh within six hours"}[kind],
        "fingerprint": ["price-refresh", kind],
        "tags": {"component": "price-refresh", "failure_kind": kind},
        "extra": details,
    })
    state["sent"][kind] = now.isoformat()
    return True


def main(*, dry_run: bool = False) -> None:
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("Monitoring is restricted to the production repository")
    now = datetime.now(UTC)
    number, state = read_state()
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
        print(json.dumps({"failure": failure, "stale": stale,
                          "last_validated_at": state["last_validated_at"]}, sort_keys=True))
        return
    if failure:
        report("refresh_failed", failure, state, now)
    if stale:
        report("catalog_stale", {
            "last_validated_at": state["last_validated_at"], "threshold_hours": 6,
            "workflow_url": f"https://github.com/{REPOSITORY}/actions/workflows/{WORKFLOW}",
        }, state, now)
    if failure_key:
        state["last_failure"] = failure_key
    unhealthy = bool(current_failure) or stale
    status = "Attention required" if unhealthy else "Recovered"
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
    # Only a fully successful read, report and durable-state write earns a ping.
    heartbeat()
    print(f"Price refresh monitoring: {status}; ledger #{number}")


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--dry-run", action="store_true")
        main(dry_run=parser.parse_args().dry_run)
    except Exception as exc:
        # Suppress exception strings/tracebacks: URLs can contain the DSN.
        print(f"::error::Price refresh monitoring failed ({type(exc).__name__}); inspect configuration and delivery", file=sys.stderr)
        raise SystemExit(1) from None
