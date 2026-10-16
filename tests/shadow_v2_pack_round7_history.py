"""A nonempty retained history with one independently supplied original."""
from __future__ import annotations

from pathlib import Path

from tests.test_async_settle_shadow_r11_validity import signed_observation
from tests.test_async_settle_shadow_v2_pack import START, review
from trusted_router.async_settle_shadow_evidence import CONTROL, SAMPLE


def nonempty_history(files):
    at = START - 86400000000 + 1000000
    _, _, observed = signed_observation(at, phase='settle', charge=999)
    revision = observed['deployment']['router_revision']
    fixed = 'b' * 40 if revision != 'b' * 40 else 'c' * 40
    original = files.obj('retained-original/resolution', dict(revision=revision, fixed_revision=fixed, at_us=at))
    resolution = dict(at_us=at, revision=revision, fixed_revision=fixed, serving_since_us=START,
        artifact_sha256=original['sha256'])
    rows = [dict(kind=SAMPLE, id=observed['authorization_day'] + '/' + observed['authorization_id'], body=observed)]
    manifest = dict(v=1, day='2026-10-12', instance_boot_ids=[], router_revisions=[fixed], go_revisions=[],
        configuration_sha256='c' * 64, admission_disabled_from_us=START,
        admission_disabled_until_us=START + 86400000000, first_evidence_at_us=None,
        completeness='complete', gap_intervals=[], proof_manifest_sha256=None)
    from tests.test_async_settle_shadow_accounting import Database
    from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
    database = Database()
    EvidenceStore(database).publish_manifest(manifest, __import__('time').monotonic() + 10)
    import json
    rows.append(dict(kind=CONTROL, id='2026-10-12/manifest-v1',
                     body=json.loads(database.rows[CONTROL, '2026-10-12/manifest-v1'])))
    payload = files.obj('retained-original/payload', dict(rows=rows, days=['2026-10-12'],
        proof=dict(resolved_mismatches=[resolution]), resets=[], serving=[[START, START + 86400000000, fixed]], manifests={}))
    reviewed = files.obj('retained-original/reviewed', dict(rows=rows, resets=[], resolutions=[resolution]))
    source = files.put('retained-original/source', Path('scripts/async_settle/shadow_report.py').read_bytes())
    arguments = dict(payload=payload, source=source, dependencies=[original], reviewed_history=reviewed,
        approvals=[review(files, reviewed, 'retained-original/review'),
                   review(files, original, 'retained-original/resolution-review')])
    return arguments, original
