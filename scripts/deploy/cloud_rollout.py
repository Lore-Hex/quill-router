#!/usr/bin/env python3
"""Two-cloud release admission shared by control planes and enclave gateways.

The existing GCS mutex object becomes a generation-CAS journal, not two
independent locks. Old clients cannot parse it and therefore fail closed.
Expired/failed reservations are NEVER reclaimed just because time elapsed.

Waiters queue in the same journal and are admitted first come, first served
across both repositories (docs/design/deploy-fifo-admission.md). A queue
ticket confers no right to mutate production; only a lease does.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import signal
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
COMPONENTS = ("control-plane", "gateway", "public-surface")
TICKET = re.compile(r"[a-f0-9]{32}")
# Admission queue. A waiting client polls every 20 s and rewrites its heartbeat
# at most every HEARTBEAT_SECONDS. Hosts' clocks can disagree, so no decision
# that can hurt a live waiter compares one host's clock with another's:
#   * Skipping a waiter for ordering needs the heartbeat to look older than
#     WAITER_STALE_SECONDS AND to have stayed unchanged for
#     STALE_CONFIRM_SECONDS on the observer's own clock (longer than one
#     heartbeat interval plus a poll), so a live waiter is never skipped.
#   * Deleting one needs its owner run completed, or its heartbeat unchanged
#     for OBSERVED_DEAD_SECONDS on the observer's own clock, or its declared
#     deadline exceeded by WAITER_ABANDONED_SECONDS (a day; no clock is off
#     by that much).
#   * A single attempt with no observation history (no wait) treats a waiter
#     as live until its deadline plus WAITER_EXPIRY_GRACE: skew there can only
#     make that caller wait, never drop anyone.
MAX_QUEUE = 32
HEARTBEAT_SECONDS = 120
WAITER_STALE_SECONDS = 600
STALE_CONFIRM_SECONDS = 150
OBSERVED_DEAD_SECONDS = 900
WAITER_EXPIRY_GRACE = 1800
WAITER_ABANDONED_SECONDS = 86_400
# The client stops polling at its wait deadline; the entry outlives that by
# this margin so a final attempt is never refused as already expired.
WAITER_DEADLINE_MARGIN = 120
# A repository name can be re-created after a rename; its immutable id cannot.
GITHUB_REPOSITORY_IDS = {
    "Lore-Hex/quill-router": 1227431177,
    "Lore-Hex/quill-cloud-proxy": 1223401116,
}


class Refused(RuntimeError):
    """Unknown or unsafe evidence; do not mutate production."""


class Busy(Refused):
    """A live reservation conflicts with this request."""


class Conflict(RuntimeError):
    """Another writer changed the journal; reread, never overwrite."""


class _Unchanged(Exception):  # noqa: N818 - control flow, not an error
    """A transaction decided without needing a journal write."""

    def __init__(self, value: Any) -> None:
        super().__init__()
        self.value = value


class Waiting:
    """A queued waiter that is not yet admissible, and why."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


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
    state = copy.deepcopy(raw)
    set_queue(state, sanitized_queue(raw, leases))
    return state


def _int(value: Any) -> bool:
    return type(value) is int


def valid_waiter(entry: Any) -> bool:
    return (isinstance(entry, dict)
            and isinstance(entry.get("ticket"), str) and TICKET.fullmatch(entry["ticket"]) is not None
            and entry.get("cloud") in CLOUDS
            and entry.get("component") in COMPONENTS
            and isinstance(entry.get("owner"), str) and valid_owner(entry["owner"])
            and all(_int(entry.get(k)) for k in ("enqueued_at", "heartbeat_at", "expires_at", "pid"))
            and entry["enqueued_at"] <= entry["heartbeat_at"] < entry["expires_at"]
            and isinstance(entry.get("host"), str))


