"""User-facing routes degrade, not 500, when ClickHouse is unreachable.

Regression for QUILL-ROUTER-G1 (/status.json) and QUILL-ROUTER-G3
(/console/activity): every ClickHouse endpoint's connect timed out,
``send_with_failover`` re-raised ``httpx.ConnectTimeout``, and nothing between
it and the route caught it.

The failing reads go through a real ``OperationalAnalyticsClient`` whose
transport raises ``httpx.ConnectTimeout``, so the exception reaching each route
is exactly the one production raised. Patches target the backend CLASS, never
the ``STORE`` proxy (issue #333).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.operational_analytics import (
    UNREACHABLE_HOLD_SECONDS,
    AnalyticsUnreachable,
    OperationalAnalyticsClient,
    is_analytics_unavailable,
)
from trusted_router.routes.console import activity as activity_routes
from trusted_router.storage import STORE, InMemoryStore

ANALYTICS_LOGGER_EVENT = "operational_analytics.unavailable"


def _unreachable_clickhouse() -> OperationalAnalyticsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    return OperationalAnalyticsClient(
        base_url="http://clickhouse-a.internal:8123,http://clickhouse-b.internal:8123",
        user="reader",
        password="inert",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )


def _read_through_unreachable_clickhouse(*_args: Any, **_kwargs: Any) -> Any:
    return _unreachable_clickhouse()._query("SELECT 1 FORMAT JSON")


def _app_client() -> TestClient:
    return TestClient(create_app(Settings(environment="test"), init_observability=False))


def _signed_in_client() -> TestClient:
    client = _app_client()
    user = STORE.ensure_user("clickhouse-outage@example.com")
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label=user.email,
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client


@pytest.fixture(autouse=True)
def _clear_usage_cache() -> Any:
    activity_routes._USAGE_CACHE.clear()
    yield
    activity_routes._USAGE_CACHE.clear()


def test_the_simulated_failure_is_the_production_exception() -> None:
    with pytest.raises(httpx.ConnectTimeout) as raised:
        _read_through_unreachable_clickhouse()
    assert is_analytics_unavailable(raised.value)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectTimeout("t"), True),
        (httpx.ConnectError("refused"), True),
        (httpx.ReadTimeout("slow"), True),
        (httpx.RemoteProtocolError("disconnected"), True),
        (503, True),
        (502, True),
        (504, True),
        (500, False),
        (400, False),
        (RuntimeError("ClickHouse response did not contain data rows"), False),
        (ValueError("bad"), False),
    ],
)
def test_only_unreachability_counts_as_unavailable(exc: Any, expected: bool) -> None:
    if isinstance(exc, int):
        request = httpx.Request("POST", "http://clickhouse.internal:8123")
        exc = httpx.HTTPStatusError(
            "status", request=request, response=httpx.Response(exc, request=request)
        )
    assert is_analytics_unavailable(exc) is expected


# ------------------------------------------------------------------ /status.json


def test_status_json_is_200_and_unknown_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        InMemoryStore, "synthetic_probe_samples", _read_through_unreachable_clickhouse
    )
    monkeypatch.setattr(InMemoryStore, "synthetic_rollups", _read_through_unreachable_clickhouse)

    with caplog.at_level(logging.WARNING):
        response = _app_client().get("/status.json")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # Unknown is the truth; nothing may read as healthy.
    assert data["overall_status"] == "unknown"
    assert data["overall_status_label"] == "Unknown"
    assert data["summary"]["headline"] == "Status Data Temporarily Unavailable"
    assert "not a report that systems are healthy" in data["summary"]["detail"]
    assert data["samples"] == []
    assert data["monitor_freshness"]["is_stale"] is True
    assert all(component["status"] != "up" for component in data["components"])
    # The schema is intact, including the drain-lag section the fleet
    # checker requires on every deployment.
    for key in ("components", "windows", "slo_classes", "current", "analytics"):
        assert key in data
    records = [r for r in caplog.records if ANALYTICS_LOGGER_EVENT in r.getMessage()]
    assert records, "the ClickHouse outage must still be logged"
    assert all(r.levelno == logging.WARNING for r in records)
    assert getattr(records[0], "error_class", None) == "ConnectTimeout"


def test_status_page_renders_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        InMemoryStore, "synthetic_probe_samples", _read_through_unreachable_clickhouse
    )
    monkeypatch.setattr(InMemoryStore, "synthetic_rollups", _read_through_unreachable_clickhouse)

    response = _app_client().get("/status", headers={"accept": "text/html"})

    assert response.status_code == 200, response.text
    assert "Status Data Temporarily Unavailable" in response.text
    assert "All Systems Operational" not in response.text


def test_status_history_is_a_retryable_503_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        InMemoryStore, "synthetic_probe_samples", _read_through_unreachable_clickhouse
    )

    response = _app_client().get("/status/history?window=24h&format=json")

    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "30"


def test_status_json_still_raises_on_a_non_availability_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema or code defect is not an outage and must not be masked."""

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("ClickHouse response did not contain data rows")

    monkeypatch.setattr(InMemoryStore, "synthetic_probe_samples", broken)

    client = TestClient(
        create_app(Settings(environment="test"), init_observability=False),
        raise_server_exceptions=False,
    )
    assert client.get("/status.json").status_code == 500


