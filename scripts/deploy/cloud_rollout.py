#!/usr/bin/env python3
"""Two-cloud release admission shared by control planes and enclave gateways.

The existing GCS mutex object becomes a generation-CAS journal, not two
independent locks. Old clients cannot parse it and therefore fail closed.
Expired/failed reservations are NEVER reclaimed just because time elapsed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

VERSION = 2
URI = "gs://tr-deploy-mutex-quill-cloud-proxy/locks/trusted-router-production.json"
CLOUDS = ("gcp", "aws", "azure")
PLANES = {
    "gcp": ("https://trustedrouter.com", "https://api.trustedrouter.com"),
    "aws": ("https://aws.trustedrouter.com", "https://api-aws.trustedrouter.com"),
    "azure": ("https://azure.trustedrouter.com", "https://api-azure.trustedrouter.com"),
}
SHA = re.compile(r"[a-f0-9]{7,40}")
MAX_BYTES = 1_048_576


class Refused(RuntimeError):
    """Unknown or unsafe evidence; do not mutate production."""


class Busy(Refused):
    """A live reservation conflicts with this request."""


class Conflict(RuntimeError):
    """Another writer changed the journal; reread, never overwrite."""


class Store(Protocol):
    def read(self) -> tuple[dict[str, Any] | None, int]: ...
    def write(self, record: dict[str, Any], generation: int) -> None: ...


class GCSStore:
    def __init__(self) -> None:
        self.gcloud = shutil.which("gcloud")
        if not self.gcloud:
            raise Refused("gcloud is required for the shared release journal")

    def verify_retention(self) -> None:
        result = self.command("buckets", "describe", URI.rsplit("/locks/", 1)[0], "--format=json")
        if result.returncode:
            raise Refused("cannot verify release journal retention")
        bucket = json.loads(result.stdout)
        if not isinstance(bucket, dict) or not bucket:
            raise Refused("invalid release journal bucket evidence")
        for field in ("lifecycle", "lifecycle_config"):
            lifecycle = bucket.get(field) or {}
            if not isinstance(lifecycle, dict) or lifecycle.get("rule"):
                raise Refused("remove journal bucket lifecycle rules before enabling two-cloud rollouts")

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603 - fixed CLI and GCS object; no shell
                [str(self.gcloud), "storage", *args], capture_output=True,
                text=True, timeout=45, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused("release journal operation failed; reservation may remain") from exc

    def read(self) -> tuple[dict[str, Any] | None, int]:
        result = self.command("objects", "describe", URI, "--format=value(generation)")
        if result.returncode:
            if re.search(r"\b404\b|not found|No URLs matched", result.stderr, re.IGNORECASE):
                return None, 0
            raise Refused("release journal unreadable; not treated as unlocked")
        if not result.stdout.strip().isdigit() or int(result.stdout.strip()) <= 0:
            raise Refused("invalid journal generation")
        generation = int(result.stdout.strip())
        with tempfile.TemporaryDirectory(prefix="tr-rollout-") as directory:
            path = Path(directory) / "state.json"
            result = self.command("cp", f"{URI}#{generation}", str(path))
            if result.returncode:
                raise Conflict("journal changed or its pinned generation is unavailable")
            if path.stat().st_size > MAX_BYTES:
                raise Refused("oversized release journal")
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise Refused("invalid release journal JSON") from exc
        if not isinstance(record, dict):
            raise Refused("invalid release journal object")
        return record, generation

    def write(self, record: dict[str, Any], generation: int) -> None:
        with tempfile.TemporaryDirectory(prefix="tr-rollout-") as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            result = self.command("cp", str(path), URI, f"--if-generation-match={generation}")
        if result.returncode:
            if re.search(r"\b412\b|precondition|conditionNotMet", result.stderr, re.IGNORECASE):
                raise Conflict("journal compare-and-swap lost")
            raise Refused("journal write failed or ambiguous; never delete on ambiguity")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any,
                         headers: Any, newurl: Any) -> None:
        raise Refused("cloud-local health must not redirect to another cloud")


def fetch(url: str) -> dict[str, Any]:
    if not url.startswith("https://"):
        raise Refused("HTTPS required")
    request = urllib.request.Request(  # noqa: S310 - fixed HTTPS origins in PLANES
        f"{url}?rollout_check={uuid.uuid4().hex}",
        headers={"Accept": "application/json", "Cache-Control": "no-cache",
                 "User-Agent": "TrustedRouter-Release-Coordinator/2"},
    )
    for attempt in range(3):
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise Refused("oversized cloud health response")
                payload = json.loads(raw)
            break
        except OSError as exc:
            delay = 2 ** attempt
            if isinstance(exc, urllib.error.HTTPError):
                if exc.code not in {429, 502, 503, 504}:
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after is not None:
                    # Long or date-form retry windows fail closed; never retry
                    # earlier than the server asked or hold a job indefinitely.
                    if not retry_after.isdigit() or int(retry_after) > 5:
                        raise
                    delay = max(delay, int(retry_after))
            if attempt == 2:
                raise
            time.sleep(delay)
    if not isinstance(payload, dict):
        raise Refused("invalid cloud health response")
    return payload


def probe_cloud(cloud: str) -> str:
    """Check both cloud-local services, never the global blended status page.

    Regional readiness, billing and attestation proofs remain the deployment
    workflow's completion gates. This is a fresh additional admission check,
    not a substitute for those gates or proof of an inference request.
    """
    plane, gateway = PLANES[cloud]
    try:
        before = fetch(f"{plane}/trust/control-plane.json")
        if fetch(f"{plane}/health").get("status") != "ok":
            raise Refused("control plane unhealthy")
        if fetch(f"{gateway}/health").get("status") != "ok":
            raise Refused("gateway unhealthy")
        after = fetch(f"{plane}/trust/control-plane.json")
        release = before.get("release")
        api_base = before.get("api_base_url")
        if (before != after or not isinstance(release, str)
                or not SHA.fullmatch(release)
                or not isinstance(api_base, str) or api_base.rstrip("/") != f"{gateway}/v1"):
            raise Refused("cloud-local release evidence missing, changing, or wrong cloud")
        return release
    except (OSError, ValueError, Refused) as exc:
        raise Refused(f"{cloud} is not a verified healthy holdback") from exc


def state_record(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {"schema_version": VERSION, "leases": {}, "holdback": None}
    if raw.get("schema_version") != VERSION:
        raise Busy("legacy or unknown deployment lock exists; wait for its owner, do not delete it")
    leases = raw.get("leases")
    if not isinstance(leases, dict) or len(leases) > 2 or not set(leases) <= set(CLOUDS):
        raise Refused("invalid release journal leases")
    for cloud, lease in leases.items():
        if (not isinstance(lease, dict) or lease.get("cloud") != cloud
                or lease.get("state") not in {"active", "blocked"}
                or not isinstance(lease.get("operation_id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", lease["operation_id"])
                or not isinstance(lease.get("owner"), str)
                or type(lease.get("created_at")) is not int
                or type(lease.get("expires_at")) is not int
                or lease["expires_at"] <= lease["created_at"]):
            raise Refused("invalid release lease")
    holdback = raw.get("holdback")
    if leases:
        if (not isinstance(holdback, dict) or holdback.get("cloud") not in CLOUDS
                or holdback["cloud"] in leases
                or not isinstance(holdback.get("release"), str)
                or not SHA.fullmatch(holdback["release"])):
            raise Refused("invalid protected cloud")
    elif holdback is not None:
        raise Refused("holdback without a release")
    return copy.deepcopy(raw)


class Coordinator:
    def __init__(self, store: Store, probe: Callable[[str], str] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.store, self.probe, self.now = store, probe or probe_cloud, now

    def transaction(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        for _ in range(8):
            try:
                raw, generation = self.store.read()
                state = state_record(raw)
                result = change(state)
                self.store.write(state, generation)
                return result
            except Conflict:
                continue
        raise Refused("release journal contention; no unguarded deployment permitted")

    def holdback(self, excluded: set[str], previous: dict[str, Any] | None) -> dict[str, Any]:
        candidates = [c for c in CLOUDS if c not in excluded]
        if previous and previous["cloud"] in candidates:
            candidates.remove(previous["cloud"])
            candidates.insert(0, previous["cloud"])
        for cloud in candidates:
            try:
                release = self.probe(cloud)
                if previous and cloud == previous["cloud"] and release != previous["release"]:
                    raise Refused("protected cloud changed outside the rollout guard")
                return {"cloud": cloud, "release": release, "checked_at": int(self.now())}
            except Refused:
                continue
        raise Refused("no unchanged healthy cloud remains outside this rollout")

    def acquire(self, cloud: str, owner: str, component: str, ttl: int,
                operation: str | None = None) -> dict[str, Any]:
        if cloud not in CLOUDS or component not in {"control-plane", "gateway", "public-surface"}:
            raise Refused("invalid cloud or component")
        if not owner or len(owner) > 1024 or any(ord(c) < 32 for c in owner):
            raise Refused("invalid owner")
        if not 60 <= ttl <= 43_200:
            raise Refused("lease duration must be 60..43200 seconds")
        operation = operation or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", operation):
            raise Refused("invalid operation fence")

        def change(state: dict[str, Any]) -> dict[str, Any]:
            leases = state["leases"]
            if cloud in leases:
                raise Busy(f"{cloud} already reserved by {leases[cloud]['owner']}")
            if len(leases) >= 2:
                raise Busy("two clouds already reserved; the third is protected")
            protected = self.holdback(set(leases) | {cloud}, state["holdback"])
            now = int(self.now())
            lease = {"cloud": cloud, "owner": owner, "component": component,
                     "operation_id": operation, "state": "active", "created_at": now,
                     "expires_at": now + ttl, "host": socket.gethostname(),
                     # The short-lived acquire/bootstrap process is not the
                     # deploy owner. Unknown manual owners are unrecoverable
                     # automatically, rather than falsely considered stopped.
                     "pid": int(os.environ.get("TR_DEPLOY_OWNER_PID", "0"))}
            leases[cloud] = lease
            state["holdback"] = protected
            return lease

        return self.transaction(change)

    def check(self, cloud: str, operation: str) -> dict[str, Any]:
        for _ in range(8):
            try:
                raw, generation = self.store.read()
                state = state_record(raw)
                lease = self.owned(state, cloud, operation)
                if lease["state"] != "active" or lease["expires_at"] <= self.now():
                    raise Refused("lease blocked or expired; recovery required, slot remains reserved")
                protected = state["holdback"]
                if self.probe(protected["cloud"]) != protected["release"]:
                    raise Refused("protected cloud release changed")
                _, current = self.store.read()
                if current == generation:
                    return lease
            except Conflict:
                continue
        raise Refused("release journal changed during guard verification")

    @staticmethod
    def owned(state: dict[str, Any], cloud: str, operation: str) -> dict[str, Any]:
        lease = state["leases"].get(cloud)
        if not operation or not lease or lease["operation_id"] != operation:
            raise Refused("reservation not owned by this operation")
        return lease

    def release(self, cloud: str, operation: str, success: bool) -> None:
        def change(state: dict[str, Any]) -> None:
            lease = self.owned(state, cloud, operation)
            if not success:
                lease["state"] = "blocked"
                return
            if lease["state"] != "active":
                raise Refused("failed reservation requires verified recovery")
            self.probe(cloud)
            del state["leases"][cloud]
            if not state["leases"]:
                state["holdback"] = None

        self.transaction(change)

    def recover(self, cloud: str, operation: str,
                stopped: Callable[[dict[str, Any]], bool]) -> None:
        def change(state: dict[str, Any]) -> None:
            lease = self.owned(state, cloud, operation)
            if not stopped(lease):
                raise Refused("owner may still be mutating production; recovery refused")
            self.probe(cloud)
            del state["leases"][cloud]
            if not state["leases"]:
                state["holdback"] = None

        self.transaction(change)


def owner_stopped(lease: dict[str, Any]) -> bool:
    match = re.fullmatch(r"https://github.com/(Lore-Hex/(?:quill-router|quill-cloud-proxy))/actions/runs/([0-9]+)",
                         lease["owner"])
    if match:
        gh = shutil.which("gh")
        if not gh:
            return False
        result = subprocess.run(  # noqa: S603 - allowlisted repository/run ID; read only
            [gh, "run", "view", match[2], "--repo", match[1], "--json", "status"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return result.returncode == 0 and json.loads(result.stdout).get("status") == "completed"
    if (lease.get("host") != socket.gethostname() or type(lease.get("pid")) is not int
            or lease["pid"] <= 1):
        return False
    try:
        os.kill(lease["pid"], 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    return False


def emit(lease: dict[str, Any]) -> None:
    from datetime import UTC, datetime

    print(f"TR_DEPLOY_MUTEX_OPERATION={lease['operation_id']}")
    # Never expose a current GCS generation as the old shell client's delete
    # fence. Generation 0 CANNOT delete an existing object, including peers.
    print("TR_DEPLOY_MUTEX_GENERATION=0")
    print("TR_DEPLOY_MUTEX_CREATED_AT=" + datetime.fromtimestamp(lease["created_at"], UTC).isoformat())
    print(f"TR_DEPLOY_MUTEX_CLOUD={lease['cloud']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("acquire", "assert", "release", "status", "recover", "version"))
    parser.add_argument("--cloud", choices=CLOUDS, default=os.environ.get("TR_DEPLOY_MUTEX_CLOUD", "gcp"))
    parser.add_argument("--operation", default=os.environ.get("TR_DEPLOY_MUTEX_OPERATION", ""))
    parser.add_argument("--outcome", choices=("success", "failure"),
                        default=os.environ.get("TR_DEPLOY_OUTCOME", "success"))
    args = parser.parse_args()
    if args.command == "version":
        print(VERSION)
        return 0
    try:
        store = GCSStore()
        if args.command != "status":
            store.verify_retention()
        coordinator = Coordinator(store)
        if args.command == "acquire":
            if args.operation:
                emit(coordinator.check(args.cloud, args.operation))
            else:
                wait = int(os.environ.get("TR_DEPLOY_WAIT_SECONDS", "0"))
                if not 0 <= wait <= 7200:
                    raise Refused("admission wait must be 0..7200 seconds")
                deadline = time.monotonic() + wait
                while True:
                    try:
                        lease = coordinator.acquire(
                            args.cloud, os.environ.get("TR_DEPLOY_MUTEX_OWNER", f"manual:{socket.gethostname()}"),
                            os.environ.get("TR_DEPLOY_COMPONENT", "control-plane"),
                            int(os.environ.get("TR_DEPLOY_MUTEX_TTL_SECONDS", "14400")),
                        )
                        emit(lease)
                        break
                    except Busy:
                        if time.monotonic() >= deadline:
                            raise
                        print("cloud_rollout.waiting: cloud capacity reserved", file=sys.stderr)
                        time.sleep(min(20, max(0, deadline - time.monotonic())))
        elif args.command == "assert":
            coordinator.check(args.cloud, args.operation)
        elif args.command == "release":
            if args.operation:
                coordinator.release(args.cloud, args.operation, args.outcome == "success")
        elif args.command == "recover":
            coordinator.recover(args.cloud, args.operation, owner_stopped)
        else:
            raw, generation = coordinator.store.read()
            print(json.dumps({"generation": generation, "state": raw}, indent=2, sort_keys=True))
    except (Refused, Conflict, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"cloud_rollout.refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
