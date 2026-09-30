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
# Fixed action identities, not prefixes or a list inferred at scan time. New
# actions need review here; these do not authenticate to the GCP WIF provider.
KNOWN_NON_GCP_AUTH_ACTIONS = {
    "actions/checkout": "Checks out source using GitHub credentials.",
    "actions/setup-java": "Installs the Java toolchain.",
    "actions/setup-node": "Installs the Node.js toolchain.",
    "actions/setup-python": "Installs the Python toolchain.",
    "actions/cache": "Restores/saves GitHub Actions caches.",
    "actions/download-artifact": "Downloads GitHub Actions artifacts.",
    "actions/upload-artifact": "Uploads GitHub Actions artifacts.",
    "astral-sh/setup-uv": "Installs uv and manages its cache.",
    "aws-actions/configure-aws-credentials": "Obtains AWS credentials, not GCP credentials.",
    "azure/login": "Authenticates to Azure, not GCP.",
    "docker/setup-buildx-action": "Installs/configures the Docker Buildx builder.",
    "google-github-actions/setup-gcloud": "Installs gcloud; GCP authentication is a separate action.",
    "hashicorp/setup-terraform": "Installs the Terraform CLI.",
    "oven-sh/setup-bun": "Installs the Bun toolchain.",
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

    def inspect_steps(steps, path, job_id, caller, context, stack=(), trail=()):
        for index, step in enumerate(steps, start=1):
            if "uses" not in step:
                continue
            uses = step["uses"]
            label = step.get("name", step.get("id", f"#{index}"))
            location = f"{path}, job {job_id}, step {label} (caller {caller})"
            if trail:
                location += f" via {' -> '.join(trail)}"
            unresolved = f"UNRESOLVED uses {uses!r} in {location}"
            assert isinstance(uses, str) and uses and "${{" not in uses, unresolved
            step_context = context | {"env": context["env"] | step.get("env", {})}
            if uses.startswith("./"):
                directory = (root / uses).resolve()
                assert directory.is_relative_to(root.resolve()), unresolved
                definitions = [directory / name for name in ("action.yml", "action.yaml") if (directory / name).is_file()]
                assert len(definitions) == 1, f"{unresolved}: expected one action.yml/action.yaml"
                action_path = definitions[0].relative_to(root.resolve()).as_posix()
                assert action_path not in stack, f"{unresolved}: recursive composite action {action_path}"
                action = yaml.safe_load(definitions[0].read_text())
                assert isinstance(action, dict), f"{unresolved}: invalid action metadata"
                runs = action.get("runs", {})
                assert isinstance(runs, dict) and runs.get("using") == "composite" and isinstance(runs.get("steps"), list), (
                    f"{unresolved}: only local composite actions can be inspected"
                )
                inputs = {key: _resolve_static(spec.get("default"), step_context) for key, spec in action.get("inputs", {}).items()}
                inputs.update(_resolve_static(step.get("with", {}), step_context))
                # Freeze caller env references before replacing the inputs scope.
                composite_context = step_context | {"inputs": inputs, "env": _resolve_static(step_context["env"], step_context)}
                inspect_steps(
                    runs["steps"], action_path, job_id, caller, composite_context,
                    (*stack, action_path), (*trail, location),
                )
                continue
            action_name, separator, ref = uses.partition("@")
            assert separator and ref and "@" not in ref, unresolved
            if action_name == "google-github-actions/auth":
                provider = _resolve_static(step.get("with", {}).get("workload_identity_provider"), step_context)
                assert isinstance(provider, str) and provider, (
                    f"Cannot statically resolve GCP WIF provider in {location}"
                )
                if provider == WIF_PROVIDER:
                    consumers.add(caller)
            else:
                assert action_name in KNOWN_NON_GCP_AUTH_ACTIONS, unresolved

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
            for matrix in _matrix_rows(job, context) or [None]:
                job_context = context | {"matrix": matrix}
                if "uses" in job:
                    callee = job["uses"]
                    assert isinstance(callee, str) and callee.startswith("./.github/workflows/") and "${{" not in callee, (
                        f"UNRESOLVED uses {callee!r} in {path}, job {job_id}, "
                        f"step <reusable workflow> (caller {caller})"
                    )
                    inspect(callee[2:], caller, _resolve_static(job.get("with", {}), job_context), (*stack, path))
                inspect_steps(job.get("steps", []), path, job_id, caller, job_context)

    called = {
        job["uses"][2:]
        for workflow in workflows.values()
        for job in workflow.get("jobs", {}).values()
        if isinstance(job.get("uses"), str) and job["uses"].startswith("./.github/workflows/")
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
    assert set(KNOWN_WIF_ALLOWLIST_EXCEPTIONS) == {".github/workflows/deploy-growth-sync.yml"}
    _assert_workflows_using_gcp_wif_provider_are_allowlisted(ROOT)


@pytest.fixture
def wif_repository_copy(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / ".github/workflows", tmp_path / ".github/workflows")
    if (ROOT / ".github/actions").is_dir():
        shutil.copytree(ROOT / ".github/actions", tmp_path / ".github/actions")
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


def _write_composite(root, name, steps, filename='action.yml', inputs=None):
    directory = root / '.github/actions' / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(yaml.safe_dump({
        'inputs': inputs or {}, 'runs': {'using': 'composite', 'steps': steps},
    }))


@pytest.mark.parametrize('filename', ['action.yml', 'action.yaml'])
@pytest.mark.parametrize('nested', [False, True])
def test_wif_composite_wrapper_rejects_unlisted_workflow(wif_repository_copy: Path, filename, nested):
    steps = _auth_workflow()['jobs']['audit']['steps']
    _write_composite(wif_repository_copy, 'x', steps, filename)
    if nested:
        _write_composite(wif_repository_copy, 'wrapper', [{'uses': './.github/actions/x'}])
    workflow = _auth_workflow()
    workflow['jobs']['audit']['steps'] = [{'uses': './.github/actions/wrapper' if nested else './.github/actions/x'}]
    _write_workflow(wif_repository_copy, 'unlisted-workflow.yml', workflow)
    with pytest.raises(AssertionError, match=r'missing from .*unlisted-workflow\.yml'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('form', ['input', 'default', 'env', 'reusable'])
def test_wif_composite_uses_callers_identity_and_context(wif_repository_copy: Path, form):
    steps = _auth_workflow('${{ env.WIF }}' if form == 'env' else '${{ inputs.provider }}')['jobs']['audit']['steps']
    _write_composite(wif_repository_copy, 'x', steps, inputs={'provider': {'default': WIF_PROVIDER}})
    _write_composite(wif_repository_copy, 'wrapper', [{
        'uses': './.github/actions/x', 'with': {'provider': '${{ inputs.provider }}'},
    }], inputs={'provider': {'default': WIF_PROVIDER}})
    workflow = _auth_workflow()
    workflow['jobs']['audit']['steps'] = [{
        'uses': './.github/actions/wrapper',
        'env': {'WIF': WIF_PROVIDER},
        'with': {} if form == 'default' else {'provider': '${{ env.WIF }}'},
    }]
    if form == 'reusable':
        workflow['on'] = {'workflow_call': None}
        _write_workflow(wif_repository_copy, 'reusable.yml', workflow)
        workflow = {
            'on': {'workflow_dispatch': None},
            'jobs': {'call': {'uses': './.github/workflows/reusable.yml'}},
        }
    _write_workflow(wif_repository_copy, 'typed-audit.yml', workflow)
    consumers = _gcp_wif_consumers(wif_repository_copy)
    assert '.github/workflows/typed-audit.yml' in consumers
    assert '.github/workflows/reusable.yml' not in consumers
    _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_composite_resolves_inputs_per_call(wif_repository_copy: Path):
    _write_composite(wif_repository_copy, 'x', _auth_workflow('${{ inputs.provider }}')['jobs']['audit']['steps'])
    for name, provider in [('typed-audit.yml', 'another-provider'), ('unlisted-workflow.yml', WIF_PROVIDER)]:
        workflow = _auth_workflow()
        workflow['jobs']['audit']['steps'] = [{'uses': './.github/actions/x', 'with': {'provider': provider}}]
        _write_workflow(wif_repository_copy, name, workflow)
    with pytest.raises(AssertionError, match=r'missing from .*unlisted-workflow\.yml'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_composite_does_not_rebind_caller_env_inputs(wif_repository_copy: Path):
    _write_composite(wif_repository_copy, 'x', _auth_workflow('${{ env.WIF }}')['jobs']['audit']['steps'], inputs={'provider': {'default': 'another-provider'}})
    callee = {
        'on': {'workflow_call': {'inputs': {'provider': {'type': 'string'}}}},
        'env': {'WIF': '${{ inputs.provider }}'},
        'jobs': {'audit': {'steps': [{'uses': './.github/actions/x'}]}},
    }
    _write_workflow(wif_repository_copy, 'reusable.yml', callee)
    _write_workflow(wif_repository_copy, 'unlisted-workflow.yml', {
        'on': {'workflow_dispatch': None},
        'jobs': {'call': {'uses': './.github/workflows/reusable.yml', 'with': {'provider': WIF_PROVIDER}}},
    })
    with pytest.raises(AssertionError, match=r'missing from .*unlisted-workflow\.yml'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


def test_wif_composite_unresolved_provider_names_call_site(wif_repository_copy: Path):
    _write_composite(wif_repository_copy, 'x', _auth_workflow('${{ inputs.provider }}')['jobs']['audit']['steps'])
    workflow = _auth_workflow()
    workflow['jobs']['audit']['steps'] = [{'id': 'wrapper', 'uses': './.github/actions/x', 'with': {'provider': '${{ secrets.WIF }}'}}]
    _write_workflow(wif_repository_copy, 'typed-audit.yml', workflow)
    with pytest.raises(AssertionError, match=r'Cannot statically resolve .*actions/x/action.yml, job audit, step Authenticate to GCP .*via .*typed-audit.yml, job audit, step wrapper'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('arguments', [{}, {'provider': WIF_PROVIDER}, {'provider': '${{ secrets.WIF }}'}])
def test_wif_external_reusable_workflow_is_unresolved(wif_repository_copy: Path, arguments):
    _write_workflow(wif_repository_copy, 'typed-audit.yml', {
        'on': {'workflow_dispatch': None},
        'jobs': {'call': {'uses': 'Lore-Hex/shared-ci/.github/workflows/gcp-auth.yml@main', 'with': arguments}},
    })
    with pytest.raises(AssertionError, match=r'UNRESOLVED uses .*gcp-auth.yml@main.*typed-audit.yml, job call, step <reusable workflow>'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('uses', [
    'Lore-Hex/shared-ci/gcp-auth@main', 'actions/setup-unknown@v1',
    'actions/checkout/wrapper@v4', 'actions/checkout', 'actions/checkout@${{ inputs.ref }}',
    'google-github-actions/auth-wrapper@v3', 'docker://unknown/image:latest', None,
])
@pytest.mark.parametrize('composite', [False, True])
def test_wif_unknown_action_is_unresolved(wif_repository_copy: Path, uses, composite):
    step = {'name': 'Unknown action', 'uses': uses, 'with': {'provider': WIF_PROVIDER}}
    workflow = _auth_workflow()
    workflow['jobs']['audit']['steps'] = [step]
    path = 'typed-audit.yml'
    if composite:
        _write_composite(wif_repository_copy, 'x', [step])
        workflow['jobs']['audit']['steps'] = [{'uses': './.github/actions/x'}]
        path = 'actions/x/action.yml'
    _write_workflow(wif_repository_copy, 'typed-audit.yml', workflow)
    with pytest.raises(AssertionError, match=rf'UNRESOLVED uses .*{re.escape(path)}, job audit, step Unknown action'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


@pytest.mark.parametrize('form', ['missing', 'recursive', 'node24', 'docker', 'ambiguous'])
def test_wif_uninspectable_local_action_is_unresolved(wif_repository_copy: Path, form):
    if form != 'missing':
        _write_composite(wif_repository_copy, 'x', [{'uses': './.github/actions/x'}])
        path = wif_repository_copy / '.github/actions/x/action.yml'
        if form in {'node24', 'docker'}:
            path.write_text(yaml.safe_dump({'runs': {'using': form, 'main': 'index.js', 'image': 'Dockerfile'}}))
        elif form == 'ambiguous':
            shutil.copy2(path, path.with_suffix('.yaml'))
    workflow = _auth_workflow()
    workflow['jobs']['audit']['steps'] = [{'uses': './.github/actions/x'}]
    _write_workflow(wif_repository_copy, 'typed-audit.yml', workflow)
    with pytest.raises(AssertionError, match=r'UNRESOLVED uses .*job audit, step #1'):
        _assert_workflows_using_gcp_wif_provider_are_allowlisted(wif_repository_copy)


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
