"""Actual handler/Sentry output, including fields added after a record factory."""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from tests.test_settle_drain_worker import config
from trusted_router.main import create_app
from trusted_router.services import settle_drain_observability as output
from trusted_router.synthetic.alerts import ops_alert


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
