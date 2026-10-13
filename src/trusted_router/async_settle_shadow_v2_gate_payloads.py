"""Materialize A2 payload dependencies from their original bounded file bytes.

This establishes schema and byte closure, not release approval or retained-tool
semantics. The loader keeps the retained adapter seam closed independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_gate_inputs import Boot, Cohort, Inventory, Ports
from trusted_router.async_settle_shadow_v2_schema import (
    Schema,
    day,
    day_at,
    decode,
    identity,
    name,
    require,
    revision,
    sha256,
)
from trusted_router.detached_jws import canonical


@dataclass
class PayloadGraph:
    files: dict[str, bytes]
    streams: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    decoded: dict[str, dict[str, Any]] = field(default_factory=dict)
    _pages: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    _indexed: set[str] = field(default_factory=set)

    def file(self, reference: dict[str, Any]) -> bytes:
        a2.FILE_REF(reference)
        raw = self.files.get(reference['path'])
        require(raw is not None, 'artifact_gap')
        assert raw is not None
        require(len(raw) == reference['bytes'] and roots.file_digest(raw) == reference['sha256'], 'artifact_gap')
        return raw

    def object(self, reference: dict[str, Any], schema: Schema) -> dict[str, Any]:
        value = contract._object_bytes(self.file(reference), schema)
        self.decoded[reference['path']] = value
        self.references(value)
        return value

    def references(self, value: Any) -> None:
        if type(value) is dict:
            if set(value) == set(a2.FILE_REF.fields):
                self.file(value)
            elif set(value) == set(a2.APPROVAL.fields):
                a2.APPROVAL(value)
                review = self.object(value['record'], a2.REVIEW_RECORD)
                require(all(review[key] == value[key] for key in a2.APPROVAL.fields if key != 'record'), 'evidence_schema_gap')
            else:
                for child in value.values():
                    self.references(child)
        elif type(value) is list:
            for child in value:
                self.references(child)

    def detail(self, reference: dict[str, Any], stream: str) -> list[dict[str, Any]]:
        """Follow the committed tail/previous chain; never infer an empty stream."""
        a2.DETAIL(reference)
        schema = roots.STREAMS[stream][1]
        require(schema is not None, 'evidence_schema_gap')
        assert schema is not None
        if stream not in self._indexed:
            # DETAIL supplies hashes, not paths. Candidate files come only from
            # the finite supplied file inventory, never from disk discovery.
            for raw in self.files.values():
                try:
                    value = contract._object_bytes(raw, schema)
                    for entry in value['entries']:
                        roots.STREAMS[stream][0](entry)
                except ValueError:
                    continue
                self._pages[stream, sha256(value)] = value
            self._indexed.add(stream)
        tail = reference['tail_sha256']
        pages: list[dict[str, Any]] = []
        visited: set[str] = set()
        while tail is not None:
            require(tail not in visited and len(pages) < reference['pages'], 'evidence_schema_gap')
            visited.add(tail)
            page = self._pages.get((stream, tail))
            require(page is not None, 'artifact_gap')
            assert page is not None
            pages.append(page)
            tail = page['previous_sha256']
        pages.reverse()
        roots.validate_detail(reference, pages, stream=stream)
        entries = [entry for page in pages for entry in page['entries']]
        self.references(entries)
        self.streams[stream + '/' + reference['root']] = entries
        return entries

    def delegated(self, value: dict[str, Any]) -> None:
        a2.DELEGATED_PAYLOAD(value)
        self.references(value)
        self.detail(value['dependencies'], 'files')

    def materialize(self, payloads: dict[str, dict[str, Any]]) -> None:
        for path, value in payloads.items():
            self.references(value)
            qualified = path.split('/', 1)[-1] if '/manifest.' in path else path
            if qualified == 'proof.shutdown_inventory_sha256':
                for entry in self.detail(value['writers'], 'shutdown_writers'):
                    measurement = self.object(entry['measurement'], a2.SHUTDOWN_MEASUREMENT)
                    require(all(measurement[key] == entry[key] for key in ('day', 'boot')), 'evidence_schema_gap')
            elif qualified in ('proof.pre_budget_sha256', 'proof.post_budget_sha256'):
                self.delegated(value['retained'])
                load = value['v2_load']
                self.detail(load['segments'], 'segments')
                self.detail(load['operations'], 'operations')
            elif qualified == 'proof.resolution_dag_sha256':
                self.delegated(value['retained'])
                for reset in self.detail(value['history'], 'reset_history'):
                    self.detail(reset['support'], 'files')
            elif qualified == 'proof.external_proofs_sha256':
                for requirement in value['requirements']:
                    proof = self.object(requirement['evidence'], a2.PROOF_RESULT)
                    require(proof['criterion'] == requirement['name'], 'evidence_schema_gap')
                    require(all(proof[key] == requirement[key] for key in ('router_revision', 'enclave_revision')), 'evidence_schema_gap')
                    self.detail(proof['sources'], 'files')
                if value['pilot'] is not None:
                    self.delegated(value['pilot'])
            elif qualified == 'manifest.independent_inventory_sha256':
                # The retained roster parser is the next adapter boundary. Do
                # not accept a manifest-shaped stand-in as a validated roster.
                serving = decode(self.file(value['serving']), 262144, retained_strings=True)
                require(type(serving) is list, 'evidence_schema_gap')

    def inventory(self, payloads: dict[str, dict[str, Any]], ports: Ports | None) -> Inventory:
        require(ports is not None and ports.roster_coverage is not None, 'artifact_gap')
        assert ports is not None and ports.roster_coverage is not None
        lifetimes: dict[tuple[str, str, str, str], set[tuple[int, int]]] = {}
        for key, envelope in payloads.items():
            if not key.endswith('/manifest.independent_inventory_sha256'):
                continue
            expected_day = key.split('/')[0]
            require(envelope['complete'] and envelope['day'] == expected_day, 'evidence_schema_gap')
            serving = decode(self.file(envelope['serving']), 262144, retained_strings=True)
            deployment = decode(self.file(envelope['deployment']), 262144, retained_strings=True)
            require(type(serving) is list and type(deployment) is dict and type(deployment.get('serving')) is list, 'evidence_schema_gap')
            measured = ports.roster_coverage(serving)
            require(measured == ports.roster_coverage(deployment['serving']), 'evidence_schema_gap')
            require({row['instance']: row['pins'] for row in serving}
                    == {row['instance']: row['pins'] for row in deployment['serving']}, 'evidence_schema_gap')
            for boot, role, rev, region, date, start, end in measured:
                identity(boot)
                a2.ROLE(role)
                revision(rev)
                name(region)
                day(date)
                require(date == expected_day == day_at(start) == day_at(end - 1), 'evidence_schema_gap')
                lifetimes.setdefault((boot, role, rev, region), set()).add((start, end))
        require(bool(lifetimes), 'artifact_gap')
        require(len({key[0] for key in lifetimes}) == len(lifetimes), 'evidence_schema_gap')
        segments = [entry for key, values in self.streams.items() if key.startswith('segments/') for entry in values]
        shutdown = [entry for key, values in self.streams.items() if key.startswith('shutdown_writers/') for entry in values]
        boots = []
        for (boot, role, rev, region), spans in sorted(lifetimes.items()):
            intervals = sorted(spans)
            require(all(a[1] == b[0] for a, b in zip(intervals, intervals[1:], strict=False)), 'evidence_schema_gap')
            boots.append(Boot(boot, role, rev, region, intervals[0][0], intervals[-1][1],
                contender=any(s['boot'] == boot and s['contender'] for s in segments),
                termination_applies=any(s['boot'] == boot and s['termination_applies'] for s in shutdown)))
        return Inventory(tuple(boots))

    def cohort(self, payloads: dict[str, dict[str, Any]]) -> Cohort:
        policy = payloads.get('proof.cohort_seed_policy_sha256')
        require(policy is not None, 'artifact_gap')
        assert policy is not None
        config = decode(self.file(policy['configuration']), 262144, retained_strings=True)
        require(canonical(config) == canonical(dict(workspace_count=1, eligible_only=True, supported_candidates=1,
            fallbacks=False, routes=['chat.completions', 'responses'])), 'evidence_schema_gap')
        require(len(policy['workspace_fingerprints']) == 1 and policy['regional_seed_policy'] == 'each_traffic_region', 'evidence_schema_gap')
        return Cohort(1, True, policy['primary_region'])
