"""Round-three boundary regressions; all I/O stays local or mocked."""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import secrets
import shutil
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import provision_async_settle_shadow_probe_workspace as provisioning
from scripts.async_settle.probe_cohort_check import check
from tests.test_async_settle_probe import success
from trusted_router.config import SERVICE_SURFACE_SECRET_OWNERS, Settings, validate_shadow_probe_key
from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import generation_id_for_authorization
from trusted_router.storage_operational_analytics import analytics_surrogate
from trusted_router.synthetic import cli, shadow_probe
from trusted_router.synthetic.shadow_probe_contract import SHAPES

SOURCE_ALLOWLIST = frozenset({
    'src/trusted_router/config.py', 'src/trusted_router/synthetic/cli.py',
    'src/trusted_router/synthetic/shadow_probe.py',
    'src/trusted_router/synthetic/shadow_probe_contract.py',
})
PROBE_MODULES = frozenset({
    'trusted_router.synthetic.shadow_probe', 'trusted_router.synthetic.shadow_probe_contract',
})


def probe_reference(value):
    # Covers settings, Python module/symbol names, env vars, CLI/job and secret
    # names. AST values also catch escaped literals; text catches comments.
    return 'shadow_probe' in value.casefold().replace('-', '_')


@lru_cache(maxsize=1024)
def parsed_source(text):
    # Content-keyed reuse: unchanged scratch-copy files need no second parse.
    # Every path is still read; changing even one byte invalidates the entry.
    return ast.parse(text)


@lru_cache(maxsize=1024)
def syntax_nodes(tree):
    return tuple(ast.walk(tree))


@lru_cache(maxsize=1024)
def contains_probe_reference(text):
    return probe_reference(text) or any(
        probe_reference(value) for node in syntax_nodes(parsed_source(text))
        for _, value in ast.iter_fields(node) if isinstance(value, str))


def source_inventory(root):
    return {path.relative_to(root).as_posix(): (text, parsed_source(text))
            for path in sorted((root / 'src').rglob('*.py')) if (text := path.read_text()) is not None}


def assert_probe_references_isolated(root, inventory):
    offenders = []
    for name, (text, _tree) in inventory.items():
        if name in SOURCE_ALLOWLIST:
            continue
        if contains_probe_reference(text):
            offenders.append(name)
    for name in ('scripts/deploy/synthetic.sh', 'scripts/provision_synthetic_monitor.py'):
        text = (root / name).read_text()
        if probe_reference(text):
            offenders.append(name)
        if name.endswith('.py'):
            assert not any(probe_reference(value) for node in ast.walk(ast.parse(text))
                           for _, value in ast.iter_fields(node) if isinstance(value, str)), name
    assert not offenders, f'probe reference outside exact allowlist: {offenders}'


