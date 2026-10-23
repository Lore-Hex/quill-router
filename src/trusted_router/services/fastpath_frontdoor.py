"""The fast path's front door, as the control plane calls it for the pilot.

`docs/design/fast-admission-pilot.md`: for a workspace in the pilot set, from
a control plane in a pilot region, an authorize is admitted by the fast path
(`fastpath/internal/frontdoor`, `POST /v1/authorize`) under a lease instead
of a credit reservation, and the authorization's terminal is forwarded there
(`/v1/settle`, `/v1/refund`). Everything else about the request stays on
today's path: the key, the model, the estimate, the authorization row and the
generation's record are Python's.

The front door answers every handled request 200 with the outcome in
`Status`. Three answers mean "take today's path" and are not errors:
`off` (the workspace is not on the fast path's own switch), `busy` (no lease
took it; the design says never 402 for this, and today's path reserves as it
always did) and `invalid` (a request no retry makes good). An unreachable
front door, a timeout or a body that does not parse count the same, and a
breaker keeps a failing front door from being tried on every request.

The front doors are the fleet's serving members (`tr_fastpath_member`), read
from Spanner and cached: a node replaced by a deploy changes address, and the
membership row is the only registry the fleet has.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger("trusted_router.fastpath")

# The fleet's serving front doors, each a host:port the control plane
# reaches inside the VPC.
FAST_PATH_FRONT_DOORS = (
    "/* fastpath_front_doors */ SELECT address FROM tr_fastpath_member "
    "WHERE state = 'serving' ORDER BY address"
)
FRONT_DOORS_TTL_SECONDS = 30.0
# Consecutive failures before a front door is skipped, and for how long.
BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 0.25
# How many front doors one call may try within its budget: the fleet has
# two, and a stale membership row must not spend the budget on a third.
DOORS_PER_CALL = 2
# Calls in flight at once; past it a call is today's path at once.
WORKERS = 64

ADMITTED = "admitted"
BUSY = "busy"
OFF = "off"
INVALID = "invalid"
WON = "won"
RECORDED = "recorded"
SETTLED = "settled"
FAILED = "failed"


@dataclass(frozen=True)
class Admitted:
    """An admitted authorize: the sealed envelope its terminal must carry,
    and the hold's end of life."""

    envelope: str
    end_of_life: datetime


@dataclass(frozen=True)
class Terminal:
    """A terminal's answer: `won` with the charge the owner decided,
    `recorded` (durable, the lease's order decides), or `settled` (the lease
    is closed; the disposition answers)."""

    status: str
    kind: str
    charge: int
    outcome: str
    cost: int
    cost_known: bool


class FrontDoorUnavailable(RuntimeError):
    """No front door answered a terminal. The caller keeps the terminal for
    a retry; a terminal is never dropped."""


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float = 0.0

    def is_open(self, now: float) -> bool:
        if self.failures < BREAKER_THRESHOLD:
            return False
        if now - self.opened_at >= BREAKER_COOLDOWN_SECONDS:
            self.failures = 0
            return False
        return True

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= BREAKER_THRESHOLD:
            self.opened_at = now

    def record_success(self) -> None:
        self.failures = 0


