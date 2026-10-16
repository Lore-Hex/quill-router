"""Nonempty retained support enters the composed producer graph."""
from __future__ import annotations

import copy
import json
import shutil

import pytest

from scripts.async_settle import shadow_v2_window as command
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_pack_round7_history import nonempty_history
from tests.test_async_settle_shadow_v2_pack_round3_composition import composed  # noqa: F401
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_pack_history import history
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles


def test_composed_nonempty_retained_history(tmp_path, composed):  # noqa: F811
    root, original = composed
    source = tmp_path / 'source'
    shutil.copytree(root / 'capture', source)
    plan = copy.deepcopy(original)
    files = PackFiles(source)
    arguments, support = nonempty_history(files)
    plan['captures']['History'] = files.obj('captures/NonemptyHistory', arguments)
    plan['files'].extend(files.refs.values())
    result = command.collect_day(source, tmp_path / 'out', plan)
    assert result['gate']['verdict'] == 'BLOCKED'
    captured = PackFiles(tmp_path / 'out')
    envelope = json.loads((captured.root / 'proof.resolution_dag_sha256').read_bytes())
    bundle = json.loads((captured.root / 'stage/transport.json').read_bytes())
    lookup = PayloadGraph({row['name']: row['raw'].encode() for row in bundle['preimages']})
    entries = lookup.detail(envelope['history'], 'reset_history')
    assert entries and any(entry['fixed_revision'] for entry in entries)
    assert captured.read(support) == files.read(support)
    (source / support['path']).unlink()
    with pytest.raises(ValueError, match='^MissingProducer:History:retained-original/resolution$'):
        command.collect_day(source, tmp_path / 'missing', plan)


@pytest.mark.parametrize('damage', ['removed', 'changed'])
def test_nonempty_support_semantic_and_byte_cases(tmp_path, damage):
    files = PackFiles(tmp_path)
    arguments, support = nonempty_history(files)
    result = history(files, **arguments)
    assert PayloadGraph(files.originals()).detail(result['history'], 'reset_history')
    if damage == 'removed':
        arguments['dependencies'] = []
        with pytest.raises(ValueError, match='^ResolutionSupport$'):
            history(files, **arguments)
    else:
        # A different original has its own valid file reference. The resolution
        # still names its one original supporting record, which is now absent.
        replacement = files.obj('retained-original/changed', dict(revision='c' * 40))
        arguments['dependencies'] = [replacement]
        with pytest.raises(ValueError, match='^ResolutionSupport$'):
            history(files, **arguments)
    # Keep the separate changed-byte case: this exercises the original file read.
    original = files.root / support['path']
    original.write_bytes(b'{}')
    arguments['dependencies'] = [support]
    with pytest.raises(ValueError, match='^artifact_gap$'):
        history(files, **arguments)
