from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INFRA = (ROOT / "scripts/deploy/infra.sh").read_text(encoding="utf-8")
LIB = (ROOT / "scripts/deploy/_lib.sh").read_text(encoding="utf-8")
PROJECT_ID = "quill-cloud-proxy"
LEGACY_IDENTITY = "123456789-compute@developer.gserviceaccount.com"
SYNTHETIC_IDENTITY = f"tr-synthetic@{PROJECT_ID}.iam.gserviceaccount.com"
INTERNAL_IDENTITY = f"tr-internal@{PROJECT_ID}.iam.gserviceaccount.com"
DEPLOY_IDENTITY = f"tr-deploy@{PROJECT_ID}.iam.gserviceaccount.com"

CANONICAL_JOBS = (
    (
        "us-central1",
        "trusted-router-synthetic-us-central1",
        "trusted-router-synthetic-us-central1-every-three-minutes",
    ),
    (
        "europe-west1",
        "trusted-router-throughput-europe-west1",
        "trusted-router-throughput-europe-west1-every-five-minutes",
    ),
    (
        "asia-northeast1",
        "trusted-router-image-generation-asia-northeast1",
        "trusted-router-image-generation-asia-northeast1-every-six-hours",
    ),
    (
        "us-east1",
        "trusted-router-video-generation-us-east1",
        "trusted-router-video-generation-us-east1-daily",
    ),
)


def _function_body(source: str, name: str, next_name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index(f"{next_name}() {{", start)
    return source[start:end]


RETIREMENT_HELPERS = "\n".join(
    (
        _function_body(
            INFRA,
            "verify_identity_resource_manager_ancestors_empty",
            "verify_synthetic_retirement_identity_ready",
        ),
        _function_body(
            INFRA,
            "verify_synthetic_retirement_identity_ready",
            "verify_legacy_synthetic_secret_access_ready",
        ),
        _function_body(
            INFRA,
            "verify_legacy_synthetic_secret_access_ready",
            "synthetic_job_inventory_lines",
        ),
        _function_body(
            INFRA,
            "synthetic_job_inventory_lines",
            "cloud_run_inventory_lines",
        ),
        _function_body(
            INFRA,
            "cloud_run_inventory_lines",
            "cloud_run_job_service_account",
        ),
        _function_body(
            INFRA,
            "cloud_run_job_service_account",
            "verify_legacy_cloud_run_service_inventory",
        ),
        _function_body(
            INFRA,
            "verify_legacy_synthetic_jobs_ready",
            "describe_iam_role_definition",
        ),
    )
)

FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]


def option(name):
    prefix = name + "="
    for index, value in enumerate(args):
        if value.startswith(prefix):
            return value[len(prefix):]
        if value == name and index + 1 < len(args):
            return args[index + 1]
    return ""


def emit(value):
    print(json.dumps(value, separators=(",", ":")))
    raise SystemExit(0)


if args[:3] == ["iam", "service-accounts", "describe"]:
    emit(json.loads(os.environ["FAKE_ACCOUNT_JSON"]))
if args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
    emit(json.loads(os.environ["FAKE_ACCOUNT_POLICY"]))
if args[:2] == ["projects", "get-ancestors"]:
    emit(json.loads(os.environ["FAKE_ANCESTORS"]))
if args[:3] == ["resource-manager", "folders", "get-iam-policy"]:
    emit(json.loads(os.environ["FAKE_FOLDER_POLICY"]))
if args[:2] == ["organizations", "get-iam-policy"]:
    emit(json.loads(os.environ["FAKE_ORGANIZATION_POLICY"]))
if args[:3] == ["run", "jobs", "list"]:
    emit(json.loads(os.environ["FAKE_JOB_INVENTORY"]))
if args[:3] == ["run", "jobs", "describe"]:
    key = option("--region") + "/" + args[3]
    identities = json.loads(os.environ["FAKE_JOB_IDENTITIES"])
    if key not in identities:
        raise SystemExit(3)
    emit(
        {
            "spec": {
                "template": {
                    "template": {"serviceAccount": identities[key]}
                }
            }
        }
    )
if args[:3] == ["run", "jobs", "get-iam-policy"]:
    emit(json.loads(os.environ["FAKE_JOB_POLICY"]))
