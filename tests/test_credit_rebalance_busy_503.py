"""The rebalance-busy authorize 503 carries its own mark and is otherwise the
storage 503 it always was.

The typed authorize's credit recovery answers a hold that the workspace's
aggregate credit covers, but no single shard does, with a retryable 503 when
the per-process rebalance cooldown is held by the workspace's other requests
(storage_gcp.py, "credit rebalance cooldown exhausted"). It is a designed
answer, not a storage failure, and until now nothing machine-readable told
it from one: both were a StoreUnavailable, one WARNING line each.

Here: that one site raises CreditRebalanceBusy, a StoreUnavailable to every
handler; the route writes billing.credit_rebalance_busy once for it and for
no other 503; and the response is byte for byte the storage 503 the same
site answered before. Ids are synthetic.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import Aborted, DeadlineExceeded, ServiceUnavailable

from tests.test_gateway_authorize_spanner_operations import (
    _lookup_body,
    _request,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - pytest fixture
)
from trusted_router import gateway_timing
from trusted_router import storage_gcp as gcp
from trusted_router.config import Settings
from trusted_router.main import _ApplicationConsoleFormatter, create_app
from trusted_router.routes.internal import gateway
from trusted_router.services import settle_outbox_apply
from trusted_router.storage_errors import (
    CreditRebalanceBusy,
    StoreConflict,
    StoreUnavailable,
    conflict_store_error_types,
    is_conflict_error,
    is_transient_store_error,
    transient_store_error_types,
)
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount
from trusted_router.strict_budget import StrictBudgetBusy

WS = "ws-rebalance-busy"
AUTHORIZE = "/internal/gateway/authorize"  # the path the enclave calls
TRACE = "97947552453ac21dc57168fd64afffe7"
REQUEST_ID = "req-rebalance-busy"
BUSY_MESSAGE = "credit escrow rebalance is busy; retry"
EVENT = "billing.credit_rebalance_busy"
LINE_PREFIX = f"INFO trusted_router.routes.internal.gateway {EVENT} "
ENVELOPES = Path(__file__).parent / "fixtures/speculation_v1/authorize-error-envelopes.json"
# The fixture catalog prices this body's hold at 82,960 (42,000 prompt tokens
# at 1 per million and 8,192 output at 5 per million): more than either
# shard below holds, less than both together.
HOLD = 82_960
SHARDS = (50_000, 50_000)


def _seed(*, cooldown_held: bool = True, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    """A workspace whose credit covers the hold only across its two shards."""
    store, database, key = _seed_typed_gateway_store(workspace_id=WS)
    store._write_entity("credit", WS, CreditAccount(workspace_id=WS, shard_count=len(SHARDS)))
    table = database.typed[CREDIT_BALANCE_TABLE]
    for shard, total in enumerate(SHARDS):
        table[(WS, shard)] = {
            "workspace_id": WS,
            "shard": shard,
            "total_credits": total,
            "total_usage": 0,
            "reserved": 0,
            "source_updated_at": None,
            "updated_at": None,
        }
    candidates = tuple(range(len(SHARDS)))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    if cooldown_held:
        # The workspace's other requests hold the cooldown for the whole wait.
        monkeypatch.setattr(store, "_credit_rebalance_cooldown_allows", lambda _ws: False)
    monkeypatch.setattr(gcp.time, "sleep", lambda _seconds: None)
    return store, database, key


def _body(key: Any) -> dict[str, Any]:
    body = _lookup_body(key)
    body.estimated_input_tokens = 42_000
    body.max_output_tokens = 8192
    return body.model_dump(exclude_none=True)


def _client() -> TestClient:
    return TestClient(
        create_app(Settings(environment="test"), configure_store_arg=False, init_observability=False)
    )


def _post(key: Any) -> Any:
    return _client().post(
        AUTHORIZE,
        json=_body(key),
        headers={"X-Request-ID": REQUEST_ID, "X-Cloud-Trace-Context": f"{TRACE}/123;o=1"},
    )


def _messages(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage().startswith(event + " ")]


def _rows(database: Any) -> dict[Any, tuple[int, int, int]]:
    return {
        key: (row["total_credits"], row["total_usage"], row["reserved"])
        for key, row in database.typed[CREDIT_BALANCE_TABLE].items()
        if key[0] == WS
    }


def _storage_503_envelope() -> dict[str, Any]:
    """The storage 503 as pinned for the enclave (its timing is all zero under
    the fixed clock): the bytes a rebalance-busy 503 must still be."""
    return dict(json.loads(ENVELOPES.read_text())["cases"]["storage_unavailable"])


# ---------------------------------------------------------------------------
# The type: a StoreUnavailable to every handler.


def test_the_type_is_a_store_unavailable_and_nothing_else() -> None:
    error = CreditRebalanceBusy(BUSY_MESSAGE)
    assert isinstance(error, StoreUnavailable)
    assert not isinstance(error, StoreConflict)
    assert str(error) == BUSY_MESSAGE
    # The predicates and the except tuples every caller uses see it through
    # its base: it is not listed on its own, and it is not a conflict.
    assert is_transient_store_error(error)
    assert not is_conflict_error(error)
    assert CreditRebalanceBusy not in transient_store_error_types()
    assert StoreUnavailable in transient_store_error_types()
    assert not isinstance(error, conflict_store_error_types())
    # The settle-outbox drain parks on it as on any StoreUnavailable.
    assert isinstance(error, settle_outbox_apply._TRANSIENT_STORE_EXCS)


def test_the_app_answers_it_with_the_store_unavailable_handler() -> None:
    """Starlette picks a handler by walking the exception's classes. The
    subclass has none of its own, so the storage 503 handler answers it,
    as it answers StrictBudgetBusy."""
    app = create_app(Settings(environment="test"), init_observability=False)
    handlers = app.exception_handlers
    assert CreditRebalanceBusy not in handlers
    resolved = next(handlers[cls] for cls in CreditRebalanceBusy.__mro__ if cls in handlers)
    assert resolved is handlers[StoreUnavailable]
    assert resolved is not handlers[StoreConflict]


# ---------------------------------------------------------------------------
# The store: that one site, and only it, raises the type.


def test_only_the_cooldown_exhausted_site_raises_the_type() -> None:
    """The route marks every 503 of this type as that site's, so a second
    place that raised it would be counted as a held cooldown."""
    package = Path(gcp.__file__).resolve().parent
    raised = [
        (path.relative_to(package).as_posix(), number)
        for path in sorted(package.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bCreditRebalanceBusy\(", line) and not line.startswith("class ")
    ]
    assert [name for name, _number in raised] == ["storage_gcp.py"]
    ((_name, number),) = raised
    source = Path(gcp.__file__).read_text(encoding="utf-8").splitlines()
    assert source[number - 1].strip() == f'raise CreditRebalanceBusy("{BUSY_MESSAGE}")'
    assert "credit rebalance cooldown exhausted" in "\n".join(source[number - 6:number])


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_the_cooldown_exhausted_site_raises_the_type_with_its_old_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _store, database, key = _seed(monkeypatch=monkeypatch)
    before = _rows(database)
    request = _request()

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        with pytest.raises(StoreUnavailable) as raised:
            gateway._authorize_gateway_sync(
                request, _lookup_body_for(key), Settings(environment="test")
            )

    assert type(raised.value) is CreditRebalanceBusy
    assert str(raised.value) == BUSY_MESSAGE
    assert "credit rebalance cooldown exhausted" in caplog.text
    # Nothing held and nothing written: the answer is a refusal to try now.
    assert _rows(database) == before
    assert not database.reservations
    assert not database.typed.get("tr_gateway_authorization", {})


def _lookup_body_for(key: Any) -> Any:
    body = _lookup_body(key)
    body.estimated_input_tokens = 42_000
    body.max_output_tokens = 8192
    return body


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_the_concurrent_headroom_503_stays_a_plain_store_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The store's other retryable 503 (a funded shard lost to peers on every
    bounded retry) is not this one: it keeps the base type and gets no mark."""
    from trusted_router import storage_gcp_authorize as authorize_mod

    _store, database, key = _seed(cooldown_held=False, monkeypatch=monkeypatch)
    for shard in range(len(SHARDS)):
        database.typed[CREDIT_BALANCE_TABLE][(WS, shard)]["total_credits"] = 100_000

    def always_stolen(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"outcome": authorize_mod.AuthorizeOutcome.INSUFFICIENT_CREDITS}

    monkeypatch.setattr(authorize_mod, "authorize_atomic", always_stolen)

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response = _post(key)

    assert response.status_code == 503
    (warning,) = _messages(caplog, "billing.authorize_storage_unavailable")
    assert "error_class=StoreUnavailable" in warning.getMessage()
    assert not _messages(caplog, EVENT)
    assert EVENT not in caplog.text


