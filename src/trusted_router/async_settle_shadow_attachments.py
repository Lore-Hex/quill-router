"""Bounded immutable A1 attachment lanes; a missing transport never means durable."""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from trusted_router.async_settle_shadow_v2_a1_health import HEALTH_BASIS_BATCH, PUBLISHER_TRACE_PAGE
from trusted_router.async_settle_shadow_v2_evidence import MAX_DAY_BUCKETS
from trusted_router.async_settle_shadow_v2_schema import POLICY, require, sha256


class Transport(Protocol):
    def put(self, key: str, body: bytes, timeout: float, *, deadline: float, clock: Callable[[], float]) -> None: ...
    def get(self, key: str, timeout: float, *, deadline: float, clock: Callable[[], float]) -> bytes | None: ...


@dataclass
class Object:
    key: str
    day: str
    body: bytes
    frozen: bool = False
    uncertain: bool = False


class Lane:
    """All retained/open/in-flight bytes share four objects and 256 KiB."""
    def __init__(self, stream: str, boot: str, transport: Transport | None) -> None:
        self.stream, self.boot, self.transport = stream, boot, transport
        self.schema = HEALTH_BASIS_BATCH if stream == "basis" else PUBLISHER_TRACE_PAGE
        self.queue: list[Object] = []
        self.chains: dict[str, tuple[int, str | None]] = {}
        self.retired_through = ""
        self.next_operation = float("-inf")
        self.operations = dict(put=0, get=0, sent_bytes=0, received_bytes=0)
        self.blocked = False
        self.lock = threading.RLock()

    @property
    def retained_bytes(self) -> int:
        return sum(len(o.body) for o in self.queue)

    def append(self, day: str, value: dict[str, Any], *, blocking: bool = True) -> str:
        require(self.lock.acquire(blocking=blocking), "attachment_busy")
        try:
            require(day > self.retired_through, "closed_day")
            require(day in self.chains or len(self.chains) < MAX_DAY_BUCKETS, "day_buckets")
            digest = sha256(value)
            field = "bases" if self.stream == "basis" else "entries"
            last = self.queue[-1] if self.queue else None
            if last is not None and not last.frozen and last.day == day:
                candidate = self.schema.parse(last.body)
                if self.stream == "basis":
                    by_hash = {sha256(v): v for v in candidate[field]}
                    by_hash[digest] = value
                    candidate[field] = [by_hash[k] for k in sorted(by_hash)]
                else:
                    candidate[field].append(value)
                    candidate["last_slot"] = value["slot"]
                try:
                    body = self.schema.serialize(candidate)
                except ValueError:
                    last.frozen = True
                else:
                    last.body = body
                    return digest
            if len(self.queue) >= 4:
                self.blocked = True
                raise ValueError("attachment_overflow")
            if last is not None:
                last.frozen = True
            sequence, previous = self.chains.get(day, (0, None))
            predecessor = next((item for item in reversed(self.queue) if item.day == day), None)
            if predecessor is not None:
                previous = hashlib.sha256(predecessor.body).hexdigest()
            sequence += 1
            candidate = dict(v=2, policy_version=POLICY, contract_revision="a1", boot=self.boot,
                day=day, previous_sha256=previous, **{field: [value]})
            if self.stream == "basis":
                candidate["sequence"] = f"{sequence:020}"
                key = f"health-basis-batches/{day}/{self.boot}/{sequence:020}"
            else:
                candidate.update(first_slot=value["slot"], last_slot=value["slot"])
                key = f"publisher-traces/{day}/{self.boot}/{value['slot']}"
            body = self.schema.serialize(candidate)
            self.queue.append(Object(key, day, body))
            self.chains[day] = sequence, previous
            return digest
        finally:
            self.lock.release()

    def retire(self, day: str) -> None:
        """Called only after the writer/day close result is durable."""
        with self.lock:
            require(not any(item.day == day for item in self.queue), "attachment_pending")
            self.chains.pop(day, None)
            self.retired_through = max(self.retired_through, day)

    def step(self, budget: Any) -> None:
        with self.lock:
            now = budget.clock()
            if not self.queue or self.transport is None or now < self.next_operation:
                return
            item = self.queue[0]
            item.frozen = True
            deadline = min(budget.deadline, now + 2.)
            timeout = deadline - now
            require(timeout > 0, "attachment_deadline")
            self.next_operation = now + 5
            operation = "get" if item.uncertain else "put"
            self.operations[operation] += 1
            if operation == "put":
                self.operations["sent_bytes"] += len(item.body)
        landed = False
        try:
            if operation == "put":
                self.transport.put(item.key, item.body, timeout, deadline=deadline, clock=budget.clock)
                landed = True
            else:
                found = self.transport.get(item.key, timeout, deadline=deadline, clock=budget.clock)
                with self.lock:
                    self.operations["received_bytes"] += len(found or b"")
                if found is not None:
                    if found != item.body:
                        self.blocked = True
                    require(found == item.body, "attachment_conflict")
                    landed = True
            if budget.clock() >= deadline:
                raise TimeoutError("attachment_deadline")
        except Exception:
            with self.lock:
                item.uncertain = True
            return
        with self.lock:
            if landed:
                sequence, _ = self.chains[item.day]
                self.chains[item.day] = sequence, hashlib.sha256(item.body).hexdigest()
                self.queue.pop(0)
            else:
                item.uncertain = False

    def pending(self, day: str) -> bool:
        with self.lock:
            return self.blocked or any(item.day <= day for item in self.queue)
