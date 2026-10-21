"""Async settlement admission cannot outlive its money protection."""
import pytest
from pydantic import ValidationError

from trusted_router.config import Settings


def test_record_read_backend_defaults_to_postgres():
    config = Settings(environment='test', _env_file=None)
    assert config.record_read_backend == 'postgres'


@pytest.mark.parametrize(
    ('url', 'password', 'accepted'),
    [
        ('', 'pw', False),
        (' ', 'pw', False),
        ('http://clickhouse.test:8123', '', False),
        ('http://clickhouse.test:8123', 'pw', True),
    ],
    ids=['no-url', 'blank-url', 'no-password', 'url-and-password'],
)
@pytest.mark.parametrize('source', ['fields', 'environment'])
def test_clickhouse_record_reads_require_plane_url_and_password(monkeypatch, url, password, accepted, source):
    """A plane reading records from ClickHouse needs the reader's URL and
    password; without either the store has no reader and every read fails,
    so the plane is refused at startup."""
    options = dict(
        record_read_backend='clickhouse',
        operational_analytics_clickhouse_url=url,
        operational_analytics_clickhouse_password=password,
    )
    if source == 'environment':
        for name, value in options.items():
            monkeypatch.setenv('TR_' + name.upper(), value)
        options = {}
    if accepted:
        config = Settings(environment='test', _env_file=None, **options)
        assert config.record_read_backend == 'clickhouse'
    else:
        with pytest.raises(
            ValidationError,
            match='TR_RECORD_READ_BACKEND=clickhouse requires TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL '
            'and TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD',
        ):
            Settings(environment='test', _env_file=None, **options)


def test_record_read_backend_rejects_unknown_value():
    with pytest.raises(ValidationError, match='TR_RECORD_READ_BACKEND'):
        Settings(environment='test', _env_file=None, record_read_backend='unknown')


@pytest.mark.parametrize('environment', ['canary', 'production'])
def test_postgres_outbox_needs_no_local_clickhouse(environment):
    config = Settings(
        _env_file=None,
        environment=environment,
        service_surface='observer',
        storage_backend='postgres',
        postgres_dsn='postgresql://localhost/test',
        observer_internal_token='test-observer-token',  # noqa: S106 - test credential
        sentry_dsn='https://example@example.ingest.sentry.io/1',
        operational_analytics_sink='outbox',
        operational_analytics_outbox_enabled=True,
        operational_analytics_clickhouse_url='',
        operational_analytics_clickhouse_password='',
        operational_analytics_clickhouse_write_password='',
    )
    assert config.operational_analytics_outbox_enabled
    assert not config.operational_analytics_clickhouse_url
    assert not config.operational_analytics_clickhouse_password


@pytest.mark.parametrize('serving', [None, '', 'europe-west4'])
@pytest.mark.parametrize('source', ['fields', 'environment'])
def test_serving_region_falls_back_without_changing_primary(monkeypatch, serving, source):
    monkeypatch.delenv('TR_SERVING_REGION', raising=False)
    options = dict(primary_region='us-east4')
    if serving is not None:
        options['serving_region'] = serving
    if source == 'environment':
        for name, value in options.items():
            monkeypatch.setenv('TR_' + name.upper(), value)
        options = {}
    config = Settings(environment='test', _env_file=None, **options)
    assert config.serving_region == (serving or '')
    assert config.effective_serving_region == (serving or 'us-east4')
    assert config.primary_region == 'us-east4'


@pytest.mark.parametrize('admission,protection', [(False, False), (False, True), (True, True), (True, False)])
@pytest.mark.parametrize('source', ['fields', 'environment'])
def test_async_admission_requires_protection(monkeypatch, admission, protection, source):
    options = dict(async_settle_enabled=admission, async_settle_protection=protection)
    if source == 'environment':
        for name, value in options.items():
            monkeypatch.setenv('TR_' + name.upper(), str(value).lower())
        options = {}
    if admission and not protection:
        with pytest.raises(ValidationError, match='TR_ASYNC_SETTLE_ENABLED requires TR_ASYNC_SETTLE_PROTECTION'):
            Settings(environment='test', **options)
    else:
        config = Settings(environment='test', **options)
        assert config.async_settle_admission_enabled is (admission and protection)


def test_runtime_mutation_fails_closed():
    config = Settings(environment='test')
    config.async_settle_enabled = True
    assert not config.async_settle_admission_enabled
    config.async_settle_protection = True
    assert config.async_settle_admission_enabled
    config.async_settle_protection = False
    assert not config.async_settle_admission_enabled
