import re
import shutil
from itertools import product
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WIF_PROVIDER = "projects/44325983244/locations/global/workloadIdentityPools/github-actions/providers/github"
KNOWN_WIF_ALLOWLIST_EXCEPTIONS = {
    ".github/workflows/deploy-growth-sync.yml": (
        "Uses the provider as tr-deploy but is not admitted; the IAM fix requires "
        "an operator decision (Lore-Hex/quill-router#1416)."
    ),
}


def test_schema_audit_workflow_pins_schedule_identity_permissions_and_emulator():
    workflow = yaml.safe_load((ROOT / ".github/workflows/typed-audit.yml").read_text())
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = workflow[True]  # YAML 1.1 parses the GitHub `on` key as True.
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "43 11 * * *"}]
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert set(workflow["jobs"]) == {"typed-billing-invariant-audit", "spanner-schema-drift"}
    assert all("needs" not in job for job in workflow["jobs"].values())
    job = workflow["jobs"]["spanner-schema-drift"]
    assert "permissions" not in job  # Inherit the pinned workflow permissions.
    assert job["if"] == "github.repository == 'Lore-Hex/quill-router'"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 25
    assert job["services"]["spanner"] == ci["jobs"]["spanner-emulator"]["services"]["spanner"]
    assert "@sha256:" in job["services"]["spanner"]["image"]
    steps = job["steps"]
    auth = next(step for step in steps if step.get("uses") == "google-github-actions/auth@v3")
    typed_auth = next(step for step in workflow["jobs"]["typed-billing-invariant-audit"]["steps"] if step.get("uses") == "google-github-actions/auth@v3")
    assert auth["with"] == typed_auth["with"] == {
        "workload_identity_provider": WIF_PROVIDER,
        "service_account": "tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com",
    }
    assert steps[0]["uses"] == "actions/checkout@v4"
    assert any(step.get("uses") == "google-github-actions/setup-gcloud@v3" for step in steps)
    assert any(step.get("uses") == "astral-sh/setup-uv@v7" and step.get("with") == {"version": "latest"} for step in steps)
    assert any(step.get("run") == "uv sync --frozen" for step in steps)
    assert steps[-1]["run"] == "uv run python -m scripts.audit_spanner_schema --emulator-host 127.0.0.1:9010"
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in steps)
    assert "SPANNER_EMULATOR_HOST" not in str(workflow)
    assert "secrets." not in str(workflow)


def _resolve_static(value, context, resolving=()):
    """Resolve the supported expression subset; None means unknown, never safe to ignore."""
    if isinstance(value, dict):
        return {key: _resolve_static(item, context, resolving) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_static(item, context, resolving) for item in value]
    if not isinstance(value, str) or "${{" not in value:
        return value

    def reference(expression):
        key = expression.strip()
        if not re.fullmatch(r"(?:env|matrix|inputs)(?:\.[\w-]+)+", key) or key in resolving:
            return None
        result = context
        for part in key.split("."):
            if not isinstance(result, dict) or part not in result:
                return None
            result = result[part]
        return _resolve_static(result, context, (*resolving, key))

    full = re.fullmatch(r"\$\{\{([^{}]*?)\}\}", value)
    if full:
        return reference(full[1])
    # Also support a literal provider path assembled from static components.
    pieces = re.split(r"(\$\{\{[^{}]*?\}\})", value)
    for index, piece in enumerate(pieces):
        if piece.startswith("${{"):
            resolved = reference(piece[3:-2])
            if not isinstance(resolved, (str, int, float, bool)):
                return None
            pieces[index] = str(resolved)
    result = "".join(pieces)
    return None if "${{" in result else result


