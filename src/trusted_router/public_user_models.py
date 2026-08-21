"""Bounded, process-local cache for anonymous user-model reads.

The public service is intentionally a separate DDoS bulkhead, but these reads
still reach the shared operational database.  Keep both the database work and
the process memory bounded, coalesce concurrent misses, and retain a short
stale value when the backing store is unavailable.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from trusted_router.serialization import user_model_public_shape
from trusted_router.storage_custom_models import (
    CUSTOM_MODEL_PREFIX,
    CUSTOM_MODEL_SLUG_PATTERN,
    normalize_custom_model_id,
)
from trusted_router.storage_models import UserProvidedModel

PUBLIC_USER_MODEL_LIST_LIMIT = 100
PUBLIC_USER_MODEL_LIST_MAX_BYTES = 256 * 1024
PUBLIC_USER_MODEL_DETAIL_CACHE_MAX_KEYS = 256
PUBLIC_USER_MODEL_FRESH_SECONDS = 30.0
PUBLIC_USER_MODEL_NEGATIVE_SECONDS = 15.0
PUBLIC_USER_MODEL_STALE_SECONDS = 300.0
PUBLIC_USER_MODEL_FAILURE_BACKOFF_SECONDS = 2.0
PUBLIC_USER_MODEL_MAX_CONCURRENT_MISSES = 4
PUBLIC_USER_MODEL_MISS_BUDGET = 32
PUBLIC_USER_MODEL_MISS_WINDOW_SECONDS = 30.0
PUBLIC_USER_MODEL_CACHE_CONTROL = (
    "public, max-age=15, s-maxage=30, stale-while-revalidate=300"
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class _Entry:
    value: Any
    fresh_until: float
    stale_until: float


_CONDITION = threading.Condition(threading.RLock())
_LIST_CACHE: dict[str | None, _Entry] = {}
_DETAIL_CACHE: OrderedDict[str, _Entry] = OrderedDict()
_LOADING: set[tuple[str, str | None]] = set()
_DETAIL_MISS_TIMES: deque[float] = deque()
_ACTIVE_DETAIL_MISSES = 0
_FAILED = object()


class PublicUserModelReadLimited(RuntimeError):
    """The process-wide anonymous Store-read budget is exhausted."""


class PublicUserModelUnavailable(RuntimeError):
    """The backing Store failed recently; retry after a short backoff."""


def reset_public_user_model_cache() -> None:
    """Clear process-local state when tests or the app swap Store backends."""

    global _ACTIVE_DETAIL_MISSES
    with _CONDITION:
        _LIST_CACHE.clear()
        _DETAIL_CACHE.clear()
        _LOADING.clear()
        _DETAIL_MISS_TIMES.clear()
        _ACTIVE_DETAIL_MISSES = 0
        _CONDITION.notify_all()


def normalized_public_user_model_id(model_id: str) -> str | None:
    """Return a canonical user-model id only for the persisted id grammar."""

    normalized = normalize_custom_model_id(model_id)
    if not normalized.startswith(CUSTOM_MODEL_PREFIX):
        return None
    slug = normalized.removeprefix(CUSTOM_MODEL_PREFIX)
    if not CUSTOM_MODEL_SLUG_PATTERN.fullmatch(slug):
        return None
    return normalized


def cached_public_user_model_list(
    kind: str | None,
    loader: Callable[[int], list[UserProvidedModel]],
) -> list[dict[str, Any]]:
    """Return a row- and byte-bounded public list through singleflight."""

    cache_key = ("list", kind)

    def load() -> list[dict[str, Any]]:
        models = loader(PUBLIC_USER_MODEL_LIST_LIMIT)
        return _bounded_public_shapes(models[:PUBLIC_USER_MODEL_LIST_LIMIT])

    return _cached_value(
        cache_key,
        cache=_LIST_CACHE,
        item_key=kind,
        loader=load,
        max_entries=4,
        admit_detail_miss=False,
    )


def cached_public_user_model(
    model_id: str,
    loader: Callable[[], UserProvidedModel | None],
) -> dict[str, Any] | None:
    """Return one public shape, including bounded negative-result caching."""

    cache_key = ("detail", model_id)

    def load() -> dict[str, Any] | None:
        model = loader()
        if model is None or not model.enabled or model.status != "active":
            return None
        return user_model_public_shape(model)

    return _cached_value(
        cache_key,
        cache=_DETAIL_CACHE,
        item_key=model_id,
        loader=load,
        max_entries=PUBLIC_USER_MODEL_DETAIL_CACHE_MAX_KEYS,
        admit_detail_miss=True,
    )


def _cached_value(
    cache_key: tuple[str, str | None],
    *,
    cache: dict[_T, _Entry] | OrderedDict[_T, _Entry],
    item_key: _T,
    loader: Callable[[], Any],
    max_entries: int,
    admit_detail_miss: bool,
) -> Any:
    now = time.monotonic()
    stale: _Entry | None = None
    with _CONDITION:
        entry = cache.get(item_key)
        if entry is not None and now < entry.fresh_until:
            _touch(cache, item_key)
            return _entry_value(entry)
        if entry is not None and now < entry.stale_until:
            stale = entry
        if cache_key in _LOADING:
            if stale is not None:
                _touch(cache, item_key)
                return _entry_value(stale)
            while cache_key in _LOADING:
                _CONDITION.wait()
            entry = cache.get(item_key)
            if entry is not None and time.monotonic() < entry.stale_until:
                _touch(cache, item_key)
                return _entry_value(entry)
        _LOADING.add(cache_key)

        if admit_detail_miss and not _admit_detail_miss(now):
            if stale is not None:
                _LOADING.discard(cache_key)
                _touch(cache, item_key)
                _CONDITION.notify_all()
                return _entry_value(stale)
            _store_failure(cache, item_key, max_entries=max_entries, now=now)
            _LOADING.discard(cache_key)
            _CONDITION.notify_all()
            raise PublicUserModelReadLimited

    try:
        value = loader()
    except Exception as exc:
        with _CONDITION:
            if admit_detail_miss:
                _release_detail_miss()
            _LOADING.discard(cache_key)
            if stale is None:
                _store_failure(
                    cache,
                    item_key,
                    max_entries=max_entries,
                    now=time.monotonic(),
                )
            else:
                cache[item_key] = _Entry(
                    value=stale.value,
                    fresh_until=time.monotonic()
                    + PUBLIC_USER_MODEL_FAILURE_BACKOFF_SECONDS,
                    stale_until=stale.stale_until,
                )
                _touch(cache, item_key)
            _CONDITION.notify_all()
        if stale is not None:
            return stale.value
        raise PublicUserModelUnavailable from exc

    now = time.monotonic()
    fresh_seconds = (
        PUBLIC_USER_MODEL_NEGATIVE_SECONDS
        if value is None
        else PUBLIC_USER_MODEL_FRESH_SECONDS
    )
    with _CONDITION:
        if admit_detail_miss:
            _release_detail_miss()
        cache[item_key] = _Entry(
            value=value,
            fresh_until=now + fresh_seconds,
            stale_until=now + fresh_seconds + PUBLIC_USER_MODEL_STALE_SECONDS,
        )
        _touch(cache, item_key)
        while len(cache) > max_entries:
            if isinstance(cache, OrderedDict):
                cache.popitem(last=False)
            else:
                cache.pop(next(iter(cache)))
        _LOADING.discard(cache_key)
        _CONDITION.notify_all()
    return value


def _entry_value(entry: _Entry) -> Any:
    if entry.value is _FAILED:
        raise PublicUserModelUnavailable
    return entry.value


def _admit_detail_miss(now: float) -> bool:
    global _ACTIVE_DETAIL_MISSES
    cutoff = now - PUBLIC_USER_MODEL_MISS_WINDOW_SECONDS
    while _DETAIL_MISS_TIMES and _DETAIL_MISS_TIMES[0] <= cutoff:
        _DETAIL_MISS_TIMES.popleft()
    if (
        _ACTIVE_DETAIL_MISSES >= PUBLIC_USER_MODEL_MAX_CONCURRENT_MISSES
        or len(_DETAIL_MISS_TIMES) >= PUBLIC_USER_MODEL_MISS_BUDGET
    ):
        return False
    _ACTIVE_DETAIL_MISSES += 1
    _DETAIL_MISS_TIMES.append(now)
    return True


def _release_detail_miss() -> None:
    global _ACTIVE_DETAIL_MISSES
    _ACTIVE_DETAIL_MISSES = max(0, _ACTIVE_DETAIL_MISSES - 1)


def _store_failure(
    cache: dict[_T, _Entry] | OrderedDict[_T, _Entry],
    item_key: _T,
    *,
    max_entries: int,
    now: float,
) -> None:
    cache[item_key] = _Entry(
        value=_FAILED,
        fresh_until=now + PUBLIC_USER_MODEL_FAILURE_BACKOFF_SECONDS,
        stale_until=now + PUBLIC_USER_MODEL_FAILURE_BACKOFF_SECONDS,
    )
    _touch(cache, item_key)
    while len(cache) > max_entries:
        if isinstance(cache, OrderedDict):
            cache.popitem(last=False)
        else:
            cache.pop(next(iter(cache)))


def _touch(cache: dict[_T, _Entry] | OrderedDict[_T, _Entry], key: _T) -> None:
    if isinstance(cache, OrderedDict):
        cache.move_to_end(key)


def _bounded_public_shapes(
    models: list[UserProvidedModel],
) -> list[dict[str, Any]]:
    shapes: list[dict[str, Any]] = []
    # Count the exact compact JSON envelope plus each comma-separated item.
    used_bytes = len(b'{"data":[]}')
    for model in models:
        shape = user_model_public_shape(model)
        encoded = json.dumps(shape, separators=(",", ":"), ensure_ascii=False).encode()
        item_bytes = len(encoded) + (1 if shapes else 0)
        if used_bytes + item_bytes > PUBLIC_USER_MODEL_LIST_MAX_BYTES:
            break
        shapes.append(shape)
        used_bytes += item_bytes
    return shapes