def assert_request_import_graph_isolated(inventory):
    modules = {}
    for path, (_, tree) in inventory.items():
        name = path.removeprefix('src/').removesuffix('.py').replace('/', '.')
        modules[name.removesuffix('.__init__')] = (tree, name.endswith('.__init__'))

    cli_module = 'trusted_router.synthetic.cli'
    cli_tree = modules[cli_module][0]
    command_names = {'main', 'run_shadow_probe'}
    command_defs = {node.name: node for node in cli_tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in command_names}
    assert command_defs.keys() == command_names
    entry = cli_tree.body[-1]
    assert isinstance(entry, ast.If)
    assert ast.dump(entry.test) == ast.dump(ast.parse('__name__ == "__main__"', mode='eval').body)
    # No helper/rotation function may call, alias or look up either command.
    live_cli = ast.Module(body=[node for node in cli_tree.body
                               if node not in [*command_defs.values(), entry]], type_ignores=[])
    for node in ast.walk(live_cli):
        assert not (isinstance(node, ast.Name) and node.id in command_names)
        assert not (isinstance(node, ast.Attribute) and node.attr in command_names)
        assert not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value in command_names)
    modules[cli_module] = (live_cli, False)

    def dependencies(module):
        tree, package_init = modules[module]
        package = module if package_init else module.rpartition('.')[0]
        imports = set()
        for node in syntax_nodes(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != cli_module for alias in node.names), 'request imports CLI module'
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ''
                if node.level:
                    parents = package.split('.')
                    prefix = '.'.join(parents[:len(parents) - node.level + 1]
                                      + ([prefix] if prefix else []))
                if prefix == cli_module:
                    assert {alias.name for alias in node.names} == {'rotation_pass'}, 'request imports CLI command'
                assert all(prefix + '.' + alias.name != cli_module for alias in node.names), 'request imports CLI module'
                imports.add(prefix)
                imports.update(prefix + '.' + alias.name for alias in node.names)
            elif isinstance(node, ast.Call) and node.args:
                # Literal dynamic imports, including aliased importlib calls.
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    value = node.args[0].value
                    if value.startswith('trusted_router.'):
                        assert value != cli_module, 'request imports CLI module'
                        imports.add(value)
        # Importing a submodule also executes its package initializers.
        return {'.'.join(parts[:i]) for name in imports
                if (parts := name.split('.')) for i in range(1, len(parts) + 1)
                if '.'.join(parts[:i]) in modules}

    pending = [('trusted_router.main',)]
    seen = set()
    while pending:
        chain = pending.pop()
        module = chain[-1]
        assert module not in PROBE_MODULES, 'request import reaches probe: ' + ' -> '.join(chain)
        if module in seen:
            continue
        seen.add(module)
        pending.extend((*chain, dependency) for dependency in dependencies(module))
    # Fail if the walk silently stopped at an unresolved entry/package import.
    assert {'trusted_router.routes.inference', 'trusted_router.routes.internal.gateway',
            'trusted_router.routes.internal.synthetic'} <= seen
    return seen


def test_tree_probe_isolation():
    root = Path(__file__).resolve().parents[1]
    inventory = source_inventory(root)
    assert SOURCE_ALLOWLIST <= inventory.keys()
    assert_probe_references_isolated(root, inventory)
    assert_request_import_graph_isolated(inventory)


