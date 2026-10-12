"""Bounded immutable A1 attachment lanes; a missing transport never means durable."""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from trusted_router.async_settle_shadow_v2_a1_health import HEALTH_BASIS_BATCH, PUBLISHER_TRACE_PAGE
from trusted_router.async_settle_shadow_v2_schema import POLICY, require, sha256


class Transport(Protocol):
    def put(self, key: str, body: bytes, timeout: float) -> None: ...
    def get(self, key: str, timeout: float) -> bytes | None: ...


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
        self.sequence = 0
        self.previous: str | None = None
        self.day = ""
        self.next_operation = float("-inf")
        self.operations = dict(put=0, get=0, sent_bytes=0, received_bytes=0)
        self.blocked = False
        self.lock = threading.RLock()

    @property
    def retained_bytes(self) -> int:
        return sum(len(o.body) for o in self.queue)

    def append(self, day: str, value: dict[str, Any]) -> str:
        with self.lock:
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
            previous = hashlib.sha256(last.body).hexdigest() if last is not None else self.previous
            if day != self.day:
                previous = None
                self.sequence = 0
                self.day = day
            self.sequence += 1
            candidate = dict(v=2, policy_version=POLICY, contract_revision="a1", boot=self.boot,
                day=day, previous_sha256=previous, **{field: [value]})
            if self.stream == "basis":
                candidate["sequence"] = f"{self.sequence:020}"
                key = f"health-basis-batches/{day}/{self.boot}/{self.sequence:020}"
            else:
                candidate.update(first_slot=value["slot"], last_slot=value["slot"])
                key = f"publisher-traces/{day}/{self.boot}/{value['slot']}"
            body = self.schema.serialize(candidate)
            self.queue.append(Object(key, day, body))
            return digest

    def step(self, budget: Any) -> None:
        with self.lock:
            now = budget.clock()
            if not self.queue or self.transport is None or now < self.next_operation:
                return
            item = self.queue[0]
            item.frozen = True
            timeout = budget.remaining(2.)
            self.next_operation = now + 5
            operation = "get" if item.uncertain else "put"
            self.operations[operation] += 1
            if operation == "put":
                self.operations["sent_bytes"] += len(item.body)
        landed = False
        try:
            if operation == "put":
                self.transport.put(item.key, item.body, timeout)
                landed = True
            else:
                found = self.transport.get(item.key, timeout)
                with self.lock:
                    self.operations["received_bytes"] += len(found or b"")
                if found is not None:
                    if found != item.body:
                        self.blocked = True
                    require(found == item.body, "attachment_conflict")
                    landed = True
            budget.remaining(2.)
        except Exception:
            with self.lock:
                item.uncertain = True
            return
        with self.lock:
            if landed:
                self.previous = hashlib.sha256(item.body).hexdigest()
                self.queue.pop(0)
            else:
                item.uncertain = False

    def pending(self, day: str) -> bool:
        with self.lock:
            return self.blocked or any(item.day <= day for item in self.queue)