def _matrix_rows(job, context):
    matrix = _resolve_static(job.get("strategy", {}).get("matrix", {}), context)
    if not isinstance(matrix, dict):
        return [None]  # A provider depending on a dynamic matrix must fail closed.
    axes = {key: values for key, values in matrix.items() if key not in {"include", "exclude"}}
    if any(not isinstance(values, list) for values in axes.values()):
        return [None]
    included, excluded = matrix.get("include", []), matrix.get("exclude", [])
    if not all(isinstance(items, list) and all(isinstance(item, dict) for item in items) for items in (included, excluded)):
        return [None]
    original = [dict(zip(axes, values, strict=True)) for values in product(*axes.values())] if axes else []
    original = [row for row in original if not any(all(row.get(key) == value for key, value in exclusion.items()) for exclusion in excluded)]
    rows = [dict(row) for row in original]
    for addition in included:
        matched = False
        for base, row in zip(original, rows, strict=False):
            if all(key not in base or base[key] == value for key, value in addition.items()):
                row.update(addition)
                matched = True
        if not matched:
            rows.append(dict(addition))
    return rows if matrix else [{}]


def _gcp_wif_consumers(root: Path) -> set[str]:
    workflows = {
        path.relative_to(root).as_posix(): yaml.safe_load(path.read_text())
        for path in sorted((root / ".github/workflows").iterdir())
        if path.suffix in {".yml", ".yaml"}
    }
    consumers: set[str] = set()
    visited: set[str] = set()

    def inspect(path, caller, supplied_inputs, stack=()):
        assert path not in stack, f"Recursive reusable workflow call: {(*stack, path)}"
        assert path in workflows, f"Missing local reusable workflow {path}, called by {caller}"
        visited.add(path)
        workflow = workflows[path]
        triggers = workflow.get("on", workflow.get(True, {}))
        call = triggers.get("workflow_call") if isinstance(triggers, dict) else None
        inputs = {key: spec.get("default") for key, spec in (call or {}).get("inputs", {}).items()}
        inputs.update(supplied_inputs)
        for job_id, job in workflow.get("jobs", {}).items():
            context = {"inputs": inputs, "env": workflow.get("env", {}) | job.get("env", {})}
            for matrix in _matrix_rows(job, context):
                job_context = context | {"matrix": matrix}
                callee = job.get("uses", "")
                if callee.startswith("./.github/workflows/"):
                    inspect(callee[2:], caller, _resolve_static(job.get("with", {}), job_context), (*stack, path))
                for index, step in enumerate(job.get("steps", []), start=1):
                    if not step.get("uses", "").startswith("google-github-actions/auth@"):
                        continue
                    step_context = job_context | {"env": context["env"] | step.get("env", {})}
                    provider = _resolve_static(step.get("with", {}).get("workload_identity_provider"), step_context)
                    label = step.get("name", step.get("id", f"#{index}"))
                    assert isinstance(provider, str) and provider, (
                        f"Cannot statically resolve GCP WIF provider in {path}, "
                        f"job {job_id}, step {label} (caller {caller})"
                    )
                    if provider == WIF_PROVIDER:
                        consumers.add(caller)

    called = {
        job["uses"][2:]
        for workflow in workflows.values()
        for job in workflow.get("jobs", {}).values()
        if job.get("uses", "").startswith("./.github/workflows/")
    }
    for path, workflow in workflows.items():
        triggers = workflow.get("on", workflow.get(True, {}))
        trigger_names = {triggers} if isinstance(triggers, str) else set(triggers or {})
        # A call-only callee runs with its caller's workflow_ref, not its own.
        if path not in called or trigger_names != {"workflow_call"}:
            inspect(path, path, {})
    # Do not silently skip disconnected cycles of reusable workflows.
    for path in workflows.keys() - visited:
        inspect(path, path, {})
    return consumers


def _assert_workflows_using_gcp_wif_provider_are_allowlisted(root: Path) -> None:
    terraform = (root / "infra/gcp_wif.tf").read_text()
    match = re.search(r"\bquill_router_workflow_refs\s*=\s*\[(.*?)\]", terraform, re.DOTALL)
    assert match is not None, "Missing quill_router_workflow_refs in infra/gcp_wif.tf"
    allowed_refs = set(re.findall(r'^\s*"([^"\n]+)"\s*,?\s*(?:#.*)?$', match[1], re.MULTILINE))
    consumers = _gcp_wif_consumers(root)

    def admitted(path):
        return f"${{local.github_owner}}/quill-router/{path}@refs/heads/main" in allowed_refs

    stale_exceptions = {
        path: reason
        for path, reason in KNOWN_WIF_ALLOWLIST_EXCEPTIONS.items()
        if admitted(path) or not (root / path).is_file() or path not in consumers
    }
    assert not stale_exceptions, (
        "Remove stale GCP WIF allowlist exceptions: workflows must exist, remain "
        f"unadmitted, and use the provider: {stale_exceptions}"
    )
    missing = sorted(path for path in consumers if not admitted(path) and path not in KNOWN_WIF_ALLOWLIST_EXCEPTIONS)
    assert not missing, (
        "Workflows using the GCP WIF provider are missing from "
        f"infra/gcp_wif.tf quill_router_workflow_refs: {missing}"
    )


