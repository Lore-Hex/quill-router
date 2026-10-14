"""Finite offline file plans and release review subjects."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS, Files, approval
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_schema import Schema, require
from trusted_router.detached_jws import canonical


@dataclass
class PackFiles(Files):
    """Only explicitly supplied or created files enter the inventory."""
    refs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put(self, path: str, raw: bytes, schema: Schema | None = None) -> dict[str, Any]:
        target = (self.root / path).resolve()
        require(target.is_relative_to(self.root.resolve()), 'artifact_gap')
        ref = super().put(path, raw, schema)
        self.refs[path] = ref
        return ref

    def capture(self, source: Files, refs: list[dict[str, Any]]) -> None:
        for ref in refs:
            self.put(ref['path'], source.read(ref))

    def originals(self) -> dict[str, bytes]:
        return {path: self.read(ref) for path, ref in sorted(self.refs.items())}


def input_plan(files: PackFiles, references: list[dict[str, Any]], *,
               days: list[str], predecessors: list[dict[str, Any]],
               prefix: str = 'inputs') -> dict[str, Any]:
    """Partition by both INPUT_LEAF byte bound and explicit-key bound."""
    require(bool(days) and days == sorted(set(days)), 'artifact_gap')
    for day in days:
        a2.day(day)
    from trusted_router.async_settle_shadow_v2_pack_originals import release_dependency
    ordered = sorted((ref for ref in references if not release_dependency(ref['path'])), key=lambda ref: ref['path'])
    require(bool(ordered) and len({r['path'] for r in ordered}) == len(ordered), 'artifact_gap')
    for ref in [*ordered, *predecessors]:
        files.read(ref)
    require(all(ref in ordered for ref in predecessors), 'artifact_gap')
    entries: list[dict[str, Any]] = []
    batch: list[dict[str, Any]] = []
    empty_bytes = len(canonical(dict(**TAGS, keys=[], complete=True)))
    batch_bytes = empty_bytes

    def flush() -> None:
        nonlocal batch_bytes
        if not batch:
            return
        leaf = dict(**TAGS, keys=list(batch), complete=True)
        ref = files.obj(f'{prefix}/leaves/{len(entries):05d}.json', leaf, a2.INPUT_LEAF)
        entries.append(dict(bundle=ref, first_key=batch[0]['path'],
                            last_key=batch[-1]['path'], objects=len(batch)))
        batch.clear()
        batch_bytes = empty_bytes

    for ref in ordered:
        entry_bytes = len(canonical(ref))
        if len(batch) == 10000 or batch_bytes + entry_bytes + bool(batch) > a2.INPUT_LEAF.limit:
            flush()
        batch_bytes += entry_bytes + bool(batch)
        batch.append(ref)
    flush()
    inventory = files.detail(prefix + '/inventory', entries, 'input_inventory')
    plan = dict(format='shadow-v2-pack-plan-1', days=days, predecessors=predecessors,
                inventory=inventory, bundles=entries, objects=len(ordered),
                inventory_pages=[ref for path, ref in sorted(files.refs.items())
                                 if path.startswith(prefix + '/inventory/')])
    files.obj(prefix + '/plan.json', plan)
    return plan


def check_plan(files: Files, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Check the reviewed list itself before any day or predecessor selection."""
    require(plan['format'] == 'shadow-v2-pack-plan-1', 'artifact_gap')
    require(plan['days'] == sorted(set(plan['days'])) and bool(plan['days']), 'artifact_gap')
    for day in plan['days']:
        a2.day(day)
    entries = plan['bundles']
    pages = [a2.ATTACHMENT_PAGE.parse(files.read(ref)) for ref in plan['inventory_pages']]
    roots.validate_detail(plan['inventory'], pages, stream='input_inventory')
    require([entry for page in pages for entry in page['entries']] == entries, 'artifact_gap')
    require(plan['inventory']['root'] == roots.entry_root(entries, stream='input_inventory'), 'artifact_gap')
    require(plan['inventory']['count'] == len(entries) and bool(entries), 'artifact_gap')
    # Check every leaf before opening originals; a missing late bundle should
    # not require rereading all earlier original bodies to identify its gap.
    leaves = [(entry, a2.INPUT_LEAF.parse(files.read(entry['bundle']))) for entry in entries]
    found = []
    for entry, leaf in leaves:
        a2.INPUT_INVENTORY_ENTRY(entry)
        keys = leaf['keys']
        require(leaf['complete'] and bool(keys), 'artifact_gap')
        require((keys[0]['path'], keys[-1]['path'], len(keys)) ==
                (entry['first_key'], entry['last_key'], entry['objects']), 'artifact_gap')
        for ref in keys:
            files.read(ref)
        found.extend(keys)
    require(len(found) == plan['objects'], 'artifact_gap')
    require(all(ref in found for ref in plan['predecessors']), 'artifact_gap')
    return found


