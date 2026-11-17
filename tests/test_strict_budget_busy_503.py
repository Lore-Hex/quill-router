"""The strict-budget-busy authorize 503 carries its own mark, once, and is
otherwise the 503 it always was.

A key created with budget_strict authorizes through a per-process admission
slot (trusted_router/strict_budget.py): one strict authorize per key, a
caller that finds the slot taken waits STRICT_ADMISSION_WAIT_SECONDS, and a
caller still without it (or one that finds the waiters full) gets
StrictBudgetBusy, which the app's handler answers with a retryable 503. It
is a designed answer, not a storage failure, and until now nothing that
paired with the request log told it from one: the app's lines for it carried
no trace, so "TR Gateway: authorize 503 not lease headroom" paged on it.

Here: the authorize route writes billing.strict_budget_busy, with the
request's trace, once for that 503, on the typed (Spanner) path and on the
legacy (Postgres) path, and for no other 503; among every line the request
leaves, exactly one is what the counter and the deploy gate read; and the
response is unchanged. Ids are synthetic.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import Aborted, DeadlineExceeded, ServiceUnavailable

from scripts.deploy import billing_5xx_lease_headroom as pairing
from tests.fakes.spanner import make_fake_store
from tests.test_billing_5xx_lease_headroom import (
    BUSY_COUNTER,
    HEADROOM_COUNTER,
    STRICT_COUNTER,
    _metric_filters,
    app_line,
    matches,
    request_log,
)
from trusted_router import gateway_timing, strict_budget
from trusted_router.config import Settings
from trusted_router.main import _ApplicationConsoleFormatter, create_app
from trusted_router.routes.internal import gateway
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_errors import CreditRebalanceBusy, StoreConflict, StoreUnavailable
from trusted_router.strict_budget import StrictBudgetBusy, strict_budget_slot

AUTHORIZE = "/v1/internal/gateway/authorize"
TRACE = "c3b1d2e4f5a60718293a4b5c6d7e8f90"
REQUEST_ID = "req-strict-busy"
EVENT = "billing.strict_budget_busy"
LINE_PREFIX = f"INFO trusted_router.routes.internal.gateway {EVENT} "
BUSY_ANSWER = "Strict budget authorization is busy; retry with backoff."
FORMATTER = _ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s")
HEADERS = {"X-Request-ID": REQUEST_ID, "X-Cloud-Trace-Context": f"{TRACE}/123;o=1"}


@pytest.fixture(autouse=True)
def _short_admission_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The slot's one-second wait, shortened: the wait is not under test,
    the answer after it is. strict_budget_slot reads the module value."""
    monkeypatch.setattr(strict_budget, "STRICT_ADMISSION_WAIT_SECONDS", 0.05)


def _marks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The route's marks: the gateway logger's billing.strict_budget_busy."""
    return [
        record for record in caplog.records
        if record.name == gateway.logger.name and record.getMessage().startswith(EVENT + " ")
    ]


def _messages(caplog: pytest.LogCaptureFixture, logger: str, event: str) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records
        if record.name == logger and record.getMessage().startswith(event + " ")
    ]


def _rendered(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every line the request left, as Cloud Run keeps it."""
    return [FORMATTER.format(record) for record in caplog.records]


def _strict_key() -> tuple[Any, Any, Any, Any]:
    """A workspace and a strict key on the typed (fake Spanner) store."""
    store, database = make_fake_store()
    workspace = store.create_workspace("owner", "strict", trial_credit_microdollars=1_000_000)
    _raw, key = store.create_api_key(
        workspace_id=workspace.id,
        creator_user_id="owner",
        name="strict",
        budget_strict=True,
        limit_daily_microdollars=1_000_000,
    )
    configure_store(store)
    return store, database, workspace, key


def _client() -> TestClient:
    return TestClient(
        create_app(Settings(environment="test"), configure_store_arg=False, init_observability=False)
    )


def _body(key: Any) -> dict[str, Any]:
    return {
        "api_key_hash": key.hash,
        "model": "anthropic/claude-opus-4.7",
        "estimated_input_tokens": 10,
        "max_output_tokens": 10,
    }


def _reserved(database: Any, key: Any) -> int:
    return int(database.typed.get("tr_key_limit", {}).get((key.hash, 0), {}).get("reserved", 0))


