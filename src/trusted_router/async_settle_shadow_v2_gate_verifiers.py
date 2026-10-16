"""Offline callable approvals and auditable calls; pure, with no binding import."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import TrustedKey, canonical

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context
    from trusted_router.async_settle_shadow_v2_gate_inputs import VerifierApproval


def approval(ctx: Context, name: str) -> VerifierApproval:
    require(ctx.ports is not None, 'artifact_gap')
    assert ctx.ports is not None
    function = getattr(ctx.ports, 'verify_' + name, None)
    require(callable(function), 'artifact_gap')
    assert function is not None
    records = [v for v in ctx.ports.approvals if v.entry_point.endswith('.verify_' + name)]
    require(len(records) == 1, 'artifact_gap')
    approved = records[0]
    require(function is approved.function, 'evidence_schema_gap')
    require(approved.entry_point == function.__module__ + '.' + function.__name__, 'evidence_schema_gap')
    require(approved.source != b'' and approved.fixture != b'', 'artifact_gap')
    require(file_digest(approved.source) == approved.source_pin and file_digest(approved.fixture) == approved.fixture_pin, 'artifact_gap')
    require(approved.keyset == canonical([asdict(k) for k in approved.keys]), 'evidence_schema_gap')
    release_approval(ctx, approved)
    return approved


def release_approval(ctx: Context, approved: VerifierApproval) -> None:
    # Legacy typed unit inputs predate the original-byte loading boundary.
    # Every loaded certification run must establish independent release pins.
    if 'loading_issues' not in ctx.bundle.a2_streams:
        return
    from trusted_router.async_settle_shadow_v2_gate_inputs import LoadingPorts
    require(isinstance(ctx.ports, LoadingPorts) and ctx.ports.release is not None, 'artifact_gap')
    assert isinstance(ctx.ports, LoadingPorts) and ctx.ports.release is not None
    release = ctx.ports.release
    files = {p.name: p.raw for p in ctx.bundle.preimages}
    require(files.get(release.path) == release.raw, 'artifact_gap')
    expected = release.verifiers.get(approved.entry_point)
    actual = (file_digest(approved.source), file_digest(approved.fixture),
              file_digest(approved.keyset) if approved.keys else None)
    require(expected == actual, 'artifact_gap')
    cache_key = (tuple(sorted(release.artifacts.items())), ctx.bundle.preimages)
    if ctx.release_artifacts_cache != cache_key:
        for path, digest in release.artifacts.items():
            require(path in files and file_digest(files[path]) == digest, 'artifact_gap')
        ctx.release_artifacts_cache = cache_key


def check_ports(ctx: Context) -> None:
    for name in ('certificate', 'binding'):
        try:
            approval(ctx, name)
        except ValueError as exc:
            ctx.issue(str(exc), 'unbound or substituted ' + name + ' verifier')


def invoke(ctx: Context, name: str, token: str, keys: Sequence[TrustedKey], *args: Any, **kwargs: Any) -> Any:
    approved = approval(ctx, name)
    require(tuple(keys) == approved.keys, 'evidence_schema_gap')
    ctx.verifier_calls[name] = ctx.verifier_calls.get(name, 0) + 1
    function: Callable[..., Any] = approved.function
    return function(token, keys, *args, **kwargs)


def records(ctx: Context) -> dict[str, Any]:
    result: dict[str, Any] = dict(certificate=None, binding=None, retained_tools=[])
    for name in ('certificate', 'binding'):
        try:
            approved = approval(ctx, name)
        except ValueError:
            continue
        result[name] = dict(entry_point=approved.entry_point, source_sha256=file_digest(approved.source),
            fixture_sha256=file_digest(approved.fixture), keyset_sha256=file_digest(approved.keyset),
            calls=ctx.verifier_calls.get(name, 0))
    if ctx.ports is not None:
        for approved in ctx.ports.approvals:
            if approved.entry_point.endswith(('.verify_certificate', '.verify_binding')):
                continue
            # Report retained entry points and the predicate adapter. The runner,
            # view builders and nested report helper are approved transitive
            # dependencies, not additional retained entry points.
            if 'loading_issues' in ctx.bundle.a2_streams and not (
                    any(approved.entry_point.endswith('.' + entry) for entry in a2.RETAINED_ENTRY_POINTS)
                    or approved.entry_point.endswith('.unchanged_checks')):
                continue
            loaded_calls = sum(v['calls'] for v in ctx.bundle.a2_streams.get('retained_calls', []) if v['entry_point'] == approved.entry_point)
            calls = ctx.verifier_calls.get(approved.entry_point, 0) + loaded_calls
            if calls == 0:
                continue
            result['retained_tools'].append(dict(entry_point=approved.entry_point,
                source_sha256=file_digest(approved.source), fixture_sha256=file_digest(approved.fixture),
                keyset_sha256=None, calls=calls))
        result['retained_tools'].sort(key=lambda v: v['entry_point'])
    a2.VERIFIERS(result)
    return result


def check_calls(ctx: Context) -> None:
    expected = dict(binding=len(ctx.kind(a2.p1.KINDS[0])), certificate=0)
    for _, event in ctx.events():
        expected['certificate'] += event.get('proof_kind') == 'certificate'
        expected['binding'] += (event['phase'] not in ('replay', 'completion')
            and event.get('retry_original') is not None and event.get('proof_kind') != 'certificate')
    for name, count in expected.items():
        if ctx.verifier_calls.get(name, 0) != count:
            ctx.issue('evidence_schema_gap', 'missing applicable ' + name + ' verifier invocation')


def invoke_retained(ctx: Context, name: str, *args: Any, **kwargs: Any) -> Any:
    require(ctx.ports is not None, 'artifact_gap')
    assert ctx.ports is not None
    function = getattr(ctx.ports, name)
    approved = next((a for a in ctx.ports.approvals if a.entry_point == function.__module__ + '.' + function.__name__), None)
    if approved is None or approved.function is not function:
        ctx.issue('artifact_gap', 'retained validator approval absent: ' + name)
        raise ValueError('artifact_gap')
    require(file_digest(approved.source) == approved.source_pin and file_digest(approved.fixture) == approved.fixture_pin, 'artifact_gap')
    release_approval(ctx, approved)
    ctx.verifier_calls[approved.entry_point] = ctx.verifier_calls.get(approved.entry_point, 0) + 1
    from trusted_router.async_settle_shadow_v2_gate_inputs import LoadingPorts
    if isinstance(ctx.ports, LoadingPorts) and name in ('fleet_budgets_pre', 'post_flip'):
        from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
        from trusted_router.async_settle_shadow_v2_gate_retained import delegate
        graph = PayloadGraph({p.name: p.raw for p in ctx.bundle.preimages},
                             ctx.bundle.completed_at_us, ports=ctx.ports)
        envelopes = [p['retained'] for p in ctx.bundle.a2_payloads.values() if 'retained' in p]
        envelopes.extend(p['pilot'] for p in ctx.bundle.a2_payloads.values() if p.get('pilot'))
        matches = [e for e in envelopes if e['entry_point'].endswith('.' + name)]
        require(len(matches) == 1, 'artifact_gap')
        payload, result = delegate(graph, matches[0])
        require(len(args) == 1 and not kwargs and canonical(args[0]) == canonical(payload), 'evidence_schema_gap')
        return result
    return function(*args, **kwargs)