# ------------------------------------------------------------- /console/activity


def test_console_activity_renders_unavailable_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _signed_in_client()
    monkeypatch.setattr(InMemoryStore, "activity_events", _read_through_unreachable_clickhouse)

    with caplog.at_level(logging.WARNING):
        response = client.get("/console/activity")

    assert response.status_code == 200, response.text
    assert "data-activity-unavailable" in response.text
    assert "Activity is temporarily unavailable" in response.text
    # Not the empty-account message: that would claim there was no activity.
    assert "No activity yet. Make a request" not in response.text.split("Recent activity")[1]
    records = [r for r in caplog.records if ANALYTICS_LOGGER_EVENT in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)


def test_console_activity_still_shows_the_empty_state_when_reachable() -> None:
    response = _signed_in_client().get("/console/activity")

    assert response.status_code == 200, response.text
    assert "data-activity-unavailable" not in response.text


def test_console_usage_json_is_a_retryable_503_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _signed_in_client()
    monkeypatch.setattr(InMemoryStore, "usage_series", _read_through_unreachable_clickhouse)

    response = client.get("/console/activity/usage.json?range=24h")

    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "30"
    # A failure is never cached as if it were a result.
    assert len(activity_routes._USAGE_CACHE) == 0


# ------------------------------------------------------------------ /v1/activity


@pytest.mark.parametrize("group_by", [None, "request"])
def test_api_activity_is_a_retryable_503_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, group_by: str | None
) -> None:
    client = _signed_in_client()
    monkeypatch.setattr(
        InMemoryStore, "activity_events_result", _read_through_unreachable_clickhouse
    )
    monkeypatch.setattr(InMemoryStore, "activity_result", _read_through_unreachable_clickhouse)

    params = {"group_by": group_by} if group_by else {}
    response = client.get("/v1/activity", params=params)

    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "30"
    assert response.json()["error"]["type"] == "service_unavailable"


# ------------------------------------------------- benchmark-backed public pages
#
# /leaderboard, /leaderboard/video, /apps and the provider table on /status all
# read the benchmark sample table. A cold instance has no earlier snapshot to
# fall back on, so the read failure reached the route and the page was a 500.

UNAVAILABLE_SURFACES = {
    # The notice at the top of the page, not the shorter line in each table.
    "/leaderboard": (
        "leaderboard",
        "Benchmark data is temporarily unavailable, so no measurements are shown",
    ),
    "/leaderboard/video": (
        "video_leaderboard",
        "Video benchmark data is temporarily unavailable",
    ),
    "/apps": ("apps", "The app directory is temporarily unavailable."),
}
# What a page says when the read worked and simply found nothing. An outage
# must never be reported in these words. (The video page has no such line: it
# lists its configured routes whether or not they have been measured.)
EMPTY_STATE_WORDING = {
    "/leaderboard": "No measurements published for this window yet.",
    "/apps": "No titled apps in the current window yet.",
}


def _surfaces(caplog: pytest.LogCaptureFixture) -> set[str]:
    return {
        str(getattr(record, "surface", ""))
        for record in caplog.records
        if ANALYTICS_LOGGER_EVENT in record.getMessage()
    }


