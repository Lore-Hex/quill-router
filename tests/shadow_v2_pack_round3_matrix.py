"""Gate ports bound to a composed bundle's own release plan."""
from __future__ import annotations


def stage_ports(original):
    from scripts.async_settle.shadow_report import shadow_v2_ports
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_release import decode_release_plan
    raw = next(p['raw'].encode() for p in original['preimages'] if p['name'] == 'loading/release-plan.json')
    release = decode_release_plan(raw, file_digest(raw))
    return shadow_v2_ports(b'[]', b'[]', release=release)
