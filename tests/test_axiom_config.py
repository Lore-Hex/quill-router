from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import axiom_py
import axiom_py.logging as axiom_logging
import pytest
import requests
from fastapi.testclient import TestClient
from requests.exceptions import ConnectionError as RequestsConnectionError

import trusted_router.axiom_config as axiom_config
import trusted_router.routes.public as public_routes
from trusted_router.axiom_config import (
    _AxiomScrubFilter,
    _client_kwargs,
    _resolve_level,
    _SafeAxiomHandler,
    init_axiom,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services.email import EmailMessage


def _fake_axiom_token() -> str:
    return "xaat_test"


@pytest.fixture
def clean_axiom_logging_state() -> Iterator[None]:
    root = logging.getLogger()
    original_root_level = root.level
    original_disable = logging.root.manager.disable
    original_handlers = list(root.handlers)
    logger_names = ("trusted_router", "trusted_router.anything", "thirdparty")
    original_logger_states = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in logger_names
    }

    root.setLevel(logging.WARNING)
    logging.disable(logging.NOTSET)
    for name in logger_names:
        logger = logging.getLogger(name)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        logger.disabled = False

    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        for name, (level, propagate, disabled) in original_logger_states.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
        root.setLevel(original_root_level)
        logging.disable(original_disable)


class _ExplodingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("axiom dataset is not ingestible")


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(logging.makeLogRecord(record.__dict__.copy()))


class _StubAxiomClient:
    def __init__(self, *, fail_ingest: bool = False) -> None:
        self.fail_ingest = fail_ingest
        self.ingested: list[tuple[str, list[dict[str, object]]]] = []
        self.session = requests.Session()
        self.shutdown_callbacks: list[Callable[[], None]] = []

    def before_shutdown(self, callback: Callable[[], None]) -> None:
        self.shutdown_callbacks.append(callback)

    def ingest_events(self, dataset: str, events: list[dict[str, object]]) -> None:
        if self.fail_ingest:
            raise RequestsConnectionError("stale keepalive socket")
        self.ingested.append((dataset, list(events)))


def _build_wrapped_raw_axiom_handler(
    client: _StubAxiomClient,
) -> axiom_logging.AxiomHandler:
    raw_handler = axiom_logging.AxiomHandler(client, "test-logs", interval=60)
    raw_handler.flush = axiom_config._safe_flush_wrapper(raw_handler.flush)
    safe_handler = _SafeAxiomHandler(raw_handler)
    assert safe_handler.inner is raw_handler
    return raw_handler


@contextmanager
def _capture_lead_and_root_logs() -> Iterator[tuple[_CapturingHandler, _CapturingHandler]]:
    lead_logger = logging.getLogger("tr_leads.trustedos_inquiry")
    root = logging.getLogger()
    lead_capture = _CapturingHandler()
    root_capture = _CapturingHandler()
    lead_logger.addHandler(lead_capture)
    root.addHandler(root_capture)
    try:
        yield lead_capture, root_capture
    finally:
        lead_logger.removeHandler(lead_capture)
        root.removeHandler(root_capture)


def _trustedos_client(settings: Settings) -> TestClient:
    with public_routes._INQUIRY_RATE_LOCK:
        public_routes._INQUIRY_HITS.clear()
    return TestClient(create_app(settings, init_observability=False))


def _trustedos_payload(*, company: str, message: str) -> dict[str, str]:
    return {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "company": company,
        "message": message,
    }


def _scrubbed_trustedos_messages(
    caplog: pytest.LogCaptureFixture,
    event_name: str,
) -> list[str]:
    messages: list[str] = []
    for record in caplog.records:
        if record.name != "trusted_router.routes.public":
            continue
        if not record.getMessage().startswith(f"{event_name} "):
            continue
        assert _AxiomScrubFilter().filter(record) is True
        messages.append(record.getMessage())
    return messages


def _assert_inquiry_log_minimized(
    shipped_message: str,
    *,
    company: str,
    message: str,
) -> None:
    assert "company_len=" in shipped_message
    assert "message_len=" in shipped_message
    assert f"company_len={len(company)}" in shipped_message
    assert f"message_len={len(message)}" in shipped_message
    assert company not in shipped_message
    assert message not in shipped_message


