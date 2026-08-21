from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/deploy/rollout_iam_verify.sh"
PROJECT = "quill-cloud-proxy"
SURFACES = ("public", "actions", "console", "chat", "webhooks", "internal")
SERVICES = {
    "public": "trusted-router-public",
    "actions": "trusted-router-actions",
    "console": "trusted-router-console",
    "chat": "trusted-router-chat",
    "webhooks": "trusted-router-webhooks",
    "internal": "trusted-router-billing",
}


def _account(surface: str) -> str:
    return f"tr-{surface}@{PROJECT}.iam.gserviceaccount.com"


def _member(surface: str) -> str:
    return f"serviceAccount:{_account(surface)}"


def _synthetic_member() -> str:
    return f"serviceAccount:tr-synthetic@{PROJECT}.iam.gserviceaccount.com"


def _binding(
    role: str,
    surfaces: tuple[str, ...],
    *,
    condition: dict[str, str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "role": role,
        "members": [_member(surface) for surface in surfaces],
    }
    if condition is not None:
        value["condition"] = condition
    return value


def _policy(*bindings: dict[str, Any]) -> dict[str, Any]:
    return {"bindings": list(bindings)}


def _candidate(surface: str) -> str:
    return f"{SERVICES[surface]}-iamverify"


def _revision(surface: str) -> dict[str, Any]:
    env: list[dict[str, Any]] = []
    if surface in {"public", "console"}:
        env.append(
            {
                "name": "TR_ATTRIBUTION_COOKIE_SECRET",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "trustedrouter-attribution-cookie-secret",
                        "key": "7",
                    }
                },
            }
        )
    return {
        "metadata": {"name": _candidate(surface)},
        "spec": {
            "serviceAccountName": _account(surface),
            "containers": [{"env": env}],
        },
    }


def _valid_state() -> dict[str, Any]:
    deploy_member = f"serviceAccount:tr-deploy@{PROJECT}.iam.gserviceaccount.com"
    return {
        "policies": {
            "project": _policy(
                _binding(
                    "roles/serviceusage.serviceUsageConsumer",
                    ("public", "console", "chat", "webhooks", "internal"),
                )
            ),
            "spanner_instance": _policy(),
            "spanner_database": _policy(
                _binding("roles/spanner.databaseReader", ("public", "chat")),
                _binding(
                    "roles/spanner.databaseUser",
                    ("console", "webhooks", "internal"),
                ),
            ),
            "bigtable_instance": _policy(
                _binding("roles/bigtable.reader", ("public", "console")),
                _binding("roles/bigtable.user", ("internal",)),
            ),
            "bigtable_table": _policy(),
            "keyring": _policy(),
            "byok": _policy(
                _binding(
                    "roles/cloudkms.cryptoKeyEncrypterDecrypter", ("console",)
                ),
                _binding("roles/cloudkms.cryptoKeyDecrypter", ("internal",)),
            ),
            "ads": _policy(
                _binding("roles/cloudkms.cryptoKeyEncrypter", ("console",))
            ),
            "folder": _policy(),
            "organization": _policy(),
        },
        "service_account_policies": {
            _account(surface): _policy(
                {
                    "role": "roles/iam.serviceAccountUser",
                    "members": [deploy_member],
                }
            )
            for surface in SURFACES
        }
        | {
            f"tr-synthetic@{PROJECT}.iam.gserviceaccount.com": _policy(
                {
                    "role": "roles/iam.serviceAccountUser",
                    "members": [deploy_member],
                }
            ),
            f"unrelated-build@{PROJECT}.iam.gserviceaccount.com": _policy(),
        },
        "inventories": {
            "spanner_instances": ["trusted-router-nam6"],
            "spanner_databases": {"trusted-router-nam6": ["trusted-router"]},
            "bigtable_instances": ["trusted-router-logs"],
            "bigtable_tables": {
                "trusted-router-logs": ["trustedrouter-generations"]
            },
            "kms_locations": ["us-central1"],
            "kms_keyrings": {"us-central1": ["trusted-router"]},
            "kms_keys": {
                "us-central1/trusted-router": [
                    "byok-envelope",
                    "google-ads-click-envelope",
                ]
            },
            "service_accounts": [
                *[_account(surface) for surface in SURFACES],
                f"tr-synthetic@{PROJECT}.iam.gserviceaccount.com",
                f"unrelated-build@{PROJECT}.iam.gserviceaccount.com",
            ],
        },
        "inventory_policies": {},
        "secrets": {
            "trustedrouter-attribution-cookie-secret": _policy(
                _binding(
                    "roles/secretmanager.secretAccessor", ("public", "console")
                )
            ),
            "unrelated-build-secret": _policy(),
        },
        "versions": {
            "trustedrouter-attribution-cookie-secret|7": "ENABLED",
        },
        "revisions": {
            _candidate(surface): _revision(surface) for surface in SURFACES
        },
        "roles": {},
        "bucket_metadata": {
            "name": "trusted-router-rollout-recovery",
            "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True},
                "publicAccessPrevention": "enforced",
            },
            "versioning": {"enabled": True},
            "retentionPolicy": {"retentionPeriod": "604800"},
        },
        "bucket_policy": _policy(),
    }