def review_subjects(files: PackFiles, subjects: Mapping[str, dict[str, Any]], *,
                    criteria: dict[str, list[str]], required_stages: dict[str, list[str]],
                    verifiers: dict[str, Any], window: bool = False) -> dict[str, Any]:
    """Prepare exact subjects and a release plan; no review decision is created."""
    for ref in subjects.values():
        files.read(ref)
    required = sorted({name for names in required_stages.values() for name in names})
    require(set(required) <= set(criteria), 'artifact_gap')
    # A window reviews child input plans, whose bounded leaves enumerate every
    # original. Repeating every child filename here would exceed FILE_REF's
    # bound. Daily transports retain their own full release lists.
    namespaces = ['day-inputs/']
    if window and 'window/predecessor-inputs.json' in files.refs:
        for descriptor in retained_json(files.read(files.refs['window/predecessor-inputs.json'])):
            require(files.refs.get(descriptor['path']) == descriptor, 'PredecessorReview')
            graph = retained_json(files.read(descriptor))
            require(graph['format'] == 'shadow-v2-production-predecessor-1', 'PredecessorReview')
            namespace = graph['source_root']
            require(namespace not in ('', '.') and not namespace.startswith('/')
                    and '..' not in namespace.split('/'), 'PredecessorReview')
            namespaces.append(namespace.rstrip('/') + '/')
    from trusted_router.async_settle_shadow_v2_pack_originals import release_dependency
    plan: dict[str, Any] = dict(artifacts={path: ref['sha256'] for path, ref in sorted(files.refs.items())
                if not release_dependency(path)
                if not (window and path.startswith(tuple(namespaces)))
                if path not in ('loading/release-plan.json', 'review/subjects.json', 'review/sealed.json')
                and not path.startswith(('inputs/', 'gate/'))},
                criteria=criteria, verifiers=verifiers, required=required,
                required_stages=required_stages)
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    for path, original in {**files.refs, **getattr(files, 'large', {})}.items():
        if path in RELEASE_FILES:
            plan['artifacts'][path] = original['sha256']
    ref = files.obj('loading/release-plan.json', plan)
    value = dict(subjects=dict(subjects), release_plan=ref,
                 state='awaiting_review', approval_required=True)
    files.obj('review/subjects.json', value)
    return value


def seal_review(files: PackFiles, subjects: dict[str, Any], *,
                approvals: Mapping[str, dict[str, Any]], release_sha256: str) -> dict[str, Any]:
    from trusted_router.async_settle_shadow_v2_gate_release import decode_release_plan

    released = decode_release_plan(files.read(subjects['release_plan']), release_sha256)
    require(set(approvals) == set(subjects['subjects']), 'artifact_gap')
    checked = {}
    for name, subject in subjects['subjects'].items():
        record = approvals[name]['record']
        require(released.artifacts.get(record['path']) == record['sha256'], 'artifact_gap')
        require(released.artifacts.get(subject['path']) == subject['sha256'], 'artifact_gap')
        checked[name] = approval(files, approvals[name], subject)
    result = dict(release_plan=subjects['release_plan'], approvals=checked, state='reviewed')
    files.obj('review/sealed.json', result)
    return result


def read_capture(root: Path, plan_path: Path) -> tuple[PackFiles, dict[str, Any]]:
    raw = plan_path.read_bytes()
    plan = retained_json(raw)
    files = PackFiles(root)
    check_plan(files, plan)
    return files, plan