@pytest.mark.parametrize("path", sorted(UNAVAILABLE_SURFACES))
def test_benchmark_page_renders_unavailable_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    surface, notice = UNAVAILABLE_SURFACES[path]
    monkeypatch.setattr(
        InMemoryStore, "provider_benchmark_samples", _read_through_unreachable_clickhouse
    )

    with caplog.at_level(logging.WARNING):
        response = _app_client().get(path, headers={"accept": "text/html"})

    assert response.status_code == 200, response.text
    assert notice in response.text
    if path in EMPTY_STATE_WORDING:
        assert EMPTY_STATE_WORDING[path] not in response.text
    assert surface in _surfaces(caplog)
    records = [r for r in caplog.records if ANALYTICS_LOGGER_EVENT in r.getMessage()]
    assert all(r.levelno == logging.WARNING for r in records)


@pytest.mark.parametrize("path", sorted(UNAVAILABLE_SURFACES))
def test_benchmark_page_keeps_its_empty_state_when_reachable(path: str) -> None:
    response = _app_client().get(path, headers={"accept": "text/html"})

    assert response.status_code == 200, response.text
    assert UNAVAILABLE_SURFACES[path][1] not in response.text
    if path in EMPTY_STATE_WORDING:
        assert EMPTY_STATE_WORDING[path] in response.text