def sanitized_queue(raw: dict[str, Any], leases: dict[str, Any]) -> list[dict[str, Any]]:
    """The optional FIFO queue, with anything unusable dropped.

    A queue entry confers no right to mutate production, so a damaged queue
    must never block a lease check, release or recovery. Malformed entries,
    repeated tickets (a write that landed and was then retried), tickets equal
    to a lease fence (an admission that landed) and entries beyond MAX_QUEUE
    are ignored with a warning; the next fenced write drops them for good.
    Leases are still validated strictly by state_record.
    """
    queue = raw.get("queue", [])
    if not isinstance(queue, list):
        print("cloud_rollout.queue_ignored: queue is not a list", file=sys.stderr)
        return []
    fences = {lease["operation_id"] for lease in leases.values()}
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    dropped = 0
    for entry in queue:
        if (not valid_waiter(entry) or entry["ticket"] in seen or entry["ticket"] in fences
                or len(kept) >= MAX_QUEUE):
            dropped += 1
            continue
        seen.add(entry["ticket"])
        kept.append(entry)
    if dropped:
        print(f"cloud_rollout.queue_ignored: {dropped} unusable or repeated waiter entr(ies)", file=sys.stderr)
    return kept


def valid_owner(owner: str) -> bool:
    return bool(owner) and len(owner) <= 1024 and not any(ord(c) < 32 for c in owner)


def queue_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    return state.get("queue", [])


def set_queue(state: dict[str, Any], queue: list[dict[str, Any]]) -> None:
    # An empty queue is omitted: the record stays byte-for-byte the v2 format.
    if queue:
        state["queue"] = queue
    else:
        state.pop("queue", None)


