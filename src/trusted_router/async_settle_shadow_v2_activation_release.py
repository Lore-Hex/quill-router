"""Prepare reviewed pin diffs and compare existing release workflow records."""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_activation_plan import (
    ATTACHMENT_BUCKET,
    Plan,
    contained_path,
)

MONEY_OFF = dict(TR_ASYNC_SETTLE_ENABLED='false', TR_ASYNC_SETTLE_PROTECTION='false',
                 TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED='false')


# Mirrors trusted_router.config.SHADOW_CANARY_MAX_SECONDS (v2 modules do not
# import config; a test pins the two equal). The router treats a farther
# expiry as expired, so the tool refuses to write one.
CANARY_MAX_SECONDS = 14 * 86400


def canary_expiry(expires_at: str, now: float | None = None) -> str:
    """Opt-in requires a future UTC expiry the router will accept.

    Deployed routers treat pins without a valid expiry as expired, so an
    opt-in without one would be a silent no-op.
    """
    import time
    from datetime import datetime

    if not expires_at:
        raise ValueError('shadow canary expiry required for opt-in')
    if not expires_at.endswith('Z'):
        raise ValueError('shadow canary expiry must be a UTC instant ending in Z')
    try:
        expiry = datetime.fromisoformat(expires_at[:-1] + '+00:00').timestamp()
    except ValueError as error:
        raise ValueError('shadow canary expiry must be an ISO-8601 instant') from error
    current = time.time() if now is None else now
    if expiry <= current:
        raise ValueError('shadow canary expiry already passed')
    if expiry - current > CANARY_MAX_SECONDS:
        raise ValueError('shadow canary expiry exceeds the canary maximum of 14 days')
    return expires_at


def pins(plan: Plan, phase: str, *, expires_at: str = '') -> dict[str, dict[str, str]]:
    if phase not in ('shadow-opt-in', 'shadow-opt-out'):
        raise ValueError('release phase required')
    active = phase == 'shadow-opt-in'
    return dict(router={**MONEY_OFF,
        'TR_ASYNC_SETTLE_SHADOW_POLICY': 'shadow-v2' if active else '',
        # The canary's runtime stop; opt-out clears it with the other pins.
        'TR_ASYNC_SETTLE_SHADOW_EXPIRES_AT': canary_expiry(expires_at) if active else '',
        'TR_ASYNC_SETTLE_SHADOW_WORKSPACES': plan.workspace if active else '',
        'TR_ASYNC_SETTLE_SHADOW_HEALTH_REGION': plan.schedule.primary if active else '',
        # Keep the export destination through close/export/retention on opt-out.
        'TR_ASYNC_SETTLE_SHADOW_ATTACHMENT_BUCKET': ATTACHMENT_BUCKET},
        enclave=dict(TR_ASYNC_SETTLE_SHADOW='on' if active else 'off', TR_ASYNC_SETTLE_NEGOTIATE='off'))


def patch(original: str, path: str, desired: dict[str, str]) -> str:
    changed = original
    for name, value in desired.items():
        pattern = rf'(?m)^(\s*[\"\']){name}=([^\"\'\n]*)([\"\']\s*)$'
        hits = list(re.finditer(pattern, changed))
        if name in ('TR_ASYNC_SETTLE_SHADOW', 'TR_ASYNC_SETTLE_NEGOTIATE'):
            # Existing GCP deployment metadata uses the pipe-delimited tee-env form.
            hits += list(re.finditer(rf'(\|tee-env-{name}=)(on|off)(?=\|)', changed))
        if len(hits) != 1:
            raise ValueError('pin source shape differs: ' + name)
        if name in MONEY_OFF and hits[0][2] != MONEY_OFF[name]:
            raise ValueError('money-off source pin differs')
        match = hits[0]
        changed = changed[:match.start(2)] + value + changed[match.end(2):]
    return ''.join(difflib.unified_diff(original.splitlines(keepends=True), changed.splitlines(keepends=True),
                                      fromfile='a/' + path, tofile='b/' + path))


def prepare(plan: Plan, phase: str, router_root: Path, enclave_root: Path | None,
            out: Path, *, expires_at: str = '') -> dict[str, Any]:
    desired = pins(plan, phase, expires_at=expires_at)
    missing = []
    for role, root in (('router', router_root), ('enclave', enclave_root)):
        release = getattr(plan.releases, role)
        source = contained_path(root, release.pin_file) if root is not None else None
        if source is None or not source.is_file():
            missing.append('P6EnclavePinSource' if role == 'enclave' else 'P6RouterPinSource')
            continue
        raw = source.read_text()
        contained_path(out, role + '.patch').write_text(patch(raw, release.pin_file, desired[role]))
    return dict(pins=desired, missing=missing)


def workflow_record(plan: Plan, role: str, workflow: str, run: dict[str, Any],
                    jobs: list[dict[str, Any]], expected_jobs: list[str]) -> None:
    release = getattr(plan.releases, role)
    if (run['head_sha'] != release.sha or run['status'] != 'completed' or run['conclusion'] != 'success'
            or run['event'] not in ('push', 'workflow_run', 'workflow_dispatch')
            or run['head_branch'] != 'main'
            or run['path'].split('@')[0] != '.github/workflows/' + workflow
            or run['repository']['full_name'] != release.repository):
        raise ValueError('released workflow differs or incomplete')
    for name in expected_jobs:
        matches = [j for j in jobs if j['name'] == name]
        if len(matches) != 1:
            raise ValueError('required workflow job missing or repeated')
        job = matches[0]
        if (job['run_id'] != run['id'] or job['head_sha'] != release.sha or job['status'] != 'completed'
                or job['conclusion'] != 'success' or not job['completed_at']):
            raise ValueError('required workflow job incomplete')
