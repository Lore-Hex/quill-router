"""Publishes the signed edge policy to the fast path, dark (E2 P2).

docs/design/fast-admission-e2-decide.md section 4 (the contract) and
docs/design/routing-snapshot-v1.md, "The edge policy". Imported only when
``edge_policy_publish_enabled`` is on; nothing else at runtime reads it.

The body (trusted_router/edge_policy.py) is the live control-plane facts the
edge gates on before it would answer an authorize itself: the accepted image
digests (the verified Stage D policy plus the break-glass override, the set
authorize checks a boot against), the Stage D settings, whether the settle
records are durable, the pilot's gate, the per-key in-flight limit, the
monitor key's lookup hash, the async-settle shadow cohort, the regions, the
internal-only models and the async-settle facts (admission, the edge's async
switch, this process's ticket authority and typed records). The manifest
(routing_snapshot_signing.py, ``EDGE_POLICY_TYP``) is signed with the routing
snapshot's key under its own ``typ``.

A body is issued at the publisher's clock and signed in the background, by
one worker at a time, never on a poll. Startup warms the first copy before
serving requests. A timer refreshes at ``refresh_at`` (+15 s) for 30 minutes
after startup or the latest poll, keeping sparsely polled instances warm.
The worker refreshes the Stage D policy, reads the other facts and signs.
Meanwhile every poll is served the cached copy while it is within its
``not_after`` (+60 s), or answered 503 at once: ``compiling`` while the
worker runs, ``digest-policy-unavailable`` or ``policy-error`` after it
failed (no new worker for 5 s). Once the idle window ends, timers stop; a
later poll restarts them and may see ``compiling`` if its copy has expired.
Bounded idle work and nonblocking polls cannot guarantee a warm copy after
an arbitrarily long idle period. So a signing happens at most once per
refresh interval and a poll never waits on a network round trip, holding no
threadpool token that authorize shares. The edge polls at ``refresh_at``
and trusts a body until its ``not_after``, then proxies everything to
Python. A changed setting therefore reaches a polling edge within about
15 s.

Unlike the routing snapshot there is no worker process: every fact is this
process's own live state (its settings, its Stage D policy resolver, its
store, its catalog), which is the point, and the body is a few hundred
bytes, so building and signing it costs well under a millisecond.

Except for ``accepted_image_digests``: before each signing the publisher
calls ``StageDPolicyResolver.refresh_now()`` (two HTTPS fetches per release
URL, each URL's within 5 s, and a Sigstore bundle check, together failed
after 15 s, and three watermark reads or writes), so a body's set is the
release's at ``issued_at`` (decide contract decision 17). That is at most
four refreshes a minute on a recently polled (or newly started) instance:
at most 240 signatures and fact reads/hour, plus the startup publication.
After the last poll, at most 120 refresh attempts on the successful path;
failures retry at most every 5 s within the same idle window. A refresh
takes at most ``REFRESH_BOUND_SECONDS`` (30 s, Spanner session creation
aside) and may first wait out the resolver's own in-flight one, which is
why it runs in the worker. If it fails nothing new is
signed; the cached copy is served until its ``not_after``, then polls
answer 503 ``digest-policy-unavailable`` and the edge passes every request.
With no release URL configured there is nothing to refresh and the set is
the break-glass override alone, as in authorize.

Nothing here logs or returns key material: the signing key is read from its
mounted file at construction and never leaves this process; responses carry
the body and the manifest, logs carry instants, sizes, digests and exception
classes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from trusted_router.edge_policy import (
    KIND,
    LIFETIME_SECONDS,
    REFRESH_SECONDS,
    EdgePolicyV1,
    canonical_edge_policy_bytes,
)
from trusted_router.internal_model_access import internal_model_ids
from trusted_router.regions import configured_regions
from trusted_router.routing_snapshot_signing import EdgePolicySigner
from trusted_router.services.fastpath_frontdoor import records_durable
from trusted_router.services.routing_snapshot_publisher import load_signer, plane_of
from trusted_router.stage_d_policy import REFRESH_BOUND_SECONDS
from trusted_router.synthetic.funding import monitor_lookup_hash

if TYPE_CHECKING:
    from fastapi import FastAPI

    from trusted_router.config import Settings
    from trusted_router.services.async_settle import Runtime
    from trusted_router.stage_d_policy import StageDPolicyResolver

log = logging.getLogger(__name__)

RESPONSE_KIND = "tr-edge-policy-publication"
# After a failure to refresh, read the facts or sign, no attempt before this
# many seconds; the edge keeps its last good copy until that copy's not_after.
ERROR_RETRY_SECONDS = 5
# A poll that finds no valid copy while the background signing is in flight
# comes back this soon.
COMPILING_RETRY_SECONDS = 2
# Ten instances polled every 15 s globally see 150 s between round-robin
# polls. Thirty minutes also accommodates substantial load-balancer skew.
IDLE_TIMEOUT_SECONDS = 30 * 60
# A signing worker running longer than this is abandoned: polls stop
# reporting it as in flight and the next one starts a fresh worker. Its late
# outcome, if any, is discarded. Above its worst case: refresh_now() may wait
# out the kick worker's refresh, then run its own, each within
# REFRESH_BOUND_SECONDS (Spanner session creation aside), and reading the
# other facts and signing take milliseconds (5 s allowed). So only a worker stuck outside the refresh is
# abandoned. Its length against the body's 60 s lifetime does not matter: a
# replacement worker queues on the resolver's lock behind any refresh in
# flight, so the deadline only bounds how long polls answer "compiling", and
# past not_after the edge passes every request to Python either way.
WORKER_DEADLINE_SECONDS = 2 * REFRESH_BOUND_SECONDS + 5.0


class DigestPolicyUnavailable(Exception):
    """The Stage D policy refresh before a signing failed: sign nothing."""


class PolicyUnavailable(Exception):
    """No policy can be served now; the caller answers 503."""

    def __init__(self, status: str, retry_at: int, now: int) -> None:
        super().__init__(status)
        self.status = status
        self.retry_at = retry_at
        self.retry_after = max(1, retry_at - now)


@dataclass(frozen=True)
class Publication:
    issued_at: int
    refresh_at: int
    not_after: int
    sha256: str
    body: bytes
    manifest: str
    response: bytes

    def current(self, now: int) -> bool:
        return self.issued_at <= now < self.refresh_at


def ticket_authority(runtime: Runtime | None) -> bool:
    """Whether this process's async-settle runtime can issue a ticket: the
    authority facts ``async_settle.snapshot_projection`` answers
    ``async_eligible: false`` without (a signer, an epoch of at least 1, a
    journal region)."""
    return (runtime is not None and runtime.signer is not None and runtime.epoch >= 1
            and runtime.effective_journal_region != "")


def typed_records(typed_store: Any) -> bool:
    """The ``typed`` the gateway's async-v1 authorize branch passes to
    ``authorize_additions``: a typed billing store writing typed records."""
    return typed_store is not None and getattr(typed_store, "request_record_write_mode", "legacy") == "typed"


def settings_facts(
    settings: Settings, *, accepted: frozenset[str], durable: bool, internal: frozenset[str],
    ticket_authority: bool, typed_records: bool,
) -> dict[str, Any]:
    """Every body field but the version, kind, plane and window, from these
    settings and the five live facts that are not settings."""
    return {
        "accepted_image_digests": sorted(accepted),
        "stage_d": {
            "eligibility_enabled": settings.stage_d_eligibility_enabled,
            "heartbeat_enabled": settings.stage_d_heartbeat_enabled,
            "require_heartbeat_declaration": settings.stage_d_require_heartbeat_declaration,
            "pilot_workspaces": sorted(settings.stage_d_pilot_workspaces),
        },
        "records_durable": durable,
        "fastpath_pilot_enabled": settings.fastpath_pilot_enabled,
        "fastpath_pilot_workspaces": sorted(settings.fastpath_pilot_workspace_ids),
        "fastpath_pilot_regions": sorted(settings.fastpath_pilot_region_ids),
        "gateway_authorize_max_in_flight_per_key": settings.gateway_authorize_max_in_flight_per_key,
        "monitor_lookup_hash": monitor_lookup_hash(settings),
        "async_settle_shadow_policy": settings.async_settle_shadow_policy,
        "async_settle_shadow_workspaces": sorted(settings.async_settle_shadow_workspace_ids),
        "configured_regions": configured_regions(settings),
        "primary_region": settings.primary_region,
        "multi_region_enabled": settings.multi_region_enabled,
        "internal_models": sorted(internal),
        "async_settle": {
            "admission_enabled": settings.async_settle_admission_enabled,
            "edge_decide": settings.fastpath_edge_decide_async,
            "ticket_authority": ticket_authority,
            "typed_records": typed_records,
        },
    }


def build_policy(plane: BaseModel, issued_at: int, facts: dict[str, Any]) -> EdgePolicyV1:
    """The body for a window: validated, so a fact outside the schema fails here."""
    return EdgePolicyV1.model_validate({
        "v": 1,
        "kind": KIND,
        "plane": plane.model_dump(mode="json"),
        "issued_at": issued_at,
        "refresh_at": issued_at + REFRESH_SECONDS,
        "not_after": issued_at + LIFETIME_SECONDS,
        **facts,
    })


def refreshed_accepted_image_digests(
    resolver: StageDPolicyResolver | None, settings: Settings,
) -> frozenset[str]:
    """The verified Stage D set right after a synchronous refresh, plus the
    break-glass override (``gateway._stage_d_accepted_image_digests``'s
    union). Raises DigestPolicyUnavailable when the refresh fails. With no
    resolver, or no release to refresh from, the override alone."""
    accepted: frozenset[str] = frozenset()
    if resolver is not None and resolver.release_configured():
        if not resolver.refresh_now():
            raise DigestPolicyUnavailable
        accepted = resolver.accepted_image_digests()
    return accepted | settings.stage_d_accepted_gcp_digests


class LiveFacts:
    """This process's facts, read afresh at every call (each signing)."""

    def __init__(
        self, settings: Settings, *,
        resolver: Callable[[], StageDPolicyResolver | None],
        typed_store: Callable[[], Any],
        runtime: Callable[[], Runtime | None],
    ) -> None:
        self._settings, self._resolver, self._typed_store = settings, resolver, typed_store
        self._runtime = runtime

    def __call__(self) -> dict[str, Any]:
        settings = self._settings
        accepted = refreshed_accepted_image_digests(self._resolver(), settings)
        store = self._typed_store()
        return settings_facts(
            settings,
            accepted=accepted,
            durable=records_durable(settings.settle_outbox_enabled, store),
            internal=internal_model_ids(),
            ticket_authority=ticket_authority(self._runtime()),
            typed_records=typed_records(store),
        )