# ---------------------------------------------------------------------------
# The route: one line for this 503, and the response it always had.


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_the_busy_503_is_marked_once_with_the_requests_trace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _store, database, key = _seed(monkeypatch=monkeypatch)
    before = _rows(database)

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response = _post(key)

    assert response.status_code == 503
    (record,) = _messages(caplog, EVENT)
    assert record.levelno == logging.INFO
    assert record.name == gateway.logger.name
    assert record.getMessage() == (
        f"{EVENT} workspace_id={WS} request_id={REQUEST_ID} trace={TRACE}"
    )
    # As Cloud Run keeps it, which is what the counter and the gate read.
    rendered = _ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s").format(record)
    assert rendered.startswith(LINE_PREFIX)
    assert rendered.endswith(f" trace={TRACE}")
    # No key, amount or model: the workspace, the request and the trace only.
    assert key.hash not in rendered and key.lookup_hash not in rendered
    assert str(HOLD) not in rendered and "anthropic" not in rendered
    # The two lines every storage 503 of authorize leaves are still written,
    # once each, and name the type.
    (route_warning,) = _messages(caplog, "billing.authorize_storage_unavailable")
    (app_warning,) = _messages(caplog, "storage.unavailable")
    for warning in (route_warning, app_warning):
        assert warning.levelno == logging.WARNING
        assert f"request_id={REQUEST_ID}" in warning.getMessage()
        assert "error_class=CreditRebalanceBusy" in warning.getMessage()
    assert f"workspace_id={WS}" in route_warning.getMessage()
    assert "route=/internal/gateway/authorize" in app_warning.getMessage()
    assert not _messages(caplog, "billing.lease_headroom_unavailable")
    assert not _messages(caplog, "billing.insufficient_credits")
    assert _rows(database) == before
    assert not database.reservations


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_the_busy_503_is_byte_for_byte_the_storage_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: 100.0)
    _store, _database, key = _seed(monkeypatch=monkeypatch)

    response = _post(key)

    pinned = _storage_503_envelope()
    assert response.status_code == pinned["status"] == 503
    assert response.headers["retry-after"] == pinned["retry_after"] == "1"
    assert response.headers["content-type"] == pinned["content_type"]
    assert response.text == pinned["body_exact"]
    error = response.json()["error"]
    assert error["message"] == "Persistent storage is temporarily unavailable; retry."
    assert error["type"] == "service_unavailable"
    # The store's own message never reaches the caller.
    assert BUSY_MESSAGE not in response.text
    assert "rebalance" not in response.text