# ---------------------------------------------------------------------------
# The typed (Spanner) path: the slot really held.


def test_the_busy_503_is_marked_once_with_the_requests_trace(caplog: pytest.LogCaptureFixture) -> None:
    _store, database, workspace, key = _strict_key()
    client = _client()

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        with strict_budget_slot(key.hash):
            response = client.post(AUTHORIZE, json=_body(key), headers=HEADERS)

    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["message"] == BUSY_ANSWER
    (record,) = _marks(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        f"{EVENT} workspace_id={workspace.id} request_id={REQUEST_ID} trace={TRACE}"
    )
    rendered = FORMATTER.format(record)
    assert rendered.startswith(LINE_PREFIX)
    assert rendered.endswith(f" trace={TRACE}")
    # No key, amount or model: the workspace, the request and the trace only.
    assert key.hash not in caplog.text and key.lookup_hash not in caplog.text
    assert "opus" not in rendered
    # The two warnings this 503 always left are still written, once each.
    (route_warning,) = _messages(caplog, gateway.logger.name, "billing.authorize_strict_budget_busy")
    (app_warning,) = _messages(caplog, "trusted_router.main", EVENT)
    for warning in (route_warning, app_warning):
        assert warning.levelno == logging.WARNING
        assert f"request_id={REQUEST_ID}" in warning.getMessage()
        assert "error_class=StrictBudgetBusy" in warning.getMessage()
        assert "trace=" not in warning.getMessage()
    assert "route=/internal/gateway/authorize" in app_warning.getMessage()
    assert "storage.unavailable" not in caplog.text
    # Nothing held: the answer is a refusal to try now.
    assert _reserved(database, key) == 0


def test_exactly_one_of_the_requests_lines_is_counted_and_paired(caplog: pytest.LogCaptureFixture) -> None:
    """The authorize-503 alert pages on more lines than 503s as on fewer.
    Every line the busy 503 leaves, rendered as Cloud Run keeps it, run
    through the three counters' filters and the deploy gate's pairing: one
    line, the mark, is counted, by the strict counter alone, and it excuses
    the 503's request log entry."""
    _store, _database, _workspace, key = _strict_key()

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        with strict_budget_slot(key.hash):
            response = _client().post(AUTHORIZE, json=_body(key), headers=HEADERS)
    assert response.status_code == 503

    filters = _metric_filters()
    lines = _rendered(caplog)
    # The event name is on three lines: the mark and the two warnings.
    assert sum("strict_budget_busy" in line for line in lines) == 3
    counted = {
        name: [line for line in lines if matches(filters[name], app_line(line))]
        for name in (HEADROOM_COUNTER, BUSY_COUNTER, STRICT_COUNTER)
    }
    assert counted[HEADROOM_COUNTER] == [] and counted[BUSY_COUNTER] == []
    (mark,) = counted[STRICT_COUNTER]
    assert mark.startswith(LINE_PREFIX)
    request = request_log(503, AUTHORIZE, trace=TRACE)
    left, excused = pairing.pair([request], [app_line(line) for line in lines])
    assert left == []
    assert excused == {"strict budget busy": 1}
    # Two 503s under the trace: the one mark excuses one, not both.
    left, _excused = pairing.pair([request, request], [app_line(line) for line in lines])
    assert left == [request]


def test_the_mark_is_the_only_difference_in_the_answer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The differential: the same request with the mark taken out answers
    the same status, body and headers."""
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: 100.0)
    _store, _database, _workspace, key = _strict_key()
    client = _client()

    with monkeypatch.context() as unmarked:
        unmarked.setattr(gateway, "_log_strict_budget_busy", lambda *_args, **_kwargs: None)
        with caplog.at_level(logging.INFO, logger="trusted_router"):
            with strict_budget_slot(key.hash):
                plain = client.post(AUTHORIZE, json=_body(key), headers=HEADERS)
        assert not _marks(caplog)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="trusted_router"):
        with strict_budget_slot(key.hash):
            marked = client.post(AUTHORIZE, json=_body(key), headers=HEADERS)
    assert len(_marks(caplog)) == 1

    assert marked.status_code == plain.status_code == 503
    assert marked.text == plain.text
    assert dict(marked.headers) == dict(plain.headers)


def test_a_served_strict_authorize_writes_no_mark(caplog: pytest.LogCaptureFixture) -> None:
    """The slot free: the authorize is served and nothing is marked."""
    _store, _database, _workspace, key = _strict_key()

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response = _client().post(AUTHORIZE, json=_body(key), headers=HEADERS)

    assert response.status_code == 200, response.text
    assert not _marks(caplog)
    assert EVENT not in caplog.text


# ---------------------------------------------------------------------------
# Wherever the typed store raises it, and nothing else.


class _FailingTypedStore:
    """A typed store whose authorize fails with a given error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def get_typed_authorization_by_idempotency(self, *_args: object) -> None:
        return None

    def authorize_gateway_typed(self, **_kwargs: object) -> None:
        raise self._error


