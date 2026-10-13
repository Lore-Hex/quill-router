"""A2 loading seam. Original row bytes survive; undefined payloads fail closed."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a2_contract as a2
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Key, Preimage, Row
from trusted_router.async_settle_shadow_v2_schema import decode, integer, require

# Decoding is complete for contract-owned shapes. Adapting the retained tool
# payload graph, with its finite I/O sandbox and approved-delta comparison, is
# a separate P5 implementation seam; a decoded envelope is not approval.
UNDEFINED_PREIMAGES = ('retained_payload_adapter_unbound',)


def load(raw: bytes) -> Bundle:
    """Decode original row and A2 payload bytes, retaining fail-closed adaptation."""
    try:
        value = decode(raw, 262144, retained_strings=True)
        require(type(value) is dict and value.get('policy_version') == 'shadow-v2'
                and value.get('contract_revision') == 'a1', 'policy_version_gap')
        require(set(value) <= {'v', 'policy_version', 'contract_revision', 'rows', 'attachments', 'proof', 'ledgers',
                              'transport', 'fleet', 'pre', 'serving', 'completed_at_us', 'days', 'preimages', 'window'}, 'artifact_gap')
        def rows(name: str) -> tuple[Row, ...]:
            values = value.get(name, [])
            require(type(values) is list and len(values) <= 10000, 'artifact_gap')
            found = []
            for row in values:
                require(type(row) is dict and set(row) == {'kind', 'id', 'raw'}
                        and all(type(row[k]) is str for k in row), 'artifact_gap')
                found.append(Row(Key(row['kind'], row['id']), row['raw'].encode('utf-8')))
            return tuple(found)
        def original(item: Any) -> bytes | None:
            require(item is None or type(item) is str, 'artifact_gap')
            return item.encode('utf-8') if item is not None else None
        ledgers = value.get('ledgers', [])
        require(type(ledgers) is list and len(ledgers) <= 4096 and all(type(x) is str for x in ledgers), 'artifact_gap')
        days = value.get('days', [])
        require(type(days) is list and len(days) <= 32 and all(type(x) is str for x in days), 'artifact_gap')
        completed = value.get('completed_at_us', 0)
        integer(completed)
        files = value.get('preimages', [])
        require(type(files) is list and len(files) <= 10000, 'artifact_gap')
        preimages = []
        payloads = {}
        for source in files:
            require(type(source) is dict and set(source) == {'name', 'raw'}
                    and type(source['name']) is str and type(source['raw']) is str, 'evidence_schema_gap')
            name, source_raw = source['name'], source['raw'].encode('utf-8')
            preimages.append(Preimage(name, source_raw))
            qualified = name.split('/', 1)[-1] if '/manifest.' in name else name
            if qualified in a2.OBJECT_PREIMAGES:
                payloads[name] = a2._object_bytes(source_raw, a2.OBJECT_PREIMAGES[qualified])
        require(len({p.name for p in preimages}) == len(preimages), 'artifact_gap')
        return Bundle(rows=rows('rows'), attachments=rows('attachments'), proof=original(value.get('proof')),
                      ledgers=tuple(x.encode('utf-8') for x in ledgers), days=tuple(days),
                      transport=original(value.get('transport')), fleet=original(value.get('fleet')),
                      pre=original(value.get('pre')), serving=original(value.get('serving')),
                      completed_at_us=completed, undefined_preimages=UNDEFINED_PREIMAGES,
                      preimages=tuple(preimages), a2_payloads=payloads, window=value.get('window'))
    except (ValueError, UnicodeError):
        return Bundle(undefined_preimages=('invalid_or_cross_version_bundle', *UNDEFINED_PREIMAGES))
