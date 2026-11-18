"""The headroom-raced authorize 503 carries its own mark, once, and is
otherwise the storage 503 it always was.

When a workspace's credit is spread across shards and no shard in the
request's write set covers the hold, the typed authorize's credit recovery
(storage_gcp.py) makes at most three repairs: a retry on the funded shards a
snapshot found, a rebalance and re-authorize, a forced reload of the shard
count, or a short wait for the rebalance cooldown. When every one of them
ends with its repair and the workspace's own concurrent requests take the
headroom each time, aggregate exhaustion is not proven, and the answer is a
retryable 503, "credit headroom changed concurrently; retry". It is a
designed answer, not a storage failure, and until now nothing that paired
with the request log told it from one, so "TR Gateway: authorize 503 not
lease headroom" paged on it (2026-11-18 07:41Z).

Here: the recovery raises CreditHeadroomRaced, a StoreUnavailable to every
handler, only when its loop ran out of attempts; a recovery that stopped
early (the forced reload failed, or a rebalance gave an outcome it does not
handle) keeps the plain StoreUnavailable and no mark; after a key repair the
second entry alone decides; the route writes billing.credit_headroom_raced
once for that 503 and for no other; among every line the request leaves,
exactly one is what the counter and the deploy gate read; and the response
is byte for byte the storage 503. Ids are synthetic.
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

from scripts.deploy import billing_5xx_lease_headroom as pairing
from tests.test_billing_5xx_lease_headroom import (
    BUSY_COUNTER,
    HEADROOM_COUNTER,
    RACED_COUNTER,
    STRICT_COUNTER,
    _metric_filters,
    app_line,
    matches,
    request_log,
)
from tests.test_gateway_authorize_spanner_operations import (
    _lookup_body,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - pytest fixture
)
from tests.test_settle_lock_order import _fragmented_authorize, _fragmented_store
from trusted_router import gateway_timing
from trusted_router import storage_gcp as gcp
from trusted_router import storage_gcp_authorize as billing
from trusted_router import storage_gcp_credit_rebalance as credit
from trusted_router import storage_gcp_key_escrow as escrow
from trusted_router.config import Settings
from trusted_router.main import _ApplicationConsoleFormatter, create_app
from trusted_router.routes.internal import gateway
from trusted_router.services import settle_outbox_apply
from trusted_router.storage_errors import (
    CreditHeadroomRaced,
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

WS = "ws-headroom-raced"
AUTHORIZE = "/internal/gateway/authorize"  # the path the enclave calls
TRACE = "4f1c2a9e7b3d4c5e8a6f0b1c2d3e4f5a"
REQUEST_ID = "req-headroom-raced"
RACED_MESSAGE = "credit headroom changed concurrently; retry"
EVENT = "billing.credit_headroom_raced"
LINE_PREFIX = f"INFO trusted_router.routes.internal.gateway {EVENT} "
RELOAD_FAILED = "credit shard-count forced reload failed after incomplete precheck"
FORMATTER = _ApplicationConsoleFormatter("%(levelname)s %(name)s %(message)s")
ENVELOPES = Path(__file__).parent / "fixtures/speculation_v1/authorize-error-envelopes.json"
# The fixture catalog prices this body's hold at 82,960 (42,000 prompt tokens
# at 1 per million and 8,192 output at 5 per million).
HOLD = 82_960


# ---------------------------------------------------------------------------
# The type: a StoreUnavailable to every handler, and not the rebalance-busy
# type.


def test_the_type_is_a_store_unavailable_and_not_the_rebalance_busy_type() -> None:
    error = CreditHeadroomRaced(RACED_MESSAGE)
    assert isinstance(error, StoreUnavailable)
    # The rebalance-busy mark is the held cooldown's alone.
    assert not isinstance(error, CreditRebalanceBusy)
    assert not isinstance(CreditRebalanceBusy("x"), CreditHeadroomRaced)
    assert not isinstance(error, StoreConflict)
    assert str(error) == RACED_MESSAGE
    assert is_transient_store_error(error)
    assert not is_conflict_error(error)
    assert CreditHeadroomRaced not in transient_store_error_types()
    assert not isinstance(error, conflict_store_error_types())
    assert isinstance(error, settle_outbox_apply._TRANSIENT_STORE_EXCS)


def test_the_app_answers_it_with_the_store_unavailable_handler() -> None:
    app = create_app(Settings(environment="test"), init_observability=False)
    handlers = app.exception_handlers
    assert CreditHeadroomRaced not in handlers
    resolved = next(handlers[cls] for cls in CreditHeadroomRaced.__mro__ if cls in handlers)
    assert resolved is handlers[StoreUnavailable]


def test_only_the_multi_shard_refusal_raises_the_type() -> None:
    """The route marks every 503 of this type as the race's, so a second
    place that raised it would be counted as one."""
    package = Path(gcp.__file__).resolve().parent
    raised = [
        (path.relative_to(package).as_posix(), line.strip())
        for path in sorted(package.rglob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\bCreditHeadroomRaced\(", line) and not line.startswith("class ")
    ]
    assert raised == [("storage_gcp.py", f'raise CreditHeadroomRaced("{RACED_MESSAGE}")')]


# ---------------------------------------------------------------------------
# The store: which recoveries raise the type, and which keep the plain one.


def _always_insufficient(calls: list[str]) -> Any:
    def attempt(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append("authorize")
        return {"outcome": billing.AuthorizeOutcome.INSUFFICIENT_CREDITS}

    return attempt


def _raised(store: Any, key: Any, **kwargs: Any) -> StoreUnavailable:
    with pytest.raises(StoreUnavailable) as raised:
        _fragmented_authorize(store, key, **kwargs)
    assert str(raised.value) == RACED_MESSAGE
    return raised.value


@pytest.mark.parametrize("strict", [False, True], ids=["ordinary", "strict-budget"])
def test_three_funded_shard_retries_lost_raise_the_type(
    monkeypatch: pytest.MonkeyPatch, strict: bool,
) -> None:
    """The 2026-11-04 shape: a snapshot finds a funded shard on every pass and
    the retry on it loses it each time. Under a strict budget the recovery
    runs inside the key's admission slot; the answer is the same."""
    store, db, key = _fragmented_store([12_000, 0], 20_000)
    calls: list[str] = []
    monkeypatch.setattr(billing, "authorize_atomic", _always_insufficient(calls))

    error = _raised(store, key, **({"strict_budget": True} if strict else {}))

    assert type(error) is CreditHeadroomRaced
    assert calls == ["authorize"] * 4  # the first, and one retry per pass
    assert not db.reservations