def _write_manifest(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "project_id": PROJECT,
                "regions": ["us-central1"],
                "internal_regions": ["us-central1"],
                "services": [
                    {
                        "surface": surface,
                        "name": SERVICES[surface],
                        "region": "us-central1",
                        "candidate_revision": _candidate(surface),
                        "runtime_service_account": _account(surface),
                    }
                    for surface in SURFACES
                ],
            }
        ),
        encoding="utf-8",
    )


FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = json.loads(Path(os.environ["FAKE_GCLOUD_STATE"]).read_text(encoding="utf-8"))
events = Path(os.environ["FAKE_GCLOUD_EVENTS"])
args = sys.argv[1:]
if args[:1] == ["--project"]:
    if len(args) < 2 or args[1] != "quill-cloud-proxy":
        raise SystemExit(81)
    args = args[2:]
with events.open("a", encoding="utf-8") as output:
    output.write("gcloud " + " ".join(args) + "\n")

mutating = {
    "add-iam-policy-binding", "remove-iam-policy-binding", "set-iam-policy",
    "create", "delete", "deploy", "import", "update", "update-traffic",
}
if any(item in mutating for item in args):
    print("mutation rejected", file=sys.stderr)
    raise SystemExit(90)

def option(name):
    for index, value in enumerate(args):
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
        if value == name and index + 1 < len(args):
            return args[index + 1]
    return None

def finish(value):
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, separators=(",", ":")))
    raise SystemExit(0)

def inventory_policy(key, fallback=None):
    if key in state["inventory_policies"]:
        return state["inventory_policies"][key]
    return {"bindings": []} if fallback is None else fallback

if args[:2] == ["projects", "describe"]:
    finish("123456789")
if args[:2] == ["projects", "get-iam-policy"]:
    finish(state["policies"]["project"])
if args[:2] == ["projects", "get-ancestors"]:
    finish([
        {"type": "project", "id": "quill-cloud-proxy"},
        {"type": "folder", "id": "1234"},
        {"type": "organization", "id": "5678"},
    ])
if args[:3] == ["resource-manager", "folders", "get-iam-policy"]:
    finish(state["policies"]["folder"])
if args[:2] == ["organizations", "get-iam-policy"]:
    finish(state["policies"]["organization"])
if args[:3] == ["spanner", "instances", "list"]:
    finish([
        {"name": f"projects/quill-cloud-proxy/instances/{name}"}
        for name in state["inventories"]["spanner_instances"]
    ])
if args[:3] == ["spanner", "instances", "get-iam-policy"]:
    name = args[3]
    fallback = state["policies"]["spanner_instance"] if name == "trusted-router-nam6" else None
    finish(inventory_policy(f"spanner_instance|{name}", fallback))
if args[:3] == ["spanner", "databases", "list"]:
    instance = option("--instance")
    finish([
        {"name": f"projects/quill-cloud-proxy/instances/{instance}/databases/{name}"}
        for name in state["inventories"]["spanner_databases"].get(instance, [])
    ])
if args[:3] == ["spanner", "databases", "get-iam-policy"]:
    name = args[3]
    instance = option("--instance")
    fallback = (
        state["policies"]["spanner_database"]
        if instance == "trusted-router-nam6" and name == "trusted-router"
        else None
    )
    finish(inventory_policy(f"spanner_database|{instance}|{name}", fallback))
if args[:3] == ["bigtable", "instances", "list"]:
    finish([
        {"name": f"projects/quill-cloud-proxy/instances/{name}"}
        for name in state["inventories"]["bigtable_instances"]
    ])
if args[:3] == ["bigtable", "instances", "get-iam-policy"]:
    name = args[3]
    fallback = state["policies"]["bigtable_instance"] if name == "trusted-router-logs" else None
    finish(inventory_policy(f"bigtable_instance|{name}", fallback))
if args[:3] == ["bigtable", "tables", "list"]:
    instance = option("--instances")
    finish([
        {"name": f"projects/quill-cloud-proxy/instances/{instance}/tables/{name}"}
        for name in state["inventories"]["bigtable_tables"].get(instance, [])
    ])