@pytest.fixture(scope='module')
def scratch_source(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    scratch = tmp_path_factory.mktemp('probe-source-guard')
    for path in [*root.joinpath('src').rglob('*.py'),
                 root / 'scripts/deploy/synthetic.sh', root / 'scripts/provision_synthetic_monitor.py']:
        target = scratch / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return scratch


@pytest.mark.parametrize('path,reference', [
    ('src/trusted_router/routes/inference.py', 'from trusted_router.synthetic import shadow_probe'),
    ('src/trusted_router/synthetic/alerts.py', '# TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY'),
    ('src/trusted_router/synthetic/components.py', 'COHORT = "trustedrouter-async-settle-shadow-probe-api-key"'),
    ('src/trusted_router/routes/inference.py', r'COHORT = "async_settle_shadow_\x70robe_api_key"'),
])
def test_tree_guard_negative_controls(scratch_source, path, reference):
    target = scratch_source / path
    original = target.read_text()
    try:
        target.write_text(original + '\n' + reference + '\n')
        with pytest.raises(AssertionError, match='outside exact allowlist'):
            assert_probe_references_isolated(scratch_source, source_inventory(scratch_source))
    finally:
        target.write_text(original)


@pytest.mark.parametrize('reference', [
    'from trusted_router.synthetic import shadow_probe',
    'import trusted_router.synthetic.shadow_probe_contract as contract',
    'from .synthetic.shadow_probe import run_pass',
])
def test_import_graph_negative_controls(scratch_source, reference):
    target = scratch_source / 'src/trusted_router/config.py'
    original = target.read_text()
    try:
        target.write_text(original + '\n' + reference + '\n')
        with pytest.raises(AssertionError, match='request import reaches probe'):
            assert_request_import_graph_isolated(source_inventory(scratch_source))
    finally:
        target.write_text(original)


@pytest.mark.parametrize('reference', [
    'from trusted_router.synthetic import shadow_probe',
    'rotation_pass = run_shadow_probe',
])
def test_cli_route_boundary_negative_controls(scratch_source, reference):
    target = scratch_source / 'src/trusted_router/synthetic/cli.py'
    original = target.read_text()
    try:
        target.write_text(original.replace('if __name__ == "__main__":',
                                           reference + '\nif __name__ == "__main__":'))
        with pytest.raises(AssertionError):
            assert_request_import_graph_isolated(source_inventory(scratch_source))
    finally:
        target.write_text(original)


@pytest.mark.parametrize('once', [False, True])
@pytest.mark.asyncio
async def test_pass_never_ingests_and_writes_denominator(once, tmp_path, monkeypatch, capsys):
    key = secrets.token_urlsafe(32)
    path = tmp_path / 'key'
    path.write_text(key)
    path.chmod(0o600)
    ids = tmp_path / 'generations'
    monkeypatch.setenv('TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE', str(path))
    monkeypatch.delenv('TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY', raising=False)
    monkeypatch.setattr(cli, 'get_settings', lambda: Settings(environment='test'))
    calls = []
    allowed_hosts = {t.api_base_url.split('/')[2] for t in shadow_probe.targets(Settings(environment='test'))}
    def handler(request):
        calls.append(request)
        assert request.url.host in allowed_hosts
        assert request.headers['authorization'] == 'Bearer ' + key
        shape = next(s for s in SHAPES if s.body() == json.loads(request.content))
        return success(shape)
    original = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert await cli.run_shadow_probe(once=once, generation_ids_out=ids) == 0
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 33
    assert '32 requests, 0 failed' in output.out
    assert key not in output.out + output.err
    assert len(calls) == len({r.headers['idempotency-key'] for r in calls}) == 32
    assert len(ids.read_text().splitlines()) == len(set(ids.read_text().splitlines())) == 32
    assert ids.stat().st_mode & 0o777 == 0o600
    assert await cli.run_shadow_probe(once=once, generation_ids_out=ids) == 2
    assert len(calls) == 32  # exclusive output creation fails before paid calls


@pytest.mark.parametrize('shape', SHAPES, ids=lambda s: s.name)
@pytest.mark.parametrize('damage', ['missing_usage', 'missing_id', 'provider_id', 'upper', 'short', 'prefix', 'suffix', 'other_field'])
@pytest.mark.asyncio
async def test_response_requires_router_usage_without_credential_echo(shape, damage):
    key = 'a' * 16  # valid hex substring even inside an otherwise valid router ID
    inner = {'status': 'completed', 'choices': [{'finish_reason': 'stop'}],
             'usage': {'provider_usage': {'generation_id': 'gen-' + 'b' * 32}, 'input_tokens': 10, 'output_tokens': 2}}
    if damage == 'missing_usage':
        inner.pop('usage')
    elif damage == 'missing_id':
        inner['usage'].pop('provider_usage')
    elif damage == 'provider_id':
        inner['generation_id'] = inner['usage'].pop('provider_usage')['generation_id']
    elif damage == 'other_field':
        inner['output'] = 'prefix' + key + 'suffix'
    else:
        inner['usage']['provider_usage']['generation_id'] = {
            'upper': 'gen-' + 'B' * 32, 'short': 'gen-a',
            'prefix': 'gen-' + key + 'b' * 16, 'suffix': 'gen-' + 'b' * 16 + key,
        }[damage]
    payload = {'response': inner} if shape.route == '/responses' else inner
    reply = (httpx.Response(200, headers={'content-type': 'text/event-stream'},
                            text='data: ' + json.dumps(payload) + '\n\ndata: [DONE]\n\n')
             if shape.stream else httpx.Response(200, json=payload))
    lines = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply)) as client:
        result = await shadow_probe.probe(client, shadow_probe.targets(Settings(environment='test'))[0], shape,
                                          api_key=key, monitor_region='test', report=lines.append)
    assert result.status == 'down' and result.generation_id is None
    assert key not in '\n'.join(lines)


@pytest.mark.parametrize('field', sorted(set(SERVICE_SURFACE_SECRET_OWNERS) - {'async_settle_shadow_probe_api_key'} | {'bootstrap_management_key'}))
def test_central_credential_inventory(field):
    key = secrets.token_urlsafe(32)
    cfg = Settings(environment='test').model_copy(update={field: key})
    with pytest.raises(ValueError, match='distinct'):
        validate_shadow_probe_key(cfg, key)
    nested = cfg.model_copy(update={field: json.dumps({'peer': {'tokens': [key]}})})
    with pytest.raises(ValueError, match='distinct'):
        validate_shadow_probe_key(nested, key)