def test_all_workflows_using_gcp_wif_provider_are_allowlisted():
    _assert_workflows_using_gcp_wif_provider_are_allowlisted(ROOT)


@pytest.fixture
def wif_repository_copy(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / ".github/workflows", tmp_path / ".github/workflows")
    (tmp_path / "infra").mkdir()
    shutil.copy2(ROOT / "infra/gcp_wif.tf", tmp_path / "infra/gcp_wif.tf")
    return tmp_path


def test_wif_allowlist_rejects_another_unlisted_workflow(wif_repository_copy: Path):
    workflows = wif_repository_copy / ".github/workflows"
    shutil.copy2(workflows / "deploy-growth-sync.yml", workflows / "unlisted-workflow.yml")

    with pytest.raises(AssertionError, match=r"missing from .*\.github/workflows/unlisted-workflow\.yml"):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_allowlist_rejects_an_admitted_known_exception(wif_repository_copy: Path):
    terraform = wif_repository_copy / "infra/gcp_wif.tf"
    terraform.write_text(terraform.read_text().replace(
        "quill_router_workflow_refs = [",
        'quill_router_workflow_refs = [\n'
        '    "${local.github_owner}/quill-router/.github/workflows/deploy-growth-sync.yml@refs/heads/main",',
        1,
    ))

    with pytest.raises(AssertionError, match=r"Remove stale .*\.github/workflows/deploy-growth-sync\.yml"):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def _write_workflow(root, name, workflow):
    (root / '.github/workflows' / name).write_text(yaml.safe_dump(workflow))


def _auth_workflow(provider=WIF_PROVIDER):
    return {
        'on': {'workflow_dispatch': None},
        'jobs': {'audit': {'runs-on': 'ubuntu-latest', 'steps': [{
            'name': 'Authenticate to GCP',
            'uses': 'google-github-actions/auth@v3',
            'with': {'workload_identity_provider': provider},
        }]}},
    }


@pytest.mark.parametrize('form', ['matrix', 'matrix-include', 'matrix-object', 'workflow-env', 'job-env', 'step-env', 'interpolation'])
@pytest.mark.parametrize('admitted', [False, True])
def test_wif_allowlist_resolves_static_references(wif_repository_copy: Path, form, admitted):
    workflow = _auth_workflow('${{ env.WIF }}')
    job = workflow['jobs']['audit']
    step = job['steps'][0]
    if form.startswith('matrix'):
        step['with']['workload_identity_provider'] = '${{ matrix.provider }}'
        job['strategy'] = {'matrix': {'provider': ['another-provider', WIF_PROVIDER]}}
        if form == 'matrix-include':
            job['strategy'] = {'matrix': {'include': [{'provider': WIF_PROVIDER}]}}
        elif form == 'matrix-object':
            job['strategy'] = {'matrix': {'target': [{'provider': WIF_PROVIDER}]}}
            step['with']['workload_identity_provider'] = '${{ matrix.target.provider }}'
    elif form == 'interpolation':
        workflow['env'] = {'PROJECT': '44325983244', 'POOL': 'github-actions'}
        step['with']['workload_identity_provider'] = WIF_PROVIDER.replace('44325983244', '${{ env.PROJECT }}').replace('github-actions', '${{ env.POOL }}')
    else:
        scope = {'workflow-env': workflow, 'job-env': job, 'step-env': step}[form]
        scope['env'] = {'WIF': WIF_PROVIDER}
    name = 'typed-audit.yml' if admitted else 'unlisted-workflow.yml'
    _write_workflow(wif_repository_copy, name, workflow)
    if admitted:
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)
    else:
        with pytest.raises(AssertionError, match=r'missing from .*unlisted-workflow\.yml'):
            _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('scope', ['job', 'step'])
