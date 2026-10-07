"""Negate each new local assertion in an isolated copy and require a pytest assertion failure."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT/'tests').glob('test_async_settle_shadow*.py'))
if '--native' in sys.argv:
    FILES.append(ROOT/'tests/conformance/test_async_settle_shadow_native.py')


def cases():
    for path in FILES:
        source = path.read_text()
        tree = ast.parse(source)
        parents = {child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        lines = source.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1]+len(line))
        for node in ast.walk(tree):
            if not isinstance(node,ast.Assert):
                continue
            parent = node
            while parent in parents and not isinstance(parent,(ast.FunctionDef,ast.AsyncFunctionDef)):
                parent = parents[parent]
            name = getattr(parent,'name','')
            while not name.startswith('test_') and parent in parents:
                parent = parents[parent]
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = parent.name
            test = str(path.relative_to(ROOT)) + ('::'+name if name.startswith('test_') else '')
            before = offsets[node.lineno-1]+node.col_offset
            after = offsets[node.end_lineno-1]+node.end_col_offset
            expression = ast.get_source_segment(source,node.test)
            mutated = source[:before]+f'assert not ({expression}), "inverted shadow assertion {node.lineno}"'+source[after:]
            yield str(path.relative_to(ROOT)),node.lineno,test,mutated


def run(case):
    relative,line,test,mutated = case
    with tempfile.TemporaryDirectory(prefix='f2b-assertion-') as directory:
        target = Path(directory)
        for name in ('src','tests','scripts'):
            shutil.copytree(ROOT/name,target/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.pytest_cache'))
        shutil.copy2(ROOT/'pyproject.toml',target/'pyproject.toml')
        (target/relative).write_text(mutated)
        compile(mutated,relative,'exec')
        outcome = subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider','--tb=short','-x',test],  # noqa: S603
            cwd=target,capture_output=True,text=True,timeout=300,
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(target/'src')})
        killed = outcome.returncode == 1 and bool(re.search(r'\nE\s+(?:assert |AssertionError)',outcome.stdout))
        log = Path(tempfile.gettempdir())/f'f2b-assertion-{Path(relative).stem}-{line}.log'
        log.write_text(outcome.stdout+outcome.stderr)
        row = dict(file=relative,line=line,test=test,killed=killed,exit_code=outcome.returncode,log=str(log))
        print(json.dumps(row),flush=True)
        return row


def main():
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run,cases()))
    Path(tempfile.gettempdir(),'f2b-assertions.json').write_text(json.dumps(results,indent=2)+'\n')
    assert all(row['killed'] for row in results), [row for row in results if not row['killed']]


if __name__ == '__main__':
    main()