if args[:3] == ["bigtable", "tables", "get-iam-policy"]:
    name = args[3]
    instance = option("--instance")
    fallback = (
        state["policies"]["bigtable_table"]
        if instance == "trusted-router-logs" and name == "trustedrouter-generations"
        else None
    )
    finish(inventory_policy(f"bigtable_table|{instance}|{name}", fallback))
if args[:3] == ["kms", "locations", "list"]:
    finish([{"locationId": name} for name in state["inventories"]["kms_locations"]])
if args[:3] == ["kms", "keyrings", "list"]:
    location = option("--location")
    finish([
        {
            "name": (
                f"projects/quill-cloud-proxy/locations/{location}/keyRings/{name}"
            )
        }
        for name in state["inventories"]["kms_keyrings"].get(location, [])
    ])
if args[:3] == ["kms", "keyrings", "get-iam-policy"]:
    name = args[3]
    location = option("--location")
    fallback = (
        state["policies"]["keyring"]
        if location == "us-central1" and name == "trusted-router"
        else None
    )
    finish(inventory_policy(f"kms_keyring|{location}|{name}", fallback))
if args[:3] == ["kms", "keys", "list"]:
    location = option("--location")
    keyring = option("--keyring")
    finish([
        {
            "name": (
                f"projects/quill-cloud-proxy/locations/{location}/keyRings/"
                f"{keyring}/cryptoKeys/{name}"
            )
        }
        for name in state["inventories"]["kms_keys"].get(f"{location}/{keyring}", [])
    ])
if args[:3] == ["kms", "keys", "get-iam-policy"]:
    name = args[3]
    location = option("--location")
    keyring = option("--keyring")
    fallback = None
    if location == "us-central1" and keyring == "trusted-router":
        if name == "byok-envelope":
            fallback = state["policies"]["byok"]
        elif name == "google-ads-click-envelope":
            fallback = state["policies"]["ads"]
    finish(inventory_policy(f"kms_key|{location}|{keyring}|{name}", fallback))
if args[:3] == ["iam", "service-accounts", "list"]:
    finish([
        {
            "name": f"projects/quill-cloud-proxy/serviceAccounts/{100000000000000000000 + index}",
            "email": email,
            "uniqueId": str(100000000000000000000 + index),
        }
        for index, email in enumerate(state["inventories"]["service_accounts"])
    ])
if args[:3] == ["iam", "service-accounts", "describe"]:
    finish({"email": args[3], "disabled": False})
if args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
    finish(state["service_account_policies"][args[3]])
if args[:3] == ["iam", "roles", "describe"]:
    role = args[3]
    if role not in state["roles"]:
        matches = [value for name, value in state["roles"].items() if name.endswith("/roles/" + role)]
        if len(matches) != 1:
            raise SystemExit(83)
        finish(matches[0])
    finish(state["roles"][role])
if args[:3] == ["storage", "buckets", "get-iam-policy"]:
    finish(state["bucket_policy"])
if args[:3] == ["storage", "buckets", "describe"]:
    finish(state["bucket_metadata"])
if args[:2] == ["secrets", "list"]:
    finish([
        {"name": f"projects/quill-cloud-proxy/secrets/{name}"}
        for name in state["secrets"]
    ])
if args[:2] == ["secrets", "get-iam-policy"]:
    finish(state["secrets"][args[2]])
if args[:3] == ["secrets", "versions", "describe"]:
    secret = option("--secret")
    version = args[3]
    finish({
        "name": f"projects/quill-cloud-proxy/secrets/{secret}/versions/{version}",
        "state": state["versions"].get(f"{secret}|{version}", "MISSING"),
    })
if args[:3] == ["run", "revisions", "describe"]:
    finish(state["revisions"][args[3]])