def _post_failing(
    monkeypatch: pytest.MonkeyPatch, error: Exception, headers: dict[str, str],
) -> tuple[Any, Any]:
    from tests.test_gateway_aborted_handler import _seed_key, _settings

    app = create_app(_settings(), init_observability=False)
    key = _seed_key()
    monkeypatch.setattr(gateway, "typed_billing_store", lambda _store: _FailingTypedStore(error))
    response = TestClient(app).post(
        AUTHORIZE,
        headers=headers,
        json={
            "api_key_hash": key.hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 100,
            "max_output_tokens": 100,
        },
    )
    return response, key


@pytest.mark.parametrize(
    ("headers", "trace"),
    [
        ({"X-Request-ID": REQUEST_ID}, "-"),
        ({"X-Request-ID": REQUEST_ID, "traceparent": f"00-{TRACE}-00f067aa0ba902b7-01"}, TRACE),
    ],
    ids=["no-trace", "traceparent"],
)
def test_the_type_is_marked_wherever_the_typed_store_raises_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    headers: dict[str, str], trace: str,
) -> None:
    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response, key = _post_failing(
            monkeypatch, StrictBudgetBusy("Strict budget authorization is busy; retry with backoff"), headers,
        )

    assert response.status_code == 503
    assert response.json()["error"]["message"] == BUSY_ANSWER
    (record,) = _marks(caplog)
    # With no trace header the line says so, and pairs with nothing.
    assert record.getMessage() == (
        f"{EVENT} workspace_id={key.workspace_id} request_id={REQUEST_ID} trace={trace}"
    )
    assert not _messages(caplog, gateway.logger.name, "billing.credit_rebalance_busy")


@pytest.mark.parametrize(
    "error",
    [
        StoreUnavailable("backend down"),
        StoreUnavailable("Strict budget authorization is busy; retry with backoff"),  # the type marks it
        StoreUnavailable("credit headroom changed concurrently; retry"),
        CreditRebalanceBusy("credit escrow rebalance is busy; retry"),
        DeadlineExceeded("spanner deadline"),
        ServiceUnavailable("no instance"),
        StoreConflict("hot credit shard"),
        Aborted("lock wounded"),
    ],
    ids=lambda error: f"{type(error).__name__}:{str(error)[:24]}",
)
def test_no_other_authorize_503_is_marked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception,
) -> None:
    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response, _key = _post_failing(monkeypatch, error, HEADERS)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert not _marks(caplog)
    assert EVENT not in caplog.text
    # The rebalance-busy 503 keeps its own mark, once.
    expected = 1 if isinstance(error, CreditRebalanceBusy) else 0
    assert len(_messages(caplog, gateway.logger.name, "billing.credit_rebalance_busy")) == expected


# ---------------------------------------------------------------------------
# The legacy path: the Postgres store's strict key reserve takes the same slot
# (storage_postgres.py, reserve_key_limit) and raises the same type before
# any hold. The memory store stands in for it here; the type is what the
# route reads.


