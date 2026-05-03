"""Spanner IO adapter for SpannerBigtableStore feature classes.

The composed feature stores (SpannerWalletChallenges,
SpannerVerificationTokens, SpannerEmailBlocks) need a small set of Spanner
primitives — read/write/batch + transaction runner. Pulling them into a
typed adapter lets each feature class declare exactly what it depends on
without importing SpannerBigtableStore (which would be a cycle).

The adapter is a plain dataclass holding callables; SpannerBigtableStore
wires it up once in __init__ from its own bound methods. There's no logic
here, just plumbing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class SpannerIO:
    database: Any
    write_entity_batch: Callable[[Any, str, str, Any], None]
    read_entity_tx: Callable[[Any, str, str, type], Any]
    write_entity_tx: Callable[[Any, str, str, Any], None]
    write_entity: Callable[[str, str, Any], None]
    read_entity: Callable[[str, str, type], Any]
    list_entities: Callable[..., list[Any]]
    delete_entities: Callable[[str, list[str]], None]
    delete_entities_tx: Callable[[Any, str, list[str]], None]