def _assert_lead_log_local_only(
    lead_records: list[logging.LogRecord],
    root_records: list[logging.LogRecord],
    *,
    recipient: str | None,
    company: str,
    message: str,
) -> None:
    lead_logger = logging.getLogger("tr_leads.trustedos_inquiry")
    assert lead_logger.propagate is False
    assert not lead_logger.name.startswith("trusted_router")

    lead_messages = [
        record.getMessage()
        for record in lead_records
        if record.name == lead_logger.name
        and record.getMessage().startswith("trustedos_inquiry.lead ")
    ]
    assert len(lead_messages) == 1
    assert f"recipient={recipient!r}" in lead_messages[0]
    assert "name='Ada Lovelace'" in lead_messages[0]
    assert "email='ada@example.com'" in lead_messages[0]
    assert f"company={company!r}" in lead_messages[0]
    assert f"message={message!r}" in lead_messages[0]

    root_payloads = [f"{record.getMessage()} {record.__dict__!r}" for record in root_records]
    assert all(company not in payload for payload in root_payloads)
    assert all(message not in payload for payload in root_payloads)


def test_safe_axiom_handler_never_raises_from_emit(capsys) -> None:
    handler = _SafeAxiomHandler(_ExplodingHandler())
    record = logging.LogRecord(
        name="trusted_router.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="ses_notification.signature_invalid reason=forged",
        args=(),
        exc_info=None,
    )

    handler.handle(record)
    handler.handle(record)

    captured = capsys.readouterr()
    assert "axiom.emit_failed dropped=true" in captured.err
    assert captured.err.count("axiom.emit_failed") == 1


def test_axiom_timer_flush_wrapper_never_raises_and_throttles(capsys) -> None:
    client = _StubAxiomClient(fail_ingest=True)
    raw_handler = _build_wrapped_raw_axiom_handler(client)

    raw_handler.buffer.append({"message": "one"})
    raw_handler.flush()
    raw_handler.buffer.append({"message": "two"})
    raw_handler.flush()

    captured = capsys.readouterr()
    assert "axiom.flush_failed dropped=true" in captured.err
    assert captured.err.count("axiom.flush_failed") == 1


def test_axiom_timer_flush_wrapper_success_delivers_buffer(capsys) -> None:
    client = _StubAxiomClient()
    raw_handler = _build_wrapped_raw_axiom_handler(client)

    raw_handler.buffer.append({"message": "ok"})
    raw_handler.flush()

    captured = capsys.readouterr()
    assert captured.err == ""
    assert client.ingested == [("test-logs", [{"message": "ok"}])]
    assert raw_handler.buffer == []


def test_axiom_client_session_mounts_post_retry_adapter() -> None:
    client = _StubAxiomClient()

    axiom_config._mount_axiom_retry_adapter(client)
    axiom_config._mount_axiom_retry_adapter(object())

    adapter = client.session.get_adapter("https://api.axiom.co/v1/datasets/test/ingest")
    retry = adapter.max_retries
    assert retry.total == 2
    assert retry.connect == 2
    assert retry.read == 1
    assert retry.backoff_factor == 0.2
    assert retry.allowed_methods == frozenset({"POST"})


def test_axiom_scrub_filter_collapses_and_redacts_positional_args() -> None:
    record = logging.LogRecord(
        name="trusted_router.email",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="email_send.fallback %s",
        args=("body https://x/auth/verify-email?token=abc123 for a@b.com",),
        exc_info=None,
    )

    assert _AxiomScrubFilter().filter(record) is True

    assert record.args is None
    assert "token=[Filtered]" in record.msg
    assert "[Filtered-email]" in record.msg
    record_payload = repr(record.__dict__)
    assert "abc123" not in record_payload
    assert "a@b.com" not in record_payload


def test_axiom_scrub_filter_truncates_formatted_message_to_2000_chars() -> None:
    record = logging.LogRecord(
        name="trusted_router.email",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="email_send.fallback %s",
        args=("x" * 2_500,),
        exc_info=None,
    )

    assert _AxiomScrubFilter().filter(record) is True

    assert record.args is None
    assert len(record.msg) == _AxiomScrubFilter._MAX_MESSAGE_CHARS
    assert record.msg.endswith(_AxiomScrubFilter._TRUNCATION_SUFFIX)


def test_axiom_scrub_filter_tolerates_bad_format_args() -> None:
    raw_arg = "token=abc123"
    record = logging.LogRecord(
        name="trusted_router.email",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="email_send.fallback %s %s",
        args=(raw_arg,),
        exc_info=None,
    )

    assert _AxiomScrubFilter().filter(record) is True
    assert record.msg == "email_send.fallback %s %s"
    assert record.args is None
    assert raw_arg not in repr(record.__dict__)