print("unexpected fake gcloud command", file=sys.stderr)
raise SystemExit(82)
'''


def _run(
    tmp_path: Path,
    state: dict[str, Any],
    *,
    with_manifest: bool = True,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "gcloud"
    fake.write_text(FAKE_GCLOUD, encoding="utf-8")
    fake.chmod(0o755)
    state_path = tmp_path / "state.json"
    original_state = json.dumps(state, sort_keys=True)
    state_path.write_text(original_state, encoding="utf-8")
    events_path = tmp_path / "events.log"
    events_path.write_text("", encoding="utf-8")
    command = [str(VERIFIER), "--project", PROJECT]
    if with_manifest:
        manifest_path = tmp_path / "manifest.json"
        _write_manifest(manifest_path)
        command.extend(("--manifest", str(manifest_path)))
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_GCLOUD_STATE": str(state_path),
        "FAKE_GCLOUD_EVENTS": str(events_path),
        "REGION": "us-central1",
    }
    env.pop("TR_ROLLOUT_STATE_GCS_URI", None)
    env.pop("TR_ROLLOUT_STATE_GCS_ROLE", None)
    env.pop("TR_ROLLOUT_REQUIRE_RECOVERY_BUNDLE", None)
    env.pop("TR_ROLLOUT_RECOVERY_GCS_PREFIX", None)
    env.pop("TR_ROLLOUT_RECOVERY_GCS_ROLE", None)
    env.pop("TR_ROLLOUT_BUNDLE_GCS_URI", None)
    env.pop("TR_ROLLOUT_AUTHORITY_GCS_URI", None)
    env.update(extra_env or {})
    run = subprocess.run(  # noqa: S603 - repo-local script and isolated fake PATH
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert state_path.read_text(encoding="utf-8") == original_state
    events = events_path.read_text(encoding="utf-8").splitlines()
    forbidden = (
        " add-iam-policy-binding",
        " remove-iam-policy-binding",
        " set-iam-policy",
        " create ",
        " delete ",
        " deploy ",
        " import ",
        " update ",
        " update-traffic",
    )
    assert all(not any(token in event for token in forbidden) for event in events)
    return run, events


RECOVERY_BUCKET = "trusted-router-rollout-recovery"
RECOVERY_EPOCH = "manifest-20260821T120000Z-deadbeef"
RECOVERY_ROLE = f"projects/{PROJECT}/roles/trustedRouterRolloutRecovery"


def _recovery_env() -> dict[str, str]:
    prefix = f"gs://{RECOVERY_BUCKET}/trusted-router-rollouts/{PROJECT}"
    bundle = f"{prefix}/releases/{RECOVERY_EPOCH}"
    return {
        "TR_ROLLOUT_REQUIRE_RECOVERY_BUNDLE": "true",
        "TR_ROLLOUT_RECOVERY_GCS_PREFIX": prefix,
        "TR_ROLLOUT_RECOVERY_GCS_ROLE": RECOVERY_ROLE,
        "TR_ROLLOUT_BUNDLE_GCS_URI": bundle,
        "TR_ROLLOUT_AUTHORITY_GCS_URI": f"{prefix}/authority.json",
        "TR_ROLLOUT_STATE_GCS_URI": f"{bundle}/promotion-state.json",
        "TR_ROLLOUT_STATE_GCS_ROLE": RECOVERY_ROLE,
    }


def _state_with_exact_recovery() -> dict[str, Any]:
    state = _valid_state()
    state["roles"][RECOVERY_ROLE] = {
        "name": RECOVERY_ROLE,
        "deleted": False,
        "includedPermissions": [
            "storage.objects.create",
            "storage.objects.delete",
            "storage.objects.get",
        ],
    }
    authority_resource = (
        f"projects/_/buckets/{RECOVERY_BUCKET}/objects/"
        f"trusted-router-rollouts/{PROJECT}/authority.json"
    )
    bundle_resource_prefix = (
        f"projects/_/buckets/{RECOVERY_BUCKET}/objects/"
        f"trusted-router-rollouts/{PROJECT}/releases/{RECOVERY_EPOCH}/"
    )
    state["bucket_policy"] = _policy(
        {
            "role": RECOVERY_ROLE,
            "members": [
                f"serviceAccount:tr-deploy@{PROJECT}.iam.gserviceaccount.com"
            ],
            "condition": {
                "title": "trusted-router-rollout-recovery",
                "expression": (
                    f'resource.name == "{authority_resource}" || '
                    f'resource.name.startsWith("{bundle_resource_prefix}")'
                ),
            },
        }
    )
    return state


def test_happy_manifest_audits_every_scope_candidate_and_pinned_version(
    tmp_path: Path,
) -> None:
    run, events = _run(tmp_path, _valid_state())

    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "six-surface rollout IAM verification passed"
    joined = "\n".join(events)
    for command in (
        "projects get-iam-policy",
        "projects get-ancestors",
        "resource-manager folders get-iam-policy",
        "organizations get-iam-policy",
        "spanner instances get-iam-policy",
        "spanner databases get-iam-policy",
        "bigtable instances get-iam-policy",
        "bigtable tables get-iam-policy",
        "kms keyrings get-iam-policy",
        "kms keys get-iam-policy",
        "iam service-accounts get-iam-policy",
        "secrets list",
        "secrets get-iam-policy",
        "run revisions describe",
        "secrets versions describe 7",
    ):
        assert command in joined
    assert joined.count("run revisions describe") == 6


def test_stage_mode_never_reads_candidate_revisions_or_secret_versions(
    tmp_path: Path,
) -> None:
    run, events = _run(tmp_path, _valid_state(), with_manifest=False)

    assert run.returncode == 0, run.stderr
    assert not any("run revisions describe" in event for event in events)
    assert not any("secrets versions describe" in event for event in events)


@pytest.mark.parametrize(
    ("target", "principal"),
    (
        ("spanner_instance", "runtime"),
        ("spanner_database", "synthetic"),
        ("bigtable_instance", "synthetic"),
        ("bigtable_table", "runtime"),
        ("kms_keyring", "runtime"),
        ("kms_key", "synthetic"),
        ("service_account", "runtime"),
    ),
)
def test_full_project_inventory_rejects_grant_on_any_other_resource(
    tmp_path: Path,
    target: str,
    principal: str,
) -> None:
    state = _valid_state()
    member = _member("public") if principal == "runtime" else _synthetic_member()
    binding = {"role": "roles/viewer", "members": [member]}

    if target == "spanner_instance":
        state["inventories"]["spanner_instances"].append("archive-spanner")
        state["inventories"]["spanner_databases"]["archive-spanner"] = []
        state["inventory_policies"]["spanner_instance|archive-spanner"] = _policy(
            binding
        )
    elif target == "spanner_database":
        state["inventories"]["spanner_databases"]["trusted-router-nam6"].append(
            "archive-database"
        )
        state["inventory_policies"][
            "spanner_database|trusted-router-nam6|archive-database"
        ] = _policy(binding)
    elif target == "bigtable_instance":
        state["inventories"]["bigtable_instances"].append("archive-bigtable")
        state["inventories"]["bigtable_tables"]["archive-bigtable"] = []
        state["inventory_policies"]["bigtable_instance|archive-bigtable"] = _policy(
            binding
        )
    elif target == "bigtable_table":
        state["inventories"]["bigtable_tables"]["trusted-router-logs"].append(
            "archive-table"
        )
        state["inventory_policies"][
            "bigtable_table|trusted-router-logs|archive-table"
        ] = _policy(binding)
    elif target == "kms_keyring":
        state["inventories"]["kms_keyrings"]["us-central1"].append(
            "archive-keyring"
        )
        state["inventories"]["kms_keys"]["us-central1/archive-keyring"] = []
        state["inventory_policies"][
            "kms_keyring|us-central1|archive-keyring"
        ] = _policy(binding)
    elif target == "kms_key":
        state["inventories"]["kms_keys"]["us-central1/trusted-router"].append(
            "archive-key"
        )
        state["inventory_policies"][
            "kms_key|us-central1|trusted-router|archive-key"
        ] = _policy(binding)
    else:
        email = f"legacy-runtime@{PROJECT}.iam.gserviceaccount.com"
        state["inventories"]["service_accounts"].append(email)
        state["service_account_policies"][email] = _policy(
            {
                "role": "roles/iam.serviceAccountTokenCreator",
                "members": [member],
            }
        )

    run, events = _run(tmp_path, state, with_manifest=False)

    assert run.returncode != 0
    assert "unsafe six-runtime IAM" in run.stderr
    assert any(" list " in f" {event} " for event in events)


@pytest.mark.parametrize(
    "inventory",
    (
        "spanner_instances",
        "spanner_databases",
        "bigtable_instances",
        "bigtable_tables",
        "kms_locations",
        "kms_keyrings",
        "kms_keys",
        "service_accounts",
    ),
)
def test_full_project_inventory_requires_every_canonical_resource(
    tmp_path: Path,
    inventory: str,
) -> None:
    state = _valid_state()
    values = state["inventories"][inventory]
    if isinstance(values, list):
        values.clear()
    else:
        for resources in values.values():
            resources.clear()

    run, _ = _run(tmp_path, state, with_manifest=False)

    assert run.returncode != 0
    assert "absent from the project inventory" in run.stderr


def test_unknown_secret_with_direct_runtime_grant_is_rejected(tmp_path: Path) -> None:
    state = _valid_state()
    state["secrets"]["unrelated-build-secret"] = _policy(
        _binding("roles/secretmanager.secretAccessor", ("public",))
    )

    run, _ = _run(tmp_path, state)

    assert run.returncode != 0
    assert "unsafe exact accessor IAM" in run.stderr
    assert "unrelated-build-secret" not in run.stdout + run.stderr


def test_unknown_secret_with_direct_synthetic_grant_is_rejected(tmp_path: Path) -> None:
    state = _valid_state()
    state["secrets"]["unrelated-build-secret"] = _policy(
        {
            "role": "roles/secretmanager.secretAccessor",
            "members": [_synthetic_member()],
        }
    )

    run, _ = _run(tmp_path, state)

    assert run.returncode != 0
    assert "unsafe exact accessor IAM" in run.stderr
    assert "unrelated-build-secret" not in run.stdout + run.stderr


def test_observer_secret_requires_exact_internal_and_synthetic_owners(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    state["secrets"]["trustedrouter-observer-internal-token"] = _policy(
        {
            "role": "roles/secretmanager.secretAccessor",
            "members": [_member("internal"), _synthetic_member()],
        }
    )

    run, _ = _run(tmp_path, state, with_manifest=False)

    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize(
    "target",
    (
        "project",
        "spanner_instance",
        "spanner_database",
        "bigtable_instance",
        "bigtable_table",
        "keyring",
        "byok",
        "ads",
        "folder",
        "organization",
    ),
)
def test_synthetic_identity_has_no_data_or_ancestor_role(
    tmp_path: Path,
    target: str,
) -> None:
    state = _valid_state()
    state["policies"][target]["bindings"].append(
        {
            "role": "roles/viewer",
            "members": [_synthetic_member()],
        }
    )

    run, _ = _run(tmp_path, state, with_manifest=False)

    assert run.returncode != 0
    assert "unsafe six-runtime IAM" in run.stderr


def test_synthetic_identity_cannot_impersonate_runtime_account(tmp_path: Path) -> None:
    state = _valid_state()
    state["service_account_policies"][_account("internal")]["bindings"].append(
        {
            "role": "roles/iam.serviceAccountTokenCreator",
            "members": [_synthetic_member()],
        }
    )

    run, _ = _run(tmp_path, state, with_manifest=False)

    assert run.returncode != 0
    assert "runtime service account has unsafe direct IAM" in run.stderr


def test_known_secret_rejects_non_owner_accessor_principal(tmp_path: Path) -> None:
    state = _valid_state()
    state["secrets"]["trustedrouter-attribution-cookie-secret"]["bindings"].append(
        {
            "role": "roles/secretmanager.secretAccessor",
            "members": ["serviceAccount:unrelated-build@quill-cloud-proxy.iam.gserviceaccount.com"],
        }
    )

    run, _ = _run(tmp_path, state)

    assert run.returncode != 0
    assert "unsafe exact accessor IAM" in run.stderr


@pytest.mark.parametrize(
    "target",
    ("project", "spanner_database", "runtime_account", "secret"),
)
def test_public_principal_is_rejected_on_every_policy_class(
    tmp_path: Path,
    target: str,
) -> None:
    state = _valid_state()
    binding = {"role": "roles/viewer", "members": ["allUsers"]}
    if target == "runtime_account":
        state["service_account_policies"][_account("public")]["bindings"].append(
            binding
        )
    elif target == "secret":
        state["secrets"]["unrelated-build-secret"]["bindings"].append(
            {
                "role": "roles/secretmanager.secretAccessor",
                "members": ["allAuthenticatedUsers"],
            }
        )
    else:
        state["policies"][target]["bindings"].append(binding)

    run, _ = _run(tmp_path, state)

    assert run.returncode != 0
    assert "public or malformed IAM policy" in run.stderr


@pytest.mark.parametrize("parent", ("spanner_instance", "folder"))
def test_parent_scope_runtime_role_is_rejected(tmp_path: Path, parent: str) -> None:
    state = _valid_state()
    state["policies"][parent] = _policy(
        _binding("roles/viewer", ("public",))
    )

    run, _ = _run(tmp_path, state)

    assert run.returncode != 0
    assert "IAM matrix drift" in run.stderr


def test_cross_runtime_service_account_binding_is_rejected(tmp_path: Path) -> None:
    state = _valid_state()
    state["service_account_policies"][_account("public")]["bindings"].append(
        {
            "role": "roles/iam.serviceAccountTokenCreator",
            "members": [_member("chat")],
        }
    )

    run, _ = _run(tmp_path, state)

    assert run.returncode != 0
    assert "split runtime principal" in run.stderr


@pytest.mark.parametrize("drift", ("conditional", "extra"))
def test_conditional_or_extra_deploy_binding_is_rejected(
    tmp_path: Path,
    drift: str,
) -> None:
    state = _valid_state()
    bindings = state["service_account_policies"][_account("public")]["bindings"]
    if drift == "conditional":
        bindings[0]["condition"] = {
            "title": "temporary",
            "expression": "request.time < timestamp('2030-01-01T00:00:00Z')",
        }
    else:
        bindings.append(
            {
                "role": "roles/iam.serviceAccountTokenCreator",
                "members": [f"serviceAccount:tr-deploy@{PROJECT}.iam.gserviceaccount.com"],
            }
        )

    run, _ = _run(tmp_path, state)

    assert run.returncode != 0
    assert "deploy principal" in run.stderr


@pytest.mark.parametrize("drift", ("disabled", "latest"))
def test_disabled_or_latest_mounted_secret_version_is_rejected(
    tmp_path: Path,
    drift: str,
) -> None:
    state = _valid_state()
    if drift == "disabled":
        state["versions"]["trustedrouter-attribution-cookie-secret|7"] = "DISABLED"
    else:
        reference = state["revisions"][_candidate("public")]["spec"]["containers"][0][
            "env"
        ][0]["valueFrom"]["secretKeyRef"]
        reference["key"] = "latest"

    run, _ = _run(tmp_path, state)

    assert run.returncode != 0
    assert "secret" in run.stderr.lower()
    assert "trustedrouter-attribution-cookie-secret" not in run.stdout + run.stderr


@pytest.mark.parametrize(
    "permission",
    (
        "storage.objects.get",
        "storage.objects.getIamPolicy",
        "secretmanager.versions.access",
        "cloudkms.cryptoKeyVersions.useToDecrypt",
        "iam.serviceAccounts.actAs",
    ),
)
def test_project_broad_data_or_impersonation_permission_on_deploy_is_rejected(
    tmp_path: Path,
    permission: str,
) -> None:
    state = _valid_state()
    deploy_member = f"serviceAccount:tr-deploy@{PROJECT}.iam.gserviceaccount.com"
    state["policies"]["project"]["bindings"].append(
        {"role": "roles/storage.objectAdmin", "members": [deploy_member]}
    )
    state["roles"]["roles/storage.objectAdmin"] = {
        "name": "roles/storage.objectAdmin",
        "includedPermissions": [permission],
    }

    run, events = _run(tmp_path, state)

    assert run.returncode != 0
    assert "broad data or impersonation permission at project" in run.stderr
    assert any(
        "iam roles describe roles/storage.objectAdmin" in event for event in events
    )


def test_exact_object_scoped_rollout_journal_role_and_bucket_binding_are_allowed(
    tmp_path: Path,
) -> None:
    state = _valid_state()
    role = f"projects/{PROJECT}/roles/trustedRouterRolloutJournal"
    uri = "gs://trusted-router-rollout-state/releases/state.json"
    deploy_member = f"serviceAccount:tr-deploy@{PROJECT}.iam.gserviceaccount.com"
    state["roles"][role] = {
        "name": role,
        "deleted": False,
        "includedPermissions": [
            "storage.objects.get",
            "storage.objects.create",
            "storage.objects.delete",
        ],
    }
    state["bucket_policy"] = _policy(
        {
            "role": role,
            "members": [deploy_member],
            "condition": {
                "title": "trusted-router-rollout-journal",
                "description": "forward-only rollout recovery journal",
                "expression": (
                    'resource.name == "projects/_/buckets/'
                    'trusted-router-rollout-state/objects/releases/state.json"'
                ),
            },
        }
    )

    run, events = _run(
        tmp_path,
        state,
        extra_env={
            "TR_ROLLOUT_STATE_GCS_URI": uri,
            "TR_ROLLOUT_STATE_GCS_ROLE": role,
        },
    )

    assert run.returncode == 0, run.stderr
    assert any("storage buckets get-iam-policy" in event for event in events)
    assert any(
        "iam roles describe trustedRouterRolloutJournal" in event for event in events
    )


def test_rollout_journal_bucket_condition_drift_is_rejected(tmp_path: Path) -> None:
    state = _valid_state()
    role = f"projects/{PROJECT}/roles/trustedRouterRolloutJournal"
    state["roles"][role] = {
        "name": role,
        "includedPermissions": [
            "storage.objects.get",
            "storage.objects.create",
            "storage.objects.delete",
        ],
    }
    state["bucket_policy"] = _policy(
        {
            "role": role,
            "members": [
                f"serviceAccount:tr-deploy@{PROJECT}.iam.gserviceaccount.com"
            ],
            "condition": {
                "title": "trusted-router-rollout-journal",
                "expression": (
                    'resource.name.startsWith("projects/_/buckets/'
                    'trusted-router-rollout-state/objects/")'
                ),
            },
        }
    )

    run, _ = _run(
        tmp_path,
        state,
        extra_env={
            "TR_ROLLOUT_STATE_GCS_URI": (
                "gs://trusted-router-rollout-state/releases/state.json"
            ),
            "TR_ROLLOUT_STATE_GCS_ROLE": role,
        },
    )

    assert run.returncode != 0
    assert "exact object-scoped binding" in run.stderr


def test_recovery_bundle_iam_is_read_only_and_exactly_object_scoped(
    tmp_path: Path,
) -> None:
    run, events = _run(
        tmp_path,
        _state_with_exact_recovery(),
        extra_env=_recovery_env(),
    )

    assert run.returncode == 0, run.stderr
    joined = "\n".join(events)
    assert "storage buckets describe" in joined
    assert "storage buckets get-iam-policy" in joined
    assert "iam roles describe trustedRouterRolloutRecovery" in joined


def test_recovery_bundle_uses_recovery_role_without_legacy_state_role(
    tmp_path: Path,
) -> None:
    env = _recovery_env()
    env.pop("TR_ROLLOUT_STATE_GCS_ROLE")

    run, _ = _run(
        tmp_path,
        _state_with_exact_recovery(),
        extra_env=env,
    )

    assert run.returncode == 0, run.stderr


def test_recovery_bundle_rejects_broad_project_prefix_binding(tmp_path: Path) -> None:
    state = _state_with_exact_recovery()
    state["bucket_policy"]["bindings"][0]["condition"]["expression"] = (
        'resource.name.startsWith("projects/_/buckets/'
        f'{RECOVERY_BUCKET}/objects/trusted-router-rollouts/{PROJECT}/")'
    )

    run, _ = _run(tmp_path, state, extra_env=_recovery_env())

    assert run.returncode != 0
    assert "exact authority-and-bundle binding" in run.stderr


@pytest.mark.parametrize("principal", ("allUsers", "group:unknown@example.com"))
def test_recovery_bundle_rejects_public_and_unknown_principals(
    tmp_path: Path,
    principal: str,
) -> None:
    state = _state_with_exact_recovery()
    state["bucket_policy"]["bindings"].append(
        {"role": "roles/storage.objectViewer", "members": [principal]}
    )

    run, _ = _run(tmp_path, state, extra_env=_recovery_env())

    assert run.returncode != 0
    if principal == "allUsers":
        assert "public or malformed IAM policy" in run.stderr
    else:
        assert "exact authority-and-bundle binding" in run.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("uniform", False),
        ("public_prevention", "inherited"),
        ("versioning", False),
        ("retention", "604799"),
    ),
)
def test_recovery_bundle_rejects_weakened_bucket_protection(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    state = _state_with_exact_recovery()
    metadata = state["bucket_metadata"]
    if field == "uniform":
        metadata["iamConfiguration"]["uniformBucketLevelAccess"]["enabled"] = value
    elif field == "public_prevention":
        metadata["iamConfiguration"]["publicAccessPrevention"] = value
    elif field == "versioning":
        metadata["versioning"]["enabled"] = value
    else:
        metadata["retentionPolicy"]["retentionPeriod"] = value

    run, _ = _run(tmp_path, state, extra_env=_recovery_env())

    assert run.returncode != 0
    assert "bucket protection contract drifted" in run.stderr


def test_recovery_bundle_rejects_role_permission_expansion(tmp_path: Path) -> None:
    state = _state_with_exact_recovery()
    state["roles"][RECOVERY_ROLE]["includedPermissions"].append(
        "storage.objects.list"
    )

    run, _ = _run(tmp_path, state, extra_env=_recovery_env())

    assert run.returncode != 0
    assert "exact three-permission role" in run.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    (
        (
            "TR_ROLLOUT_AUTHORITY_GCS_URI",
            f"gs://{RECOVERY_BUCKET}/trusted-router-rollouts/{PROJECT}/other.json",
        ),
        (
            "TR_ROLLOUT_BUNDLE_GCS_URI",
            f"gs://{RECOVERY_BUCKET}/trusted-router-rollouts/{PROJECT}/releases/short",
        ),
        (
            "TR_ROLLOUT_STATE_GCS_URI",
            f"gs://{RECOVERY_BUCKET}/trusted-router-rollouts/{PROJECT}/promotion-state.json",
        ),
        (
            "TR_ROLLOUT_STATE_GCS_ROLE",
            f"projects/{PROJECT}/roles/aDifferentRolloutRole",
        ),
    ),
)
def test_recovery_bundle_rejects_noncanonical_authority_bundle_state_or_role(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    env = _recovery_env()
    env[name] = value

    run, events = _run(
        tmp_path,
        _state_with_exact_recovery(),
        extra_env=env,
    )

    assert run.returncode != 0
    assert not any("storage buckets" in event for event in events)
