"""Daily review subjects with the complete stage-specific procedure scope."""
from __future__ import annotations

from typing import Any

from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, review_subjects
from trusted_router.async_settle_shadow_v2_pack_procedures import REQUIRED_STAGES, check_scope
from trusted_router.async_settle_shadow_v2_schema import day as parse_day
from trusted_router.async_settle_shadow_v2_schema import require


def review_days(files: PackFiles, *, days: list[str], subjects: dict[str, dict[str, Any]],
                criteria: dict[str, list[str]], required_stages: dict[str, list[str]],
                verifiers: dict[str, Any], window: bool = False) -> dict[str, Any]:
    require(set(subjects) == set(days) and len(days) == len(set(days)), 'ReleaseReviewAssembly')
    require(all(set(names) <= set(required_stages.get(stage, []))
                for stage, names in REQUIRED_STAGES.items()), 'artifact_gap')
    expected: dict[str, set[str]] = {}
    scoped = dict(criteria)
    for stage, names in required_stages.items():
        require(len(names) == len(set(names)), 'artifact_gap')
        for name in names:
            checks = check_scope(name, stage)
            expected.setdefault(name, set()).update(checks)
            scoped[stage + '.' + name] = list(checks)
    require(all(set(criteria.get(name, [])) == checks for name, checks in expected.items()), 'artifact_gap')
    for day in days:
        parse_day(day)
        value = retained_json(files.read(subjects[day]))
        require(value.get('day') == day, 'ReleaseReviewAssembly')
        files.obj('review/days/' + day + '.json', dict(day=day, subject=subjects[day], state='awaiting_review'))
    return review_subjects(files, subjects, criteria=scoped, required_stages=required_stages, verifiers=verifiers, window=window)
