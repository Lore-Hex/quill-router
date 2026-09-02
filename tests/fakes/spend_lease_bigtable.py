from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from google.cloud.bigtable.row_filters import ValueRegexFilter


@dataclass
class Cell:
    value: bytes


class ReadRow:
    def __init__(self, values: dict[bytes, bytes]) -> None:
        self.cells = {
            "lease": {qualifier: [Cell(value)] for qualifier, value in values.items()}
        }


class FakeConditionalRow:
    def __init__(self, table: FakeBigtableTable, row_key: bytes, filter_: Any) -> None:
        self.table = table
        self.row_key = row_key
        self.filter = filter_
        self.mutations: list[tuple[bool, bytes, bytes]] = []

    def set_cell(
        self,
        _family: str,
        column: bytes,
        value: bytes,
        *,
        state: bool,
    ) -> None:
        self.mutations.append((state, column, value))

    def commit(self) -> bool:
        self.table.commit_attempts += 1
        current = self.table.rows.get(self.row_key)
        regex_filter = next(
            (
                candidate
                for candidate in self.filter.filters
                if isinstance(candidate, ValueRegexFilter)
            ),
            None,
        )
        if regex_filter is None:
            matched = current is not None and b"version" in current
        else:
            expected = regex_filter.regex.removeprefix(b"^").removesuffix(b"$")
            matched = (
                not self.table.force_cas_misses
                and current is not None
                and re.fullmatch(expected, current.get(b"version", b"")) is not None
            )
        selected = [mutation for mutation in self.mutations if mutation[0] == matched]
        if selected:
            values = self.table.rows.setdefault(self.row_key, {})
            for _state, column, value in selected:
                values[column] = value
        if self.table.raise_after_next_applied_commit and selected:
            self.table.raise_after_next_applied_commit = False
            raise TimeoutError("reply lost after durable apply")
        return matched


class FakeDirectRow:
    def __init__(self, table: FakeBigtableTable, row_key: bytes) -> None:
        self.table = table
        self.row_key = row_key
        self.deleted = False

    def delete(self) -> None:
        self.deleted = True

    def commit(self) -> None:
        self.table.commit_attempts += 1
        if self.deleted:
            self.table.rows.pop(self.row_key, None)


class FakeBigtableTable:
    def __init__(self) -> None:
        self.rows: dict[bytes, dict[bytes, bytes]] = {}
        self.commit_attempts = 0
        self.force_cas_misses = False
        self.raise_after_next_applied_commit = False

    def row(self, row_key: bytes, *, filter_: Any) -> FakeConditionalRow:
        return FakeConditionalRow(self, row_key, filter_)

    def direct_row(self, row_key: bytes) -> FakeDirectRow:
        return FakeDirectRow(self, row_key)

    def read_row(self, row_key: bytes, *, filter_: Any) -> ReadRow | None:
        del filter_
        values = self.rows.get(row_key)
        return None if values is None else ReadRow(values)