def test_three_rebalances_lost_raise_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-11-18 shape: three "credit rebalance ... outcome=moved"
    lines, each re-authorize lost to the workspace's other requests."""
    store, _db, key = _fragmented_store([6_000, 6_000], 20_000)
    calls: list[str] = []
    rebalances: list[str] = []

    def moved(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        rebalances.append("moved")
        return {"outcome": credit.RebalanceOutcome.MOVED, "moved_micro": 6_000}

    monkeypatch.setattr(billing, "authorize_atomic", _always_insufficient(calls))
    monkeypatch.setattr(credit, "rebalance_credit_for_estimate", moved)
    monkeypatch.setattr(store, "_credit_rebalance_cooldown_allows", lambda _ws: True)

    assert type(_raised(store, key)) is CreditHeadroomRaced
    assert rebalances == ["moved"] * 3
    assert calls == ["authorize"] * 4


def test_cooldown_waits_then_a_lost_rebalance_raise_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two short waits for the cooldown and a rebalance on the last pass,
    lost: every pass ended with its repair."""
    store, _db, key = _fragmented_store([6_000, 6_000], 20_000)
    allowed = iter([False, False, True])
    sleeps: list[float] = []
    monkeypatch.setattr(billing, "authorize_atomic", _always_insufficient([]))
    monkeypatch.setattr(
        credit, "rebalance_credit_for_estimate",
        lambda *_a, **_k: {"outcome": credit.RebalanceOutcome.MOVED},
    )
    monkeypatch.setattr(store, "_credit_rebalance_cooldown_allows", lambda _ws: next(allowed))
    monkeypatch.setattr(gcp.time, "sleep", sleeps.append)

    assert type(_raised(store, key)) is CreditHeadroomRaced
    assert sleeps == [0.25, 0.25]


def test_a_forced_reload_then_lost_retries_raise_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """An incomplete snapshot, a forced reload that finds a third shard and
    re-authorizes on it, then two funded-shard retries, all lost."""
    store, _db, key = _fragmented_store([12_000, 0], 20_000)
    reloaded: list[str] = []
    prechecks = iter([
        credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.INCOMPLETE),
        credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.NOT_NEEDED, 0, (0,)),
        credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.NOT_NEEDED, 0, (0,)),
    ])
    calls: list[str] = []
    monkeypatch.setattr(billing, "authorize_atomic", _always_insufficient(calls))
    monkeypatch.setattr(credit, "credit_headroom_precheck", lambda *_a, **_k: next(prechecks))
    monkeypatch.setattr(store._credit_shard_counts, "invalidate", lambda _ws: reloaded.append("reload"))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: (0, 1, 2) if reloaded else (0, 1))
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: (0, 1))

    assert type(_raised(store, key)) is CreditHeadroomRaced
    assert reloaded == ["reload"]
    assert calls == ["authorize"] * 4  # the first, the reload's, two retries


def test_the_race_after_a_key_repair_raises_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first answer is the key limit; the key repair succeeds, and the
    second entry into the credit recovery loses every pass."""
    store, _db, key = _fragmented_store([12_000, 0], 20_000)
    outcomes = iter([billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED])
    monkeypatch.setattr(
        billing, "authorize_atomic",
        lambda *_a, **_k: {"outcome": next(outcomes, billing.AuthorizeOutcome.INSUFFICIENT_CREDITS)},
    )
    repairs: list[str] = []
    monkeypatch.setattr(escrow, "rebalance_key_limit_headroom", lambda *_a, **_k: repairs.append("key") or True)

    assert type(_raised(store, key)) is CreditHeadroomRaced
    assert repairs == ["key"]


def test_a_failed_forced_reload_keeps_the_plain_store_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The loop broke out on its first pass: no repair was made, so this is
    not the race, and it stays unmarked."""
    store, _db, key = _fragmented_store([12_000, 0], 20_000)

    def fail(_ws: str) -> None:
        raise RuntimeError("shard-count read failed")

    monkeypatch.setattr(billing, "authorize_atomic", _always_insufficient([]))
    monkeypatch.setattr(
        credit, "credit_headroom_precheck",
        lambda *_a, **_k: credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.INCOMPLETE),
    )
    monkeypatch.setattr(store._credit_shard_counts, "invalidate", fail)

    with caplog.at_level(logging.WARNING, logger="trusted_router"):
        error = _raised(store, key)

    assert type(error) is StoreUnavailable
    assert RELOAD_FAILED in caplog.text


def test_a_rebalance_outcome_not_handled_keeps_the_plain_store_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _db, key = _fragmented_store([6_000, 6_000], 20_000)
    monkeypatch.setattr(billing, "authorize_atomic", _always_insufficient([]))
    monkeypatch.setattr(credit, "rebalance_credit_for_estimate", lambda *_a, **_k: {"outcome": "deferred"})
    monkeypatch.setattr(store, "_credit_rebalance_cooldown_allows", lambda _ws: True)

    assert type(_raised(store, key)) is StoreUnavailable


def test_after_a_key_repair_the_second_entry_alone_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first entry ran out of attempts, its last retry ending on the key
    limit; the key repair sent the request round again, and the second
    entry's forced reload failed. The answer is the second entry's: plain."""
    store, _db, key = _fragmented_store([12_000, 0], 20_000)
    outcomes = iter([
        billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,  # the first answer
        billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,  # retry, pass 1
        billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,  # retry, pass 2
        billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED,  # retry, pass 3
    ])
    monkeypatch.setattr(
        billing, "authorize_atomic",
        lambda *_a, **_k: {"outcome": next(outcomes, billing.AuthorizeOutcome.INSUFFICIENT_CREDITS)},
    )
    prechecks = iter([
        credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.NOT_NEEDED, 0, (0,)),
        credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.NOT_NEEDED, 0, (0,)),
        credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.NOT_NEEDED, 0, (0,)),
        credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.INCOMPLETE),
    ])
    monkeypatch.setattr(credit, "credit_headroom_precheck", lambda *_a, **_k: next(prechecks))
    monkeypatch.setattr(escrow, "rebalance_key_limit_headroom", lambda *_a, **_k: True)
    original = store._credit_shard_counts.invalidate
    entries: list[str] = []

    def invalidate(workspace_id: str) -> None:
        entries.append("invalidate")
        if len(entries) > 1:  # the second entry's forced reload
            raise RuntimeError("shard-count read failed")
        original(workspace_id)  # the second entry's proactive refresh

    monkeypatch.setattr(store._credit_shard_counts, "invalidate", invalidate)

    assert type(_raised(store, key)) is StoreUnavailable
    assert entries == ["invalidate", "invalidate"]