if args[:3] == ["scheduler", "jobs", "describe"]:
    region = option("--location")
    scheduler_name = args[3]
    scheduler_identities = json.loads(os.environ["FAKE_SCHEDULER_IDENTITIES"])
    key = region + "/" + scheduler_name
    if key not in scheduler_identities:
        raise SystemExit(3)
    job_name = scheduler_name.rsplit("-every-", 1)[0]
    if scheduler_name.endswith("-daily"):
        job_name = scheduler_name[:-len("-daily")]
    uri = (
        f"https://{region}-run.googleapis.com/apis/run.googleapis.com/v1/"
        f"namespaces/{os.environ['FAKE_PROJECT_ID']}/jobs/{job_name}:run"
    )
    emit(
        {
            "state": "ENABLED",
            "httpTarget": {
                "uri": uri,
                "httpMethod": "POST",
                "oauthToken": {
                    "serviceAccountEmail": scheduler_identities[key]
                },
            },
        }
    )
if args[:2] == ["secrets", "list"]:
    emit(json.loads(os.environ["FAKE_SECRET_INVENTORY"]))
if args[:2] == ["secrets", "get-iam-policy"]:
    policies = json.loads(os.environ["FAKE_SECRET_POLICIES"])
    if args[2] not in policies:
        raise SystemExit(3)
    emit(policies[args[2]])
if "get-iam-policy" in args:
    emit(json.loads(os.environ["FAKE_RESOURCE_POLICY"]))

print("unexpected fake gc invocation: " + " ".join(args), file=sys.stderr)
raise SystemExit(2)
'''

SHELL_HARNESS = r'''
gc() {
  "$FAKE_GCLOUD" "$@"
}

verify_exact_unconditional_roles() {
  local label="$1"
  local member="$2"
  local expected="$3"
  local policy
  local actual
  shift 3
  policy="$("$@")" || return 1
  actual="$(printf '%s' "$policy" | python3 -c '
import json
import sys

policy = json.load(sys.stdin)
member = sys.argv[1]
tokens = []
for binding in policy.get("bindings") or []:
    if member not in (binding.get("members") or []):
        continue
    prefix = "conditional:" if binding.get("condition") is not None else ""
    tokens.append(prefix + str(binding.get("role") or ""))
print("\n".join(sorted(tokens)))
' "$member")" || return 1
  if [ "$actual" != "$expected" ]; then
    echo "ERROR: ${label} roles are ${actual}, expected ${expected}" >&2
    return 1
  fi
}

