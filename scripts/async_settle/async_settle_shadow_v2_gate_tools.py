"""Explicit v2 I/O boundary for the unchanged retained Python entry points."""
from __future__ import annotations

import copy
import sys
import threading
from collections.abc import Mapping
from typing import Any, Literal

from scripts.async_settle import pilot_enablement as pilot
from scripts.async_settle import shadow_report as shadow
from trusted_router.async_settle_shadow_v2_a2_evidence import FILE_REF, RETAINED_ENTRY_POINTS
from trusted_router.async_settle_shadow_v2_gate_ports import RetainedResult
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical

_LOCK = threading.RLock()
_FUNCTIONS = {name: getattr(pilot if name.startswith('pilot_enablement.') else shadow,
                          name.split('.')[1]) for name in RETAINED_ENTRY_POINTS}


def run_retained(entry_point: str, payload: Any, files: Mapping[str, bytes]) -> RetainedResult:
    """Call the real function with its unchanged data and finite read-only files.

    These eight functions only open files through pilot.Path. The temporary
    reader changes no payload paths and cannot resolve an undeclared file.
    Scoped restoration is exception safe; CLI invocations are serialized.
    """
    require(entry_point in _FUNCTIONS, 'evidence_schema_gap')
    function = _FUNCTIONS[entry_point]
    module = pilot if entry_point.startswith('pilot_enablement.') else shadow
    require(getattr(module, entry_point.split('.')[1]) is function, 'evidence_schema_gap')
    opened: set[str] = set()
    calls: dict[str, int] = {}
    components = {}

    class FinitePath:
        def __init__(self, path: str) -> None:
            FILE_REF.fields['path'](path)
            require(path in files, 'artifact_gap')
            self.path = path

        def read_bytes(self) -> bytes:
            opened.add(self.path)
            return files[self.path]

        def read_text(self, encoding: str = 'utf-8') -> str:
            return self.read_bytes().decode(encoding)

    data = copy.deepcopy(payload)
    codes = {fn.__code__: name for name, fn in _FUNCTIONS.items()}
    codes[shadow.report.__code__] = 'shadow_report.report'
    previous_profile = sys.getprofile()
    def count_call(frame: Any, event: Literal['call', 'return', 'c_call', 'c_return', 'c_exception'], arg: Any) -> None:
        if event == 'call' and frame.f_code in codes:
            name = codes[frame.f_code]
            calls[name] = calls.get(name, 0) + 1
        if previous_profile is not None:
            previous_profile(frame, event, arg)
    with _LOCK:
        original_path = pilot.Path
        pilot.Path = FinitePath
        sys.setprofile(count_call)
        try:
            result: Any
            if entry_point == 'shadow_report.collect_rollbacks':
                # report owns resolution shape/support validation; collect alone
                # does not. Include both results so neither call can disappear.
                report = shadow.report(data['rows'], data['days'], data['proof'],
                                       not_before_us=data.get('not_before_us', 0))
                resets = data['resets']
                function(resets, [tuple(row) for row in data['serving']],
                         data['proof'].get('resolved_mismatches', []), data['manifests'])
                result = dict(resets=resets, report=report)
            else:
                result = function(data)
                if isinstance(result, set):
                    result = sorted(result)
            if entry_point in ('pilot_enablement.pre_flip', 'pilot_enablement.shadow_serving'):
                if entry_point.endswith('.pre_flip'):
                    components['shadow_serving'] = pilot.shadow_serving(data)
                components['fleet_budgets'] = pilot.fleet_budgets(
                    {**data['observer'], 'serving': data['serving'], 'rows': data['rows']})
            return RetainedResult(canonical(result), tuple(sorted(opened)), calls, components)
        finally:
            sys.setprofile(previous_profile)
            pilot.Path = original_path


def read_release_plan(raw: bytes, approved_sha256: str) -> Any:
    """Explicit independently pinned release input; never infer a trusted pin."""
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
    from trusted_router.async_settle_shadow_v2_gate_ports import ReleasePlan
    from trusted_router.async_settle_shadow_v2_schema import digest, name

    digest(approved_sha256)
    require(file_digest(raw) == approved_sha256, 'artifact_gap')
    value = retained_json(raw)
    require(type(value) is dict and set(value) == {'artifacts', 'criteria', 'verifiers', 'required'}, 'evidence_schema_gap')
    for path, pin in value['artifacts'].items():
        FILE_REF.fields['path'](path)
        digest(pin)
    for criterion, checks in value['criteria'].items():
        name(criterion)
        require(type(checks) is list and checks and len(checks) == len(set(checks)), 'evidence_schema_gap')
        for check in checks:
            name(check)
    verifiers = {}
    for entry, pins in value['verifiers'].items():
        name(entry)
        require(type(pins) is list and len(pins) == 3, 'evidence_schema_gap')
        digest(pins[0])
        digest(pins[1])
        if pins[2] is not None:
            digest(pins[2])
        verifiers[entry] = tuple(pins)
    require(type(value['required']) is list and len(value['required']) == len(set(value['required'])), 'evidence_schema_gap')
    for criterion in value['required']:
        name(criterion)
    return ReleasePlan('loading/release-plan.json', raw, value['artifacts'],
        {k: tuple(v) for k, v in value['criteria'].items()}, verifiers, tuple(value['required']))