@pytest.mark.parametrize('whitespace', [' ', '\t', '\n', '\r', '\u2003'])
def test_embedded_whitespace_rejected_at_both_boundaries(whitespace):
    key = 'fixture' + whitespace + 'key'
    with pytest.raises(ValueError):
        Settings(environment='test', async_settle_shadow_probe_api_key=key)
    with pytest.raises(ValueError):
        validate_shadow_probe_key(Settings(environment='test'), key)


def test_deployed_federation_guard_distinctness():
    from starlette.requests import Request

    from trusted_router.routes.internal.federation import require_federation_credit_peer
    privileged, probe = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    cfg = Settings(environment='worker', service_surface='internal', storage_backend='memory',
                   internal_gateway_token=secrets.token_urlsafe(32), observer_internal_token=secrets.token_urlsafe(32),
                   federation_credit_inbound_token=privileged, async_settle_shadow_probe_api_key=probe)
    request = Request({'type': 'http', 'headers': [(b'x-trustedrouter-federation-credit-token', probe.encode())]})
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        require_federation_credit_peer(request, cfg)
    request = Request({'type': 'http', 'headers': [(b'x-trustedrouter-federation-credit-token', privileged.encode())]})
    require_federation_credit_peer(request, cfg)
    with pytest.raises(ValueError):
        Settings(environment='worker', service_surface='internal', storage_backend='memory',
                   internal_gateway_token=secrets.token_urlsafe(32), observer_internal_token=secrets.token_urlsafe(32),
                 federation_credit_inbound_token=privileged, async_settle_shadow_probe_api_key=privileged)
    with pytest.raises(ValueError):
        validate_shadow_probe_key(cfg, privileged)


@pytest.mark.parametrize('stage', ['ensure_user', 'update_workspace', 'credit_workspace_typed_direct', 'create_api_key'])
@pytest.mark.parametrize('when', ['before', 'after'])
def test_resume_every_database_step(stage, when, tmp_path, monkeypatch, capsys):
    store = InMemoryStore()
    monkeypatch.setenv('TR_STORAGE_BACKEND', 'spanner-clickhouse')
    monkeypatch.setattr(provisioning, 'typed_billing_store', lambda _: SimpleNamespace(request_record_write_mode='typed'))
    original = getattr(InMemoryStore, stage)
    path = tmp_path / 'key'
    def interrupted(self, *a, **kw):
        assert path.exists() and path.stat().st_mode & 0o777 == 0o600
        if when == 'after':
            original(self, *a, **kw)
        raise RuntimeError('uncertain commit')
    monkeypatch.setattr(InMemoryStore, stage, interrupted)
    argv = ['--apply', '--key-output-file', str(path)]
    assert provisioning.main(argv, store=store) == 1
    raw = path.read_text()
    monkeypatch.setattr(InMemoryStore, stage, original)
    assert provisioning.main(argv, store=store) == 0
    assert path.read_text() == raw
    assert provisioning.main(argv, store=store) == 0
    key = store.get_key_by_raw(raw)
    assert key is not None
    assert store.credit_money_snapshot(key.workspace_id)[0] == 100_000_000
    output = capsys.readouterr()
    assert raw not in output.out + output.err


@pytest.mark.parametrize('fault', ['open', 'write', 'fsync', 'crashed_partial'])
def test_resume_interrupted_file(fault, tmp_path, monkeypatch):
    store = InMemoryStore()
    names = provisioning.cohort_names()
    path = tmp_path / 'key'
    monkeypatch.setattr(provisioning, 'typed_billing_store', lambda _: SimpleNamespace(request_record_write_mode='typed'))
    if fault == 'crashed_partial':
        path.write_text('sk-tr-v1-partial')
        path.chmod(0o600)
    else:
        with monkeypatch.context() as patch:
            if fault == 'write':
                original = os.fdopen
                class Partial:
                    def __init__(self, *a, **kw): self.file = original(*a, **kw)
                    def __enter__(self): return self
                    def write(self, value):
                        self.file.write(value[:10])
                        self.file.flush()
                        raise OSError('partial')
                    def __exit__(self, *a): self.file.close()
                patch.setattr(provisioning.os, 'fdopen', Partial)
            else:
                def fail(*a, **kw): raise OSError('file fault')
                patch.setattr(provisioning.os, fault, fail)
            with pytest.raises(OSError):
                provisioning.apply_cohort(store, names, 100_000_000, path)
        assert store.find_user_by_email(names['email']) is None
    result = provisioning.apply_cohort(store, names, 100_000_000, path)
    assert store.get_key_by_raw(path.read_text()).hash == result['key_id']


