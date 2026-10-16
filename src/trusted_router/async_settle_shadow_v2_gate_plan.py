"""Read every leaf of a reviewed input plan before composing one stage."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_gate_loading import (
    LoadingIssue,
    retained_json,
    transport_children,
)
from trusted_router.async_settle_shadow_v2_schema import day, require
from trusted_router.detached_jws import canonical


@dataclass(frozen=True)
class PlanInput:
    raw: bytes
    read: Callable[[str], bytes]

    def file(self, ref: dict[str, Any]) -> bytes:
        a2.FILE_REF(ref)
        raw = self.read(ref['path'])
        require(len(raw) == ref['bytes'] and roots.file_digest(raw) == ref['sha256'], 'artifact_gap')
        return raw

    def leaves(self, plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
        require(plan['format'] == 'shadow-v2-pack-plan-1', 'evidence_schema_gap')
        require(type(plan['days']) is list and bool(plan['days']) and plan['days'] == sorted(set(plan['days'])))
        for date in plan['days']:
            day(date)
        pages = [a2.ATTACHMENT_PAGE.parse(self.file(ref)) for ref in plan['inventory_pages']]
        roots.validate_detail(plan['inventory'], pages, stream='input_inventory')
        entries = [entry for page in pages for entry in page['entries']]
        require(entries == plan['bundles'] and bool(entries), 'artifact_gap')
        seen: dict[str, dict[str, Any]] = {}
        previous = ''
        for entry in entries:
            a2.INPUT_INVENTORY_ENTRY(entry)
            leaf = a2.INPUT_LEAF.parse(self.file(entry['bundle']))
            keys = leaf['keys']
            require(leaf['complete'] and bool(keys), 'artifact_gap')
            require((keys[0]['path'], keys[-1]['path'], len(keys)) ==
                    (entry['first_key'], entry['last_key'], entry['objects']), 'artifact_gap')
            for ref in keys:
                require(ref['path'] > previous, 'artifact_gap')
                previous = ref['path']
                self.file(ref)
                seen[ref['path']] = ref
                yield ref
        require(len(seen) == plan['objects'], 'artifact_gap')
        require(all(seen.get(ref['path']) == ref for ref in plan['predecessors']), 'artifact_gap')

    def decode(self) -> tuple[dict[str, Any], list[LoadingIssue]]:
        try:
            return self._decode()
        except (OSError, ValueError, KeyError, TypeError):
            # Retain independently readable scope for a blocked stage report.
            try:
                plan = retained_json(self.raw)
                metadata = retained_json(self.file(plan['metadata']))
                value, issues = transport_children(canonical(metadata), 262144)
            except (OSError, ValueError, KeyError, TypeError):
                value, issues = {}, []
            issues.append(LoadingIssue('artifact_gap', 'transport/input-plan', 'artifact_gap'))
            return value, issues

    def _decode(self) -> tuple[dict[str, Any], list[LoadingIssue]]:
        plan = retained_json(self.raw)
        require(plan['format'] == 'shadow-v2-captured-transport-1', 'evidence_schema_gap')
        registered = {ref['path']: ref for ref in self.leaves(plan['inputs'])}
        used: set[str] = set()

        def read(ref: dict[str, Any]) -> bytes:
            require(registered.get(ref['path']) == ref, 'artifact_gap')
            require(ref['path'] not in used, 'artifact_gap')
            used.add(ref['path'])
            return self.file(ref)

        metadata = retained_json(read(plan['metadata']))
        require(metadata['days'] == plan['inputs']['days'], 'artifact_gap')
        value, issues = transport_children(canonical(metadata), 262144)
        arrays: dict[str, list[Any]] = {name: [] for name in ('rows', 'attachments', 'ledgers', 'preimages')}
        count = 0
        for ref in plan['parts']:
            children = retained_json(read(ref))
            require(type(children) is list and len(children) <= 9999, 'evidence_schema_gap')
            for child in children:
                if set(child) == {'member', 'encoded'}:
                    encoded = bytearray()
                    for piece in child['encoded']:
                        encoded.extend(read(piece))
                        require(len(encoded) <= 2 * 1024 * 1024, 'artifact_gap')
                    decoded = retained_json(bytes(encoded), limit=2 * 1024 * 1024)
                    require(decoded['member'] == child['member'], 'artifact_gap')
                    child = decoded
                require(set(child) == {'member', 'value'} and child['member'] in arrays, 'evidence_schema_gap')
                # Apply the same original-child parser as a single transport.
                checked, failures = transport_children(canonical({child['member']: [child['value']]}), 2 * 1024 * 1024)
                issues.extend(failures)
                arrays[child['member']].extend(checked.get(child['member'], []))
                count += 1
        require(count == plan['objects'], 'artifact_gap')
        require(used == set(registered), 'artifact_gap')
        return {**value, **arrays}, issues
