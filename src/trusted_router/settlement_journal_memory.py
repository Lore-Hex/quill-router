"""Per-row atomic reference storage; deliberately no multi-RPC operation lock."""
from __future__ import annotations

import threading
from typing import Any

from trusted_router.settlement_journal import META, Compare, Read, ReadPage, Request


class InMemoryJournalStorage:
    def __init__(self) -> None:
        self.rows: dict[bytes, dict[bytes, bytes]] = {}
        self._lock = threading.Lock()

    def call(self, request: Request) -> Any:
        with self._lock:
            row = self.rows.get(request.key, {})
            if isinstance(request, ReadPage):
                return {c: v for c, v in row.items() if c in META or
                        f's/{request.start:04x}'.encode() <= c < f's/{request.stop:04x}'.encode()}
            if isinstance(request, Read):
                return {c: row[c] for c in request.columns if c in row}
            assert isinstance(request, Compare)
            if row.get(request.column) != request.expected:
                return False
            row = self.rows.setdefault(request.key, {})
            for column, value in request.updates.items():
                if value is None:
                    row.pop(column, None)
                else:
                    row[column] = value
            return True