def _post_legacy(
    monkeypatch: pytest.MonkeyPatch, reserve_key_limit_error: Exception,
) -> tuple[Any, Any, list[str]]:
    from tests.test_gateway_aborted_handler import _seed_key, _settings

    app = create_app(_settings(), init_observability=False)
    key = _seed_key()
    calls: list[str] = []

    def refuse(_self: Any, *_args: Any, **_kwargs: Any) -> Any:
        calls.append("reserve_key_limit")
        raise reserve_key_limit_error

    def reserve(_self: Any, *_args: Any, **_kwargs: Any) -> Any:
        calls.append("reserve")
        raise AssertionError("no credit hold may be taken after the key refusal")

    # The backend class, not the STORE proxy (AGENTS.md, issue #333).
    monkeypatch.setattr(InMemoryStore, "reserve_key_limit", refuse)
    monkeypatch.setattr(InMemoryStore, "reserve", reserve)
    response = TestClient(app).post(
        AUTHORIZE,
        headers=HEADERS,
        json={
            "api_key_hash": key.hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 100,
            "max_output_tokens": 100,
        },
    )
    return response, key, calls


def test_the_postgres_paths_busy_503_is_marked_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response, key, calls = _post_legacy(
            monkeypatch, StrictBudgetBusy("Strict budget authorization timed out; retry"),
        )

    assert calls == ["reserve_key_limit"]
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["message"] == BUSY_ANSWER
    (record,) = _marks(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        f"{EVENT} workspace_id={key.workspace_id} request_id={REQUEST_ID} trace={TRACE}"
    )
    # The handler's warning, once; the route's typed-path warning is not on
    # this path.
    (app_warning,) = _messages(caplog, "trusted_router.main", EVENT)
    assert "error_class=StrictBudgetBusy" in app_warning.getMessage()
    assert not _messages(caplog, gateway.logger.name, "billing.authorize_strict_budget_busy")
    # One line counted, and it pairs.
    lines = _rendered(caplog)
    assert [line for line in lines if matches(_metric_filters()[STRICT_COUNTER], app_line(line))] == [
        FORMATTER.format(record)
    ]
    assert pairing.unpaired([request_log(503, AUTHORIZE, trace=TRACE)], [app_line(line) for line in lines]) == []


def test_the_postgres_paths_other_503s_are_not_marked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response, _key, calls = _post_legacy(
            monkeypatch, StoreUnavailable("Postgres could not service the storage operation"),
        )

    assert calls == ["reserve_key_limit"]
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Persistent storage is temporarily unavailable; retry."
    assert not _marks(caplog)
    assert EVENT not in caplog.text


# ---------------------------------------------------------------------------
# The line itself.


def test_the_lines_fields_are_sanitised_like_the_routes_other_lines() -> None:
    """Its fields go through _log_value, so a newline cannot start a second
    line in a text sink, where the counter and the gate read prefixes."""
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Keep(logging.INFO)
    previous = gateway.logger.level
    gateway.logger.addHandler(handler)
    gateway.logger.setLevel(logging.INFO)
    try:
        gateway._log_strict_budget_busy(
            "ws-1", request_id=f"r\n{LINE_PREFIX}workspace_id=x request_id=y trace={TRACE}",
            trace_id=TRACE,
        )
    finally:
        gateway.logger.removeHandler(handler)
        gateway.logger.setLevel(previous)
    (record,) = records
    assert "\n" not in record.getMessage()
    assert record.getMessage().endswith(f" trace={TRACE}")


def test_the_route_writes_the_mark_at_two_sites_one_per_path() -> None:
    """One call on the typed path and one on the legacy path, which are the
    two arms of one if: a request takes one, so at most one mark. A third
    call (in the app's handler, say) would mark the typed path's 503 twice,
    and the alert pages on more lines than 503s."""
    source_root = Path(gateway.__file__).resolve().parents[2]
    calls = [
        (path.relative_to(source_root).as_posix(), number)
        for path in sorted(source_root.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\b_log_strict_budget_busy\(", line) and not line.lstrip().startswith("def ")
    ]
    assert [name for name, _number in calls] == ["routes/internal/gateway.py"] * 2, calls
    source = Path(gateway.__file__).read_text(encoding="utf-8")
    sites = [match.start() for match in re.finditer(r"(?<!def )\b_log_strict_budget_busy\(", source)]
    assert len(sites) == 2
    # Each follows its own arm's store call: the typed authorize, then the
    # legacy key reserve.
    typed_arm = source.index("_typed_store.authorize_gateway_typed(")
    legacy_arm = source.index("key_limit_reservation = STORE.reserve_key_limit(")
    assert typed_arm < sites[0] < legacy_arm < sites[1]
