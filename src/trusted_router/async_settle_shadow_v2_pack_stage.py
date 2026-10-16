"""Captured producer registration, daily review and bounded validator handoff."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_external import policy
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, review_subjects
from trusted_router.async_settle_shadow_v2_pack_scope import check_scope
from trusted_router.async_settle_shadow_v2_pack_streaming import OriginalSpool
from trusted_router.async_settle_shadow_v2_schema import require

PRODUCERS = ('NativeExport', 'Inventory', 'Readiness', 'Shutdown', 'Budget', 'ExternalPack', 'History')
VALIDATION_BYTES = 4 * 1024 * 1024


def references(value: Any) -> Iterator[dict[str, Any]]:
    if type(value) is dict:
        if set(value) == set(a2.FILE_REF.fields):
            a2.FILE_REF(value)
            yield value
        else:
            for child in value.values():
                yield from references(child)
    elif type(value) is list:
        for child in value:
            yield from references(child)


@dataclass(frozen=True)
class StageAssemblyInputs:
    days: tuple[str, ...]
    producers: dict[str, list[dict[str, Any]]]
    sources: tuple[dict[str, Any], ...]
    fixtures: tuple[dict[str, Any], ...]
    predecessors: tuple[dict[str, Any], ...]

    @classmethod
    def parse(cls, plan: dict[str, Any]) -> StageAssemblyInputs:
        require(plan['days'] == sorted(set(plan['days'])) and bool(plan['days']), 'artifact_gap')
        for day in plan['days']:
            a2.day(day)
        require(set(plan['producers']) == set(PRODUCERS), 'StageAssemblyInputs')
        require(all(bool(plan['producers'][p]) for p in PRODUCERS), 'StageAssemblyInputs')
        require(bool(plan['sources']) and bool(plan['fixtures']), 'ReleaseReviewAssembly')
        return cls(tuple(plan['days']), plan['producers'], tuple(plan['sources']),
                   tuple(plan['fixtures']), tuple(plan['predecessors']))


@dataclass(frozen=True)
class StreamingStageAssembly:
    inputs: StageAssemblyInputs
    inventory: dict[str, Any]
    objects: int

    @classmethod
    def capture(cls, source: Files, out: Path, inputs: StageAssemblyInputs) -> StreamingStageAssembly:
        """Register all producer outputs and their explicitly supplied originals.

        Digest references and nested FILE_REFs are checked against this complete
        supplied set. This is source registration, not a stage PASS assertion.
        """
        out.mkdir(parents=True, exist_ok=False)
        pages = PackFiles(out)
        with OriginalSpool(out / 'originals') as spool:
            for producer in PRODUCERS:
                spool.capture(source, iter(inputs.producers[producer]), producer=producer)
            for name, refs in (('sources', inputs.sources), ('fixtures', inputs.fixtures),
                               ('predecessors', inputs.predecessors)):
                spool.capture(source, iter(refs), producer=name)
            # Check every original's FILE_REF edges without holding its bytes.
            known = {ref['path']: ref for ref in spool.references()}
            for ref in spool.references():
                raw = spool.files.read(ref)
                try:
                    value = retained_json(raw)
                except ValueError:
                    continue  # Source/fixture bytes need not be JSON.
                for child in references(value):
                    require(known.get(child['path']) == child, 'ReleaseReviewAssembly')
                    spool.files.read(child)
            entries = spool.leaves(pages)
            count = spool.validate_leaves(pages, entries)
            inventory = pages.detail('inputs/inventory', entries, 'input_inventory')
            pages.obj('stage-inputs.json', dict(days=list(inputs.days), inventory=inventory,
                bundles=entries, objects=count, producer_files=inputs.producers,
                sources=list(inputs.sources), fixtures=list(inputs.fixtures), predecessors=list(inputs.predecessors)))
            return cls(inputs, inventory, count)


def validate_capacity(files: Files, refs: list[dict[str, Any]], *, bound: int = VALIDATION_BYTES) -> dict[str, bytes]:
    """Stop before the unchanged validator's exact-dict whole-byte boundary."""
    require(sum(ref['bytes'] for ref in refs) <= bound, 'FrozenDayValidationCapacity')
    return {ref['path']: files.read(ref) for ref in refs}


def review_days(files: PackFiles, *, days: list[str], subjects: dict[str, dict[str, Any]],
                criteria: dict[str, list[str]], required_stages: dict[str, list[str]],
                verifiers: dict[str, Any]) -> dict[str, Any]:
    require(set(subjects) == set(days), 'ReleaseReviewAssembly')
    policy(required_stages)
    expected: dict[str, set[str]] = {}
    for stage, names in required_stages.items():
        for name in names:
            expected.setdefault(name, set()).update(check_scope(name, stage))
    require(all(set(criteria.get(name, [])) == checks for name, checks in expected.items()), 'artifact_gap')
    for day in days:
        a2.day(day)
        value = retained_json(files.read(subjects[day]))
        require(value.get('day') == day, 'ReleaseReviewAssembly')
        files.obj('review/days/' + day + '.json', dict(day=day, subject=subjects[day], state='awaiting_review'))
    return review_subjects(files, subjects, criteria=criteria, required_stages=required_stages, verifiers=verifiers)
