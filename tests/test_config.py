"""Async settlement admission cannot outlive its money protection."""
import pytest
from pydantic import ValidationError

from trusted_router.config import Settings


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
