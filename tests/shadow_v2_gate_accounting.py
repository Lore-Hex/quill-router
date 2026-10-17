"""Compare every bundle accounting result field and ordered diagnostic."""
from __future__ import annotations

from dataclasses import replace
from typing import Any


def assert_accounting_equal(actual: Any, expected: Any) -> None:
    assert actual.complete == expected.complete
    assert replace(actual, missing=()) == replace(expected, missing=())
    assert actual.missing == expected.missing