# ---------------------------------------------------------------------------
# The route: one line for this 503, and the response it always had.


def _seed(monkeypatch: pytest.MonkeyPatch, shards: tuple[int, ...]) -> tuple[Any, Any, Any]:
    """A workspace whose credit sits on several shards."""
    store, database, key = _seed_typed_gateway_store(workspace_id=WS)
    store._write_entity("credit", WS, CreditAccount(workspace_id=WS, shard_count=len(shards)))
    table = database.typed[CREDIT_BALANCE_TABLE]
    for shard, total in enumerate(shards):
        table[(WS, shard)] = {
            "workspace_id": WS,
            "shard": shard,
            "total_credits": total,
            "total_usage": 0,
            "reserved": 0,
            "source_updated_at": None,
            "updated_at": None,
        }
    candidates = tuple(range(len(shards)))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_credit_rebalance_cooldown_allows", lambda _ws: True)
    monkeypatch.setattr(gcp.time, "sleep", lambda _seconds: None)
    return store, database, key


def _seed_race(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    """Both shards can fund the hold on every snapshot, and every authorize
    loses it to the workspace's other requests."""
    seeded = _seed(monkeypatch, (100_000, 100_000))
    monkeypatch.setattr(
        billing, "authorize_atomic",
        lambda *_a, **_k: {"outcome": billing.AuthorizeOutcome.INSUFFICIENT_CREDITS},
    )
    return seeded


def _post(key: Any, headers: dict[str, str] | None = None) -> Any:
    body = _lookup_body(key)
    body.estimated_input_tokens = 42_000
    body.max_output_tokens = 8192
    client = TestClient(
        create_app(Settings(environment="test"), configure_store_arg=False, init_observability=False)
    )
    return client.post(
        AUTHORIZE,
        json=body.model_dump(exclude_none=True),
        headers=headers if headers is not None else {
            "X-Request-ID": REQUEST_ID, "X-Cloud-Trace-Context": f"{TRACE}/123;o=1",
        },
    )


def _marks(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records
        if record.name == gateway.logger.name and record.getMessage().startswith(EVENT + " ")
    ]


def _messages(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage().startswith(event + " ")]


def _rows(database: Any) -> dict[Any, tuple[int, int, int]]:
    return {
        key: (row["total_credits"], row["total_usage"], row["reserved"])
        for key, row in database.typed[CREDIT_BALANCE_TABLE].items()
        if key[0] == WS
    }


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_the_raced_503_is_marked_once_with_the_requests_trace(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _store, database, key = _seed_race(monkeypatch)
    before = _rows(database)

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response = _post(key)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    (record,) = _marks(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == f"{EVENT} workspace_id={WS} request_id={REQUEST_ID} trace={TRACE}"
    rendered = FORMATTER.format(record)
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
        assert "error_class=CreditHeadroomRaced" in warning.getMessage()
    # No other mark, and no 402's line.
    for other in (
        "billing.credit_rebalance_busy", "billing.strict_budget_busy",
        "billing.lease_headroom_unavailable", "billing.insufficient_credits",
    ):
        assert not [r for r in caplog.records if r.name == gateway.logger.name and r.getMessage().startswith(other)]
    # Nothing held: the answer is a refusal to try now.
    assert _rows(database) == before
    assert not database.reservations


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_exactly_one_of_the_requests_lines_is_counted_and_paired(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The authorize-503 alert pages on more lines than 503s as on fewer.
    Every line the raced 503 leaves, rendered as Cloud Run keeps it, run
    through the four counters' filters and the deploy gate's pairing: one
    line, the mark, is counted, by the raced counter alone, and it excuses
    the 503's request log entry."""
    _store, _database, key = _seed_race(monkeypatch)

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        assert _post(key).status_code == 503

    filters = _metric_filters()
    lines = [FORMATTER.format(record) for record in caplog.records]
    counted = {
        name: [line for line in lines if matches(filters[name], app_line(line))]
        for name in (HEADROOM_COUNTER, BUSY_COUNTER, STRICT_COUNTER, RACED_COUNTER)
    }
    assert counted[HEADROOM_COUNTER] == counted[BUSY_COUNTER] == counted[STRICT_COUNTER] == []
    (mark,) = counted[RACED_COUNTER]
    assert mark.startswith(LINE_PREFIX)
    request = request_log(503, AUTHORIZE, trace=TRACE)
    left, excused = pairing.pair([request], [app_line(line) for line in lines])
    assert left == []
    assert excused == {"headroom raced": 1}
    # Two 503s under the trace: the one mark excuses one, not both.
    left, _excused = pairing.pair([request, request], [app_line(line) for line in lines])
    assert left == [request]


@pytest.mark.usefixtures("fixed_operation_catalog")
def test_the_raced_503_is_byte_for_byte_the_storage_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: 100.0)
    _store, _database, key = _seed_race(monkeypatch)

    response = _post(key)

    pinned = dict(json.loads(ENVELOPES.read_text())["cases"]["storage_unavailable"])
    assert response.status_code == pinned["status"] == 503
    assert response.headers["retry-after"] == pinned["retry_after"] == "1"
    assert response.headers["content-type"] == pinned["content_type"]
    assert response.text == pinned["body_exact"]
    assert RACED_MESSAGE not in response.text


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
        before.setattr(gcp, "CreditHeadroomRaced", StoreUnavailable)
        _store, _database, key = _seed_race(before)
        with caplog.at_level(logging.INFO, logger="trusted_router"):
            plain = _post(key)
        assert not _marks(caplog)
        (warning,) = _messages(caplog, "billing.authorize_storage_unavailable")
        assert "error_class=StoreUnavailable" in warning.getMessage()
    caplog.clear()

    _store, _database, key = _seed_race(monkeypatch)
    with caplog.at_level(logging.INFO, logger="trusted_router"):
        marked = _post(key)
    assert len(_marks(caplog)) == 1

    assert (marked.status_code, marked.text, dict(marked.headers)) == (
        plain.status_code, plain.text, dict(plain.headers),
    )
    assert marked.status_code == 503


def _break_on_reload(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    def fail(_ws: str) -> None:
        raise RuntimeError("shard-count read failed")

    monkeypatch.setattr(
        credit, "credit_headroom_precheck",
        lambda *_a, **_k: credit.CreditHeadroomPrecheck(credit.RebalanceOutcome.INCOMPLETE),
    )
    monkeypatch.setattr(store._credit_shard_counts, "invalidate", fail)


def _break_on_outcome(monkeypatch: pytest.MonkeyPatch, _store: Any) -> None:
    monkeypatch.setattr(credit, "rebalance_credit_for_estimate", lambda *_a, **_k: {"outcome": "deferred"})


@pytest.mark.usefixtures("fixed_operation_catalog")
@pytest.mark.parametrize("stop", [_break_on_reload, _break_on_outcome], ids=["reload-failed", "unknown-outcome"])
def test_a_recovery_that_stopped_early_is_answered_unmarked(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stop: Any,
) -> None:
    """Credit on two shards, neither covering the hold alone: the real
    authorize refuses, and the recovery stops before any repair. The same
    503 and message, the base type, and no mark: it pages as unmarked."""
    store, database, key = _seed(monkeypatch, (50_000, 50_000))
    stop(monkeypatch, store)
    before = _rows(database)

    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response = _post(key)

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Persistent storage is temporarily unavailable; retry."
    (warning,) = _messages(caplog, "billing.authorize_storage_unavailable")
    assert "error_class=StoreUnavailable" in warning.getMessage()
    assert not _marks(caplog)
    assert EVENT not in caplog.text
    assert _rows(database) == before


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
        "/v1/internal/gateway/authorize",
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
        response, key = _post_failing(monkeypatch, CreditHeadroomRaced(RACED_MESSAGE), headers)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    (record,) = _marks(caplog)
    # With no trace header the line says so, and pairs with nothing.
    assert record.getMessage() == (
        f"{EVENT} workspace_id={key.workspace_id} request_id={REQUEST_ID} trace={trace}"
    )
    assert not _messages(caplog, "billing.credit_rebalance_busy")


@pytest.mark.parametrize(
    "error",
    [
        StoreUnavailable("backend down"),
        StoreUnavailable(RACED_MESSAGE),  # the type marks it, never the text
        CreditRebalanceBusy("credit escrow rebalance is busy; retry"),
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
    headers = {"X-Request-ID": REQUEST_ID, "X-Cloud-Trace-Context": f"{TRACE}/1;o=1"}
    with caplog.at_level(logging.INFO, logger="trusted_router"):
        response, _key = _post_failing(monkeypatch, error, headers)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert not _marks(caplog)
    assert EVENT not in caplog.text


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
        gateway._log_credit_headroom_raced(
            WS, request_id=f"r\n{LINE_PREFIX}workspace_id=x request_id=y trace={TRACE}",
            trace_id=TRACE,
        )
    finally:
        gateway.logger.removeHandler(handler)
        gateway.logger.setLevel(previous)
    (record,) = records
    assert "\n" not in record.getMessage()
    assert record.getMessage().endswith(f" trace={TRACE}")