def test_wif_env_uses_most_specific_scope(wif_repository_copy: Path, scope):
    workflow = _auth_workflow('${{ env.WIF }}')
    workflow['env'] = {'WIF': WIF_PROVIDER}
    job = workflow['jobs']['audit']
    target = job if scope == 'job' else job['steps'][0]
    target['env'] = {'WIF': 'another-provider'}
    _write_workflow(wif_repository_copy, 'unlisted-workflow.yml', workflow)
    _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('provider', [
    '${{ env.MISSING }}', '${{ matrix.provider }}', '${{ inputs.provider }}',
    '${{ secrets.WIF }}', '${{ vars.WIF }}', '${{ env.WIF || vars.WIF }}', None,
])
def test_wif_unresolved_provider_names_file_and_step(wif_repository_copy: Path, provider):
    _write_workflow(wif_repository_copy, 'typed-audit.yml', _auth_workflow(provider))
    with pytest.raises(AssertionError, match=r'Cannot statically resolve .*typed-audit\.yml, job audit, step Authenticate to GCP'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_cyclic_env_is_unresolved(wif_repository_copy: Path):
    workflow = _auth_workflow('${{ env.WIF }}')
    workflow['env'] = {'WIF': '${{ env.OTHER }}', 'OTHER': '${{ env.WIF }}'}
    _write_workflow(wif_repository_copy, 'typed-audit.yml', workflow)
    with pytest.raises(AssertionError, match=r'Cannot statically resolve .*step Authenticate to GCP'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('form', ['literal', 'input', 'default', 'matrix-input', 'nested-input'])
@pytest.mark.parametrize('admitted', [False, True])
def test_wif_reusable_workflow_checks_caller_identity(wif_repository_copy: Path, form, admitted):
    callee = _auth_workflow(WIF_PROVIDER if form == 'literal' else '${{ inputs.provider }}')
    callee['on'] = {'workflow_call': {'inputs': {'provider': {'type': 'string', 'default': WIF_PROVIDER}}}}
    caller = {'on': {'workflow_dispatch': None}, 'jobs': {'call': {'uses': './.github/workflows/reusable.yml'}}}
    call = caller['jobs']['call']
    if form in {'input', 'nested-input'}:
        callee['on']['workflow_call']['inputs']['provider'] = {'type': 'string', 'required': True}
        call['with'] = {'provider': WIF_PROVIDER}
    elif form == 'matrix-input':
        call['strategy'] = {'matrix': {'provider': [WIF_PROVIDER]}}
        call['with'] = {'provider': '${{ matrix.provider }}'}
    if form == 'nested-input':
        wrapper = {
            'on': {'workflow_call': {'inputs': {'provider': {'type': 'string', 'required': True}}}},
            'jobs': {'nested': {'uses': './.github/workflows/reusable.yml', 'with': {'provider': '${{ inputs.provider }}'}}},
        }
        _write_workflow(wif_repository_copy, 'wrapper.yml', wrapper)
        call['uses'] = './.github/workflows/wrapper.yml'
    _write_workflow(wif_repository_copy, 'reusable.yml', callee)
    _write_workflow(wif_repository_copy, 'typed-audit.yml' if admitted else 'unlisted-workflow.yml', caller)
    if admitted:
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)
    else:
        with pytest.raises(AssertionError, match=r'missing from .*unlisted-workflow\.yml') as error:
            _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)
        assert 'reusable.yml' not in str(error.value)
        assert 'wrapper.yml' not in str(error.value)