def test_trustedos_inquiry_log_ships_lengths_not_free_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent_messages: list[EmailMessage] = []

    class FakeEmailService:
        def send(self, message: EmailMessage) -> bool:
            sent_messages.append(message)
            return True

    monkeypatch.setattr(public_routes, "get_email_service", lambda _settings: FakeEmailService())
    client = _trustedos_client(
        Settings(
            environment="test",
            partner_inquiry_email="leads@example.com",
        )
    )
    message_body = "Please call about SSN 123-45-6789 and card 4111111111111111."

    with caplog.at_level(logging.INFO, logger="trusted_router.routes.public"):
        response = client.post(
            "/trustedos/inquiry",
            json={
                "name": "Ada Lovelace",
                "email": "ada@example.com",
                "company": "Analytical Engines Ltd",
                "message": message_body,
            },
        )

    assert response.status_code == 200
    assert sent_messages
    assert message_body in sent_messages[0].text_body
    received_records = [
        record
        for record in caplog.records
        if record.name == "trusted_router.routes.public"
        and record.getMessage().startswith("trustedos_inquiry.received ")
    ]
    assert len(received_records) == 1

    record = received_records[0]
    assert _AxiomScrubFilter().filter(record) is True
    shipped_message = record.getMessage()
    assert "name='Ada Lovelace'" in shipped_message
    assert "email='[Filtered-email]'" in shipped_message
    assert "company_len=22" in shipped_message
    assert f"message_len={len(message_body)}" in shipped_message
    assert "message=" not in shipped_message
    assert message_body not in shipped_message
    assert "Analytical Engines Ltd" not in shipped_message
    assert "ada@example.com" not in shipped_message


def test_trustedos_inquiry_no_recipient_log_ships_lengths_not_free_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    company = "Round Two Company Marker"
    message = "Round two no recipient message marker."
    client = _trustedos_client(
        Settings(
            environment="test",
            partner_inquiry_email=None,
            ses_from_email=None,
        )
    )

    with _capture_lead_and_root_logs() as (lead_capture, root_capture):
        with caplog.at_level(logging.ERROR, logger="trusted_router.routes.public"):
            response = client.post(
                "/trustedos/inquiry",
                json=_trustedos_payload(company=company, message=message),
            )

    assert response.status_code == 200
    shipped_messages = _scrubbed_trustedos_messages(caplog, "trustedos_inquiry.no_recipient")
    assert len(shipped_messages) == 1
    _assert_inquiry_log_minimized(shipped_messages[0], company=company, message=message)
    _assert_lead_log_local_only(
        lead_capture.records,
        root_capture.records,
        recipient=None,
        company=company,
        message=message,
    )


def test_trustedos_inquiry_send_failed_log_ships_lengths_not_free_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    company = "Round Two Send Failure Company Marker"
    message = "Round two send failure message marker."

    class FailingEmailService:
        def send(self, _message: EmailMessage) -> bool:
            raise RuntimeError("smtp unavailable")

    monkeypatch.setattr(public_routes, "get_email_service", lambda _settings: FailingEmailService())
    client = _trustedos_client(
        Settings(
            environment="test",
            partner_inquiry_email="leads@example.com",
        )
    )

    with caplog.at_level(logging.ERROR, logger="trusted_router.routes.public"):
        response = client.post(
            "/trustedos/inquiry",
            json=_trustedos_payload(company=company, message=message),
        )

    assert response.status_code == 200
    shipped_messages = _scrubbed_trustedos_messages(caplog, "trustedos_inquiry.send_failed")
    assert len(shipped_messages) == 1
    _assert_inquiry_log_minimized(shipped_messages[0], company=company, message=message)


def test_trustedos_inquiry_delivery_failed_log_ships_lengths_not_free_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    company = "Round Two Delivery Failure Company Marker"
    message = "Round two delivery failure message marker."

    class RefusingEmailService:
        def send(self, _message: EmailMessage) -> bool:
            return False

    monkeypatch.setattr(public_routes, "get_email_service", lambda _settings: RefusingEmailService())
    client = _trustedos_client(
        Settings(
            environment="test",
            partner_inquiry_email="leads@example.com",
        )
    )

    with _capture_lead_and_root_logs() as (lead_capture, root_capture):
        with caplog.at_level(logging.ERROR, logger="trusted_router.routes.public"):
            response = client.post(
                "/trustedos/inquiry",
                json=_trustedos_payload(company=company, message=message),
            )

    assert response.status_code == 200
    shipped_messages = _scrubbed_trustedos_messages(caplog, "trustedos_inquiry.delivery_failed")
    assert len(shipped_messages) == 1
    _assert_inquiry_log_minimized(shipped_messages[0], company=company, message=message)
    _assert_lead_log_local_only(
        lead_capture.records,
        root_capture.records,
        recipient="leads@example.com",
        company=company,
        message=message,
    )


def test_axiom_client_kwargs_use_edge_url_for_edge_deployments() -> None:
    kwargs = _client_kwargs(
        token=_fake_axiom_token(),
        org_id=None,
        axiom_url="https://eu-central-1.aws.edge.axiom.co",
    )

    assert kwargs == {
        "token": "xaat_test",
        "edge_url": "https://eu-central-1.aws.edge.axiom.co",
    }


