"""Batch F1 assertion negations in a disposable copy; never edit live sources.

Run with the repository Python environment. Each assertion gets an independent
module and ordinary function-scoped pytest fixtures. Native assertions require
CI's emulator and are explicitly outside this local sensitivity sweep.
"""
# ruff: noqa: S108 - requested disposable evidence paths
from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ('tests/test_async_settle_proof.py', 'tests/test_async_settle_proof_faults.py',
         'tests/test_async_settle_proof_oracle.py')

REPORT_PLUGIN = '''
import json
import os
from pathlib import Path

def pytest_runtest_logreport(report):
    if report.when != 'call' or os.environ.get('PYTEST_XDIST_WORKER'):
        return
    with Path('assertion-reports.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(nodeid=report.nodeid, outcome=report.outcome,
                                    message=str(report.longrepr) if report.failed else ''))+'\\n')
'''


def run() -> None:
    manifest, selections = [], []
    with tempfile.TemporaryDirectory(prefix='f1-assertion-batch-') as directory:
        target = Path(directory)
        for folder in ('src', 'tests', 'scripts', 'docs'):
            shutil.copytree(ROOT/folder, target/folder,
                            ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
        shutil.copy2(ROOT/'pyproject.toml', target/'pyproject.toml')
        (target/'f1_probe_report.py').write_text(REPORT_PLUGIN)
        # Sensitivity needs one positive vector plus all scenario branches;
        # the unmodified 28-vector matrix runs separately in the regression gate.
        cases = target/'tests/test_async_settle_handler.py'
        cases.write_text(cases.read_text().replace(
            "SUPPORTED = [c for c in CASES if c['expected_exclusion'] is None]",
            "SUPPORTED = [c for c in CASES if c['expected_exclusion'] is None][:1]"))
        for relative in FILES:
            parsed = ast.parse((ROOT/relative).read_text())
            # Generated assertion probes run by explicit node id, outside CI's
            # file inventory. Keep every assertion/parameter, but do not label
            # these disposable copies as dedicated-job test files.
            for node in ast.walk(parsed):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    node.decorator_list = [decorator for decorator in node.decorator_list
                                           if ast.unparse(decorator) != 'pytest.mark.proof_oracle']
            for function in parsed.body:
                if not isinstance(function, ast.FunctionDef):
                    continue
                names = [function.name]
                if function.name == 'error_envelope':
                    names = ['test_error_envelopes_real_http']
                if function.name == 'run_four_paths':
                    names = ['test_four_path_scenario_axes', 'test_four_path_billing_state']
                for assertion in ast.walk(function):
                    if not isinstance(assertion, ast.Assert):
                        continue
                    marker = f'F1 assertion probe {relative}:{assertion.lineno}'
                    entry = dict(file=relative, line=assertion.lineno, scope=function.name,
                                 expression=ast.unparse(assertion.test), marker=marker, variants={})
                    for variant in ('inverted', 'original'):
                        tree = copy.deepcopy(parsed)
                        node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assert)
                                    and n.lineno == assertion.lineno)
                        if variant == 'inverted':
                            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
                        node.msg = ast.Constant(marker)
                        name = f'tests/_f1_assert_{len(manifest):03}_{variant}.py'
                        (target/name).write_text(ast.unparse(ast.fix_missing_locations(tree)))
                        entry['variants'][variant] = name
                        selections.extend(name+'::'+function for function in names)
                    manifest.append(entry)
        completed = subprocess.run(  # noqa: S603 - controlled disposable modules
            [sys.executable, '-m', 'pytest', '-q', '-n', '6', '-p', 'no:cacheprovider',
             '-p', 'f1_probe_report', '--runxfail', '--tb=short', '--disable-warnings', *selections],
            cwd=target, env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(target), str(target/'src'))),
                            'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, timeout=1800)
        Path('/tmp/f1-assertion-batch.log').write_text(completed.stdout+completed.stderr)
        reports = [json.loads(line) for line in (target/'assertion-reports.jsonl').read_text().splitlines()]
        results = []
        for entry in manifest:
            outcome, witness = 'unproven', None
            for variant in ('inverted', 'original'):
                witness = next((r for r in reports if r['outcome'] == 'failed'
                                and r['nodeid'].startswith(entry['variants'][variant]+'::')
                                and 'AssertionError: '+entry['marker'] in r['message']), None)
                if witness is not None:
                    outcome = 'inverted-red' if variant == 'inverted' else 'finding-already-red'
                    break
            result = {k: v for k, v in entry.items() if k != 'variants'}
            result.update(result=outcome, test=witness['nodeid'] if witness else None)
            results.append(result)
            print(json.dumps(result), flush=True)
        Path('/tmp/f1-assertions-final.json').write_text(json.dumps(results, indent=2)+'\n')
    if completed.returncode != 1 or any(row['result'] == 'unproven' for row in results):
        raise SystemExit('Assertion sweep incomplete; inspect /tmp/f1-assertions-final.json')


if __name__ == '__main__':
    run()
