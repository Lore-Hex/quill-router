"""A2 loading seam. Original row bytes survive; undefined payloads fail closed."""
from __future__ import annotations

from typing import Any

from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Key, Row
from trusted_router.async_settle_shadow_v2_schema import decode, integer, require

UNDEFINED_PREIMAGES = ('independent_inventory', 'shutdown_inventory', 'cohort_seed_policy',
    'pre_budget', 'post_budget', 'external_proofs', 'resolution_dag', 'reset_support',
    'bundle_envelope', 'signature_verification_material', 'proof_root_preimages')


def load(raw: bytes) -> Bundle:
    """Decode bounded transport without making an unsupported authenticity claim.

    The temporary envelope carries ORIGINAL JSON strings, never reserialized
    row objects. Its absence of a contract schema is itself a blocking preimage.
    A2 must define its schema and bind undefined payloads before this can PASS.
    """
    try:
        value = decode(raw, 262144, retained_strings=True)
        require(type(value) is dict and value.get('policy_version') == 'shadow-v2'
                and value.get('contract_revision') == 'a1', 'policy_version_gap')
        require(set(value) <= {'v', 'policy_version', 'contract_revision', 'rows', 'attachments', 'proof', 'ledgers',
                              'transport', 'fleet', 'pre', 'serving', 'completed_at_us', 'days'}, 'artifact_gap')
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
        return Bundle(rows=rows('rows'), attachments=rows('attachments'), proof=original(value.get('proof')),
                      ledgers=tuple(x.encode('utf-8') for x in ledgers), days=tuple(days),
                      transport=original(value.get('transport')), fleet=original(value.get('fleet')),
                      pre=original(value.get('pre')), serving=original(value.get('serving')),
                      completed_at_us=completed, undefined_preimages=UNDEFINED_PREIMAGES)
    except (ValueError, UnicodeError):
        return Bundle(undefined_preimages=('invalid_or_cross_version_bundle', *UNDEFINED_PREIMAGES))
