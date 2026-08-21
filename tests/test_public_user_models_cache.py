from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import trusted_router.public_user_models as public_cache
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import UserProvidedModel


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolated_public_cache() -> Iterator[None]:
    public_cache.reset_public_user_model_cache()
    yield
    public_cache.reset_public_user_model_cache()


def _model(
    suffix: str,
    *,
    name: str | None = None,
    description: str = "",
) -> UserProvidedModel:
    return UserProvidedModel(
        id=f"trustedrouter/user-{suffix}",
        owner_user_id="owner",
        owner_workspace_id="workspace",
        name=name or f"Public model {suffix}",
        description=description,
        display_name="community-operator",
        kind="machine",
    )


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    return TestClient(
        create_app(Settings(environment="test"), init_observability=False),
        raise_server_exceptions=raise_server_exceptions,
    )


def test_list_and_detail_cache_hits_repeat_zero_store_work_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("cache-hit")
    calls = {"list": 0, "detail": 0, "owner": 0}

    def list_models(
        _self: InMemoryStore,
        *,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[UserProvidedModel]:
        calls["list"] += 1
        assert kind is None
        assert limit == public_cache.PUBLIC_USER_MODEL_LIST_LIMIT
        return [model]

    def get_model(_self: InMemoryStore, model_id: str) -> UserProvidedModel | None:
        calls["detail"] += 1
        assert model_id == model.id
        return model

    def get_owner(_self: InMemoryStore, user_id: str) -> None:
        calls["owner"] += 1
        assert user_id == model.owner_user_id
        return None

    monkeypatch.setattr(InMemoryStore, "list_public_user_models", list_models)
    monkeypatch.setattr(InMemoryStore, "get_user_model", get_model)
    monkeypatch.setattr(InMemoryStore, "get_user", get_owner)
    client = _client()

    first_list = client.get("/v1/models/user-provided")
    assert first_list.status_code == 200
    after_first_list = calls.copy()
    assert after_first_list == {"list": 1, "detail": 0, "owner": 1}

    second_list = client.get("/v1/models/user-provided")
    assert second_list.status_code == 200
    assert second_list.json() == first_list.json()
    assert calls == after_first_list

    first_detail = client.get(f"/v1/models/user-provided/{model.id}")
    assert first_detail.status_code == 200
    after_first_detail = calls.copy()
    assert after_first_detail == {"list": 1, "detail": 1, "owner": 2}

    second_detail = client.get(f"/v1/models/user-provided/{model.id}")
    assert second_detail.status_code == 200
    assert second_detail.json() == first_detail.json()
    assert calls == after_first_detail


@pytest.mark.parametrize("cache_kind", ("list", "detail"))
def test_concurrent_cache_miss_collapses_to_one_loader(
    cache_kind: str,
) -> None:
    workers = 8
    start = threading.Barrier(workers + 1)
    loader_entered = threading.Event()
    release_loader = threading.Event()
    call_lock = threading.Lock()
    loader_calls = 0
    model = _model(f"singleflight-{cache_kind}")

    def load_list(limit: int) -> list[UserProvidedModel]:
        nonlocal loader_calls
        assert limit == public_cache.PUBLIC_USER_MODEL_LIST_LIMIT
        with call_lock:
            loader_calls += 1
        loader_entered.set()
        assert release_loader.wait(timeout=5)
        return [model]

    def load_detail() -> UserProvidedModel:
        nonlocal loader_calls
        with call_lock:
            loader_calls += 1
        loader_entered.set()
        assert release_loader.wait(timeout=5)
        return model

    def invoke() -> object:
        start.wait(timeout=5)
        if cache_kind == "list":
            return public_cache.cached_public_user_model_list(None, load_list)
        return public_cache.cached_public_user_model(model.id, load_detail)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(invoke) for _ in range(workers)]
        start.wait(timeout=5)
        try:
            assert loader_entered.wait(timeout=5)
        finally:
            release_loader.set()
        results = [future.result(timeout=5) for future in futures]

    assert loader_calls == 1
    assert all(result == results[0] for result in results)


def test_stale_list_loader_failure_has_sequential_failure_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(public_cache.time, "monotonic", clock)
    model = _model("stale-list", name="Last known good")
    loader_calls = 0
    outage = False

    def loader(limit: int) -> list[UserProvidedModel]:
        nonlocal loader_calls
        loader_calls += 1
        assert limit == public_cache.PUBLIC_USER_MODEL_LIST_LIMIT
        if outage:
            raise RuntimeError("store unavailable")
        return [model]

    initial = public_cache.cached_public_user_model_list("machine", loader)
    assert loader_calls == 1
    clock.advance(public_cache.PUBLIC_USER_MODEL_FRESH_SECONDS + 0.1)
    outage = True

    first_stale = public_cache.cached_public_user_model_list("machine", loader)
    assert first_stale == initial
    assert loader_calls == 2

    clock.advance(public_cache.PUBLIC_USER_MODEL_FAILURE_BACKOFF_SECONDS - 0.01)
    second_stale = public_cache.cached_public_user_model_list("machine", loader)
    assert second_stale == initial
    assert loader_calls == 2

    clock.advance(0.02)
    third_stale = public_cache.cached_public_user_model_list("machine", loader)
    assert third_stale == initial
    assert loader_calls == 3


