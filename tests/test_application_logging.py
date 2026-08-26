from __future__ import annotations

import logging
import queue
from collections.abc import Iterator

import pytest

from trusted_router.axiom_config import _DroppingQueueHandler
from trusted_router.config import Settings
from trusted_router.main import (
    _APP_CONSOLE_HANDLER_MARKER,
    create_app,
)


@pytest.fixture
def isolated_application_logging() -> Iterator[None]:
    """Restore the process-global logging graph after each regression test."""
    root = logging.getLogger()
    app_logger = logging.getLogger("trusted_router")
    original_root_handlers = list(root.handlers)
    original_app_handlers = list(app_logger.handlers)
    original_state = (app_logger.level, app_logger.propagate, app_logger.disabled)
    try:
        for handler in list(app_logger.handlers):
            if getattr(handler, _APP_CONSOLE_HANDLER_MARKER, False):
                app_logger.removeHandler(handler)
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in original_root_handlers:
                root.removeHandler(handler)
        for handler in list(app_logger.handlers):
            if handler not in original_app_handlers:
                app_logger.removeHandler(handler)
                handler.close()
        for handler in original_app_handlers:
            if handler not in app_logger.handlers:
                app_logger.addHandler(handler)
        app_logger.setLevel(original_state[0])
        app_logger.propagate = original_state[1]
        app_logger.disabled = original_state[2]


def test_entrypoint_app_logger_reaches_container_stream_with_structured_extra(
    capfd: pytest.CaptureFixture[str],
    isolated_application_logging: None,
) -> None:
    create_app(
        Settings(environment="test"),
        configure_store_arg=False,
        init_observability=False,
    )

    logging.getLogger("trusted_router.regression.sink").warning(
        "sink delivery warning",
        extra={"sink": {"backend": "direct", "dropped_total": 7}},
    )

    captured = capfd.readouterr()
    assert "WARNING trusted_router.regression.sink sink delivery warning" in (
        captured.err + captured.out
    )


def test_axiom_root_handler_can_no_longer_swallow_application_logs(
    capfd: pytest.CaptureFixture[str],
    isolated_application_logging: None,
) -> None:
    """Pin the production mechanism behind the missing Cloud Logging lines.

    A root handler suppresses logging.lastResort.  Axiom installs precisely
    such a non-console handler in production, so before the app-owned stream
    handler this warning had no path to stderr or stdout.
    """
    root = logging.getLogger()
    root.addHandler(_DroppingQueueHandler(queue.Queue()))

    create_app(
        Settings(environment="test"),
        configure_store_arg=False,
        init_observability=False,
    )
    logging.getLogger("trusted_router.regression.axiom").warning("visible despite axiom")

    captured = capfd.readouterr()
    assert "visible despite axiom" in captured.err + captured.out


def test_application_console_configuration_is_idempotent(
    capfd: pytest.CaptureFixture[str],
    isolated_application_logging: None,
) -> None:
    settings = Settings(environment="test")
    create_app(settings, configure_store_arg=False, init_observability=False)
    create_app(settings, configure_store_arg=False, init_observability=False)

    logging.getLogger("trusted_router.regression.reload").warning("only once")

    captured = capfd.readouterr()
    assert (captured.err + captured.out).count("only once") == 1
