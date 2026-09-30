"""Execute every rules.json mutation on disposable copies, never the worktree.

Every mutant runs all protocol tests, including the entire literal corpus.
A mutant is red only on a test failure. Compile/import/setup failures are
build-broken, never evidence of a killed mutant. Equivalent survivors require
an explicit, reviewable explanation in the pinned inventory and output table.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
MODULE = Path('src/trusted_router/speculation_protocol.py')


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    original = (ROOT / MODULE).read_text()
    rules = json.loads((ROOT / 'tests/fixtures/speculation_v1/rules.json').read_text())['rules']
    rows = []
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='astra-spec-mutations-', dir='/private/tmp') as tmp:
        target = Path(tmp)
        (target / MODULE).parent.mkdir(parents=True)
        shutil.copytree(ROOT / 'tests/fixtures/speculation_v1', target / 'tests/fixtures/speculation_v1')
        shutil.copyfile(ROOT / 'tests/test_speculation_protocol.py', target / 'tests/test_speculation_protocol.py')
        tests = load('speculation_mutation_tests', target / 'tests/test_speculation_protocol.py')
        cases = {c['name']: c for c in tests.CASES}
        verdicts = {c['name']: c for c in tests.read('verdict-vectors.json')['vectors']}
        wire = target / 'tests/fixtures/speculation_v1/provider-wire.json'
        original_wire = wire.read_bytes()
        original_protocol = tests.protocol
        corpus = []
        for name, test in vars(tests).items():
            if not name.startswith('test_'):
                continue
            if name == 'test_literal':
                corpus.extend((case['name'], test, (case,)) for case in cases.values())
            elif name == 'test_verdict':
                corpus.extend(('verdict:' + case['name'], test, (case,)) for case in verdicts.values())
            else:
                corpus.append((name, test, ()))

        def run_corpus():
            failures = []
            for name, test, args in corpus:
                mutant = tests.protocol
                try:
                    # Inventory completeness describes the original guards, not
                    # intentionally edited predicates. Run this structural check
                    # on the original module; all behavior tests use the mutant.
                    if name == 'test_guard_inventory':
                        tests.protocol = original_protocol
                    test(*args)
                except AssertionError as exc:
                    failures.append(('red', name, str(exc)))
                except Exception as exc:
                    failures.append(('red', name, 'test raised ' + repr(exc)))
                finally:
                    tests.protocol = mutant
            return failures

        # Confirm ALL tests in the unmodified harness before trying any mutants.
        assert not run_corpus()
        for rule in rules:
            failures = []
            tests_run = 0
            code = original
            wire.write_bytes(original_wire)
            try:
                if rule['function'] == '<fixture>':
                    wire.write_bytes(original_wire.replace(b'fixture', b'fixturf', 1))
                else:
                    node = next((n for n in ast.parse(original).body if isinstance(n, ast.FunctionDef) and n.name == rule['function']), None)
                    body = ast.get_source_segment(original, node) if node else original
                    assert body is not None and body.count(rule['before']) == 1, rule
                    changed = body.replace(rule['before'], rule['after'], 1)
                    code = original.replace(body, changed, 1)
                # Compile and import are separately classified from test assertions.
                compile(code, str(target / MODULE), 'exec')
                (target / MODULE).write_text(code)
                tests.protocol = load('speculation_mutant', target / MODULE)
                status, assertion = 'survived', ''
            except Exception as exc:
                status, assertion = 'build-broken', repr(exc)
            else:
                failures = run_corpus()
                tests_run = len(corpus)
                if failures:
                    status, failed_test, assertion = failures[0]
                    assertion = failed_test + ': ' + assertion
            row = {'guard': rule['guard'], 'literal': rule['literal_case'], 'result': status, 'assertion': assertion,
                   'tests_run': tests_run}
            if 'equivalent' in rule:
                row['equivalent'] = rule['equivalent']
            rows.append(row)
            print(json.dumps(row), flush=True)
    print(json.dumps({'inventory_size': len(rows), 'summary': dict(Counter(r['result'] for r in rows))}), flush=True)
    assert all(r['result'] == 'red' or (r['result'] == 'survived' and r.get('equivalent')) for r in rows)


if __name__ == '__main__':
    main()
