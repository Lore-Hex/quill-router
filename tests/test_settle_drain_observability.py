"""Actual handler/Sentry output, including fields added after a record factory."""
from __future__ import annotations

import logging
import subprocess
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from tests.test_settle_drain_worker import config
from trusted_router.main import create_app
from trusted_router.services import settle_drain_observability as output
from trusted_router.synthetic.alerts import ops_alert


@pytest.mark.parametrize('use_colors', [False, True])
def test_real_uvicorn_handlers_drop_worker_access_logs_and_restore_logging(use_colors):
    # dictConfig closes existing handlers, including pytest's capture handlers.
    # Exercise the real configuration in a child so it cannot poison later tests.
    script = textwrap.dedent('''\
        import copy
        import io
        import logging
        import logging.config
        import sys

        from uvicorn.config import LOGGING_CONFIG
        from uvicorn.logging import AccessFormatter, DefaultFormatter

        from trusted_router.services import settle_drain_observability as output

        config = copy.deepcopy(LOGGING_CONFIG)
        for formatter in config['formatters'].values():
            formatter['use_colors'] = sys.argv[1] == 'True'
        logging.config.dictConfig(config)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(logging.StreamHandler())
        named = logging.getLogger('library')
        named.propagate = False
        named.addHandler(logging.StreamHandler())
        fallback = logging.getLogger('fallback')
        fallback.propagate = False
        logging.lastResort = logging.StreamHandler()
        access = logging.getLogger('uvicorn.access')
        handlers = {
            'root': root.handlers[0],
            'named': named.handlers[0],
            'default': logging.getLogger('uvicorn').handlers[0],
            'access': access.handlers[0],
            'lastResort': logging.lastResort,
        }
        assert isinstance(handlers['default'].formatter, DefaultFormatter)
        assert isinstance(handlers['access'].formatter, AccessFormatter)
        streams = {name: io.StringIO() for name in handlers}
        errors = []
        def handle_error(record):
            errors.append(str(sys.exc_info()[1]))
        for name, handler in handlers.items():
            handler.setStream(streams[name])
            handler.handleError = handle_error
        before = {name: (handler.formatter, list(handler.filters))
                  for name, handler in handlers.items()}
        cleanup = output.install_logging()
        try:
            for handler in handlers.values():
                assert any(isinstance(f, output.WorkerLogFilter) for f in handler.filters)
            # The exact message and five positional arguments used by Uvicorn's
            # HTTP protocols, including deliberately sensitive request content.
            access.info('%s - "%s %s HTTP/%s" %d',
                        '192.0.2.123:45678', 'GET', '/health?private-query=secret', '1.1', 200)
            for logger in (logging.getLogger('root.child'), named,
                           logging.getLogger('uvicorn.error'), fallback):
                try:
                    raise ValueError('private-exception')
                except ValueError:
                    logger.exception('private-message %s', 'private-arg',
                                     extra={'color_message': 'private-color', 'secret': 'private-extra'},
                                     stack_info=True)
            assert errors == [], errors
            assert streams['access'].getvalue() == ''
            for name, logger_name in (('root', 'root.child'), ('named', 'library'),
                                      ('default', 'uvicorn.error'), ('lastResort', 'fallback')):
                value = streams[name].getvalue()
                assert f'logger={logger_name} level=ERROR exception_class=ValueError' in value
                assert 'private-' not in value, value
            logging.getLogger(output.WORKER_LOGGER).warning('async_drain.shutdown_inflight')
            assert 'async_drain.shutdown_inflight' in streams['root'].getvalue()
        finally:
            cleanup()
        for name, handler in handlers.items():
            assert (handler.formatter, handler.filters) == before[name]
        # The same handlers still work on ordinary surfaces after worker cleanup.
        access.info('%s - "%s %s HTTP/%s" %d', '192.0.2.123:45678', 'GET', '/health', '1.1', 200)
        assert errors == [], errors
        assert '192.0.2.123:45678' in streams['access'].getvalue()
        assert 'GET /health HTTP/1.1' in streams['access'].getvalue()
        for logger in (logging.getLogger('root.child'), named,
                       logging.getLogger('uvicorn.error'), fallback):
            logger.warning('ordinary-service-diagnostic')
        for name in ('root', 'named', 'default', 'lastResort'):
            assert 'ordinary-service-diagnostic' in streams[name].getvalue()
    ''')
    result = subprocess.run(  # noqa: S603 - fixed local test program
        [sys.executable, '-c', script, str(use_colors)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_handler_boundary_removes_extras_factory_fields_and_exception_text(caplog):
    factory = logging.getLogRecordFactory()
    def previous(*args, **kw):
        record = factory(*args, **kw)
        record.original_message = record.getMessage()
        return record
    logging.setLogRecordFactory(previous)
    cleanup = output.install_logging()
    try:
        with caplog.at_level(logging.WARNING):
            try:
                raise ValueError('private-reservation private-workspace')
            except ValueError:
                logging.getLogger('primitive').exception('private-authorization',
                    extra={'key_hash': 'private-key', 'nested': {'workspace': 'private-workspace'}})
        [record] = caplog.records
        assert record.getMessage() == 'logger=primitive level=ERROR exception_class=ValueError'
        assert not any('private-' in str(value) for value in vars(record).values())
        assert not {'key_hash', 'nested', 'original_message'} & vars(record).keys()
        assert record.exc_info is record.exc_text is record.stack_info is None
        assert logging.getLogRecordFactory() is previous
    finally:
        cleanup()
        logging.setLogRecordFactory(factory)
    caplog.clear()
    # Same logger/handler on an ordinary surface keeps its original payload.
    logging.getLogger('primitive').warning('router diagnostic', extra={'key_hash': 'router-key'})
    assert caplog.records[0].key_hash == 'router-key'
    assert caplog.records[0].getMessage() == 'router diagnostic'


def test_worker_events_pass_through_unchanged(caplog):
    cleanup = output.install_logging()
    try:
        logging.getLogger(output.WORKER_LOGGER).warning('async_drain.resolve_failed error_class=%s', 'TimeoutError')
        assert caplog.records[0].getMessage() == 'async_drain.resolve_failed error_class=TimeoutError'
    finally:
        cleanup()


def test_worker_ops_alert_reaches_configured_sentry_sink(monkeypatch):
    import sentry_sdk
    from sentry_sdk.transport import Transport
    captured = []
    class Sink(Transport):
        def capture_envelope(self, envelope):
            captured.extend(item.payload.json for item in envelope.items if item.type == 'event')
    original = sentry_sdk.Client
    def client(**kw):
        assert kw['dsn'] == 'https://public@example.com/1'
        return original(**kw, transport=Sink)
    monkeypatch.setattr(sentry_sdk, 'Client', client)
    monkeypatch.setattr('trusted_router.sentry_config.sentry_should_init', lambda settings: True)
    before = sentry_sdk.get_global_scope().client
    app = create_app(config(settle_outbox_fast_drain_enabled=False, sentry_dsn='https://public@example.com/1'),
                     configure_store_arg=False, init_observability=False)
    with TestClient(app):
        assert ops_alert('lost private-authorization private-reservation',
                         fingerprint=['settle-outbox', 'lost-charge'],
                         tags={'workspace_id': 'private-workspace', 'key_hash': 'private-key'})
    assert sentry_sdk.get_global_scope().client is before
    assert len(captured) == 1
    assert captured[0]['fingerprint'] == ['ops-alert', 'settle-outbox', 'lost-charge']
    assert captured[0]['message'] == 'ops-alert settle-outbox lost-charge'
    assert 'private-' not in str(captured)
