"""Reviewed output reservations and exact following-pass receipt records."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import Files, approval
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_output_costs import (
    directory_references,
    following_totals,
)
from trusted_router.async_settle_shadow_v2_pack_passes import compare_later_files
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical


@dataclass
class OutputPass:
    source: Path
    out: Path
    plan: dict[str, Any]
    missing: str | None = None

    @classmethod
    def begin(cls, source: Path, out: Path, spec: dict[str, Any]) -> OutputPass:
        plan = spec.get('file_output', {})
        result = cls(source, out, plan)
        try:
            require('later_file_plan' in plan and 'later_file_review' in plan, 'MissingLaterFilePlan')
            files = Files(source)
            approval(files, plan['later_file_review'], plan['later_file_plan'])
            reserved = retained_json(files.read(plan['later_file_plan']))
            require(bool(reserved['reservations']), 'LaterOutputPopulation')
            require(len({r['path'] for r in reserved['reservations']}) == len(reserved['reservations']), 'LaterOutputPopulation')
            require(all(type(r['bytes']) is int and r['bytes'] >= 0 and r['operations'] == 1
                and r['native_rpcs'] == 0 and r['operation'] == 'put' for r in reserved['reservations']), 'LaterOutputOperation')
            if any((source / name).is_file() for name in ('pass-result.json', 'stage/collection-result.json', 'status.json')):
                require(reserved['source_pass'] > 1, 'FollowingOutputPopulation')
            if reserved['source_pass'] > 1:
                require('previous_record' in reserved and plan.get('previous_record') == reserved['previous_record'], 'FollowingOutputPopulation')
                record = retained_json(Files(source.parent).read(reserved['previous_record']))
                following_totals(files, [record], current_pass=reserved['output_pass'])
            else:
                require('previous_record' not in reserved and 'previous_record' not in plan, 'FollowingOutputPopulation')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result.missing = str(exc)
        return result

    def finish(self, result: dict[str, Any], *, status: str) -> dict[str, Any]:
        """The receipt lives beside the pass, outside its own source commitment."""
        if self.missing:
            result = {**result, 'complete': False, 'missing': sorted(set([*result.get('missing', []), self.missing]))}
        target = self.out / status
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(canonical(result))
        if self.missing:
            return {**result, 'later_output_costs': None}
        try:
            files = PackFiles(self.out)
            references = directory_references(self.out)
            costs = compare_later_files(files, self.plan, references, plan_files=Files(self.source))
            record_path = self.out.with_name(self.out.name + '.output-receipt.json')
            require(not record_path.exists(), 'OutputRecordExists')
            record_path.write_bytes(canonical(costs))
            return {**result, 'later_output_costs': costs,
                'output_record': dict(path=record_path.name, sha256=hashlib.sha256(record_path.read_bytes()).hexdigest(), bytes=record_path.stat().st_size)}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = {**result, 'complete': False, 'missing': sorted(set([*result.get('missing', []), str(exc)]))}
            target.write_bytes(canonical(result))
            return {**result, 'later_output_costs': None}