verify_all_synthetic_retirement_contracts() {
  verify_synthetic_retirement_identity_ready || return 1
  verify_legacy_synthetic_secret_access_ready || return 1
  verify_legacy_synthetic_jobs_ready
}
'''


def _valid_secret_policy() -> dict[str, object]:
    return {
        "bindings": [
            {
                "role": "roles/secretmanager.secretAccessor",
                "members": [
                    f"serviceAccount:{INTERNAL_IDENTITY}",
                    f"serviceAccount:{SYNTHETIC_IDENTITY}",
                ],
            }
        ]
    }


def _base_state() -> dict[str, str]:
    inventory = [
        {
            "metadata": {
                "name": job_name,
                "labels": {"cloud.googleapis.com/location": region},
            }
        }
        for region, job_name, _ in CANONICAL_JOBS
    ]
    job_identities = {
        f"{region}/{job_name}": SYNTHETIC_IDENTITY
        for region, job_name, _ in CANONICAL_JOBS
    }
    scheduler_identities = {
        f"{region}/{scheduler_name}": SYNTHETIC_IDENTITY
        for region, _, scheduler_name in CANONICAL_JOBS
    }
    return {
        "FAKE_ACCOUNT_JSON": json.dumps(
            {"email": SYNTHETIC_IDENTITY, "disabled": False}
        ),
        "FAKE_ACCOUNT_POLICY": json.dumps(
            {
                "bindings": [
                    {
                        "role": "roles/iam.serviceAccountUser",
                        "members": [f"serviceAccount:{DEPLOY_IDENTITY}"],
                    }
                ]
            }
        ),
        "FAKE_ANCESTORS": json.dumps(
            [
                {"type": "project", "id": PROJECT_ID},
                {"type": "folder", "id": "456789"},
                {"type": "organization", "id": "987654"},
            ]
        ),
        "FAKE_FOLDER_POLICY": json.dumps({"bindings": []}),
        "FAKE_ORGANIZATION_POLICY": json.dumps({"bindings": []}),
        "FAKE_JOB_INVENTORY": json.dumps(inventory),
        "FAKE_JOB_IDENTITIES": json.dumps(job_identities),
        "FAKE_JOB_POLICY": json.dumps(
            {
                "bindings": [
                    {
                        "role": "roles/run.invoker",
                        "members": [f"serviceAccount:{SYNTHETIC_IDENTITY}"],
                    }
                ]
            }
        ),
        "FAKE_SCHEDULER_IDENTITIES": json.dumps(scheduler_identities),
        "FAKE_SECRET_INVENTORY": json.dumps(
            [
                {"name": "trustedrouter-observer-internal-token"},
                {"name": "trustedrouter-synthetic-monitor-api-key"},
                {"name": "owner-managed-existing-secret"},
            ]
        ),
        "FAKE_SECRET_POLICIES": json.dumps(
            {
                "trustedrouter-observer-internal-token": _valid_secret_policy(),
                "trustedrouter-synthetic-monitor-api-key": _valid_secret_policy(),
                "owner-managed-existing-secret": {
                    "bindings": [
                        {
                            "role": "roles/secretmanager.viewer",
                            "members": ["group:unrelated-owners@example.com"],
                        }
                    ]
                },
            }
        ),
        "FAKE_RESOURCE_POLICY": json.dumps({"bindings": []}),
    }


def _run_retirement_helper(
    tmp_path: Path,
    function_name: str,
    *,
    state_updates: dict[str, str] | None = None,
    synthetic_identity: str = SYNTHETIC_IDENTITY,
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_gcloud = tmp_path / "fake-gc"
    fake_gcloud.write_text(FAKE_GCLOUD, encoding="utf-8")
    fake_gcloud.chmod(0o755)
    helpers = tmp_path / "retirement-helpers.sh"
    helpers.write_text(RETIREMENT_HELPERS + SHELL_HARNESS, encoding="utf-8")

    env = {
        **os.environ,
        **_base_state(),
        "FAKE_GCLOUD": str(fake_gcloud),
        "FAKE_PROJECT_ID": PROJECT_ID,
        "PROJECT_ID": PROJECT_ID,
        "PROJECT_NUMBER": "123456789",
        "RUN_SERVICE_ACCOUNT": LEGACY_IDENTITY,
        "SYNTHETIC_RUN_SERVICE_ACCOUNT": synthetic_identity,
        "INTERNAL_RUN_SERVICE_ACCOUNT": INTERNAL_IDENTITY,
        "DEPLOY_SERVICE_ACCOUNT": DEPLOY_IDENTITY,
        "SPANNER_INSTANCE_ID": "trusted-router",
        "SPANNER_DATABASE_ID": "trusted-router",
        "BIGTABLE_INSTANCE_ID": "trusted-router",
        "BIGTABLE_GENERATION_TABLE": "generations",
        "KMS_KEYRING_ID": "trusted-router",
        "BYOK_KMS_KEY_ID": "byok",
        "GOOGLE_ADS_KMS_KEY_ID": "google-ads",
        "REGION": "us-central1",
        "TR_SYNTHETIC_MONITOR_REGIONS": "us-central1",
        "TR_SYNTHETIC_THROUGHPUT_REGION": "europe-west1",
        "TR_SYNTHETIC_IMAGE_REGION": "asia-northeast1",
        "TR_SYNTHETIC_VIDEO_REGION": "us-east1",
    }
    env.update(state_updates or {})
    return subprocess.run(  # noqa: S603 - fixed shell and test-owned helper
        [
            "/bin/bash",
            "-c",
            'set -uo pipefail; source "$1"; "$2"',
            "bash",
            str(helpers),
            function_name,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_legacy_retirement_requires_a_separate_canonical_synthetic_identity() -> None:
    assert (
        'SYNTHETIC_RUN_SERVICE_ACCOUNT="${TR_SYNTHETIC_RUN_SERVICE_ACCOUNT:-'
        'tr-synthetic@${PROJECT_ID}.iam.gserviceaccount.com}"' in LIB
    )
    assert (
        'ensure_runtime_service_account "$SYNTHETIC_RUN_SERVICE_ACCOUNT" '
        '"TrustedRouter synthetic jobs"' in INFRA
    )
    assert "verify_synthetic_service_account_policy preflight" in INFRA
    assert "verify_synthetic_service_account_policy post" in INFRA
    assert INFRA.count("verify_synthetic_data_iam_empty") >= 3
    preflight_section = INFRA.index(
        'log "preflighting all runtime IAM removal targets and direct ancestor policies"'
    )
    preflight = INFRA.index(
        "  verify_synthetic_retirement_identity_ready", preflight_section
    )
    first_legacy_removal = INFRA.index(
        'remove_project_role_if_present "$member" "roles/secretmanager.secretAccessor"',
        INFRA.index(
            'if [ "$TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM" = "1" ]; then',
            INFRA.index('log "removing broad legacy'),
        ),
    )
    assert preflight < first_legacy_removal
    jobs = _function_body(
        INFRA,
        "verify_legacy_synthetic_jobs_ready",
        "describe_iam_role_definition",
    )
    assert 'local member="serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"' in jobs
    assert 'if [ "$identity" = "$RUN_SERVICE_ACCOUNT" ]' in jobs
    assert '"$SYNTHETIC_RUN_SERVICE_ACCOUNT" \\' in jobs


def test_synthetic_retirement_contract_accepts_only_the_narrow_migrated_state(
    tmp_path: Path,
) -> None:
    run = _run_retirement_helper(
        tmp_path, "verify_all_synthetic_retirement_contracts"
    )

    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize("canonical_index", range(len(CANONICAL_JOBS)))
def test_each_canonical_job_still_using_legacy_blocks_retirement(
    tmp_path: Path,
    canonical_index: int,
) -> None:
    region, job_name, _ = CANONICAL_JOBS[canonical_index]
    identities = json.loads(_base_state()["FAKE_JOB_IDENTITIES"])
    identities[f"{region}/{job_name}"] = LEGACY_IDENTITY

    run = _run_retirement_helper(
        tmp_path,
        "verify_legacy_synthetic_jobs_ready",
        state_updates={"FAKE_JOB_IDENTITIES": json.dumps(identities)},
    )

    assert run.returncode != 0
    assert "still uses legacy identity" in run.stderr


def test_unaccounted_job_using_legacy_also_blocks_retirement(tmp_path: Path) -> None:
    state = _base_state()
    inventory = json.loads(state["FAKE_JOB_INVENTORY"])
    inventory.append(
        {
            "metadata": {
                "name": "forgotten-synthetic",
                "labels": {"cloud.googleapis.com/location": "australia-southeast1"},
            }
        }
    )
    identities = json.loads(state["FAKE_JOB_IDENTITIES"])
    identities["australia-southeast1/forgotten-synthetic"] = LEGACY_IDENTITY

    run = _run_retirement_helper(
        tmp_path,
        "verify_legacy_synthetic_jobs_ready",
        state_updates={
            "FAKE_JOB_INVENTORY": json.dumps(inventory),
            "FAKE_JOB_IDENTITIES": json.dumps(identities),
        },
    )

    assert run.returncode != 0
    assert "forgotten-synthetic still uses legacy identity" in run.stderr


def test_canonical_scheduler_using_legacy_blocks_retirement(tmp_path: Path) -> None:
    region, _, scheduler_name = CANONICAL_JOBS[0]
    schedulers = json.loads(_base_state()["FAKE_SCHEDULER_IDENTITIES"])
    schedulers[f"{region}/{scheduler_name}"] = LEGACY_IDENTITY

    run = _run_retirement_helper(
        tmp_path,
        "verify_legacy_synthetic_jobs_ready",
        state_updates={"FAKE_SCHEDULER_IDENTITIES": json.dumps(schedulers)},
    )

    assert run.returncode != 0
    assert "canonical synthetic scheduler" in run.stderr
    assert "is unsafe" in run.stderr


def test_legacy_job_invoker_binding_blocks_retirement(tmp_path: Path) -> None:
    policy = {
        "bindings": [
            {
                "role": "roles/run.invoker",
                "members": [
                    f"serviceAccount:{SYNTHETIC_IDENTITY}",
                    f"serviceAccount:{LEGACY_IDENTITY}",
                ],
            }
        ]
    }

    run = _run_retirement_helper(
        tmp_path,
        "verify_legacy_synthetic_jobs_ready",
        state_updates={"FAKE_JOB_POLICY": json.dumps(policy)},
    )

    assert run.returncode != 0
    assert "must have only the dedicated synthetic invoker" in run.stderr


def test_legacy_secret_consumer_blocks_retirement(tmp_path: Path) -> None:
    policy = _valid_secret_policy()
    members = policy["bindings"][0]["members"]  # type: ignore[index]
    assert isinstance(members, list)
    members[1] = f"serviceAccount:{LEGACY_IDENTITY}"
    policies = json.loads(_base_state()["FAKE_SECRET_POLICIES"])
    policies["trustedrouter-observer-internal-token"] = policy

    run = _run_retirement_helper(
        tmp_path,
        "verify_legacy_synthetic_secret_access_ready",
        state_updates={"FAKE_SECRET_POLICIES": json.dumps(policies)},
    )

    assert run.returncode != 0
    assert "exactly the internal and dedicated synthetic consumers" in run.stderr


def test_unknown_secret_with_synthetic_accessor_blocks_retirement(
    tmp_path: Path,
) -> None:
    inventory = json.loads(_base_state()["FAKE_SECRET_INVENTORY"])
    inventory.append({"name": "owner-managed-future-secret"})
    policies = json.loads(_base_state()["FAKE_SECRET_POLICIES"])
    policies["owner-managed-future-secret"] = {
        "bindings": [
            {
                "role": "roles/secretmanager.secretAccessor",
                "members": [f"serviceAccount:{SYNTHETIC_IDENTITY}"],
            },
            {
                "role": "roles/secretmanager.viewer",
                "members": ["group:unrelated-owners@example.com"],
            },
        ]
    }

    run = _run_retirement_helper(
        tmp_path,
        "verify_legacy_synthetic_secret_access_ready",
        state_updates={
            "FAKE_SECRET_INVENTORY": json.dumps(inventory),
            "FAKE_SECRET_POLICIES": json.dumps(policies),
        },
    )

    assert run.returncode != 0
    assert "non-synthetic secret owner-managed-future-secret roles are" in run.stderr
    assert "roles/secretmanager.secretAccessor" in run.stderr


def test_unapproved_or_broad_synthetic_identity_blocks_retirement(
    tmp_path: Path,
) -> None:
    unapproved = _run_retirement_helper(
        tmp_path / "unapproved",
        "verify_synthetic_retirement_identity_ready",
        synthetic_identity=f"custom-monitor@{PROJECT_ID}.iam.gserviceaccount.com",
    )
    assert unapproved.returncode != 0
    assert "canonical dedicated synthetic identity" in unapproved.stderr

    broad_policy = {
        "bindings": [
            {
                "role": "roles/editor",
                "members": [f"serviceAccount:{SYNTHETIC_IDENTITY}"],
            }
        ]
    }
    broad = _run_retirement_helper(
        tmp_path / "broad",
        "verify_synthetic_retirement_identity_ready",
        state_updates={"FAKE_RESOURCE_POLICY": json.dumps(broad_policy)},
    )
    assert broad.returncode != 0
    assert "synthetic identity project roles are roles/editor" in broad.stderr


@pytest.mark.parametrize(
    ("policy_variable", "error_fragment"),
    (
        (
            "FAKE_FOLDER_POLICY",
            "synthetic identity inherited folder 456789 roles are roles/editor",
        ),
        (
            "FAKE_ORGANIZATION_POLICY",
            "synthetic identity inherited organization 987654 roles are roles/editor",
        ),
    ),
)
def test_direct_folder_or_organization_binding_blocks_retirement(
    tmp_path: Path,
    policy_variable: str,
    error_fragment: str,
) -> None:
    ancestor_policy = {
        "bindings": [
            {
                "role": "roles/editor",
                "members": [f"serviceAccount:{SYNTHETIC_IDENTITY}"],
            }
        ]
    }

    run = _run_retirement_helper(
        tmp_path,
        "verify_synthetic_retirement_identity_ready",
        state_updates={policy_variable: json.dumps(ancestor_policy)},
    )

    assert run.returncode != 0
    assert error_fragment in run.stderr