def redacted(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """The journal for `status` output, with waiter tickets shortened.

    Lease fences stay whole: an operator needs them for recovery, and they are
    not secrets (anyone who can read the journal sees them, and using one needs
    write access to it). Tickets are never needed by an operator.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("queue"), list):
        return raw
    shown = copy.deepcopy(raw)
    for entry in shown["queue"]:
        if isinstance(entry, dict) and isinstance(entry.get("ticket"), str):
            entry["ticket"] = entry["ticket"][:6] + "..."
    return shown


def admission_blocker(state: dict[str, Any], cloud: str, ahead: list[dict[str, Any]]) -> str | None:
    """Why a request for `cloud` behind the live waiters `ahead` must wait, or None."""
    leases = state["leases"]
    if cloud in leases:
        return f"{cloud} already reserved by {leases[cloud]['owner']}"
    if len(leases) >= 2:
        return "two clouds already reserved; the third is protected"
    earlier = [e for e in ahead if e["cloud"] == cloud]
    if earlier:
        return f"{len(earlier)} earlier waiter(s) for {cloud}, first {earlier[0]['owner']}"
    # Earlier waiters for other free clouds take the remaining slots first.
    others = {e["cloud"] for e in ahead if e["cloud"] not in leases}
    if len(others) >= 2 - len(leases):
        return "earlier waiters for " + ", ".join(sorted(others)) + " take the free slot(s) first"
    return None


class Coordinator:
    def __init__(self, store: Store, probe: Callable[[str], str] | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.store, self.probe, self.now = store, probe or probe_cloud, now
        # ticket -> (heartbeat_at last seen, this host's time it was first seen)
        self.observed: dict[str, tuple[int, float]] = {}

    def observe(self, queue: list[dict[str, Any]], now: float) -> None:
        for entry in queue:
            seen = self.observed.get(entry["ticket"])
            if seen is None or seen[0] != entry["heartbeat_at"]:
                self.observed[entry["ticket"]] = (entry["heartbeat_at"], now)

    def unchanged_for(self, entry: dict[str, Any], now: float) -> float:
        """How long this host has seen this exact heartbeat, on its own clock."""
        seen = self.observed.get(entry["ticket"])
        if seen is None or seen[0] != entry["heartbeat_at"]:
            return 0.0
        return now - seen[1]

    def live(self, entry: dict[str, Any], now: float, *, history: bool = True) -> bool:
        """Whether a waiter still holds its place in line."""
        if now - entry["heartbeat_at"] < WAITER_STALE_SECONDS and now < entry["expires_at"]:
            return True
        if not history:
            return now < entry["expires_at"] + WAITER_EXPIRY_GRACE
        return self.unchanged_for(entry, now) < STALE_CONFIRM_SECONDS

    def expire(self, state: dict[str, Any], now: float) -> None:
        """Drop provably dead waiters. Only ever called inside a fenced write."""
        set_queue(state, [
            e for e in queue_of(state)
            if self.unchanged_for(e, now) < OBSERVED_DEAD_SECONDS
            and now < e["expires_at"] + WAITER_ABANDONED_SECONDS
        ])

    def transaction(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        for _ in range(8):
            try:
                raw, generation = self.store.read()
                state = state_record(raw)
                try:
                    result = change(state)
                except _Unchanged as unchanged:
                    return unchanged.value
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

    @staticmethod
    def validate_request(cloud: str, owner: str, component: str) -> None:
        if cloud not in CLOUDS or component not in COMPONENTS:
            raise Refused("invalid cloud or component")
        if not valid_owner(owner):
            raise Refused("invalid owner")

    @staticmethod
    def validate_ttl(ttl: int) -> None:
        if not 60 <= ttl <= 43_200:
            raise Refused("lease duration must be 60..43200 seconds")

    def grant(self, state: dict[str, Any], cloud: str, owner: str, component: str,
              ttl: int, operation: str) -> dict[str, Any]:
        """Write a lease into `state` (inside a transaction) once admission is decided."""
        leases = state["leases"]
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

    def acquire(self, cloud: str, owner: str, component: str, ttl: int,
                operation: str | None = None) -> dict[str, Any]:
        """Admit immediately, or raise Busy. Never jumps a live queued waiter."""
        self.validate_request(cloud, owner, component)
        self.validate_ttl(ttl)
        operation = operation or uuid.uuid4().hex
        if not TICKET.fullmatch(operation):
            raise Refused("invalid operation fence")

        def change(state: dict[str, Any]) -> dict[str, Any]:
            now = self.now()
            ahead = [e for e in queue_of(state) if self.live(e, now, history=False)]
            reason = admission_blocker(state, cloud, ahead)
            if reason:
                raise Busy(reason)
            if any(e["ticket"] == operation for e in queue_of(state)):
                raise Refused("operation fence is already a queued ticket")
            return self.grant(state, cloud, owner, component, ttl, operation)

        return self.transaction(change)

    def enqueue(self, cloud: str, owner: str, component: str, wait: int,
                ticket: str | None = None) -> str:
        """Join the back of the shared admission queue; return the ticket.

        Idempotent per ticket: a write that landed but reported a conflict is
        retried by rereading, finds the ticket queued, and does not add it again.
        """
        self.validate_request(cloud, owner, component)
        if not 0 <= wait <= MAX_ADMISSION_WAIT_SECONDS:
            raise Refused(f"admission wait must be 0..{MAX_ADMISSION_WAIT_SECONDS} seconds")
        ticket = ticket or uuid.uuid4().hex
        if not TICKET.fullmatch(ticket):
            raise Refused("invalid waiter ticket")

        def change(state: dict[str, Any]) -> str:
            if any(e["ticket"] == ticket for e in queue_of(state)):
                raise _Unchanged(ticket)
            if any(lease["operation_id"] == ticket for lease in state["leases"].values()):
                raise Refused("waiter ticket is already a lease fence")
            now = int(self.now())
            self.observe(queue_of(state), now)
            self.expire(state, now)
            queue = queue_of(state)
            if len(queue) >= MAX_QUEUE:
                raise Refused("release admission queue is full")
            queue.append({"ticket": ticket, "cloud": cloud, "owner": owner,
                          "component": component, "enqueued_at": now, "heartbeat_at": now,
                          "expires_at": now + wait + WAITER_DEADLINE_MARGIN,
                          "host": socket.gethostname(),
                          "pid": int(os.environ.get("TR_DEPLOY_OWNER_PID", "0"))})
            set_queue(state, queue)
            return ticket

        return self.transaction(change)

    def try_admit(self, ticket: str, ttl: int) -> dict[str, Any]:
        """Convert this ticket into a lease when it is first in line, else raise Busy.

        Admission removes the ticket and writes the lease in one CAS write, so
        a ticket is never both queued and leased. The lease's operation fence
        is the ticket.
        """
        self.validate_ttl(ttl)

        def change(state: dict[str, Any]) -> dict[str, Any] | Waiting:
            # An admission write that landed but reported a conflict left the
            # lease, fenced by this ticket, in place: that lease is ours.
            for lease in state["leases"].values():
                if lease["operation_id"] == ticket:
                    raise _Unchanged(lease)
            queue = queue_of(state)
            index = next((i for i, e in enumerate(queue) if e["ticket"] == ticket), None)
            if index is None:
                raise Refused("waiter ticket is no longer queued; not admitted out of order")
            entry = queue[index]
            now = self.now()
            if now >= entry["expires_at"]:
                raise Refused("waiter deadline passed; not admitted")
            self.observe(queue, now)
            ahead = [e for e in queue[:index] if self.live(e, now)]
            reason = admission_blocker(state, entry["cloud"], ahead)
            if reason is None:
                lease = self.grant(state, entry["cloud"], entry["owner"], entry["component"], ttl, ticket)
                del queue[index]
                self.expire(state, now)
                return lease
            if now - entry["heartbeat_at"] < HEARTBEAT_SECONDS:
                raise _Unchanged(Waiting(reason))
            entry["heartbeat_at"] = int(now)
            self.expire(state, now)
            return Waiting(reason)

        result = self.transaction(change)
        if isinstance(result, Waiting):
            raise Busy(result.reason)
        return result

    def dequeue(self, ticket: str) -> None:
        """Remove only this waiter's own ticket. Never touches a lease."""
        def change(state: dict[str, Any]) -> None:
            queue = queue_of(state)
            remaining = [e for e in queue if e["ticket"] != ticket]
            if len(remaining) == len(queue):
                raise _Unchanged(None)
            set_queue(state, remaining)

        self.transaction(change)

    def prune(self, stopped: Callable[[dict[str, Any]], bool]) -> int:
        """Remove stale waiters whose owner is proven stopped.

        The owner check runs outside the transaction. Each removal is fenced by
        the exact (ticket, heartbeat_at) observed when it was checked and by
        the journal generation, so a waiter that heartbeats meanwhile stays.
        """
        raw, _ = self.store.read()
        now = self.now()
        stale = [e for e in queue_of(state_record(raw)) if now - e["heartbeat_at"] >= WAITER_STALE_SECONDS]
        dead = {(e["ticket"], e["heartbeat_at"]) for e in stale if stopped(e)}
        if not dead:
            return 0

        def change(state: dict[str, Any]) -> int:
            queue = queue_of(state)
            remaining = [e for e in queue if (e["ticket"], e["heartbeat_at"]) not in dead]
            if len(remaining) == len(queue):
                raise _Unchanged(0)
            set_queue(state, remaining)
            return len(queue) - len(remaining)

        return self.transaction(change)

    @staticmethod
    def guarded(state: dict[str, Any]) -> tuple[Any, Any]:
        """What a lease check depends on. The waiter queue is not part of it."""
        return state["leases"], state["holdback"]

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
                current_raw, current = self.store.read()
                # Waiter heartbeats move the generation without touching any
                # lease or the holdback; only those must be unchanged.
                if current == generation or self.guarded(state_record(current_raw)) == self.guarded(state):
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


# A control-plane deploy waits behind a gateway rollout, which holds the gcp
# lease for about 2 h 20 min; deploy.yml waits up to 3 h.
MAX_ADMISSION_WAIT_SECONDS = 4 * 3600


def admission_wait(environ: Any) -> int:
    """TR_DEPLOY_WAIT_SECONDS, validated: how long acquire may wait for a lease."""
    wait = int(environ.get("TR_DEPLOY_WAIT_SECONDS", "0"))
    if not 0 <= wait <= MAX_ADMISSION_WAIT_SECONDS:
        raise Refused(f"admission wait must be 0..{MAX_ADMISSION_WAIT_SECONDS} seconds")
    return wait


def _owner_check_refused(reason: str) -> bool:
    # Recovery is an operator action; a bare refusal leaves them guessing whether to
    # fix the gh credential or wait for the lease TTL, so every refusal names its cause.
    print(f"cloud_rollout.owner_check: {reason}", file=sys.stderr)
    return False


def _github_api(gh: str, path: str, env: dict[str, str]) -> tuple[int, Any]:
    result = subprocess.run(  # noqa: S603 - allowlisted repository/run ID; read only
        [gh, "api", "--hostname", "github.com", path],
        capture_output=True, text=True, timeout=30, check=False, env=env,
    )
    return result.returncode, json.loads(result.stdout)


def _github_failure(path: str, code: int, body: Any) -> str:
    status = body.get("status") if isinstance(body, dict) else None
    detail = f"{path}: exit {code}, status={status!r}"
    if str(status) in {"401", "403", "404"}:
        return (f"{detail}; cannot verify repository/Actions access;"
                " give gh a credential with actions:read and retry")
    return f"GitHub API failure ({detail}); retry later"


def _actions_readable(gh: str, repo: str, run_id: str, env: dict[str, str]) -> str | None:
    path = f"repos/{repo}"
    try:
        code, body = _github_api(gh, path, env)
        if code != 0 or not isinstance(body, dict):
            return _github_failure(path, code, body)
        repository_id = body.get("id")
        if type(repository_id) is not int or repository_id != GITHUB_REPOSITORY_IDS[repo]:
            return (f"https://github.com/{repo} now resolves to a different repository"
                    f" (id {repository_id!r}), not the allowlisted one")
        path = f"repos/{repo}/actions/runs?per_page=100"
        code, body = _github_api(gh, path, env)
        if (code != 0 or not isinstance(body, dict)
                or type(body.get("total_count")) is not int
                or not isinstance(body.get("workflow_runs"), list)):
            return _github_failure(path, code, body)
        for run in body["workflow_runs"]:
            if isinstance(run, dict) and run.get("id") == int(run_id):
                return (f"{path}: listing still shows run {run_id}"
                        f" (status {run.get('status')!r}); not deleted")
        # Run ids are global: the URL must not pair a live run with the wrong repo.
        for other in GITHUB_REPOSITORY_IDS:
            if other == repo:
                continue
            path = f"repos/{other}/actions/runs/{run_id}"
            code, body = _github_api(gh, path, env)
            if code == 0:
                status = body.get("status") if isinstance(body, dict) else None
                return f"run {run_id} exists in {other} with status {status!r}"
            if not isinstance(body, dict) or body.get("status") != "404":
                return _github_failure(path, code, body)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"GitHub API failure ({path}: {type(exc).__name__}: {exc}); retry later"
    return None


def owner_stopped(lease: dict[str, Any], now: Callable[[], float] = time.time) -> bool:
    match = re.fullmatch(r"https://github.com/(Lore-Hex/(?:quill-router|quill-cloud-proxy))/actions/runs/([0-9]+)",
                         lease["owner"])
    if match:
        gh = shutil.which("gh")
        if not gh:
            return _owner_check_refused("gh is not installed; the owner run cannot be inspected")
        run_ref = f"repos/{match[1]}/actions/runs/{match[2]}"
        try:
            credential = subprocess.run(  # noqa: S603 - fixed CLI credential lookup; captured only
                [gh, "auth", "token", "--hostname", "github.com"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            token = credential.stdout.strip()
            if credential.returncode != 0 or not token:
                return _owner_check_refused(
                    "gh has no github.com credential; run `gh auth login` (or set GH_TOKEN) and retry")
            # Bind the client credential once so concurrent gh auth switches cannot
            # change later calls. Server-side permission flapping three times within
            # one recovery is out of scope; allowlisted workflows are trusted lease writers.
            env = {**os.environ, "GH_TOKEN": token}
            code, body = _github_api(gh, run_ref, env)
            if not isinstance(body, dict):
                return _owner_check_refused(_github_failure(run_ref, code, body))
            if code == 0:
                status = body.get("status")
                if status == "completed":
                    return True
                return _owner_check_refused(
                    f"{run_ref}: status={status!r}; the owner run has not completed")
            # Require the exact run endpoint's structured 404; unauthorized reads
            # look like deletion too, so this evidence alone is insufficient.
            if body.get("status") != "404":
                return _owner_check_refused(_github_failure(run_ref, code, body))
            # Fence expiry before any further network call, even for deleted runs.
            expires = lease.get("expires_at")
            checked_at = int(now())
            if type(expires) is not int or expires >= checked_at:
                return _owner_check_refused(
                    f"{run_ref}: lease expires_at={expires!r} has not elapsed"
                    f" (now={checked_at}); wait for the TTL")
            reason = _actions_readable(gh, match[1], match[2], env)
            if reason is not None:
                return _owner_check_refused(reason)
            # Sandwich a fresh 404 between fresh identity/access probes. Do not
            # cache evidence: server-side permissions can change.
            code, body = _github_api(gh, run_ref, env)
            if code == 0:
                status = body.get("status") if isinstance(body, dict) else None
                return _owner_check_refused(
                    f"run {match[2]} reappeared with status {status!r}")
            if not isinstance(body, dict) or body.get("status") != "404":
                return _owner_check_refused(_github_failure(run_ref, code, body))
            reason = _actions_readable(gh, match[1], match[2], env)
            if reason is not None:
                return _owner_check_refused(reason)
            return True
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return _owner_check_refused(
                f"GitHub API failure ({run_ref}: {type(exc).__name__}: {exc}); retry later")
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


PRUNE_INTERVAL_SECONDS = 300


def acquire_in_order(coordinator: Coordinator, cloud: str, owner: str, component: str,
                     ttl: int, wait: int,
                     stopped: Callable[[dict[str, Any]], bool] = owner_stopped,
                     sleep: Callable[[float], None] = time.sleep,
                     monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Wait in the shared FIFO queue for up to `wait` seconds, then hold a lease.

    With no wait there is a single attempt, which still never jumps a live
    waiter. A waiter that gives up removes only its own ticket; if even that
    fails, the ticket goes stale (skipped) and is later pruned with proof.
    """
    if wait == 0:
        return coordinator.acquire(cloud, owner, component, ttl)
    coordinator.validate_ttl(ttl)
    deadline = monotonic() + wait
    # Chosen before the first write, so even an ambiguous enqueue is undone by
    # the finally block below instead of leaving an orphan in line.
    ticket = uuid.uuid4().hex
    admitted = False
    next_prune = monotonic() + PRUNE_INTERVAL_SECONDS
    try:
        coordinator.enqueue(cloud, owner, component, wait, ticket=ticket)
        print(f"cloud_rollout.queued: waiting for {cloud}", file=sys.stderr)
        while True:
            try:
                lease = coordinator.try_admit(ticket, ttl)
                admitted = True
                return lease
            except Busy as exc:
                if monotonic() >= deadline:
                    raise
                print(f"cloud_rollout.waiting: {exc}", file=sys.stderr)
            if monotonic() >= next_prune:
                next_prune = monotonic() + PRUNE_INTERVAL_SECONDS
                try:
                    removed = coordinator.prune(stopped)
                    if removed:
                        print(f"cloud_rollout.pruned: {removed} stopped waiter(s)", file=sys.stderr)
                except (Refused, Conflict) as exc:
                    print(f"cloud_rollout.prune_skipped: {exc}", file=sys.stderr)
            sleep(min(20, max(0, deadline - monotonic())))
    finally:
        if not admitted:
            try:
                coordinator.dequeue(ticket)
            except (Refused, Conflict, OSError, subprocess.SubprocessError) as exc:
                print(f"cloud_rollout.dequeue_skipped: {exc}; the ticket will go stale", file=sys.stderr)


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
            # A cancelled job runs the finally block that removes its own ticket.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            if args.operation:
                emit(coordinator.check(args.cloud, args.operation))
            else:
                emit(acquire_in_order(
                    coordinator, args.cloud,
                    os.environ.get("TR_DEPLOY_MUTEX_OWNER", f"manual:{socket.gethostname()}"),
                    os.environ.get("TR_DEPLOY_COMPONENT", "control-plane"),
                    int(os.environ.get("TR_DEPLOY_MUTEX_TTL_SECONDS", "14400")),
                    admission_wait(os.environ),
                ))
        elif args.command == "assert":
            coordinator.check(args.cloud, args.operation)
        elif args.command == "release":
            if args.operation:
                coordinator.release(args.cloud, args.operation, args.outcome == "success")
        elif args.command == "recover":
            coordinator.recover(args.cloud, args.operation, owner_stopped)
        else:
            raw, generation = coordinator.store.read()
            print(json.dumps({"generation": generation, "state": redacted(raw)}, indent=2, sort_keys=True))
    except (Refused, Conflict, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"cloud_rollout.refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