def test_axiom_client_kwargs_keep_standard_api_url_for_non_edge() -> None:
    kwargs = _client_kwargs(
        token=_fake_axiom_token(),
        org_id="org_1",
        axiom_url="https://api.axiom.co",
    )

    assert kwargs == {
        "token": "xaat_test",
        "org_id": "org_1",
        "url": "https://api.axiom.co",
    }


def test_init_axiom_sets_package_logger_level_and_keeps_third_party_info_gated(
    monkeypatch: pytest.MonkeyPatch,
    clean_axiom_logging_state: None,
) -> None:
    captured_records: list[logging.LogRecord] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class CapturingAxiomHandler(logging.Handler):
        def __init__(self, client: FakeClient, dataset: str) -> None:
            super().__init__()
            self.client = client
            self.dataset = dataset

        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    monkeypatch.setenv("AXIOM_API_TOKEN", _fake_axiom_token())
    monkeypatch.delenv("AXIOM_TOKEN", raising=False)
    monkeypatch.delenv("AXIOM_ORG_ID", raising=False)
    monkeypatch.setattr(axiom_config, "_running_under_pytest", lambda _settings: False)
    monkeypatch.setattr(axiom_py, "Client", FakeClient)
    monkeypatch.setattr(axiom_logging, "AxiomHandler", CapturingAxiomHandler)

    settings = Settings(
        environment="local",
        axiom_dataset="test-logs",
        axiom_log_level="INFO",
    )

    init_axiom(settings)

    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("trusted_router").level == _resolve_level(settings.axiom_log_level)

    logging.getLogger("trusted_router.anything").info("app info reaches axiom")
    logging.getLogger("thirdparty").info("third-party info stays gated")

    assert any(
        record.name == "trusted_router.anything"
        and record.getMessage() == "app info reaches axiom"
        for record in captured_records
    )
    assert all(record.name != "thirdparty" for record in captured_records)


def test_init_axiom_caps_package_logger_level_at_warning_but_keeps_handler_level(
    monkeypatch: pytest.MonkeyPatch,
    clean_axiom_logging_state: None,
) -> None:
    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class CapturingAxiomHandler(logging.Handler):
        def __init__(self, client: FakeClient, dataset: str) -> None:
            super().__init__()
            self.client = client
            self.dataset = dataset

        def emit(self, record: logging.LogRecord) -> None:
            pass

    monkeypatch.setenv("AXIOM_API_TOKEN", _fake_axiom_token())
    monkeypatch.delenv("AXIOM_TOKEN", raising=False)
    monkeypatch.delenv("AXIOM_ORG_ID", raising=False)
    monkeypatch.setattr(axiom_config, "_running_under_pytest", lambda _settings: False)
    monkeypatch.setattr(axiom_py, "Client", FakeClient)
    monkeypatch.setattr(axiom_logging, "AxiomHandler", CapturingAxiomHandler)

    init_axiom(
        Settings(
            environment="local",
            axiom_dataset="test-logs",
            axiom_log_level="ERROR",
        )
    )

    installed_handlers = [
        handler for handler in logging.getLogger().handlers if isinstance(handler, _SafeAxiomHandler)
    ]
    assert len(installed_handlers) == 1
    assert logging.getLogger("trusted_router").level == logging.WARNING
    assert installed_handlers[0].level == logging.ERROR


def _record(logger_name: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name, level=logging.INFO, pathname=__file__,
        lineno=1, msg="hello", args=(), exc_info=None,
    )


def test_noise_filter_drops_third_party_transport_chatter() -> None:
    """urllib3.connectionpool was 235/238 Axiom events in a 2h window
    (Sentry envelope uploads). The noise filter must drop it and its
    friends so the dataset stays app-events-only."""
    from trusted_router.axiom_config import _AxiomNoiseFilter

    f = _AxiomNoiseFilter()
    for name in [
        "urllib3",
        "urllib3.connectionpool",
        "sentry_sdk.errors",
        "google.auth.transport.requests",
        "grpc._channel",
        "httpx",
        "httpcore.http11",
        "hpack.hpack",
    ]:
        assert f.filter(_record(name)) is False, name


def test_noise_filter_keeps_app_and_server_logs() -> None:
    """App loggers and uvicorn error logs must pass through — and a
    prefix must not shadow lookalike names (e.g. `googleapis_custom`
    is not `google.*`)."""
    from trusted_router.axiom_config import _AxiomNoiseFilter

    f = _AxiomNoiseFilter()
    for name in [
        "trusted_router.routes.inference",
        "trusted_router.storage_gcp",
        "uvicorn.error",
        "root",
        "googleapis_custom",
        "urllib3x",
    ]:
        assert f.filter(_record(name)) is True, name