def _spawn_thread(run: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=run, name="edge-policy-sign", daemon=True)
    thread.start()
    return thread


def _schedule_refresh(delay: float, run: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, run)
    timer.daemon = True
    timer.name = "edge-policy-refresh"
    timer.start()
    return timer


class EdgePolicyPublisher:
    """Signs the edge policy in the background and serves the last signed copy.

    A poll never waits on a refresh or a signature: it holds the state lock
    only to read the cache and, when the cached copy is past its
    ``refresh_at`` (or there is none), to start the single background worker.
    ``spawn`` runs the worker (a daemon thread; tests may run it inline).
    """

    def __init__(
        self, *, signer: EdgePolicySigner, plane: BaseModel,
        facts: Callable[[], dict[str, Any]], clock: Callable[[], float] = time.time,
        spawn: Callable[[Callable[[], None]], Any] = _spawn_thread,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._signer, self._plane, self._facts, self._clock = signer, plane, facts, clock
        self._spawn, self._monotonic = spawn, monotonic
        # Re-entrant: an inline worker (tests) records its outcome under the
        # lock the poll that started it holds.
        self._lock = threading.RLock()
        self._cached: Publication | None = None
        self._worker: Any = None
        self._worker_started = 0.0
        # Bumped per worker: only the latest worker's outcome is recorded.
        self._generation = 0
        self.abandoned = 0
        # After a failure, (status, until): no new worker before then.
        self._failed: tuple[str, int] = ("", 0)
        self.signatures = 0
        self.attempts = 0
        self._maintaining = False
        self._stopped = False
        self._active_until = 0.0
        self._timer: Any = None

    def start(self) -> None:
        """Warm at startup, then refresh for a bounded idle window.

        The lifespan awaits this first worker outside the authorize pool.
        Construction alone does no work (facts may not yet be installed).
        """
        with self._lock:
            if self._maintaining or self._stopped:
                return
            self._maintaining = True
            self._active_until = self._monotonic() + IDLE_TIMEOUT_SECONDS
            self._maintain_locked()

    def stop(self) -> None:
        """Cancel scheduled refreshes and discard any in-flight outcome."""
        with self._lock:
            self._stopped = True
            self._generation += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _maintain_locked(self) -> None:
        if self._stopped or self._monotonic() >= self._active_until:
            return
        now = int(self._clock())
        if self._cached is None or not self._cached.current(now):
            self._start_locked(now)
        # While a worker runs, check again after the failure backoff. The
        # timer itself never reads facts or signs and shares no request pool.
        due = now + ERROR_RETRY_SECONDS
        if self._cached is not None and self._cached.current(now):
            due = self._cached.refresh_at
        elif self._failed[1] > now:
            due = self._failed[1]
        delay = min(max(1, due - self._clock()), self._active_until - self._monotonic())
        self._timer = _schedule_refresh(max(0, delay), self._refresh_due)

    def _refresh_due(self) -> None:
        with self._lock:
            self._timer = None
            self._maintain_locked()

    def current(self) -> Publication:
        """The cached publication while it is within its not_after; raises
        PolicyUnavailable at once when there is none."""
        with self._lock:
            now = int(self._clock())
            if self._maintaining and not self._stopped:
                self._active_until = self._monotonic() + IDLE_TIMEOUT_SECONDS
                if self._timer is None:
                    self._maintain_locked()
            cached = self._cached
            if cached is not None and cached.current(now):
                return cached
            self._start_locked(now)
            cached = self._cached
            if cached is not None and cached.issued_at <= now < cached.not_after:
                return cached
            if self._in_flight():
                raise PolicyUnavailable("compiling", now + COMPILING_RETRY_SECONDS, now)
            status, until = self._failed
            if status:
                raise PolicyUnavailable(status, max(until, now + 1), now)
            raise PolicyUnavailable("compiling", now + COMPILING_RETRY_SECONDS, now)

    def idle(self, timeout: float) -> bool:
        """Wait for the background worker, if any (tests and shutdown)."""
        with self._lock:
            worker = self._worker
        if worker is not None and hasattr(worker, "join"):
            worker.join(timeout)
        return not self._in_flight()

    def _in_flight(self) -> bool:
        worker = self._worker
        if worker is None or not worker.is_alive():
            return False
        if self._monotonic() - self._worker_started <= WORKER_DEADLINE_SECONDS:
            return True
        with self._lock:
            if self._worker is worker:
                # Hung (a fetch past its timeouts, a stuck verifier): abandon
                # it. The daemon thread is left to finish or not; its outcome
                # carries a stale generation and is discarded.
                self._worker, self._generation = None, self._generation + 1
                self.abandoned += 1
                log.warning("edge policy signing worker abandoned after %d s",
                            int(WORKER_DEADLINE_SECONDS))
        return False

    def _start_locked(self, now: int) -> None:
        if self._stopped or self._in_flight() or now < self._failed[1]:
            return
        self.attempts += 1
        self._generation += 1
        generation = self._generation
        self._worker_started = self._monotonic()
        self._worker = self._spawn(lambda: self._sign_in_background(generation))

    def _sign_in_background(self, generation: int) -> None:
        try:
            # The facts first: the Stage D refresh is a network round trip,
            # and issued_at is the clock after it.
            facts = self._facts()
            publication = self._publish(int(self._clock()), facts)
        except DigestPolicyUnavailable:
            # The resolver logged its failure's class.
            status = "digest-policy-unavailable"
        except Exception as exc:
            # Logged by class alone: a message could carry a setting's value.
            log.warning("edge policy publication failed: %s", type(exc).__name__)
            status = "policy-error"
        else:
            with self._lock:
                if generation == self._generation:
                    self._cached, self._failed = publication, ("", 0)
            return
        now = int(self._clock())
        with self._lock:
            if generation != self._generation:
                return
            self._failed = (status, now + ERROR_RETRY_SECONDS)
        log.warning("edge policy unavailable: %s", status)

    def _publish(self, issued_at: int, facts: dict[str, Any]) -> Publication:
        policy = build_policy(self._plane, issued_at, facts)
        body = canonical_edge_policy_bytes(policy)
        manifest = self._signer.sign(body)
        self.signatures += 1
        sha256 = hashlib.sha256(body).hexdigest()
        response = json.dumps(
            {
                "v": 1,
                "kind": RESPONSE_KIND,
                "manifest": manifest,
                "body": base64.b64encode(body).decode("ascii"),
            },
            sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        log.info("edge policy published: sha256=%s size=%d issued_at=%d",
                 sha256, len(body), issued_at)
        return Publication(
            issued_at=issued_at, refresh_at=policy.refresh_at, not_after=policy.not_after,
            sha256=sha256, body=body, manifest=manifest, response=response,
        )


def _typed_store() -> Any:
    from trusted_router.storage import STORE, typed_billing_store

    return typed_billing_store(STORE)


def build_publisher(
    settings: Settings, *,
    resolver: Callable[[], StageDPolicyResolver | None] = lambda: None,
    typed_store: Callable[[], Any] = _typed_store,
    runtime: Callable[[], Runtime | None] = lambda: None,
    facts: Callable[[], dict[str, Any]] | None = None,
    clock: Callable[[], float] = time.time,
    spawn: Callable[[Callable[[], None]], Any] = _spawn_thread,
    monotonic: Callable[[], float] = time.monotonic,
) -> EdgePolicyPublisher:
    """The publisher for this process; raises ValueError when the routing key is unusable.

    ``facts`` replaces the live facts (tests).
    """
    signer = EdgePolicySigner(load_signer(settings))
    return EdgePolicyPublisher(
        signer=signer, plane=plane_of(settings),
        facts=facts or LiveFacts(settings, resolver=resolver, typed_store=typed_store, runtime=runtime),
        clock=clock, spawn=spawn, monotonic=monotonic,
    )


def install(app: FastAPI, settings: Settings) -> None:
    """Put the publisher, or why there is none, on ``app.state``.

    The Stage D policy resolver and the async-settle runtime are read from
    ``app.state`` at each signing: both are installed after this runs.
    """
    state = app.state
    try:
        state.edge_policy_publisher = build_publisher(
            settings, resolver=lambda: getattr(state, "stage_d_policy_resolver", None),
            runtime=lambda: getattr(state, "async_settle", None),
        )
        state.edge_policy_status = "serving"
    except ValueError as exc:
        state.edge_policy_publisher = None
        state.edge_policy_status = "signer-unavailable"
        # load_signer's reasons name no key material.
        log.warning("edge policy publisher disabled: %s", exc)
        return

    @app.on_event("startup")
    async def warm() -> None:
        import asyncio

        publisher = state.edge_policy_publisher
        publisher.start()
        # Startup can wait; polls cannot. No AnyIO/default executor token is
        # held while the dedicated signing worker reads this process's facts.
        deadline = time.monotonic() + WORKER_DEADLINE_SECONDS
        while not publisher.idle(0) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    @app.on_event("shutdown")
    async def stop() -> None:
        state.edge_policy_publisher.stop()