def test_detail_admission_exhaustion_serves_valid_stale_without_overwriting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(public_cache.time, "monotonic", clock)
    old_model = _model("stale-detail", name="Last known good")
    new_model = _model("stale-detail", name="Recovered value")

    initial = public_cache.cached_public_user_model(old_model.id, lambda: old_model)
    assert initial is not None and initial["name"] == "Last known good"
    clock.advance(public_cache.PUBLIC_USER_MODEL_FRESH_SECONDS + 0.1)

    for index in range(public_cache.PUBLIC_USER_MODEL_MISS_BUDGET):
        model_id = f"trustedrouter/user-budget-burn-{index:02d}"
        assert public_cache.cached_public_user_model(model_id, lambda: None) is None

    entry_before = public_cache._DETAIL_CACHE[old_model.id]
    stale_loader_calls = 0

    def should_not_load_while_limited() -> UserProvidedModel:
        nonlocal stale_loader_calls
        stale_loader_calls += 1
        return new_model

    limited_result = public_cache.cached_public_user_model(
        old_model.id,
        should_not_load_while_limited,
    )
    assert limited_result is not None and limited_result["name"] == "Last known good"
    assert stale_loader_calls == 0
    assert public_cache._DETAIL_CACHE[old_model.id] is entry_before
    assert public_cache._DETAIL_CACHE[old_model.id].value["name"] == "Last known good"

    clock.advance(public_cache.PUBLIC_USER_MODEL_MISS_WINDOW_SECONDS + 0.1)
    recovered = public_cache.cached_public_user_model(old_model.id, lambda: new_model)
    assert recovered is not None and recovered["name"] == "Recovered value"


def test_unique_valid_detail_ids_are_bounded_by_global_miss_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_ids: list[str] = []

    def missing_model(_self: InMemoryStore, model_id: str) -> None:
        store_ids.append(model_id)
        return None

    monkeypatch.setattr(InMemoryStore, "get_user_model", missing_model)
    client = _client()
    statuses: list[int] = []
    responses = []
    total = public_cache.PUBLIC_USER_MODEL_MISS_BUDGET + 5

    for index in range(total):
        model_id = f"trustedrouter/user-budget-miss-{index:02d}"
        response = client.get(f"/v1/models/user-provided/{model_id}")
        responses.append(response)
        statuses.append(response.status_code)

    budget = public_cache.PUBLIC_USER_MODEL_MISS_BUDGET
    assert statuses[:budget] == [404] * budget
    assert statuses[budget:] == [429] * (total - budget)
    assert len(store_ids) == budget
    assert all(response.headers["cache-control"] == "no-store" for response in responses[budget:])
    assert all(response.headers["retry-after"] == "30" for response in responses[budget:])


def test_unique_valid_detail_ids_are_bounded_by_global_miss_concurrency() -> None:
    extra = 2
    total = public_cache.PUBLIC_USER_MODEL_MAX_CONCURRENT_MISSES + extra
    start = threading.Barrier(total + 1)
    all_slots_active = threading.Event()
    extra_attempts_finished = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0
    finished_before_release = 0
    max_active = 0
    loader_calls = 0

    def loader() -> None:
        nonlocal active, loader_calls, max_active
        with state_lock:
            active += 1
            loader_calls += 1
            max_active = max(max_active, active)
            if active == public_cache.PUBLIC_USER_MODEL_MAX_CONCURRENT_MISSES:
                all_slots_active.set()
        try:
            assert release.wait(timeout=5)
        finally:
            with state_lock:
                active -= 1
        return None

    def invoke(index: int) -> str:
        nonlocal finished_before_release
        start.wait(timeout=5)
        try:
            public_cache.cached_public_user_model(
                f"trustedrouter/user-concurrent-miss-{index:02d}",
                loader,
            )
        except public_cache.PublicUserModelReadLimited:
            outcome = "limited"
        else:
            outcome = "loaded"
        with state_lock:
            finished_before_release += 1
            if finished_before_release == extra:
                extra_attempts_finished.set()
        return outcome

    with ThreadPoolExecutor(max_workers=total) as executor:
        futures = [executor.submit(invoke, index) for index in range(total)]
        start.wait(timeout=5)
        try:
            assert all_slots_active.wait(timeout=5)
            # The admitted loaders remain blocked. Requiring the two excess
            # calls to finish before releasing them makes the ceiling proof
            # independent of thread scheduling order.
            assert extra_attempts_finished.wait(timeout=5)
        finally:
            release.set()
        outcomes = [future.result(timeout=5) for future in futures]

    limit = public_cache.PUBLIC_USER_MODEL_MAX_CONCURRENT_MISSES
    assert loader_calls == limit
    assert max_active == limit
    assert outcomes.count("loaded") == limit
    assert outcomes.count("limited") == extra