def test_missing_or_mismatched_credential_never_succeeds(tmp_path, monkeypatch, capsys):
    store = InMemoryStore()
    monkeypatch.setenv('TR_STORAGE_BACKEND', 'spanner-clickhouse')
    monkeypatch.setattr(provisioning, 'typed_billing_store', lambda _: SimpleNamespace(request_record_write_mode='typed'))
    path = tmp_path / 'key'
    argv = ['--apply', '--key-output-file', str(path)]
    assert provisioning.main(argv, store=store) == 0
    raw = path.read_text()
    path.unlink()
    assert provisioning.main(argv, store=store) == 1
    assert 'disable the active key' in capsys.readouterr().err
    path.write_text('sk-tr-v1-' + 'a' * 43)
    path.chmod(0o600)
    assert provisioning.main(argv, store=store) == 1
    assert store.get_key_by_raw(raw) is not None


def test_reconcile_each_identity_and_conservative_loss_veto():
    from trusted_router.async_settle_shadow_evidence import SAMPLE
    now = dt.datetime.now(dt.UTC)
    rows = [dict(workspace_fingerprint=analytics_surrogate('workspace', 'cohort'),
                 observed_at_us=int(now.timestamp()*1e6), authorization_id='auth-' + str(i),
                 adapter=shape.provider, route_type='responses' if shape.route == '/responses' else 'chat.completions',
                 streamed=shape.stream, classification='exact') for i, shape in enumerate(SHAPES)]
    ids = [generation_id_for_authorization(row['authorization_id']) for row in rows]
    counters = []
    evidence = SimpleNamespace(day=lambda kind, day: [(str(i), json.dumps(row)) for i, row in enumerate(rows if kind == SAMPLE else counters)] if day == now.date().isoformat() else [])
    assert not check(evidence, 'cohort', now, ids)['errors']
    assert check(evidence, 'cohort', now)['errors'] == ['no denominator']
    missing = rows.pop()
    assert any(ids[-1] in e for e in check(evidence, 'cohort', now, ids)['errors'])
    rows.append(missing | {'classification': 'unevaluable'})
    assert any(ids[-1] in e for e in check(evidence, 'cohort', now, ids)['errors'])
    rows[-1] = missing
    rows.append(missing)
    assert check(evidence, 'cohort', now, ids)['errors']
    rows.pop()
    counters.append(dict(flushed_at_us=int(now.timestamp()*1e6), drops=[{'reason': 'queue_full', 'count': 1}]))
    assert check(evidence, 'cohort', now, ids)['errors']