@pytest.mark.parametrize("path", [*sorted(UNAVAILABLE_SURFACES), "/leaderboard/video.json"])
def test_benchmark_page_still_raises_on_a_non_availability_defect(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("ClickHouse response did not contain data rows")

    monkeypatch.setattr(InMemoryStore, "provider_benchmark_samples", broken)

    client = TestClient(
        create_app(Settings(environment="test"), init_observability=False),
        raise_server_exceptions=False,
    )
    assert client.get(path).status_code == 500


def test_video_leaderboard_json_is_a_retryable_503_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feed has no place for a notice; empty rows would read as real ones."""
    monkeypatch.setattr(
        InMemoryStore, "provider_benchmark_samples", _read_through_unreachable_clickhouse
    )

    response = _app_client().get("/leaderboard/video.json")

    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "30"


def test_video_leaderboard_json_still_answers_when_reachable() -> None:
    response = _app_client().get("/leaderboard/video.json")

    assert response.status_code == 200, response.text
    assert "data_unavailable" not in response.json()["data"]


def test_status_page_renders_when_every_analytics_read_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The status page also reads the leaderboard snapshot for its provider
    table, so degrading the synthetic reads alone still left it a 500."""
    for method in ("synthetic_probe_samples", "synthetic_rollups", "provider_benchmark_samples"):
        monkeypatch.setattr(InMemoryStore, method, _read_through_unreachable_clickhouse)

    with caplog.at_level(logging.WARNING):
        response = _app_client().get("/status", headers={"accept": "text/html"})

    assert response.status_code == 200, response.text
    assert "Status Data Temporarily Unavailable" in response.text
    assert "All Systems Operational" not in response.text
    assert "leaderboard" in _surfaces(caplog)


def test_an_unavailable_snapshot_is_not_kept_for_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside the test environment a good snapshot is cached for a minute. The
    unavailable one must not be: the next read has to try ClickHouse again."""
    from trusted_router.routes import public as public_routes

    settings = Settings(environment="local")
    for cache in ("_LEADERBOARD_CACHE", "_VIDEO_LEADERBOARD_CACHE", "_APPS_CACHE"):
        monkeypatch.setattr(public_routes, cache, None)
    monkeypatch.setattr(
        public_routes, "_precomputed_public_analytics_snapshot", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        InMemoryStore, "provider_benchmark_samples", _read_through_unreachable_clickhouse
    )

    assert public_routes._leaderboard_snapshot(settings)["data_unavailable"] is True
    assert public_routes._video_leaderboard_snapshot(settings)["data_unavailable"] is True
    assert public_routes._apps_snapshot(settings)["data_unavailable"] is True
    assert public_routes._LEADERBOARD_CACHE is None
    assert public_routes._VIDEO_LEADERBOARD_CACHE is None
    assert public_routes._APPS_CACHE is None


def test_a_warm_instance_serves_its_last_snapshot_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from trusted_router.routes import public as public_routes

    settings = Settings(environment="local")
    for cache in ("_LEADERBOARD_CACHE", "_VIDEO_LEADERBOARD_CACHE", "_APPS_CACHE"):
        # Older than every snapshot TTL, so each read goes to the store.
        monkeypatch.setattr(public_routes, cache, (-1e9, {"marker": cache}))
    monkeypatch.setattr(
        public_routes, "_precomputed_public_analytics_snapshot", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        InMemoryStore, "provider_benchmark_samples", _read_through_unreachable_clickhouse
    )

    with caplog.at_level(logging.WARNING):
        assert public_routes._leaderboard_snapshot(settings) == {"marker": "_LEADERBOARD_CACHE"}
        assert public_routes._video_leaderboard_snapshot(settings) == {
            "marker": "_VIDEO_LEADERBOARD_CACHE"
        }
        assert public_routes._apps_snapshot(settings) == {"marker": "_APPS_CACHE"}

    assert {
        "leaderboard_serving_stale",
        "video_leaderboard_serving_stale",
        "apps_serving_stale",
    } <= _surfaces(caplog)
    # An outage is a warning, not an error event per page view.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ------------------------------------------------- one outage, one wait
#
# Every read of an unreachable cluster waits out the connect timeout of every
# endpoint. A cold status page makes several reads in a row, so the page that
# should say "unavailable" took longer than the deploy smoke test allows. A
# client that has just failed to connect anywhere does not try again for a
# few seconds.


class _Cluster:
    """A two-endpoint ClickHouse whose behaviour and clock the test controls."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.attempts = 0
        # One answer for every endpoint, or one per host.
        self.answer: Any = httpx.ConnectTimeout
        self.client = OperationalAnalyticsClient(
            base_url="http://clickhouse-a.internal:8123,http://clickhouse-b.internal:8123",
            user="reader",
            password="inert",  # noqa: S106 - inert test credential.
            transport=httpx.MockTransport(self._handle),
            clock=lambda: self.now,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        answer = self.answer
        if isinstance(answer, dict):
            answer = answer[request.url.host]
        if isinstance(answer, int):
            return httpx.Response(answer, json={"data": [{"ok": 1}]}, request=request)
        raise answer("simulated", request=request)

    def read(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.client._query("SELECT 1 FORMAT JSON")


@pytest.mark.parametrize("failure", [httpx.ConnectTimeout, httpx.ConnectError])
def test_an_unreachable_cluster_is_tried_once_then_left_alone(failure: Any) -> None:
    """Black-holed packets time out and a closed port refuses. Both mean no
    endpoint was reached."""
    cluster = _Cluster()
    cluster.answer = failure

    with pytest.raises(failure):
        cluster.read()
    assert cluster.attempts == 2  # both endpoints

    for _ in range(5):
        with pytest.raises(AnalyticsUnreachable) as raised:
            cluster.read()
        # The routes degrade on this exactly as they do on the real failure.
        assert is_analytics_unavailable(raised.value)
    assert cluster.attempts == 2


def test_reads_resume_once_the_hold_is_over() -> None:
    cluster = _Cluster()
    with pytest.raises(httpx.ConnectTimeout):
        cluster.read()

    cluster.now += UNREACHABLE_HOLD_SECONDS - 0.01
    with pytest.raises(AnalyticsUnreachable):
        cluster.read()
    assert cluster.attempts == 2

    cluster.now += 0.01
    cluster.answer = 200
    assert cluster.read() == [{"ok": 1}]
    assert cluster.read() == [{"ok": 1}]
    assert cluster.attempts == 4


def test_a_cluster_still_down_after_the_hold_is_held_again() -> None:
    cluster = _Cluster()
    with pytest.raises(httpx.ConnectTimeout):
        cluster.read()
    cluster.now += UNREACHABLE_HOLD_SECONDS

    with pytest.raises(httpx.ConnectTimeout):
        cluster.read()
    assert cluster.attempts == 4
    with pytest.raises(AnalyticsUnreachable):
        cluster.read()
    assert cluster.attempts == 4


def test_the_hold_is_short() -> None:
    # Reads must come back within seconds of the cluster coming back.
    assert 0 < UNREACHABLE_HOLD_SECONDS <= 15


@pytest.mark.parametrize("answer", [500, 401, 503, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_only_a_failure_to_connect_holds_later_reads(answer: Any) -> None:
    """A server that answered, or accepted the query and then stalled, is
    there. Whatever went wrong belongs to that query, and the next one is sent."""
    cluster = _Cluster()
    cluster.answer = answer
    expected = httpx.HTTPStatusError if isinstance(answer, int) else answer

    with pytest.raises(expected):
        cluster.read()
    first = cluster.attempts
    assert first >= 1
    with pytest.raises(expected):
        cluster.read()
    assert cluster.attempts == 2 * first


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("failure", [httpx.ConnectTimeout, httpx.ConnectError])
def test_an_endpoint_that_answered_keeps_the_next_read_going(failure: Any, status: int) -> None:
    """The first endpoint answers with a status the read moves on from, and
    the last one cannot be reached. The read fails as a failure to connect,
    yet one endpoint is there, so the next read is sent."""
    cluster = _Cluster()
    cluster.answer = {"clickhouse-a.internal": status, "clickhouse-b.internal": failure}

    with pytest.raises(failure):
        cluster.read()
    assert cluster.attempts == 2
    with pytest.raises(failure):
        cluster.read()
    assert cluster.attempts == 4


def test_an_answer_counts_only_for_the_read_that_got_it() -> None:
    """An endpoint that answered one read says nothing about the next. Once
    no endpoint answers, later reads are held, however well earlier ones went."""
    cluster = _Cluster()
    cluster.answer = {
        "clickhouse-a.internal": 503,
        "clickhouse-b.internal": httpx.ConnectError,
    }
    with pytest.raises(httpx.ConnectError):
        cluster.read()
    assert cluster.attempts == 2

    cluster.answer = httpx.ConnectError
    with pytest.raises(httpx.ConnectError) as raised:
        cluster.read()
    assert not isinstance(raised.value, AnalyticsUnreachable)
    assert cluster.attempts == 4
    with pytest.raises(AnalyticsUnreachable):
        cluster.read()
    assert cluster.attempts == 4


def test_an_endpoint_that_hung_up_without_a_response_has_not_answered() -> None:
    """A read that may be replayed moves on from an endpoint that took the
    query and closed the connection. If the last endpoint then cannot be
    reached, nothing answered, and later reads are held."""
    cluster = _Cluster()
    cluster.answer = {
        "clickhouse-a.internal": httpx.RemoteProtocolError,
        "clickhouse-b.internal": httpx.ConnectError,
    }

    with pytest.raises(httpx.ConnectError):
        cluster.client._query("SELECT 1 FORMAT JSON", retry_read_disconnect=True)
    assert cluster.attempts == 2
    with pytest.raises(AnalyticsUnreachable):
        cluster.read()
    assert cluster.attempts == 2


def test_one_reachable_endpoint_is_enough() -> None:
    cluster = _Cluster()
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "clickhouse-a.internal":
            raise httpx.ConnectTimeout("simulated", request=request)
        return httpx.Response(200, json={"data": [{"ok": 1}]}, request=request)

    cluster.client._transport = httpx.MockTransport(handle)

    assert cluster.read() == [{"ok": 1}]
    assert cluster.read() == [{"ok": 1}]
    assert seen == ["clickhouse-a.internal", "clickhouse-b.internal"] * 2


@pytest.mark.parametrize("path", ["/status", "/status.json"])
def test_a_cold_status_page_waits_for_an_unreachable_cluster_only_once(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Every analytics read behind the page goes through the one client the
    store holds. Only the first may reach for the network.

    Outside the test environment a cold page asks for the stored snapshot
    several times as well as the raw samples, so this runs a local app."""
    from trusted_router.routes import public as public_routes

    cluster = _Cluster()
    reads: list[str] = []

    def reader(name: str) -> Any:
        def read(*_args: Any, **_kwargs: Any) -> Any:
            reads.append(name)
            return cluster.read()

        return read

    for method in (
        "public_analytics_snapshot",
        "synthetic_probe_samples",
        "synthetic_rollups",
        "provider_benchmark_samples",
    ):
        monkeypatch.setattr(InMemoryStore, method, reader(method), raising=False)
    for cache in (
        "_STATUS_CACHE",
        "_STATUS_ANALYTICS_CACHE",
        "_FLEET_CACHE",
        "_LEADERBOARD_CACHE",
        "_VIDEO_LEADERBOARD_CACHE",
        "_APPS_CACHE",
        "_ENCRYPTED_AI_CACHE",
        "_LEADERBOARD_EVIDENCE_CACHE",
    ):
        monkeypatch.setattr(public_routes, cache, None)
    monkeypatch.setattr(public_routes, "_STATUS_RESPONSE_CACHE", OrderedDict())
    client = TestClient(create_app(Settings(environment="local"), init_observability=False))

    response = client.get(path, headers={"accept": "text/html"})

    assert response.status_code == 200, response.text
    # Without the hold each of these would wait out both endpoints.
    assert len(reads) >= 3, reads
    assert "public_analytics_snapshot" in reads
    assert cluster.attempts == 2