@pytest.mark.parametrize(
    "invalid_id",
    (
        "openai/gpt-4",
        "trustedrouter/user--leading-dash",
        "trustedrouter/user-bad!",
        "trustedrouter/user-bad/extra",
    ),
)
def test_invalid_detail_ids_return_404_with_zero_model_store_work(
    monkeypatch: pytest.MonkeyPatch,
    invalid_id: str,
) -> None:
    store_calls = 0

    def forbidden(*_args: object, **_kwargs: object) -> None:
        nonlocal store_calls
        store_calls += 1
        raise AssertionError("invalid public model id reached Store")

    monkeypatch.setattr(InMemoryStore, "get_user_model", forbidden)
    monkeypatch.setattr(InMemoryStore, "get_user", forbidden)
    client = _client()

    encoded = quote(invalid_id, safe="/!")
    response = client.get(f"/v1/models/user-provided/{encoded}")

    assert response.status_code == 404
    assert response.headers["cache-control"] == public_cache.PUBLIC_USER_MODEL_CACHE_CONTROL
    assert store_calls == 0


def test_list_route_bounds_rows_and_actual_serialized_response_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = public_cache.PUBLIC_USER_MODEL_LIST_LIMIT
    small_models = [_model(f"small-{index:03d}") for index in range(limit + 25)]
    # Multi-byte text catches an implementation that bounds characters but
    # emits far more UTF-8 bytes than the response budget permits.
    huge_description = "▦" * 10_000
    huge_models = [
        _model(f"large-{index:03d}", description=huge_description)
        for index in range(limit + 25)
    ]
    store_calls: list[tuple[str | None, int | None]] = []

    def list_models(
        _self: InMemoryStore,
        *,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[UserProvidedModel]:
        store_calls.append((kind, limit))
        return huge_models if kind == "machine" else small_models

    monkeypatch.setattr(InMemoryStore, "list_public_user_models", list_models)
    client = _client()

    row_limited = client.get("/v1/models/user-provided")
    assert row_limited.status_code == 200
    assert len(row_limited.json()["data"]) == public_cache.PUBLIC_USER_MODEL_LIST_LIMIT

    byte_limited = client.get("/v1/models/user-provided?kind=machine")
    assert byte_limited.status_code == 200
    assert 0 < len(byte_limited.json()["data"]) < public_cache.PUBLIC_USER_MODEL_LIST_LIMIT
    assert len(byte_limited.content) <= public_cache.PUBLIC_USER_MODEL_LIST_MAX_BYTES
    assert store_calls == [(None, limit), ("machine", limit)]


@pytest.mark.parametrize(
    ("path", "store_method"),
    (
        ("/v1/models/user-provided", "list_public_user_models"),
        (
            "/v1/models/user-provided/trustedrouter/user-loader-failure",
            "get_user_model",
        ),
    ),
)
def test_loader_failure_is_503_and_retries_no_store_work_during_backoff(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    store_method: str,
) -> None:
    store_calls = 0

    def unavailable(*_args: object, **_kwargs: object) -> None:
        nonlocal store_calls
        store_calls += 1
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(InMemoryStore, store_method, unavailable)
    client = _client(raise_server_exceptions=False)

    first = client.get(path)
    second = client.get(path)

    assert first.status_code == 503
    assert second.status_code == 503
    assert first.headers["retry-after"] == "2"
    assert second.headers["retry-after"] == "2"
    assert first.headers["cache-control"] == "no-store"
    assert second.headers["cache-control"] == "no-store"
    assert store_calls == 1


@pytest.mark.parametrize("cache_kind", ("list", "detail"))
def test_loader_failure_backoff_expires_before_store_retry(
    monkeypatch: pytest.MonkeyPatch,
    cache_kind: str,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(public_cache.time, "monotonic", clock)
    loader_calls = 0

    def fail_list(_limit: int) -> list[UserProvidedModel]:
        nonlocal loader_calls
        loader_calls += 1
        raise RuntimeError("store unavailable")

    def fail_detail() -> None:
        nonlocal loader_calls
        loader_calls += 1
        raise RuntimeError("store unavailable")

    def read() -> object:
        if cache_kind == "list":
            return public_cache.cached_public_user_model_list(None, fail_list)
        return public_cache.cached_public_user_model(
            "trustedrouter/user-failure-backoff",
            fail_detail,
        )

    with pytest.raises(public_cache.PublicUserModelUnavailable):
        read()
    with pytest.raises(public_cache.PublicUserModelUnavailable):
        read()
    assert loader_calls == 1

    clock.advance(public_cache.PUBLIC_USER_MODEL_FAILURE_BACKOFF_SECONDS + 0.01)
    with pytest.raises(public_cache.PublicUserModelUnavailable):
        read()
    assert loader_calls == 2