def _answer(response: Any) -> tuple[int, str, dict[str, str]]:
    return response.status_code, response.text, dict(response.headers)


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_the_response_is_what_the_same_site_answered_as_a_plain_store_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The differential. The site raising the base type, as it did before
    the type existed, and the site raising the type answer the same status,
    body and headers; the line is the only difference."""
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: 100.0)

    with monkeypatch.context() as before:
        # The raise looks the name up in the module: this is yesterday's site.
        before.setattr(gcp, "CreditRebalanceBusy", StoreUnavailable)
        _store, _database, key = _seed(monkeypatch=before)
        with caplog.at_level(logging.INFO, logger="trusted_router"):
            plain = _post(key)
        assert "credit rebalance cooldown exhausted" in caplog.text
        assert not _messages(caplog, EVENT)
        (warning,) = _messages(caplog, "billing.authorize_storage_unavailable")
        assert "error_class=StoreUnavailable" in warning.getMessage()
    caplog.clear()

    _store, _database, key = _seed(monkeypatch=monkeypatch)
    with caplog.at_level(logging.INFO, logger="trusted_router"):
        marked = _post(key)
    assert len(_messages(caplog, EVENT)) == 1

    assert _answer(marked) == _answer(plain)
    assert marked.status_code == 503


class _FailingTypedStore:
    """A typed store whose authorize fails with a given error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def get_typed_authorization_by_idempotency(self, *_args: object) -> None:
        return None

    def authorize_gateway_typed(self, **_kwargs: object) -> None:
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        StoreUnavailable("backend down"),
        StoreUnavailable(BUSY_MESSAGE),  # the type marks it, never the text
        StoreUnavailable("credit headroom changed concurrently; retry"),
        StrictBudgetBusy("Strict budget authorization is busy; retry with backoff"),
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
    from tests.test_gateway_aborted_handler import _seed_key, _settings

    app = create_app(_settings(), init_observability=False)
    key = _seed_key()
    monkeypatch.setattr(gateway, "typed_billing_store", lambda _store: _FailingTypedStore(error))

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response = TestClient(app).post(
            "/v1/internal/gateway/authorize",
            headers={"X-Request-ID": REQUEST_ID, "X-Cloud-Trace-Context": f"{TRACE}/1;o=1"},
            json={
                "api_key_hash": key.hash,
                "model": "anthropic/claude-haiku-4.5",
                "estimated_input_tokens": 100,
                "max_output_tokens": 100,
            },
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert EVENT not in caplog.text


def test_the_marked_type_is_marked_wherever_the_typed_store_raises_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The control for the cases above: the same route and the same fake
    store, raising the type, writes the line once."""
    from tests.test_gateway_aborted_handler import _seed_key, _settings

    app = create_app(_settings(), init_observability=False)
    key = _seed_key()
    monkeypatch.setattr(
        gateway, "typed_billing_store",
        lambda _store: _FailingTypedStore(CreditRebalanceBusy(BUSY_MESSAGE)),
    )

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response = TestClient(app).post(
            "/v1/internal/gateway/authorize",
            headers={"X-Request-ID": REQUEST_ID},
            json={
                "api_key_hash": key.hash,
                "model": "anthropic/claude-haiku-4.5",
                "estimated_input_tokens": 100,
                "max_output_tokens": 100,
            },
        )

    assert response.status_code == 503
    (record,) = _messages(caplog, EVENT)
    # No trace header on the request: the line says so, and pairs with nothing.
    assert record.getMessage() == (
        f"{EVENT} workspace_id={key.workspace_id} request_id={REQUEST_ID} trace=-"
    )


def test_the_lines_fields_are_sanitised_like_the_routes_other_lines() -> None:
    """The middleware already narrows X-Request-ID to letters, digits, dashes
    and underscores. The line does not rely on that: its fields go through
    _log_value, so a newline cannot start a second line in a text sink,
    where the counter and the gate read prefixes."""
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Keep(logging.INFO)
    previous = gateway.logger.level
    gateway.logger.addHandler(handler)
    gateway.logger.setLevel(logging.INFO)
    try:
        gateway._log_credit_rebalance_busy(
            WS, request_id=f"r\n{LINE_PREFIX}workspace_id=x request_id=y trace={TRACE}",
            trace_id=TRACE,
        )
    finally:
        gateway.logger.removeHandler(handler)
        gateway.logger.setLevel(previous)
    (record,) = records
    assert "\n" not in record.getMessage()
    assert record.getMessage().endswith(f" trace={TRACE}")
