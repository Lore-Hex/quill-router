"""Expected basis references and independently backed terminal selection."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
from trusted_router import async_settle_shadow_v2_contract as decision_contract
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_schema import day_at, require, sha256
from trusted_router.detached_jws import canonical


@dataclass(frozen=True)
class BasisReferenceIndex:
    keys: tuple[str, ...]
    digests: tuple[str, ...]
    missing: tuple[str, ...]

    @classmethod
    def build(cls, *, receipts: Iterable[dict[str, Any]], samples: Iterable[dict[str, Any]],
              readers: Iterable[dict[str, Any]], batches: Iterable[tuple[str, dict[str, Any]]]) -> BasisReferenceIndex:
        expected: dict[str, set[tuple[str, str, int | None]]] = {}
        slots: set[tuple[str, str, int]] = set()
        receipt_predictions: dict[tuple[str, str, str], dict[str, Any]] = {}
        missing: list[str] = []
        for page in receipts:
            p1.PAGE(page)
            for event in page['events']:
                prediction = event.get('prediction')
                if prediction is not None:
                    coordinate = (page['day'], page['boot'], event['seq'])
                    require(coordinate not in receipt_predictions, 'artifact_gap')
                    receipt_predictions[coordinate] = prediction
                    expected.setdefault(prediction['basis_sha256'], set()).add(
                        (page['day'], page['boot'], prediction['health_slot']))
        for sample in samples:
            p1.SAMPLE(sample)
            locator = sample['original_terminal_receipt']
            coordinate = (locator['day'], locator['boot'], locator['event_seq'])
            prediction = receipt_predictions.get(coordinate)
            if prediction is None:
                missing.append('sample_receipt:' + '/'.join(coordinate))
                continue
            require(sample['deployment']['instance'] == locator['boot'] and
                    sample['health_slot'] == prediction['health_slot'], 'health_source_gap')
            if sample['health_slot'] is not None:
                slots.add((locator['day'], locator['boot'], sample['health_slot']))
        # Reader capture format records the original basis digest and page key;
        # it is input from the reader, never manufactured from the batch listing.
        named_pages = set()
        page_demands: dict[str, set[str]] = {}
        for reader in readers:
            expected.setdefault(reader['basis_sha256'], set()).add(
                (reader['day'], reader['boot'], reader['health_slot']))
            named_pages.add(reader['page'])
            page_demands.setdefault(reader['page'], set()).add(reader['basis_sha256'])
        available = {}
        for key, page in batches:
            health.HEALTH_BASIS_BATCH(page)
            require(key == f"health-basis-batches/{page['day']}/{page['boot']}/{page['sequence']}", 'artifact_gap')
            require(key not in available, 'artifact_gap')
            available[key] = page
        found = set()
        found_slots = set()
        selected = set(named_pages)
        for key, page in available.items():
            used = key in named_pages
            missing.extend('page_basis:' + key + ':' + digest for digest in
                page_demands.get(key, set()) - {sha256(b) for b in page['bases']})
            for basis in page['bases']:
                digest = sha256(basis)
                slot = basis['body']['slot'] if basis['body'] is not None else None
                identity = (page['day'], basis['reader_boot'], slot)
                if digest in expected:
                    require(identity in expected[digest], 'health_source_gap')
                    found.add(digest)
                    used = True
                if identity in slots and digest in expected:
                    found_slots.add(identity)
                    used = True
                if digest not in expected:
                    missing.append('unreferenced_basis:' + digest)
            if used:
                selected.add(key)
        # Exact predecessor links are references too. Walk only from demanded
        # pages; an unrelated populated chain cannot create its own expectation.
        hashes = {sha256(p): k for k, p in available.items()}
        pending = list(selected)
        while pending:
            key = pending.pop()
            if key not in available:
                missing.append('page:' + key)
                continue
            previous = available[key]['previous_sha256']
            if previous is not None:
                predecessor = hashes.get(previous)
                if predecessor is None:
                    missing.append('previous:' + previous)
                else:
                    current, old = available[key], available[predecessor]
                    require((current['day'], current['boot'], int(current['sequence']) - 1) ==
                            (old['day'], old['boot'], int(old['sequence'])), 'artifact_gap')
                    if predecessor not in selected:
                        selected.add(predecessor)
                        pending.append(predecessor)
        missing.extend('basis:' + digest for digest in set(expected) - found)
        missing.extend('slot:' + '/'.join(map(str, item)) for item in slots - found_slots)
        missing.extend('unreferenced_page:' + key for key in set(available) - selected)
        return cls(tuple(sorted(selected)), tuple(sorted(expected)), tuple(sorted(set(missing))))


class TerminalReconciliation:
    """Index originals before invoking the frozen contextual terminal join."""

    def __init__(self, pages: Iterable[dict[str, Any]], samples: Iterable[dict[str, Any]],
                 *, spool: Path | None = None):
        from trusted_router.async_settle_shadow_v2_pack_records import RecordGroups, RecordMap
        self.scratch: Any = None
        self.day_status: dict[str, bool] = {}
        self.events: Any = {} if spool is None else RecordMap(spool / 'events')
        self.terminals: Any = {} if spool is None else RecordGroups(spool / 'terminals')
        self.samples: Any = {} if spool is None else RecordGroups(spool / 'samples')
        def append(groups: Any, key: Any, value: Any) -> None:
            if spool is None:
                groups.setdefault(key, []).append(value)
            else:
                groups.append(key, value)
        for page in pages:
            p1.PAGE(page)
            for event in page['events']:
                key = page['day'], page['boot'], event['seq']
                require(key not in self.events, 'authorization_terminal_conflict')
                self.events[key] = page, event
                if event['phase'] in ('settle', 'refund'):
                    append(self.terminals, sha256(event['identity']), (page, event))
        for sample in samples:
            p1.SAMPLE(sample)
            loc = sample['original_terminal_receipt']
            append(self.samples, (loc['day'], loc['boot'], loc['event_seq']), sample)

    def original(self, reference: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
        found = self.events.get((reference['day'], reference['boot'], reference['event_seq']))
        return None if found is None else (found[0], found[1]['seq'])

    def terminal(self, obligation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        origin = obligation['original_receipt']
        found = self.events.get((origin['day'], origin['boot'], origin['event_seq']))
        require(found is not None, 'authorization_link_gap')
        assert found is not None
        issue_page, issuance = found
        if issuance['phase'] == 'replay':
            ref = issuance['retry_original']
            found = self.events.get((ref['day'], ref['boot'], ref['event_seq']))
            require(found is not None and found[1]['phase'] == 'authorize', 'authorization_link_gap')
            assert found is not None
            issuance = found[1]
        candidates = self.terminals.get(obligation['identity_sha256'], [])
        if not candidates:
            return None
        # Retries remain independent attempts; choose the unique original and
        # require every retry's original coordinates to resolve to that original.
        require(issuance['facts'] is not None, 'authorization_link_gap')
        decision = decision_contract.decide(decision_contract.RequestedFacts.from_object(issuance['facts']),
            issuance['decision']['reason'], canonical(issuance['candidates']) if issuance['candidates'] is not None else None,
            complete_candidates=issuance['candidates'] is not None)
        require(decision.eligibility == issuance['eligibility'] == 'eligible' and all(
            getattr(decision, k) == issuance['decision'][k] for k in
            ('reason', 'facts_sha256', 'candidate_set_sha256')), 'authorization_link_gap')
        originals = [(p, e) for p, e in candidates if e['retry_original'] is None]
        require(len(originals) == 1, 'authorization_terminal_conflict')
        page, event = originals[0]
        for _, retry in candidates:
            require(retry['partition'] == 'A' and retry['comparison'] == event['comparison']
                    and retry['payload_sha256'] == event['payload_sha256']
                    and retry['booking'] == event['booking'], 'authorization_terminal_conflict')
            if retry['retry_original'] is not None:
                require(retry['retry_original'] == dict(day=page['day'], boot=page['boot'], event_seq=event['seq']),
                        'authorization_link_gap')
        key = page['day'], page['boot'], event['seq']
        samples = self.samples.get(key, [])
        require(len(samples) == 1, 'eligible_coverage_gap')
        sample = samples[0]
        require(event['partition'] == 'A' and event['proof_kind'] == sample['proof_kind'] == 'compared',
                'authorization_terminal_conflict')
        require(sample['classification'] == event['comparison'] and sample['eligibility']['observed'] is True
                and sample['provenance']['binding_verified'] is True
                and sample['provenance']['raw_matches_body'] is True, 'eligible_coverage_gap')
        require(sample['authorization_day'] == event['authorization_day'] == issuance['authorization_day']
                and sample['authorization_id'] == event['authorization_id'] == issuance['authorization_id']
                and sample['deployment']['instance'] == page['boot']
                and sample['deployment']['router_revision'] == event['router_revision']
                and sample['payload_hash'] == event['payload_sha256']
                and sample['booking']['attempted_kind'] == event['phase']
                and sample['booking']['outcome'] == event['booking']['outcome']
                and event['identity'] == issuance['identity'] and event['decision'] == issuance['decision'],
                'authorization_terminal_conflict')
        args = {}
        if (found[0]['day'], found[0]['boot'], found[1]['seq']) != (issue_page['day'], issue_page['boot'], origin['event_seq']):
            args = dict(original_page=found[0], original_event_seq=found[1]['seq'])
        locator = joins.event_locator(page, event['seq'])
        joins.validate_terminal_join(obligation, issue_page, page, locator, **args)
        return page, locator

    def support_days(self, selected: list[str]) -> tuple[str, ...]:
        days = set(selected)
        for (day, _, _), (page, event) in self.events.items():
            if day not in selected or event['phase'] not in ('authorize', 'replay'):
                continue
            args = {}
            if event['phase'] == 'replay':
                found = self.original(event['retry_original'])
                require(found is not None, 'authorization_link_gap')
                assert found is not None
                args = dict(original_page=found[0], original_event_seq=found[1])
                days.add(found[0]['day'])
            obligation = joins.authorization_obligation(page, event['seq'], **args)
            first = event['event_at_us'] // 86400000000
            last = (obligation['horizon_at_us'] - 1) // 86400000000
            days.update(day_at(d * 86400000000) for d in range(first, last + 1))
            terminal = self.terminal(obligation)
            if terminal is not None:
                days.add(terminal[0]['day'])
        return tuple(sorted(days))


    def required_through(self, selected: list[str]) -> dict[str, int]:
        """Keep the original issue horizon when comparing support-day completion."""
        required: dict[str, int] = {}
        for (day, _, _), (page, event) in self.events.items():
            if day not in selected or event['phase'] not in ('authorize', 'replay'):
                continue
            args = {}
            if event['phase'] == 'replay':
                found = self.original(event['retry_original'])
                require(found is not None, 'authorization_link_gap')
                assert found is not None
                args = dict(original_page=found[0], original_event_seq=found[1])
            obligation = joins.authorization_obligation(page, event['seq'], **args)
            horizon = obligation['horizon_at_us']
            for number in range(event['event_at_us'] // 86400000000, (horizon - 1) // 86400000000 + 1):
                key = day_at(number * 86400000000)
                required[key] = max(required.get(key, 0), min(horizon, (number + 1) * 86400000000))
        return required
