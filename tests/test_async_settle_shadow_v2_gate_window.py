"""One session-owned full seven-day churn construction and rendered gate."""
from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from tests.shadow_v2_gate_fixture_generator import build, ports
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle
from trusted_router.async_settle_shadow_v2_gate_result import render
from trusted_router.async_settle_shadow_v2_gates import evaluate

_collection_trace = sys.gettrace()
try:
    sys.settrace(None)
    from tests.test_async_settle_shadow_v2_evidence import (
        exhaustive,
        no_line_tracing,
        untraced,  # noqa: F401
    )
finally:
    sys.settrace(_collection_trace)


@pytest.fixture(scope='session')
def seven_days(tmp_path_factory: Any) -> Bundle:
    # No test mutates this frozen bundle or its byte objects. Negative variants
    # replace only one immutable row/tuple. Short traced witnesses cover the
    # same schema and gate branches as this exhaustive 302,402-slot population.
    with no_line_tracing():
        return build(tmp_path_factory.mktemp('p5-seven-days'), seconds=604804)


@exhaustive
@pytest.mark.xdist_group('p5-seven-days')
def test_seven_day_churn_render(seven_days: Bundle) -> None:
    result = evaluate(seven_days, ports(), gate='pre-flip')
    raw, attachments = render(result)
    report = json.loads(attachments[0])
    assert report['elapsed_seconds'] == 604802
    assert report['partition'] == dict(T=1, A=1, B=0, C=0)
    assert report['verifiers']['certificate']['calls'] == 0
    assert report['timing']['publisher_slot_rpc_elapsed_us']['count'] == 302402
    assert len(report['days']) == 8
    assert json.loads(raw)['verdict'] == 'PASS', [(f.gap, f.detail) for f in result.failures]