def test_wif_reusable_workflow_resolves_inputs_per_caller(wif_repository_copy: Path):
    callee = _auth_workflow('${{ inputs.provider }}')
    callee['on'] = {'workflow_call': {'inputs': {'provider': {'type': 'string'}}}}
    _write_workflow(wif_repository_copy, 'reusable.yml', callee)
    for name, provider in [('typed-audit.yml', 'another-provider'), ('unlisted-workflow.yml', WIF_PROVIDER)]:
        _write_workflow(wif_repository_copy, name, {
            'on': {'workflow_dispatch': None},
            'jobs': {'call': {'uses': './.github/workflows/reusable.yml', 'with': {'provider': provider}}},
        })
    with pytest.raises(AssertionError, match=r'missing from .*unlisted-workflow\.yml'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('provider', ['${{ inputs.provider }}', '${{ env.WIF }}'])
def test_wif_reusable_workflow_unresolved_provider_fails(wif_repository_copy: Path, provider):
    callee = _auth_workflow(provider)
    callee['on'] = {'workflow_call': None}
    _write_workflow(wif_repository_copy, 'reusable.yml', callee)
    _write_workflow(wif_repository_copy, 'typed-audit.yml', {
        'on': {'workflow_dispatch': None},
        'env': {'WIF': WIF_PROVIDER},  # Caller env is not inherited by the callee.
        'jobs': {'call': {'uses': './.github/workflows/reusable.yml', 'with': {'provider': '${{ secrets.WIF }}'}}},
    })
    with pytest.raises(AssertionError, match=r'Cannot statically resolve .*reusable\.yml, job audit, step Authenticate to GCP .*caller .*typed-audit\.yml'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('change', ['delete', 'remove-auth', 'other-provider'])
def test_wif_allowlist_rejects_non_consuming_known_exception(wif_repository_copy: Path, change):
    path = wif_repository_copy / '.github/workflows/deploy-growth-sync.yml'
    if change == 'delete':
        path.unlink()
    else:
        workflow = _auth_workflow('another-provider')
        if change == 'remove-auth':
            workflow['jobs']['audit']['steps'] = [{'run': 'true'}]
        path.write_text(yaml.safe_dump(workflow))
    with pytest.raises(AssertionError, match=r'Remove stale .*deploy-growth-sync\.yml'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_matrix_include_and_exclude_follow_actual_rows(wif_repository_copy: Path):
    workflow = _auth_workflow('${{ env.WIF }}')
    job = workflow['jobs']['audit']
    job['env'] = {'WIF': '${{ matrix.provider }}'}
    job['strategy'] = {'matrix': {
        'provider': [WIF_PROVIDER, 'another-provider'],
        'exclude': [{'provider': WIF_PROVIDER}],
    }}
    _write_workflow(wif_repository_copy, 'unlisted-workflow.yml', workflow)
    _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)
    job['strategy']['matrix']['include'] = [{'provider': WIF_PROVIDER}]
    _write_workflow(wif_repository_copy, 'unlisted-workflow.yml', workflow)
    with pytest.raises(AssertionError, match=r'missing from .*unlisted-workflow\.yml'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_dynamic_matrix_fails_with_unnamed_step_location(wif_repository_copy: Path):
    workflow = _auth_workflow('${{ matrix.provider }}')
    job = workflow['jobs']['audit']
    job['strategy'] = {'matrix': '${{ fromJSON(needs.setup.outputs.matrix) }}'}
    del job['steps'][0]['name']
    _write_workflow(wif_repository_copy, 'unlisted-workflow.yml', workflow)
    with pytest.raises(AssertionError, match=r'Cannot statically resolve .*unlisted-workflow\.yml, job audit, step #1'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_yaml_alias_provider_is_detected(wif_repository_copy: Path):
    (wif_repository_copy / '.github/workflows/unlisted-workflow.yml').write_text(
        'on: workflow_dispatch\nenv:\n  WIF: &provider ' + WIF_PROVIDER + '\n'
        'jobs:\n  audit:\n    steps:\n'
        '      - uses: google-github-actions/auth@v3\n'
        '        with:\n          workload_identity_provider: *provider\n'
    )
    with pytest.raises(AssertionError, match=r'missing from .*unlisted-workflow\.yml'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('callee', ['missing.yml', 'typed-audit.yml'])
def test_wif_reusable_missing_or_recursive_call_fails(wif_repository_copy: Path, callee):
    _write_workflow(wif_repository_copy, 'typed-audit.yml', {
        'on': {'workflow_call': None},
        'jobs': {'call': {'uses': f'./.github/workflows/{callee}'}},
    })
    with pytest.raises(AssertionError, match='Missing local reusable workflow|Recursive reusable workflow call'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)