def test_public_coverage_residual_is_quantified_through_activity_rollup_projection():
    from clickhouse.rollup_client_events import aggregate_client_rollups
    from tests.test_clickhouse_client_rollup import START, _counter, _coverage
    from tests.test_operational_analytics import _generation
    from trusted_router.client_reliability import _all_traffic_section, _status_all_traffic
    from trusted_router.storage_operational_analytics import activity_payload
    generation = _generation()
    generation.workspace_id = 'dedicated-probe'
    generation.synthetic = True
    generation.created_at = START.isoformat()
    activity = activity_payload(generation)
    assert activity['synthetic'] is True
    cfg = Settings(environment='test')
    daily = 2 * 4 * len(SHAPES) * cfg.async_settle_shadow_probe_repetitions * 1440 // cfg.async_settle_shadow_probe_cadence_minutes
    assert cfg.async_settle_shadow_probe_cadence_minutes == 4
    assert daily == 23040
    # All four regional targets normally book in us-central1. Eight shape
    # buckets, spread over 8 or 16 writers, yield 360 or 180 per writer-day.
    assert [daily // len(SHAPES) // writers for writers in (8, 16)] == [360, 180]
    # This cohort is real traffic without client telemetry. All-traffic
    # coverage includes synthetic traffic, so adding it correctly lowers the
    # share with client telemetry. Nothing is hidden or mislabelled; any
    # explicit exclusion is a separate owner-requested policy change.
    counters = [_counter('ordinary', requests=10000, first_attempt_success=10000)]
    coverage = [_coverage('ordinary', requests=20000)]
    def rollup(cov, events):
        return aggregate_client_rollups(events, cov, period='5m', period_start=START, computed_at=START+dt.timedelta(minutes=1))
    def public(rows):
        return _status_all_traffic(_all_traffic_section({'24h': [r for r in rows if r['scope'] == 'fleet_all']}))
    before_rows = rollup(coverage, counters)
    before = public(before_rows)
    assert before['windows']['24h']['coverage'] == 0.5
    coverage.append(_coverage(activity['tenant_id'], requests=daily, synthetic=int(activity['synthetic'])))
    after_rows = rollup(coverage, counters)
    after = public(after_rows)
    assert after['windows']['24h']['coverage'] == round(10000 / 43040, 4) == 0.2323
    assert [r for r in before_rows if r['scope'] != 'fleet_all'] == [
        r for r in after_rows if r['scope'] != 'fleet_all']
    # Compare every public field, not just the coverage ratio: observed
    # volume, availability, latency, host and SDK breakdowns stay unchanged.
    after['windows']['24h']['coverage'] = before['windows']['24h']['coverage']
    assert after == before
    # The SDK alternative changes the same public ratio even at 100% delivery.
    counters.append(_counter(activity['tenant_id'], requests=daily, first_attempt_success=daily, synthetic=1))
    assert public(rollup(coverage, counters))['windows']['24h']['coverage'] == round(33040 / 43040, 4) == 0.7677


def test_settings_repr_and_validation_exception_redact():
    raw = secrets.token_urlsafe(32)
    assert raw not in repr(Settings(environment='test', async_settle_shadow_probe_api_key=raw))
    with pytest.raises(ValueError) as error:
        Settings(environment='test', async_settle_shadow_probe_api_key=raw, synthetic_monitor_api_key=raw)
    assert raw not in str(error.value) and raw not in repr(error.value)


def test_pristine_personal_workspace_rejects_other_members(tmp_path, monkeypatch):
    store = InMemoryStore()
    names = provisioning.cohort_names()
    user = store.ensure_user(names['email'], email=names['email'], trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    store.add_members(workspace.id, ['other@fixture.invalid'])
    monkeypatch.setattr(provisioning, 'typed_billing_store', lambda _: SimpleNamespace(request_record_write_mode='typed'))
    with pytest.raises(ValueError, match='pristine'):
        provisioning.apply_cohort(store, names, 100_000_000, tmp_path / 'key')
    assert not (tmp_path / 'key').exists()
    assert store.credit_money_snapshot(workspace.id)[0] == 0


def test_interrupted_cleanup_recovers_on_both_sides_of_unlink(tmp_path, monkeypatch):
    original_unlink, original_fdopen = os.unlink, os.fdopen
    for after in (False, True):
        store = InMemoryStore()
        path = tmp_path / str(after)
        with monkeypatch.context() as patch:
            class Partial:
                def __init__(self, *a, **kw): self.file = original_fdopen(*a, **kw)
                def __enter__(self): return self
                def write(self, value):
                    self.file.write(value[:10])
                    self.file.flush()
                    raise OSError('partial')
                def __exit__(self, *a): self.file.close()
            def interrupted_unlink(candidate, *a, _path=path, _after=after, **kw):
                if Path(candidate) != _path:
                    return original_unlink(candidate, *a, **kw)
                if _after:
                    original_unlink(candidate, *a, **kw)
                raise OSError('uncertain unlink')
            patch.setattr(provisioning.os, 'fdopen', Partial)
            patch.setattr(provisioning.os, 'unlink', interrupted_unlink)
            with pytest.raises(OSError):
                provisioning.apply_cohort(store, provisioning.cohort_names(), 100_000_000, path)
        assert store.find_user_by_email(provisioning.cohort_names()['email']) is None
        result = provisioning.apply_cohort(store, provisioning.cohort_names(), 100_000_000, path)
        assert store.get_key_by_raw(path.read_text()).hash == result['key_id']
