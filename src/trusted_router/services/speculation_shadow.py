"""Non-authoritative shadow evidence. No money or provider dispatch capability.

Request callbacks only copy allowlisted facts and try a bounded queue. All IO,
including recovery and signing, belongs to an independent worker/refresh context.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import queue
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from trusted_router.speculation_protocol import (
    SHADOW_TYP,
    TrustedKey,
    classify_verdict,
    cost_ceiling,
    verify_grant,
    workspace_allowance,
)
from trusted_router.store_protocol import ShadowStore, ShadowTransaction

TIER_CEILINGS = {2: 25_000_000, 3: 100_000_000}
TABLES = frozenset({"event", "success", "scope", "producer", "paid", "route", "grant", "exposure"})
MAX_BATCH = 64
MAX_BODY = 32768
HISTORY_SECONDS = 600
MAX_DELIVERY_SECONDS = 86400  # replay acceptance; TTL keeps dedup for seven days


class ShadowMiss(ValueError):
    """A stable, content-free refusal; never an ordinary authorize failure."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def identity(*parts: str) -> str:
    return hashlib.sha256(canonical(parts)).hexdigest()


@dataclass(frozen=True)
class Outcome:
    event_id: str
    producer: str
    incarnation: str
    sequence: int
    occurred_at: int
    workspace_id: str = ""
    key_id: str = ""
    lookup_digest: str = ""
    invocation_nonce: str = ""
    authorization_id: str = ""
    replay: bool = False
    boot_verified: bool = False
    endpoint_ids: tuple[str, ...] = ()
    status: int = 500
    reason: str = "infrastructure_error"
    rate_scope: str = ""
    timing: tuple[tuple[str, int], ...] = ()
    boot_id: str = ""
    route_identity: tuple[str, ...] = ()


@dataclass
class Observation:
    dispatcher: Dispatcher
    request_identity: object = field(default_factory=object)
    retired: bool = False
    sealed: bool = False
    timing: tuple[tuple[str, int], ...] = ()
    route_identity: tuple[str, ...] = ()
    boot_id: str = ""
    workspace_id: str = ""
    key_id: str = ""
    lookup_digest: str = ""
    invocation_nonce: str = ""
    boot_verified: bool = False
    reason: str = ""
    rate_scope: str = ""
    authorization_id: str = ""
    replay: bool = False
    endpoint_ids: tuple[str, ...] = ()

    def __setattr__(self, name: str, value: Any) -> None:
        if self.__dict__.get("sealed", False) and name != "retired":
            record_loss("sealed-write-" + name, self)
            return
        super().__setattr__(name, value)


class Continuation:
    """Explicit, one-use authority for the designated async-to-sync worker."""

    def __init__(self, observation: Observation) -> None:
        self.observation = observation
        self._claimed = False
        self._lock = threading.Lock()

    def claim(self) -> Observation | None:
        # Never wait on another invocation. A failed claim gets its own scope.
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._claimed or self.observation.sealed or self.observation.retired:
                return None
            self._claimed = True
            return self.observation
        finally:
            self._lock.release()