class SpannerFrontDoors:
    """The serving members' addresses, read from Spanner at most every
    FRONT_DOORS_TTL_SECONDS. An empty read keeps the last addresses: a
    membership read that fails must not turn the pilot off for a request,
    and must not turn it on either."""

    def __init__(self, database: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._database = database
        self._clock = clock
        self._lock = threading.Lock()
        self._addresses: tuple[str, ...] = ()
        self._read_at = float("-inf")

    def __call__(self, timeout_seconds: float | None = None) -> tuple[str, ...]:
        """The addresses, read within `timeout_seconds` when the cache is
        stale: the read is part of the caller's budget."""
        now = self._clock()
        with self._lock:
            if now - self._read_at < FRONT_DOORS_TTL_SECONDS:
                return self._addresses
            try:
                with self._database.snapshot() as snap:
                    rows = (
                        snap.execute_sql(FAST_PATH_FRONT_DOORS, timeout=timeout_seconds)
                        if timeout_seconds is not None
                        else snap.execute_sql(FAST_PATH_FRONT_DOORS)
                    )
                    found = tuple(str(row[0]) for row in rows)
            except Exception:  # noqa: BLE001 - a membership read is advisory; the request goes on
                logger.warning("fastpath front doors: membership read failed; keeping %d", len(self._addresses))
                self._read_at = now
                return self._addresses
            self._addresses = found
            self._read_at = now
            return found


class FrontDoorClient:
    """Calls the front doors in turn, with one breaker per address.

    `front_doors` yields the serving addresses; `timeout_seconds` is the
    whole budget of one call, spent inline on a customer request.
    """

    def __init__(
        self,
        front_doors: Callable[[float], Sequence[str]],
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._front_doors = front_doors
        self._budget = float(timeout_seconds)
        self._clock = clock
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport, follow_redirects=False)
        self._breakers: dict[str, _Breaker] = {}
        self._turn = 0
        self._lock = threading.Lock()
        # The whole call, the membership read included, runs on a thread of
        # its own so the caller's wait is bounded by the budget whatever the
        # SDK or the socket do; a call left behind ends on its own timeouts.
        # One permit per thread: a call with no permit is today's path at
        # once, and nothing is ever queued to start later with a budget of
        # its own.
        self._permits = threading.BoundedSemaphore(WORKERS)

    def close(self) -> None:
        self._client.close()

    # -- authorize --------------------------------------------------------

    def authorize(self, *, workspace_id: str, request: str, estimate: int, stream: bool, boot: bytes) -> Admitted | None:
        """An admission under a lease, or None: today's path. None covers
        `off`, `busy`, `invalid`, a front door that failed, and no front door
        at all; the reason is logged, never raised, since a request must not
        fail for a fast path that is not there."""
        if estimate < 0 or not boot or not workspace_id:
            return None
        body = {
            "Workspace": workspace_id,
            "Request": request,
            "Estimate": int(estimate),
            "Stream": bool(stream),
            "Boot": base64.b64encode(boot).decode("ascii"),
        }
        answer = self._call("/v1/authorize", body)
        if answer is None:
            return None
        status = answer.get("Status")
        if status != ADMITTED:
            logger.info("fastpath authorize %s workspace=%s", status, workspace_id)
            return None
        envelope = answer.get("Envelope")
        end_of_life = _parse_time(answer.get("EndOfLife"))
        if not isinstance(envelope, str) or not envelope or end_of_life is None:
            logger.warning("fastpath authorize admitted without an envelope or end of life")
            return None
        return Admitted(envelope=envelope, end_of_life=end_of_life)

    # -- terminals --------------------------------------------------------

    def settle(self, *, envelope: str, charge: int, full: bytes, money: dict[str, Any]) -> Terminal:
        """The authorization's settle, with the charge Python priced and the
        full record the pending work needs. Raises FrontDoorUnavailable when
        no front door answered."""
        body = {"Envelope": envelope, "Charge": int(charge), "Full": _bytes(full), "Money": _money(money)}
        return self._terminal("/v1/settle", body)

    def refund(self, *, envelope: str, money: dict[str, Any]) -> Terminal:
        body = {"Envelope": envelope, "Money": _money(money)}
        return self._terminal("/v1/refund", body)

    def _terminal(self, path: str, body: dict[str, Any]) -> Terminal:
        answer = self._call(path, body)
        if answer is None:
            raise FrontDoorUnavailable(path)
        try:
            return Terminal(
                status=str(answer["Status"]),
                kind=str(answer.get("Kind") or ""),
                charge=int(answer.get("Charge") or 0),
                outcome=str(answer.get("Outcome") or ""),
                cost=int(answer.get("Cost") or 0),
                cost_known=bool(answer.get("CostKnown", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FrontDoorUnavailable(f"{path}: malformed answer") from exc

    # -- transport --------------------------------------------------------

    def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON call within one budget for the whole call, the membership
        read included: the caller waits at most the budget. None when no
        front door answered 200 with a JSON object in time."""
        if not self._permits.acquire(blocking=False):
            logger.warning("fastpath %s: every worker busy", path)
            return None
        deadline = self._clock() + self._budget
        done = threading.Event()
        answer: list[dict[str, Any] | None] = [None]

        def run() -> None:
            try:
                answer[0] = self._call_within(path, body, deadline)
            except Exception:  # noqa: BLE001 - a front door call must not fail the request
                logger.exception("fastpath %s failed", path)
            finally:
                done.set()
                self._permits.release()

        worker = threading.Thread(target=run, name="fastpath-front-door", daemon=True)
        try:
            worker.start()
        except RuntimeError:
            # Not started, so nothing runs later: the permit comes back now.
            self._permits.release()
            logger.warning("fastpath %s: no thread", path)
            return None
        if not done.wait(self._budget):
            logger.warning("fastpath %s: budget spent", path)
            return None
        return answer[0]

    def _call_within(self, path: str, body: dict[str, Any], deadline: float) -> dict[str, Any] | None:
        """The call itself, tried on at most DOORS_PER_CALL front doors the
        breakers allow, in turn from a rotating start, each request given what
        remains of the budget. Past the deadline nothing is sent, and an
        answer that comes late changes no breaker: the caller has gone."""
        if self._clock() >= deadline:
            return None
        addresses = tuple(self._front_doors(self._budget))
        if not addresses:
            logger.warning("fastpath %s: no serving front door", path)
            return None
        with self._lock:
            start = self._turn % len(addresses)
            self._turn += 1
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        for i in range(min(len(addresses), DOORS_PER_CALL)):
            address = addresses[(start + i) % len(addresses)]
            breaker = self._breakers.setdefault(address, _Breaker())
            now = self._clock()
            if breaker.is_open(now):
                continue
            remaining = deadline - now
            if remaining <= 0:
                logger.warning("fastpath %s: budget spent before %s", path, address)
                break
            try:
                response = self._client.post(
                    f"http://{address}{path}",
                    content=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=httpx.Timeout(remaining, connect=min(CONNECT_TIMEOUT_SECONDS, remaining)),
                )
            except httpx.HTTPError as exc:
                self._failed(breaker, deadline)
                logger.warning("fastpath %s via %s failed: %s", path, address, type(exc).__name__)
                continue
            if response.status_code != 200:
                # 400 and 500 are the front door's own answers to a bad body or
                # a bad encode, not its health; 502 is an owner it could not
                # reach. None is retried on another front door.
                self._failed(breaker, deadline)
                logger.warning("fastpath %s via %s answered %d", path, address, response.status_code)
                continue
            try:
                answer = response.json()
            except ValueError:
                self._failed(breaker, deadline)
                logger.warning("fastpath %s via %s answered no JSON", path, address)
                continue
            if not isinstance(answer, dict):
                self._failed(breaker, deadline)
                continue
            if self._clock() >= deadline:
                # Too late for the caller, who has gone: neither health nor
                # failure, the breaker is left as it was.
                return None
            breaker.record_success()
            return answer
        return None

    def _failed(self, breaker: _Breaker, deadline: float) -> None:
        """A failure counts only while the caller is still waiting."""
        now = self._clock()
        if now < deadline:
            breaker.record_failure(now)


def _bytes(value: bytes) -> str:
    """Go's []byte on the wire: standard base64."""
    return base64.b64encode(value).decode("ascii")


def _money(money: dict[str, Any]) -> str:
    """The money fields as the front door takes them, []byte of a JSON
    document, which the drain log keeps as sent."""
    return _bytes(json.dumps(money, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # Go writes nanoseconds; Python parses at most microseconds.
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        i = 0
        while i < len(rest) and rest[i].isdigit():
            digits += rest[i]
            i += 1
        text = f"{head}.{digits[:6]}{rest[i:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def records_durable(settle_outbox_enabled: bool, typed_store: Any) -> bool:
    """The leased settle writes its records, its resolved intent and its
    activity row in one commit: it needs the settle outbox and a store that
    builds activity rows in that commit. The pilot's ``records_durable`` and
    the fast path's edge policy both read this."""
    return settle_outbox_enabled and callable(
        getattr(getattr(typed_store, "_operational_analytics_outbox", None), "activity_insert_statement", None)
    )


def pilot_reason(
    *,
    enabled: bool,
    workspace_id: str,
    pilot_workspace_ids: frozenset[str],
    region: str,
    pilot_region_ids: frozenset[str],
    stream: bool,
    standard_endpoint_pricing: bool,
    credit_funded: bool,
    boot_verified: bool,
    service_tier: str | None,
    key_capped: bool,
    budget_strict: bool,
    app_markup: bool,
    records_durable: bool,
) -> str | None:
    """Why an authorize stays on today's path, or None when the pilot takes
    it. The cohort is the design's §4.11 at its narrowest: a credit-funded
    key on a standard route, not streaming, with a verified boot, no
    service tier, no key cap, no markup, for a pilot workspace, from a pilot
    region. An idempotency key does not exclude a request (the enclave sends
    one on every authorize; docs/design/fast-admission-pilot.md), and neither
    does its idempotency_key_source. A speculation descriptor never reaches
    an authorize body (it has a route of its own)."""
    if not enabled:
        return "disabled"
    if workspace_id not in pilot_workspace_ids:
        return "workspace"
    if region not in pilot_region_ids:
        return "region"
    if stream:
        return "stream"
    if not standard_endpoint_pricing:
        return "pricing"
    if not credit_funded:
        return "funding"
    if not boot_verified:
        return "boot"
    if service_tier:
        return "service_tier"
    if key_capped or budget_strict:
        return "key_cap"
    if app_markup:
        return "markup"
    if not records_durable:
        # The leased settle writes the activity row in its own commit, so
        # the pilot needs the durable activity outbox, not the direct sink.
        return "records"
    return None
