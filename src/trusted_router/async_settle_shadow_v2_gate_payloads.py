"""Materialize A2 payload dependencies from their original bounded file bytes.

This establishes schema and byte closure, not release approval or retained-tool
semantics. The loader keeps the retained adapter seam closed independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_gate_inputs import (
    Boot,
    Cohort,
    Inventory,
    Key,
    Ports,
    Row,
    Shutdown,
)
from trusted_router.async_settle_shadow_v2_gate_loading import (
    LIMITS,
    LoadingIssue,
    PayloadError,
    error_gap,
    retained_json,
)
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
    completed_at_us: int | None = None
    streams: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    decoded: dict[str, Any] = field(default_factory=dict)
    _pages: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    _bad_pages: dict[tuple[str, str], PayloadError] = field(default_factory=dict)
    _containers: dict[int, list[tuple[str, str, dict[str, Any] | None, str | None]]] = field(default_factory=dict)
    _indexed: set[str] = field(default_factory=set)
    _lookups: dict[bytes, Any] = field(default_factory=dict)
    issues: list[LoadingIssue] = field(default_factory=list)
    limits: dict[str, dict[str, Any]] = field(default_factory=dict)
    shutdown: Shutdown | None = None
    ports: Ports | None = None

    def reject(self, path: str, exc: Exception) -> None:
        if isinstance(exc, PayloadError):
            path = exc.path
        self.issues.append(LoadingIssue(error_gap(exc), path, str(exc)))

    def file(self, reference: dict[str, Any]) -> bytes:
        a2.FILE_REF(reference)
        raw = self.files.get(reference['path'])
        if raw is None or len(raw) != reference['bytes'] or roots.file_digest(raw) != reference['sha256']:
            raise PayloadError(reference['path'], 'artifact_gap')
        return raw

    def object(self, reference: dict[str, Any], schema: Schema) -> dict[str, Any]:
        raw = self.file(reference)
        try:
            value = contract._object_bytes(raw, schema)
        except ValueError as exc:
            raise PayloadError(reference['path'], error_gap(exc)) from exc
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
                if self.completed_at_us is not None:
                    require(review['reviewed_at_us'] <= self.completed_at_us, 'evidence_schema_gap')
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
            if id(schema) not in self._containers:
                containers: list[tuple[str, str, dict[str, Any] | None, str | None]] = []
                for path, raw in self.files.items():
                    # Lookup only. Most dependencies are source/fixture files,
                    # not page containers. Reject them before the much more
                    # expensive contract traversal; accepted pages still go
                    # through the contract on their original bytes below.
                    if raw not in self._lookups:
                        try:
                            self._lookups[raw] = retained_json(raw)
                        except ValueError:
                            self._lookups[raw] = None
                    lookup = self._lookups[raw]
                    if type(lookup) is not dict or 'entries' not in lookup:
                        continue
                    try:
                        value = contract._object_bytes(raw, schema)
                    except ValueError as exc:
                        try:
                            # Lookup only: a committed but invalid page must be
                            # a schema failure, not misreported as missing bytes.
                            rejected = retained_json(raw)
                            if type(rejected) is dict and 'entries' in rejected:
                                containers.append((path, sha256(rejected), None, error_gap(exc)))
                        except ValueError:
                            pass
                    else:
                        containers.append((path, sha256(value), value, None))
                self._containers[id(schema)] = containers
            for path, digest, candidate, error in self._containers[id(schema)]:
                if candidate is None:
                    self._bad_pages[stream, digest] = PayloadError(path, error or 'evidence_schema_gap')
                    continue
                try:
                    for entry in candidate['entries']:
                        roots.STREAMS[stream][0](entry)
                except ValueError as exc:
                    self._bad_pages[stream, digest] = PayloadError(path, error_gap(exc))
                    continue
                self._pages[stream, digest] = candidate
            self._indexed.add(stream)
        tail = reference['tail_sha256']
        pages: list[dict[str, Any]] = []
        visited: set[str] = set()
        while tail is not None:
            require(tail not in visited and len(pages) < reference['pages'], 'evidence_schema_gap')
            visited.add(tail)
            page = self._pages.get((stream, tail))
            if page is None and (stream, tail) in self._bad_pages:
                raise self._bad_pages[stream, tail]
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

    def approved_subject(self, approval: dict[str, Any], reference: dict[str, Any]) -> None:
        self.references(approval)
        self.file(reference)
        require(approval['subject_sha256'] == reference['sha256'], 'artifact_gap')

    def delegated(self, value: dict[str, Any]) -> None:
        a2.DELEGATED_PAYLOAD(value)
        self.references(value)
        self.detail(value['dependencies'], 'files')
        # Decode the untouched v1 payload, retaining binary64 values. Semantic
        # validation is performed by the named retained callable, never A2.
        self.decoded[value['payload']['path']] = retained_json(self.file(value['payload']))

    def materialize(self, payloads: dict[str, dict[str, Any]]) -> None:
        for path, value in payloads.items():
            try:
                self.materialize_one(path, value)
            except (ValueError, KeyError, TypeError) as exc:
                self.reject(path, exc)
        pre = payloads.get('proof.pre_budget_sha256')
        post = payloads.get('proof.post_budget_sha256')
        if pre is not None and post is not None and post['pre_envelope_sha256'] != sha256(pre):
            self.reject('proof.post_budget_sha256', ValueError('artifact_gap'))
        if set(self.limits) == {'pre', 'post'} and self.limits['pre'] != self.limits['post']:
            self.reject('proof.post_budget_sha256', ValueError('evidence_schema_gap'))

    def materialize_one(self, path: str, value: dict[str, Any]) -> None:
        self.references(value)
        qualified = path.split('/', 1)[-1] if '/manifest.' in path else path
        if qualified == 'proof.shutdown_inventory_sha256':
            require(value['complete'], 'artifact_gap')
            self.approved_subject(value['approval'], value['measurements'])
            writers = self.detail(value['writers'], 'shutdown_writers')
            measured_inventory = retained_json(self.file(value['measurements']))
            require(measured_inventory == [entry['measurement'] for entry in writers], 'evidence_schema_gap')
            measurements: list[dict[str, Any]] = []
            aliases: list[Row] = []
            for entry in writers:
                measurement = self.object(entry['measurement'], a2.SHUTDOWN_MEASUREMENT)
                require(all(measurement[key] == entry[key] for key in ('day', 'boot')), 'evidence_schema_gap')
                require(not entry['termination_applies'] or entry['grace_us'] == measurement['grace_us'], 'evidence_schema_gap')
                require(day_at(entry['interval_end_us'] - 1) == entry['day'], 'evidence_schema_gap')
                require((entry['reason'] == 'hard_kill') == (measurement['outcome'] == 'hard_kill'), 'evidence_schema_gap')
                if self.ports is not None:
                    from trusted_router.async_settle_shadow_v2_gate_inputs import LoadingPorts
                    from trusted_router.async_settle_shadow_v2_gate_release import approved_file
                    approved_file(self, value['approval']['record'])
                    approved_file(self, measurement['source'])
                    parsers = self.ports.measurements if isinstance(self.ports, LoadingPorts) else {}
                    parser = parsers.get(measurement['source']['path'])
                    require(parser is not None, 'evidence_schema_gap')
                    assert parser is not None
                    original = parser(self.file(measurement['source']))
                    require(all(original.get(k) == v for k, v in measurement.items() if k not in ('v', 'policy_version', 'contract_revision', 'source')), 'evidence_schema_gap')
                measurements.append(measurement)
                aliases.extend((Row(Key('shutdown_entry', 'shutdown/' + entry['day'] + '/' + entry['boot']), a2.SHUTDOWN_ENTRY.serialize(entry)),
                    Row(Key('shutdown_measurement', entry['measurement']['path']), self.file(entry['measurement']))))
                for field_name, schema in (('original_close', a1.CLOSURE), ('close_result', a1.CLOSE_RESULT)):
                    if entry[field_name] is not None:
                        close = self.object(entry[field_name], schema)
                        require(all(close[k] == entry[k] for k in ('day', 'boot')), 'evidence_schema_gap')
            self.shutdown = Shutdown(tuple(writers), tuple(measurements), tuple(aliases))
        elif qualified in ('proof.pre_budget_sha256', 'proof.post_budget_sha256'):
            require(value['stage'] == ('pre' if qualified == 'proof.pre_budget_sha256' else 'post'), 'evidence_schema_gap')
            self.approved_subject(value['approval'], value['retained']['payload'])
            self.delegated(value['retained'])
            retained_json(self.file(value['output']))
            load = value['v2_load']
            self.approved_subject(load['approval'], load['limits'])
            limits = self.object(load['limits'], LIMITS)
            self.limits[value['stage']] = limits
            segments = self.detail(load['segments'], 'segments')
            ends: dict[tuple[str, str], int] = {}
            for segment in segments:
                require(segment['day'] == day_at(segment['start_us']), 'evidence_schema_gap')
                require(segment['start_us'] == segment['end_us'] or segment['day'] == day_at(segment['end_us'] - 1), 'evidence_schema_gap')
                require(load['interval']['start_us'] <= segment['start_us'] <= segment['end_us'] <= load['interval']['end_us'], 'evidence_schema_gap')
                key = (segment['day'], segment['boot'])
                require(ends.get(key, 0) <= segment['start_us'], 'evidence_schema_gap')
                ends[key] = segment['end_us']
            for operation in self.detail(load['operations'], 'operations'):
                require(load['interval']['start_us'] <= operation['at_us'] < load['interval']['end_us'], 'evidence_schema_gap')
                require(operation['bytes'] == (0 if operation['operation'] == 'delete' else operation['object']['bytes']), 'evidence_schema_gap')
        elif qualified == 'proof.resolution_dag_sha256':
            self.delegated(value['retained'])
            for reset in self.detail(value['history'], 'reset_history'):
                self.detail(reset['support'], 'files')
        elif qualified == 'proof.external_proofs_sha256':
            for requirement in value['requirements']:
                self.approved_subject(requirement['approval'], requirement['evidence'])
                proof = self.object(requirement['evidence'], a2.PROOF_RESULT)
                require(proof['criterion'] == requirement['name'], 'evidence_schema_gap')
                require(all(proof[key] == requirement[key] for key in ('router_revision', 'enclave_revision')), 'evidence_schema_gap')
                self.detail(proof['sources'], 'files')
            if value['pilot'] is not None:
                self.delegated(value['pilot'])
        elif qualified == 'manifest.independent_inventory_sha256':
            # The retained roster parser is the next adapter boundary. Do
            # not accept a manifest-shaped stand-in as a validated roster.
            self.approved_subject(value['approval'], value['deployment'])
            serving = retained_json(self.file(value['serving']))
            require(type(serving) is list, 'evidence_schema_gap')
        elif qualified == 'proof.cohort_seed_policy_sha256':
            self.approved_subject(value['approval'], value['configuration'])

    def inventory(self, payloads: dict[str, dict[str, Any]], ports: Ports | None) -> Inventory:
        require(ports is not None and ports.roster_coverage is not None, 'artifact_gap')
        assert ports is not None and ports.roster_coverage is not None
        lifetimes: dict[tuple[str, str, str, str], set[tuple[int, int]]] = {}
        for key, envelope in payloads.items():
            if key != 'manifest.independent_inventory_sha256' and not key.endswith('/manifest.independent_inventory_sha256'):
                continue
            expected_day = key.split('/')[0] if '/' in key else envelope['day']
            require(envelope['complete'] and envelope['day'] == expected_day, 'evidence_schema_gap')
            serving = decode(self.file(envelope['serving']), 262144, retained_strings=True)
            deployment = decode(self.file(envelope['deployment']), 262144, retained_strings=True)
            require(type(serving) is list and type(deployment) is dict and type(deployment.get('serving')) is list, 'evidence_schema_gap')
            measured = ports.roster_coverage(serving)
            require(measured == ports.roster_coverage(deployment['serving']), 'evidence_schema_gap')
            from trusted_router.async_settle_shadow_v2_gate_inputs import LoadingPorts
            if isinstance(ports, LoadingPorts):
                require(ports.gate_roster is not None, 'artifact_gap')
                assert ports.gate_roster is not None
                require(ports.gate_roster(serving, deployment['serving']), 'evidence_schema_gap')
            require({row['instance']: row['pins'] for row in serving}
                    == {row['instance']: row['pins'] for row in deployment['serving']}, 'evidence_schema_gap')
            for row in serving:
                pins = row['pins']
                expected_pins = (dict(TR_ASYNC_SETTLE_ENABLED='false', TR_ASYNC_SETTLE_PROTECTION='false', TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED='false') if row['role'] == 'router' else dict(TR_ASYNC_SETTLE_NEGOTIATE='off', TR_ASYNC_SETTLE_SHADOW='on'))
                require(all(pins.get(k) == v for k, v in expected_pins.items()), 'evidence_schema_gap')
            for boot, role, rev, region, date, start, end in measured:
                identity(boot)
                a2.ROLE(role)
                revision(rev)
                name(region)
                day(date)
                require(date == expected_day == day_at(start) == day_at(end - 1), 'evidence_schema_gap')
                require(0 < start < end, 'evidence_schema_gap')
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
