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


_KEPT = 'spanner channel failover armed client=%s'
_FAILOVER = 'trusted_router.storage_gcp_channel_failover'
_LOG_METHODS = frozenset({'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'log'})
# Reviewed argument expressions for the positions that are not class names,
# by module. A class-name position takes exactly type(x).__name__, and no
# other position may. Anything else needs a review and an entry here.
_REVIEWED_ARGUMENTS = {
    'storage_gcp_channel_failover': frozenset({
        'name', 'self._name', 'suspicion.reason', 'suspicion.stalled_ms', 'waiting', 'streak',
        'probe_ms', 'failovers', "'answered' if standby_answered else 'silent'",
        "'answered' if active_answered else 'silent'",
    }),
    'storage_gcp_transport_watch': frozenset({
        'name', 'self._name', 'state', "previous or 'none'",
        '0 if previous is None else max(0, round((now - since) * 1000))',
        'max(0, round((now - started) * 1000))', '_inline()', 'self._ttl_seconds()', 'suppressed',
        # logger.log levels, not output text
        'level', 'logging.WARNING if slow else logging.INFO',
    }),
}
# Every binding, anywhere in the module, of a name the reviewed expressions
# read. A new one (error_class = settings.provider_key, say) needs a review.
_REVIEWED_BINDINGS = {
    'storage_gcp_channel_failover': frozenset({
        'param name', 'param suspicion', 'self._name = name',
        'waiting = [start for start in self.inflight.values() if start >= self.last_answer]',
        'waiting = len(active.inflight)', 'streak = active.deadline_streak',
        'failovers = self._failovers', 'probe_ms = max(0, round((now - suspicion.started) * 1000))',
        'suspicion = self._suspect(now)', 'suspicion = self._suspect(self._clock())',
        'standby_answered = _answered_within(standby_future, wait)',
        'active_answered = _answered_within(active_future, wait)',
    }),
    'storage_gcp_transport_watch': frozenset({
        'param name', 'param now', 'param started', 'self._name = name', 'def _inline',
        "state = str(getattr(connectivity, 'name', connectivity))", 'now = time.monotonic()',
        "level = logging.WARNING if state == 'TRANSIENT_FAILURE' else logging.INFO",
        'started = time.monotonic()', 'slow = now - started >= SLOW_REFRESH_SECONDS',
        'suppressed = 0 if slow else self._quick.admit(now)', 'suppressed = self._failures.admit(now)',
        'now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)',
        '(previous, since) = (self._previous, self._since)',
        '(suppressed, self._suppressed) = (self._suppressed, 0)',
    }),
}
_FIXED_VOCABULARY_MODULES = tuple(_REVIEWED_ARGUMENTS)
_CLASS_POSITIONS = (output._EXCEPTION_CLASS, output._CREDENTIALS_CLASS)


def _source(module):
    from pathlib import Path

    return (Path(output.__file__).resolve().parents[1] / f'{module}.py').read_text()


def _log_calls(source):
    """Every call of a log method, whatever it is called on: ``logger``,
    ``self._logger``, an alias or a ``logging.getLogger(...)`` chain."""
    import ast

    return [node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS]


def _is_class_name(node):
    import ast

    return (isinstance(node, ast.Attribute) and node.attr == '__name__'
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
            and node.value.func.id == 'type' and len(node.value.args) == 1
            and not node.value.keywords)


def _indirect_log_references(tree):
    """A log method used other than by calling it directly, or Logger._log."""
    import ast

    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    names = _LOG_METHODS | {'_log'}
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names and (
                node.attr == '_log' or id(node) not in called):
            problems.append(f'reference: {ast.unparse(node)}')
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ('getattr', 'setattr', 'hasattr')
              and any(isinstance(arg, ast.Constant) and arg.value in names for arg in node.args)):
            problems.append(f'reference: {ast.unparse(node)}')
        elif isinstance(node, ast.ImportFrom) and node.module == 'logging' and any(
                alias.name in names or alias.name == '*' for alias in node.names):
            problems.append(f'import: {ast.unparse(node)}')
    return problems


def _bindings(tree, names):
    import ast

    def binds(target):
        return any((isinstance(node, ast.Name) and node.id in names)
                   or (isinstance(node, ast.Attribute) and ast.unparse(node) in names)
                   for node in ast.walk(target))

    found = set()
    for node in ast.walk(tree):
        pairs = []
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            pairs = [(node.target, node.value)]
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            pairs = [(node.target, node.iter)]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            pairs = [(node.optional_vars, node.context_expr)]
        elif isinstance(node, ast.arguments):
            found |= {f'param {arg.arg}' for arg in (*node.posonlyargs, *node.args, *node.kwonlyargs,
                                                      *filter(None, (node.vararg, node.kwarg)))
                      if arg.arg in names}
        elif isinstance(node, ast.ExceptHandler) and node.name in names:
            found.add(f'except {node.name}')
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            found.add(f'def {node.name}')
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found |= {f'import {alias.asname or alias.name}' for alias in node.names
                      if (alias.asname or alias.name).split('.')[0] in names}
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found |= {f'global {name}' for name in node.names if name in names}
        found |= {f'{ast.unparse(target)} = {ast.unparse(value) if value is not None else ""}'
                  for target, value in pairs if binds(target)}
    return found


def _read_names(expressions):
    import ast

    names = {'self._name'}
    for expression in expressions:
        names |= {node.id for node in ast.walk(ast.parse(expression, mode='eval'))
                  if isinstance(node, ast.Name)}
    return names - {'max', 'round', 'type', 'str', 'logging', 'self'}


def _log_call_problems(source, logger_name, reviewed, reviewed_bindings=None):
    import ast

    tree = ast.parse(source)
    problems = _indirect_log_references(tree)
    for call in _log_calls(source):
        text = ast.unparse(call)
        method = call.func.attr
        if method in ('exception', 'critical') or call.keywords:
            problems.append(f'method or keywords: {text}')
            continue
        args = list(call.args)
        if method == 'log':
            if not args or ast.unparse(args[0]) not in reviewed:
                problems.append(f'level: {text}')
                continue
            args = args[1:]
        if not args or not (isinstance(args[0], ast.Constant) and type(args[0].value) is str):
            problems.append(f'format: {text}')
            continue
        positions = output.FIXED_FORMATS.get((logger_name, args[0].value))
        if positions is None or len(positions) != len(args) - 1:
            problems.append(f'unlisted format: {text}')
            continue
        for arg, position in zip(args[1:], positions, strict=True):
            if position in _CLASS_POSITIONS:
                if not _is_class_name(arg):
                    problems.append(f'class position {ast.unparse(arg)}: {text}')
            elif _is_class_name(arg) or not (isinstance(arg, ast.Constant) or ast.unparse(arg) in reviewed):
                problems.append(f'argument {ast.unparse(arg)}: {text}')
    if reviewed_bindings is not None:
        problems += [f'binding: {binding}'
                     for binding in sorted(_bindings(tree, _read_names(reviewed)) - reviewed_bindings)]
    return problems


@pytest.mark.parametrize('module', _FIXED_VOCABULARY_MODULES)
def test_fixed_vocabulary_modules_log_only_listed_formats(module):
    import importlib

    name = f'trusted_router.{module}'
    assert importlib.import_module(name).logger.name == name
    assert len(_log_calls(_source(module))) >= 6
    assert _log_call_problems(_source(module), name, _REVIEWED_ARGUMENTS[module],
                              _REVIEWED_BINDINGS[module]) == []


def test_every_listed_format_is_used_and_fits_its_positions():
    import ast
    import re

    used = set()
    for module in _FIXED_VOCABULARY_MODULES:
        for call in _log_calls(_source(module)):
            message = call.args[1] if call.func.attr == 'log' else call.args[0]
            assert isinstance(message, ast.Constant)
            used.add((f'trusted_router.{module}', message.value))
    assert set(output.FIXED_FORMATS) == used
    for (_name, message), positions in output.FIXED_FORMATS.items():
        specs = re.findall(r'%(.)', message)
        assert len(specs) == len(positions), message
        for spec, position in zip(specs, positions, strict=True):
            assert (spec == 'd') == isinstance(position, range), message
            if isinstance(position, range):
                assert len(str(max(abs(position.start), abs(position.stop)))) <= 9, message


@pytest.mark.parametrize('module, injected', [
    # The independent reviews' injections; each must fail the guard.
    ('storage_gcp_channel_failover',
     'import logging\nlogging.getLogger(__name__).warning(f"spanner channel failover armed {database.name}")'),
    ('storage_gcp_channel_failover',
     'logger.info("spanner channel failover armed client=%s", getattr(database, "api_key", None))'),
    ('storage_gcp_channel_failover', 'self._logger.info("spanner channel failover armed client=%s", token)'),
    ('storage_gcp_channel_failover',
     'log = logger\nlog.warning("spanner channel failover armed client=%s key=%s", name, key)'),
    ('storage_gcp_channel_failover', 'logger.info("spanner channel failover armed client=%s", name, exc_info=True)'),
    ('storage_gcp_channel_failover', 'logger.exception("spanner channel failover armed client=%s", name)'),
    ('storage_gcp_channel_failover', 'logger.info("spanner channel failover armed client=%s", *names)'),
    ('storage_gcp_channel_failover', 'logger.info("spanner channel failover armed client=" + name)'),
    ('storage_gcp_channel_failover', 'logger.warn("spanner channel failover armed client=%s", token)'),
    ('storage_gcp_channel_failover', 'w = logger.warning\nw("spanner channel failover armed client=%s", name)'),
    ('storage_gcp_channel_failover', 'getattr(logger, "warning")("spanner channel failover armed client=%s", name)'),
    ('storage_gcp_channel_failover',
     'import functools\nfunctools.partial(logger.info, "spanner channel failover armed client=%s")(name)'),
    ('storage_gcp_channel_failover',
     'logger._log(20, "spanner channel failover armed client=%s", (name,))'),
    ('storage_gcp_channel_failover', 'from logging import warning'),
    # A class-name position takes exactly type(x).__name__, and only it does.
    ('storage_gcp_channel_failover',
     'logger.warning("spanner channel failover dropped client=%s error_class=%s", name, settings.key)'),
    ('storage_gcp_channel_failover',
     'logger.warning("spanner channel failover dropped client=%s error_class=%s", name, name)'),
    ('storage_gcp_channel_failover',
     'logger.info("spanner channel failover armed client=%s", type(token).__name__)'),
    # A reviewed name rebound to something unreviewed.
    ('storage_gcp_channel_failover', 'name = settings.provider_key'),
    ('storage_gcp_channel_failover', 'for waiting in secrets:\n    pass'),
    ('storage_gcp_transport_watch', 'suppressed = settings.provider_key'),
    ('storage_gcp_transport_watch', 'state = token'),
    ('storage_gcp_transport_watch', 'import secrets as since'),
])
def test_log_call_guard_rejects_unreviewed_calls(module, injected):
    source = _source(module) + '\n\ndef _injected(self):\n' + ''.join(
        '    ' + line + '\n' for line in injected.splitlines())
    assert _log_call_problems(source, f'trusted_router.{module}', _REVIEWED_ARGUMENTS[module],
                              _REVIEWED_BINDINGS[module])


def test_no_other_module_logs_under_the_fixed_vocabulary_names():
    import ast
    from pathlib import Path

    names = {name for name, _message in output.FIXED_FORMATS}
    root = Path(output.__file__).resolve().parents[1]
    problems = []
    for path in sorted(root.rglob('*.py')):
        module = 'trusted_router.' + '.'.join(path.relative_to(root).with_suffix('').parts)
        tree = ast.parse(path.read_text())
        # A loop over literal names (services/resale.py filters three loggers).
        literal_loops = {node.target.id for node in ast.walk(tree)
                         if isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                         and isinstance(node.iter, (ast.Tuple, ast.List))
                         and all(isinstance(item, ast.Constant) for item in node.iter.elts)}
        for node in ast.walk(tree):
            if module == output.__name__:
                break  # the allow-list itself holds the names
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(
                    node.value == name or node.value.startswith(name + '.') for name in names):
                problems.append(f'{module}: {node.value}')
            if (isinstance(node, ast.Call) and node.args
                    and isinstance(node.func, (ast.Name, ast.Attribute))
                    and getattr(node.func, 'id', getattr(node.func, 'attr', None)) == 'getLogger'):
                arg = node.args[0]
                if not isinstance(arg, ast.Constant) and not (
                        isinstance(arg, ast.Name) and (arg.id == '__name__' or arg.id in literal_loops)):
                    problems.append(f'{module}: getLogger({ast.unparse(arg)})')
    assert problems == []


def _sample(position):
    if isinstance(position, range):
        return position.stop - 1
    if position is bool:
        return True
    if position is output._EXCEPTION_CLASS:
        return 'TimeoutError'
    if position is output._CREDENTIALS_CLASS:
        return 'AnonymousCredentials'
    return sorted(position)[0]


def test_listed_lines_keep_their_text_on_the_worker(caplog):
    cleanup = output.install_logging()
    try:
        with caplog.at_level(logging.DEBUG):
            for (name, message), positions in output.FIXED_FORMATS.items():
                args = tuple(_sample(position) for position in positions)
                logging.getLogger(name).warning(message, *args)
                assert caplog.records[-1].getMessage() == message % args
    finally:
        cleanup()


def test_class_positions_take_real_class_names_only(caplog):
    class LoadedLaterError(Exception):
        pass

    unavailable = 'spanner channel failover unavailable client=%s error_class=%s'
    watched = 'spanner credential refresh watched client=%s kind=%s'
    watch = 'trusted_router.storage_gcp_transport_watch'
    cleanup = output.install_logging()
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger(_FAILOVER).warning(unavailable, 'primary', 'LoadedLaterError')
            logging.getLogger(_FAILOVER).warning(unavailable, 'primary', 'AnonymousCredentials')
            logging.getLogger(watch).info(watched, 'primary', 'AnonymousCredentials')
            logging.getLogger(watch).info(watched, 'primary', 'TimeoutError')
    finally:
        cleanup()
    assert [record.getMessage() for record in caplog.records] == [
        'spanner channel failover unavailable client=primary error_class=LoadedLaterError',
        f'logger={_FAILOVER} level=WARNING exception_class=none',
        'spanner credential refresh watched client=primary kind=AnonymousCredentials',
        f'logger={watch} level=INFO exception_class=none',
    ]


@pytest.mark.parametrize('name, message, args', [
    (_FAILOVER, _KEPT, ('sk-tr-v1-' + 'a' * 40,)),
    (_FAILOVER, _KEPT, ('ya29.' + 'b' * 53,)),
    (_FAILOVER, _KEPT, ('secondary',)),
    (_FAILOVER, _KEPT, ('',)),
    (_FAILOVER, _KEPT, (None,)),
    (_FAILOVER, _KEPT, (b'primary',)),
    (_FAILOVER, _KEPT, (type('Word', (str,), {})('primary'),)),
    (_FAILOVER, _KEPT, ({'client': 'primary'},)),
    (_FAILOVER, _KEPT, ()),
    (_FAILOVER, _KEPT, ('primary', 'primary')),
    (_FAILOVER, type('Format', (str,), {})(_KEPT), ('primary',)),
    # A message off the list, with no arguments (an f-string's shape).
    (_FAILOVER, 'spanner channel failover armed client=sk-tr-v1-private', ()),
    (_FAILOVER, 'spanner channel failover unavailable client=%s error_class=%s',
     ('primary', 'ya29.private-token')),
    (_FAILOVER, 'spanner channel failover unavailable client=%s error_class=%s',
     ('primary', 'sk-tr-v1-private')),
    (_FAILOVER, 'spanner channel probe kept client=%s reason=%s standby=%s active=%s '
     'stalled_ms=%d probe_ms=%d', ('primary', 'waiting', 'answered', 'silent', True, 1)),
    ('trusted_router.storage_gcp_transport_watch',
     'spanner credential refresh ok elapsed_ms=%d inline=%s ttl_s=%d suppressed=%d client=%s',
     (1, 'yes', 1, 1, 'primary')),
    # Class-name positions: a string that only looks like a class name.
    (_FAILOVER, 'spanner channel failover unavailable client=%s error_class=%s',
     ('primary', 'AIzaSy' + 'C' * 33)),
    (_FAILOVER, 'spanner channel failover unavailable client=%s error_class=%s',
     ('primary', 'AKIA' + 'Q' * 16)),
    (_FAILOVER, 'spanner channel failover unavailable client=%s error_class=%s',
     ('primary', 'Hunter2Password')),
    (_FAILOVER, 'spanner channel failover unavailable client=%s error_class=%s',
     ('primary', '_Bearer_eyJ' + 'h' * 30)),
    ('trusted_router.storage_gcp_transport_watch',
     'spanner credential refresh watched client=%s kind=%s', ('primary', 'Hunter2Password')),
    # Int positions are bounded: no 16-digit number, no negative duration.
    ('trusted_router.storage_gcp_transport_watch',
     'spanner credential refresh ok elapsed_ms=%d inline=%s ttl_s=%d suppressed=%d client=%s',
     (4111111111111111, True, 1, 1, 'primary')),
    ('trusted_router.storage_gcp_transport_watch',
     'spanner credential refresh ok elapsed_ms=%d inline=%s ttl_s=%d suppressed=%d client=%s',
     (-1, True, 1, 1, 'primary')),
    ('trusted_router.storage_gcp_transport_watch',
     'spanner credential refresh ok elapsed_ms=%d inline=%s ttl_s=%d suppressed=%d client=%s',
     (1, True, 1, 1_000_001, 'primary')),
    # Exact logger names only: no children, no near names, no other module.
    (_FAILOVER + '.child', _KEPT, ('primary',)),
    (_FAILOVER + '2', _KEPT, ('primary',)),
    ('trusted_router.storage_gcp', _KEPT, ('primary',)),
    ('trusted_router.storage_gcp_transport_watch', _KEPT, ('primary',)),
])
def test_unlisted_records_are_scrubbed(name, message, args, caplog):
    cleanup = output.install_logging()
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger(name).info(message, *args)
    finally:
        cleanup()
    assert [record.getMessage() for record in caplog.records] == [
        f'logger={name} level=INFO exception_class=none']
    assert 'private' not in caplog.text and 'sk-tr' not in caplog.text and 'ya29' not in caplog.text


def test_listed_line_never_carries_an_exception_or_stack(caplog):
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger = logging.getLogger(_FAILOVER)
    logger.addHandler(handler)
    cleanup = output.install_logging()
    try:
        with caplog.at_level(logging.INFO, logger=_FAILOVER):
            try:
                raise ValueError('private-exception ya29.private-token')
            except ValueError:
                logger.info(_KEPT, 'primary', exc_info=True, stack_info=True,
                            extra={'secret': 'private-extra'})
    finally:
        cleanup()
        logger.removeHandler(handler)
    [record] = caplog.records
    assert record.getMessage() == 'spanner channel failover armed client=primary'
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert not hasattr(record, 'secret')
    assert stream.getvalue() == 'spanner channel failover armed client=primary\n'


def test_other_storage_loggers_stay_scrubbed(caplog):
    cleanup = output.install_logging()
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger('trusted_router.storage_gcp_authorize').info('gateway reap reservation_id=%s', 'r1')
    finally:
        cleanup()
    assert caplog.records[0].getMessage() == (
        'logger=trusted_router.storage_gcp_authorize level=INFO exception_class=none')


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