def seal(observation: Observation, timing: Mapping[str, int]) -> None:
    if not observation.sealed:
        observation.timing = tuple((k, int(timing[k])) for k in
                                  ("total_ms", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms", "spanner_rpcs") if k in timing)
        observation.sealed = True


_CURRENT: contextvars.ContextVar[Observation | None] = contextvars.ContextVar("speculation_shadow", default=None)
_RUNTIME: Dispatcher | None = None
# Last resort: a single process-local reference assignment, with no locks,
# callbacks, allocation, logging or IO. Never cleared during process lifetime.
_COVERAGE_UNKNOWN = False


def _record_loss(site: str, observation: Observation | None, dispatcher: Dispatcher | None) -> None:
    observation = observation or _CURRENT.get()
    dispatcher = dispatcher or (observation.dispatcher if observation is not None else _RUNTIME)
    if dispatcher is not None:
        dispatcher.loss.set()
        if not dispatcher.loss_reason:
            dispatcher.loss_reason = "observer-" + site


def record_loss(site: str, observation: Observation | None = None, dispatcher: Dispatcher | None = None, *, deferred: list[str] | None = None) -> None:
    """Contain ordinary recorder faults; process-control exceptions still escape.

    Both failure classes close coverage, with no request-path logging or IO.
    """
    global _COVERAGE_UNKNOWN
    try:
        if deferred is not None:
            deferred.append(site)
        else:
            _record_loss(site, observation, dispatcher)
    except Exception:
        _COVERAGE_UNKNOWN = True
    except BaseException:
        try:
            _COVERAGE_UNKNOWN = True
        finally:
            raise


def restore_context(variable: contextvars.ContextVar[Any], token: contextvars.Token[Any], previous: Any) -> None:
    """Restore even if reset fails (including a stale/already-used token)."""
    global _COVERAGE_UNKNOWN
    try:
        variable.reset(token)
    except BaseException:
        try:
            variable.set(previous)
        except Exception:
            # A retired observation is inert even if neither restoration works.
            _COVERAGE_UNKNOWN = True
        except BaseException:
            try:
                _COVERAGE_UNKNOWN = True
            finally:
                raise
        raise


@contextmanager
def isolate(site: str, observation: Observation | None = None, *, deferred: list[str] | None = None) -> Iterator[None]:
    """Guard callbacks AND their argument evaluation, never the ordinary operation.

    Process termination/cancellation (BaseException) is deliberately not swallowed.
    The first content-free site reason survives later callbacks and queue overflow.
    """
    try:
        yield
    except Exception:
        record_loss(site, observation, deferred=deferred)


def resolved(key: Any, nonce: str | None) -> None:
    observation = _CURRENT.get()
    if observation is not None and (not observation.retired or observation.sealed) and key is not None:
        observation.workspace_id = key.workspace_id
        observation.key_id = key.hash
        observation.lookup_digest = key.lookup_hash
        observation.invocation_nonce = nonce or ""


def reason(code: str, rate_scope: str = "") -> None:
    observation = _CURRENT.get()
    if observation is not None and (not observation.retired or observation.sealed):
        observation.reason, observation.rate_scope = code, rate_scope


def boot_verified(verified: bool, kid: str = "") -> None:
    observation = _CURRENT.get()
    if observation is not None and (not observation.retired or observation.sealed):
        observation.boot_verified = verified
        observation.boot_id = kid if verified else ""


def authorized(authorization: Any, endpoint_ids: tuple[str, ...], replay: bool, route_identity: tuple[str, ...] = ()) -> None:
    observation = _CURRENT.get()
    if observation is not None and (not observation.retired or observation.sealed):
        observation.authorization_id = authorization.id
        observation.invocation_nonce = authorization.invocation_nonce or observation.invocation_nonce
        observation.endpoint_ids = endpoint_ids[:16]
        observation.replay = replay
        observation.route_identity = route_identity


@contextmanager
def outcome_scope(settings: Any, request_identity: object | None = None, continuation: Continuation | None = None) -> Iterator[Observation | None]:
    if not settings.speculative_provider_shadow_enabled or _RUNTIME is None:
        yield None
        return
    attached = continuation.claim() if continuation is not None else None
    if attached is not None:
        previous = _CURRENT.get()
        token = _CURRENT.set(attached)
        try:
            yield None  # The async owner alone seals and submits this event.
        finally:
            restore_context(_CURRENT, token, previous)
        return
    observation = Observation(_RUNTIME, request_identity=request_identity if request_identity is not None else object())
    previous = _CURRENT.get()
    token = _CURRENT.set(observation)
    try:
        yield observation
    finally:
        try:
            observation.retired = True
        finally:
            restore_context(_CURRENT, token, previous)


def complete(observation: Observation | None, timing: Mapping[str, int], error: BaseException | None = None, *, deferred: list[str] | None = None) -> None:
    if observation is None:
        return
    # Never parse error messages or queue a response/body/BYOK object.
    aborted = error is not None and not isinstance(error, Exception)
    status = 500 if aborted else int(getattr(error, "status_code", 500)) if error is not None else 200
    code = "aborted" if aborted else observation.reason
    if not code:
        detail = getattr(error, "detail", None)
        typed = detail.get("error", {}).get("type", "") if isinstance(detail, dict) and isinstance(detail.get("error"), dict) else ""
        code = {"key_limit_exceeded": "key_limit_exceeded", "key_window_limit_exceeded": "key_window_limit_exceeded", "insufficient_credits": "credit_exhausted",
                "billing_paused": "billing_paused"}.get(typed, "success" if not error else "request_error")
    with isolate("submit", observation, deferred=deferred):
        observation.dispatcher.try_submit(observation, status, code, dict(observation.timing) if observation.sealed else timing, deferred=deferred)


class Dispatcher:
    def __init__(self, store: ShadowStore, producer: str, *, capacity: int = 1024, membership: str = "") -> None:
        self.store, self.producer = store, producer
        self.incarnation = uuid.uuid4().hex
        self.membership = membership
        self.pending: queue.Queue[Outcome] = queue.Queue(maxsize=capacity)
        self.loss = threading.Event()  # separate from the bounded queue; sticky
        self.loss_reason = ""
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.sequence = 0
        self.health = "starting"
        self.last_rpcs = 0
        self.total_rpcs = 0
        self.thread: threading.Thread | None = None

    def coverage_lost(self) -> bool:
        return _COVERAGE_UNKNOWN or self.loss.is_set()

    def coverage_loss_reason(self) -> str:
        return "coverage-unknown" if _COVERAGE_UNKNOWN else self.loss_reason

    def try_submit(self, observation: Observation, status: int, code: str, timing: Mapping[str, int], *, deferred: list[str] | None = None) -> None:
        # The worker NEVER takes this lock. Contention is a coverage loss, not a wait.
        if not self.lock.acquire(blocking=False):
            record_loss("queue", dispatcher=self, deferred=deferred)
            return
        try:
            self.sequence += 1
            event = Outcome(uuid.uuid4().hex, self.producer, self.incarnation, self.sequence, int(time.time()),
                            observation.workspace_id, observation.key_id, observation.lookup_digest,
                            observation.invocation_nonce, observation.authorization_id, observation.replay,
                            observation.boot_verified, observation.endpoint_ids, status, code,
                            observation.rate_scope, tuple((k, int(timing[k])) for k in
                            ("total_ms", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms", "spanner_rpcs") if k in timing), observation.boot_id, observation.route_identity)
            self.pending.put_nowait(event)
        except queue.Full:
            record_loss("queue", dispatcher=self, deferred=deferred)
        finally:
            self.lock.release()

    def start(self) -> None:
        self.thread = threading.Thread(target=lambda: contextvars.Context().run(self.run), daemon=True, name="speculation-shadow")
        self.thread.start()

    def run(self) -> None:
        from trusted_router.storage_gcp_io import count_spanner_rpcs, spanner_rpc_budget
        @spanner_rpc_budget(2.0)
        def flush(event: Outcome | None) -> None:
            with count_spanner_rpcs() as counter:
                try:
                    self.store.ready()
                    self.store.transaction(lambda tx: project(tx, event, self.producer, self.incarnation,
                                                             self.sequence, self.coverage_lost(), int(time.time()), self.membership))
                finally:
                    self.last_rpcs = counter.value()
                    self.total_rpcs += self.last_rpcs
        while not self.stopped.is_set():
            event = None
            try:
                event = self.pending.get(timeout=1)
            except queue.Empty:
                pass
            try:
                flush(event)
                # Loss remains sticky for this incarnation. A clean restart and
                # full fifteen-minute interval are required after local data loss.
                self.health = "coverage-lost" if self.coverage_lost() else "observing"
            except Exception:
                record_loss("worker", dispatcher=self)
                self.health = "shadow-storage-unavailable"
            finally:
                if event is not None:
                    self.pending.task_done()

    def close(self) -> None:
        record_loss("close", dispatcher=self)
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3)


def project(tx: ShadowTransaction, event: Outcome | None, producer: str, incarnation: str,
            submitted: int, loss: bool, now: int, membership: str = "") -> None:
    previous = tx.get("producer", producer)
    state = previous or {"incarnation": incarnation, "sequence": 0, "clean_since": now}
    if state["incarnation"] != incarnation or state.get("expires_at", now) < now or state.get("membership", "") != membership:
        state = {"incarnation": incarnation, "sequence": 0, "clean_since": now}
    if loss:
        state["clean_since"] = now
        state["lost"] = True
    state.update(submitted=submitted, expires_at=now + 5, membership=membership)
    # Older deliveries cannot resurrect dedup entries after TTL. Fail coverage
    # closed even for old denials; do not silently discard adverse evidence.
    if event is not None and not now - MAX_DELIVERY_SECONDS <= event.occurred_at <= now:
        state["clean_since"] = now
        state["lost"] = True
        event = None
    if event is not None:
        event_key = event.incarnation + ":" + str(event.sequence)
        if tx.get("event", event_key) is None:
            if event.sequence != state["sequence"] + 1 or not event.workspace_id or not event.key_id:
                state["clean_since"] = now
            state["sequence"] = max(state["sequence"], event.sequence)
            tx.put("event", event_key, {**asdict(event), "received_at": now})
            if event.workspace_id and event.key_id:
                verdict = classify_verdict(source="authenticated_router", status=event.status,
                    reason=event.reason, workspace_id=event.workspace_id, key_id=event.key_id,
                    rate_scope=event.rate_scope) if event.status >= 400 else {"durable_scope": "none", "local_infrastructure_breaker": "none"}
                workspace_key = identity("workspace", event.workspace_id)
                key_key = identity("key", event.workspace_id, event.key_id)
                for scope_key, scope in ((workspace_key, "workspace"), (key_key, "key")):
                    row = tx.get("scope", scope_key) or {"clean_since": now, "sequence": 0, "successes": [], "epoch": 0}
                    row["sequence"] += 1
                    if verdict["durable_scope"] == scope or (scope == "key" and verdict["local_infrastructure_breaker"] != "none"):
                        # Receipt time, not a late event's timestamp: a delayed
                        # deny invalidates the entire newly observed clean interval.
                        row["clean_since"] = now
                        row["epoch"] += 1
                    if scope == "key" and event.status == 200 and event.authorization_id and not event.replay and now - HISTORY_SECONDS <= event.occurred_at <= now:
                        unique = identity(event.authorization_id)
                        invocation = identity("invocation", event.key_id, event.invocation_nonce) if event.invocation_nonce else unique
                        if tx.get("success", unique) is None and tx.get("success", invocation) is None:
                            tx.put("success", unique, {"occurred_at": event.occurred_at})
                            tx.put("success", invocation, {"occurred_at": event.occurred_at})
                            row["successes"].append([event.authorization_id, event.occurred_at])
                            row["endpoint_ids"] = list(event.endpoint_ids)
                            row["route_identity"] = list(event.route_identity)
                    row["successes"] = sorted((s for s in row["successes"] if now - 600 <= s[1] <= now), key=lambda s: s[1])[-20:]
                    tx.put("scope", scope_key, row)
    tx.put("producer", producer, state)


def paid_lower_bound(total_available: int, evidence: Mapping[str, Any], now: int) -> int:
    if evidence.get("complete") is not True or evidence.get("conserved") is not True or evidence.get("unresolved_recovery"):
        raise ShadowMiss("paid-evidence-incomplete")
    if not evidence.get("version") or not 0 <= evidence.get("as_of", -1) <= now < evidence.get("expires_at", 0):
        raise ShadowMiss("paid-evidence-stale")
    # Evidence binds the complete inflow ledger to the CURRENT typed credits.
    accounted, current = evidence.get("accounted_credits_micro"), evidence.get("current_credits_micro")
    if type(accounted) is not int or type(current) is not int or accounted < 0 or accounted != current:
        raise ShadowMiss("paid-conservation")
    if type(total_available) is not int or total_available < 0:
        raise ShadowMiss("credit-snapshot-invalid")
    nonpaid = evidence.get("nonqualifying_upper_micro")
    if type(nonpaid) is not int or nonpaid < 0:
        raise ShadowMiss("paid-evidence-incomplete")
    return max(0, total_available - nonpaid)


def eligibility(facts: Mapping[str, Any], paid: Mapping[str, Any], history: Mapping[str, Any], now: int) -> int:
    if facts.get("key_eligible") is not True:
        raise ShadowMiss("key-ineligible")
    if facts.get("shards_complete") is not True or facts.get("tier") not in TIER_CEILINGS or facts.get("latched") is not False or facts.get("paused") is not False:
        raise ShadowMiss("trust-ineligible")
    if not 0 < facts.get("reconciled_through", 0) <= now < facts.get("trust_fresh_until", 0):
        raise ShadowMiss("trust-reconciliation-stale")
    if any(type(facts.get(k)) is not int or facts[k] < 0 for k in ("credits", "usage", "reserved")):
        raise ShadowMiss("credit-snapshot-invalid")
    paid = {**paid, "current_credits_micro": facts["credits"]}
    headroom = paid_lower_bound(max(0, facts["credits"] - facts["usage"] - facts["reserved"]), paid, now)
    if headroom < 5_000_000:
        raise ShadowMiss("paid-headroom")
    if history.get("clean_since", now) > now - 900:
        raise ShadowMiss("history-clean-interval")
    if history.get("count", 0) < 20 or history.get("last_success_at", 0) < now - 30:
        raise ShadowMiss("history-count")
    return headroom


def freeze_route(route: Mapping[str, Any], applicability: Mapping[str, Any]) -> dict[str, Any]:
    """Only independently certified v1 data, never customer prices or BYOK blobs."""
    from trusted_router.speculation_protocol import ROUTE_INTEGERS, ROUTE_STRINGS
    required = set((ROUTE_STRINGS + " " + ROUTE_INTEGERS).split()) | {"stage_d"}
    if (set(route) != required or not all(applicability.get(k) for k in
            ("vendor_source", "source_revision", "policy_version"))
            or applicability.get("cap_certified") is not True or applicability.get("input_certified") is not True):
        raise ShadowMiss("route-evidence-incomplete")
    result = dict(route)
    result["catalog_hash"] = hashlib.sha256(canonical({"route": dict(route), "applicability": dict(applicability)})).hexdigest()
    return result


class ShadowSigner:
    def __init__(self, private_key: Any, trusted: TrustedKey) -> None:
        if trusted.purpose != "shadow-grant":
            raise ShadowMiss("issuer-purpose")
        self.private_key, self.trusted = private_key, trusted

    def sign(self, claims: dict[str, Any], context: Mapping[str, Any], now: int) -> str:
        def enc(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        header = {"alg": "EdDSA", "kid": self.trusted.kid, "typ": SHADOW_TYP}
        material = enc(canonical(header)) + "." + enc(canonical(claims))
        token = material + "." + enc(self.private_key.sign(material.encode("ascii")))
        verify_grant(token, [self.trusted], context, now, shadow=True)
        return token


@dataclass
class RefreshService:
    store: ShadowStore
    signer: ShadowSigner | None
    settings: Any
    dispatcher: Dispatcher
    lock: Any = field(default_factory=threading.Lock)
    rates: dict[str, float] = field(default_factory=dict)
    active: dict[str, int] = field(default_factory=dict)
    last_rpcs: int = 0
    total_rpcs: int = 0

    def refresh(self, items: list[dict[str, str]], auth: Any, body: bytes, method: str, path: str) -> list[dict[str, str]]:
        from trusted_router.gateway_boot import verify_boot_auth
        from trusted_router.storage_gcp_io import count_spanner_rpcs, spanner_rpc_budget
        @spanner_rpc_budget(3.0)
        def work() -> list[dict[str, str]]:
            if not self.lock.acquire(blocking=False):
                raise ShadowMiss("refresh-busy")
            try:
                now = int(time.time())
                monotonic = time.monotonic()
                self.rates = {k: v for k, v in self.rates.items() if v > monotonic - 10}
                if auth.kid in self.rates or len(self.rates) >= 256:
                    raise ShadowMiss("refresh-rate-limited")
                self.rates[auth.kid] = monotonic
                boot = self.store.boot(auth.kid)
                if not verify_boot_auth(boot=boot, auth=auth, method=method, path=path, exact_body_bytes=body,
                        signed_lookup_hash="batch", resolved_lookup_hash="batch",
                        accepted_image_digests=self.settings.speculation_shadow_images):
                    raise ShadowMiss("boot-auth-invalid")
                # Shared across router replicas; the local limit above also
                # bounds rejected/unknown boot traffic before any source reads.
                def admit_refresh(tx: ShadowTransaction) -> None:
                    rate_key = identity("refresh-rate", auth.kid)
                    previous = tx.get("grant", rate_key)
                    if previous is not None and previous["refresh_after"] > now:
                        raise ShadowMiss("refresh-rate-limited")
                    tx.put("grant", rate_key, {"refresh_after": now + 10})
                self.store.transaction(admit_refresh)
                self.active = {k: expiry for k, expiry in self.active.items() if expiry > now}
                incoming = {identity(i["lookup_digest"], auth.kid) for i in items}
                if len(self.active.keys() | incoming) > 256:
                    raise ShadowMiss("active-key-capacity")
                self.active.update(dict.fromkeys(incoming, now + 30))
                results = []
                for item in {identity(i["lookup_digest"], i["workspace_id"], i["key_id"]): i for i in items}.values():
                    try:
                        facts = self.store.resolve(item["lookup_digest"], now)
                        if any(facts.get(k) != item[k] for k in ("lookup_digest", "workspace_id", "key_id")):
                            raise ShadowMiss("batch-identity-mismatch")
                        token = self.mint(facts, auth.kid, now)
                        results.append({**item, "grant": token})
                    except ShadowMiss as exc:
                        results.append({**item, "miss": str(exc)})
                return results
            finally:
                self.lock.release()
        with count_spanner_rpcs() as counter:
            try:
                return work()
            finally:
                self.last_rpcs = counter.value()
                self.total_rpcs += self.last_rpcs

    def mint(self, facts: dict[str, Any], boot_id: str, now: int) -> str:
        settings = self.settings
        if settings.speculation_shadow_image_policy_version <= 0 or settings.speculation_shadow_policy_expires_at <= now:
            raise ShadowMiss("shadow-policy-stale")
        if facts["workspace_id"] not in settings.speculation_shadow_workspaces:
            raise ShadowMiss("workspace-not-allowed")
        if self.dispatcher.coverage_lost() or self.dispatcher.health != "observing":
            raise ShadowMiss("coverage-lost")
        if self.signer is None:
            raise ShadowMiss("shadow-issuer-unavailable")
        slot = settings.speculation_shadow_slots.get(boot_id)
        if not slot or not settings.speculation_shadow_producers:
            raise ShadowMiss("membership-unavailable")
        signer = self.signer
        def allocate(tx: ShadowTransaction) -> str:
            clean = 0
            for producer in settings.speculation_shadow_producers:
                coverage = tx.get("producer", producer)
                if not coverage or coverage.get("lost") or coverage.get("membership") != self.dispatcher.membership or coverage["expires_at"] <= now or coverage["submitted"] != coverage["sequence"]:
                    raise ShadowMiss("coverage-incomplete")
                clean = max(clean, coverage["clean_since"])
            ws, key = facts["workspace_id"], facts["key_id"]
            workspace = tx.get("scope", identity("workspace", ws))
            key_state = tx.get("scope", identity("key", ws, key))
            if not workspace or not key_state:
                raise ShadowMiss("projection-missing")
            successes = [s for s in key_state["successes"] if now - 600 <= s[1] < now]
            history = {"count": len(successes), "last_success_at": max((s[1] for s in successes), default=0),
                       "sequence": key_state["sequence"], "clean_since": max(clean, workspace["clean_since"], key_state["clean_since"]), "window_start": now - 600}
            paid = tx.get("paid", ws) or {}
            headroom = eligibility(facts, paid, history, now)
            endpoints = key_state.get("endpoint_ids", [])
            endpoint = endpoints[0] if endpoints else ""
            if endpoint not in settings.speculation_shadow_routes:
                raise ShadowMiss("route-not-allowed")
            evidence = tx.get("route", endpoint)
            if not evidence or evidence.get("complete") is not True or evidence.get("expires_at", 0) <= now:
                raise ShadowMiss("route-evidence-missing")
            route = freeze_route(evidence["route"], evidence["applicability"])
            if [route[k] for k in ("endpoint_id", "provider", "upstream_model", "region")] != key_state.get("route_identity"):
                raise ShadowMiss("route-identity-mismatch")
            if route["region"] != slot["region"]:
                raise ShadowMiss("route-region")
            b = cost_ceiling(*(route[k] for k in ("input_bound", "input_rate_micro_per_m", "output_limit", "output_rate_micro_per_m", "maximum_request_fees_micro")))
            if not 0 < b <= 10_000:
                raise ShadowMiss("route-cost")
            context = {k: facts[k] for k in ("workspace_id", "key_id", "lookup_digest")}
            context.update(boot_id=boot_id, stable_slot_id=slot["slot"], region=slot["region"], generation=1,
                           workspace_epoch=workspace["epoch"], key_epoch=key_state["epoch"],
                           image_policy_version=settings.speculation_shadow_image_policy_version,
                           route=route, tier_ceiling_micro=TIER_CEILINGS[facts["tier"]])
            deadline = min(now + 30, facts["key_expires_at"], facts["trust_fresh_until"], route["price_expires_at"])
            if deadline - 2 <= now:
                raise ShadowMiss("start-window-exhausted")
            cache_key = identity(ws, key, boot_id)
            old = tx.get("grant", cache_key)
            if old:
                try:
                    cached = verify_grant(old["token"], [signer.trusted], {**context, "generation": old["generation"]}, now, shadow=True).claims
                    if (cached["trust_fresh_until"] <= facts["trust_fresh_until"]
                            and cached["key_expires_at"] <= facts["key_expires_at"]
                            and cached["route"]["price_expires_at"] <= route["price_expires_at"]
                            and now < cached["start_before"] <= deadline - 2):
                        return str(old["token"])
                except ValueError:
                    pass
                context["generation"] = old["generation"] + 1
            exposure_keys = (identity("workspace", ws), identity("slot", slot["slot"]), "fleet")
            bounds = (workspace_allowance(TIER_CEILINGS[facts["tier"]], headroom), 1_000_000, 10_000_000)
            exposures = []
            for scope, bound in zip(exposure_keys, bounds, strict=True):
                retained = tx.get("exposure", scope) or {"micro": 0, "ordinal": 0, "owner": boot_id}
                if scope == exposure_keys[0] and retained["owner"] != boot_id:
                    raise ShadowMiss("owner-unavailable")
                if retained["micro"] + b > bound:
                    raise ShadowMiss("retained-budget")
                exposures.append(retained)
            claims = {k: context[k] for k in context if k not in {"route", "tier_ceiling_micro"}}
            claims.update(v=1, iss=signer.trusted.iss, aud=signer.trusted.aud, environment=signer.trusted.environment,
                          plane=signer.trusted.plane, grant_id=identity(ws, key, boot_id, str(context["generation"])),
                          tier=facts["tier"], paid_headroom_micro=headroom, iat=now, exp=deadline, start_before=deadline - 2,
                          key_expires_at=facts["key_expires_at"], trust_fresh_until=facts["trust_fresh_until"],
                          per_request_ceiling_micro=10_000, history=history, route=route,
                          permits=[{"ordinal": exposures[0]["ordinal"], "b_micro": b}])
            token = signer.sign(claims, context, now)
            for scope, retained in zip(exposure_keys, exposures, strict=True):
                tx.put("exposure", scope, {**retained, "micro": retained["micro"] + b, "ordinal": retained["ordinal"] + 1})
            tx.put("grant", cache_key, {"token": token, "generation": context["generation"]})
            return token
        return str(self.store.transaction(allocate))


def install(app: Any, settings: Any) -> None:
    """Register lifecycle only when enabled. Off does not even import the adapter."""
    if not settings.speculative_provider_shadow_enabled:
        return

    @app.on_event("startup")
    def start() -> None:
        global _RUNTIME
        from trusted_router.storage import speculation_shadow_store
        store = speculation_shadow_store(settings)
        if store is None:
            app.state.speculation_shadow_status = "shadow-not-supported"
            return
        if not settings.speculation_shadow_producer or settings.speculation_shadow_producer not in settings.speculation_shadow_producers:
            app.state.speculation_shadow_status = "membership-unavailable"
            return
        dispatcher = Dispatcher(store, settings.speculation_shadow_producer, membership=identity(*sorted(settings.speculation_shadow_producers)))
        signer = None
        try:
            from pathlib import Path

            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            private = serialization.load_pem_private_key(Path(settings.speculation_shadow_private_key_file).read_bytes(), password=None)
            if not isinstance(private, Ed25519PrivateKey) or not all((settings.speculation_shadow_kid, settings.speculation_shadow_issuer, settings.speculation_shadow_audience)):
                raise ShadowMiss("issuer-configuration")
            public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).rstrip(b"=").decode("ascii")
            signer = ShadowSigner(private, TrustedKey(settings.speculation_shadow_kid, "shadow-grant", public,
                settings.speculation_shadow_issuer, settings.speculation_shadow_audience, settings.environment, settings.speculation_shadow_plane))
        except Exception:
            app.state.speculation_shadow_status = "shadow-issuer-unavailable"
        app.state.speculation_shadow = RefreshService(store, signer, settings, dispatcher)
        _RUNTIME = dispatcher
        dispatcher.start()

    @app.on_event("shutdown")
    def stop() -> None:
        global _RUNTIME
        service = getattr(app.state, "speculation_shadow", None)
        if service is not None:
            service.dispatcher.close()
            if _RUNTIME is service.dispatcher:
                _RUNTIME = None
