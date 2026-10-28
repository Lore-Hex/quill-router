"""Production manifest absence allows observation, never certified readiness."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.test_async_settle_shadow_authorize_context import (
    CASES,
    assert_probe_compares,
    authorize,
    probe_context,
)
from tests.test_async_settle_shadow_cohort_differential import registered_apps  # noqa: F401
from tests.test_async_settle_shadow_cohort_runtime import installed
from trusted_router.services import async_settle_shadow_startup_harness as startup


def install_startup(monkeypatch, env, manifest):
    if manifest is None:
        monkeypatch.delenv(startup.MANIFEST_ENV, raising=False)
    else:
        monkeypatch.setenv(startup.MANIFEST_ENV, manifest)
    app = SimpleNamespace(state=SimpleNamespace(async_settle_shadow_observation=env.service))
    startup.install(app, env.cfg, env.shell)
    harness = app.state.async_settle_shadow_startup
    # Only boot descriptor I/O is stubbed; the real manifest loader and latch run.
    monkeypatch.setattr(harness.emitter, 'prepare', lambda: None)
    monkeypatch.setattr(startup, 'read_fd', lambda: None)
    return app, harness


@pytest.mark.parametrize('name', CASES)
@pytest.mark.parametrize('manifest', [None, ''], ids=['unset', 'empty'])
def test_unprovisioned_literal_probe_compares(monkeypatch, registered_apps, name, manifest):  # noqa: F811
    with probe_context(monkeypatch, registered_apps, name) as env:
        app, harness = install_startup(monkeypatch, env, manifest)
        asyncio.run(app.state.async_settle_shadow_startup_begin())
        assert not harness.admission.ready and harness.admission.unprovisioned
        assert 'ReleaseStartupInputs' in harness.missing
        assert_probe_compares(env)
        counter = env.service.counters[env.events()[0]['authorization_day']]
        assert counter['diagnostics']['startup_unprovisioned'] == 1
        assert counter['requested_eligible'] == counter['proof_compared'] == counter['A'] == 1
        env.service.journal.checkpoint(counter['day'])
        row = env.service.counter_row(counter['day'], int(env.service.store.wall()*1e6), closed=False)
        assert row.value()['diagnostics']['startup_unprovisioned'] == 1
        # A successful comparison does not prove this boot ready.
        assert not harness.admission.ready


@pytest.mark.parametrize('state', ['pending', 'missing-path', 'invalid-json', 'invalid-schema', 'whitespace'])
def test_configured_manifest_keeps_strict_latch(monkeypatch, registered_apps, tmp_path, state):  # noqa: F811
    path = tmp_path / 'startup.json'
    if state == 'invalid-json':
        path.write_text('not json')
    elif state == 'invalid-schema':
        path.write_text('{}')
    with probe_context(monkeypatch, registered_apps, 'openai_chat_buffered') as env:
        app, harness = install_startup(monkeypatch, env, ' ' if state == 'whitespace' else str(path))
        # Provisioning is captured at install; deleting the setting cannot bypass it.
        monkeypatch.delenv(startup.MANIFEST_ENV)
        if state != 'pending':
            asyncio.run(app.state.async_settle_shadow_startup_begin())
            assert 'ReleaseStartupInputs' in harness.missing
        assert not harness.admission.ready and not harness.admission.unprovisioned
        data = authorize(env).json()['data']
        event = env.events()[0]
        assert not env.mapped
        assert event['eligibility'] == 'unknown' and event['facts'] is None and event['candidates'] is None
        assert event['diagnostic']['reason'] == 'missing_context'
        assert 'billing_shadow_binding' not in data
        counter = env.service.counters[event['authorization_day']]
        assert counter['requested_unknown'] == 1 and counter['requested_eligible'] == 0
        assert 'startup_unprovisioned' not in counter['diagnostics']


@pytest.mark.parametrize('options', [dict(pin=''), dict(member=False), dict(money=True), dict(marker=False)],
                         ids=['policy-off', 'outside-cohort', 'money-on', 'marker-absent'])
def test_unprovisioned_diagnostic_only_counts_opted_v2(monkeypatch, registered_apps, options):  # noqa: F811
    with probe_context(monkeypatch, registered_apps, 'openai_chat_buffered', **options) as env:
        install_startup(monkeypatch, env, None)
        data = authorize(env).json()['data']
        env.shell.join()
        assert 'billing_shadow_binding' not in data
        assert all('startup_unprovisioned' not in c['diagnostics'] for c in env.service.counters.values())


@pytest.mark.parametrize('refusal', ['overflow', 'bytes', 'names', 'sealed'])
def test_unprovisioned_diagnostic_refusal_is_sticky_loss(refusal):
    shell, service, _ = installed()
    day, counter = next(iter(service.counters.items()))
    target = counter['diagnostics']
    if refusal == 'overflow':
        target['startup_unprovisioned'] = 2**63-1
    elif refusal == 'bytes':
        cells = service.port.fold_cells[day]
        service.port.fold_cells[day] = (*cells[:3], 65536)
    elif refusal == 'names':
        target.update({f'diagnostic_{n}': 1 for n in range(128)})
    else:
        service.journal.days[day].sealing = True
    before = dict(target)
    try:
        service.port.record_startup_unprovisioned(int(service.store.wall()*1e6))
        assert service.port.lost.is_set()
        assert target == before
    finally:
        shell.close()
        shell.worker.join(2)
