from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = (ROOT / "scripts/deploy/_lib.sh").read_text(encoding="utf-8")
INFRA = (ROOT / "scripts/deploy/infra.sh").read_text(encoding="utf-8")
SECRETS = (ROOT / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
SYNTHETIC = (ROOT / "scripts/deploy/synthetic.sh").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs/runbooks/ddos-edge-hardening.md").read_text(encoding="utf-8")
PRICE_REFRESH = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
    encoding="utf-8"
)

RUNTIME_VARIABLES = (
    "PUBLIC_RUN_SERVICE_ACCOUNT",
    "ACTIONS_RUN_SERVICE_ACCOUNT",
    "CONSOLE_RUN_SERVICE_ACCOUNT",
    "CHAT_RUN_SERVICE_ACCOUNT",
    "WEBHOOKS_RUN_SERVICE_ACCOUNT",
    "INTERNAL_RUN_SERVICE_ACCOUNT",
)


def _shell_words(source: str) -> str:
    return " ".join(source.replace("\\\n", " ").split())


def _function_body(source: str, name: str, next_name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index(f"{next_name}() {{", start)
    return source[start:end]


def _infra_is_resource_scoped(source: str) -> bool:
    words = _shell_words(source)
    required = (
        'gc spanner databases add-iam-policy-binding "$SPANNER_DATABASE_ID"',
        'gc bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE_ID"',
        'gc kms keys add-iam-policy-binding "$BYOK_KMS_KEY_ID"',
        'gc kms keys add-iam-policy-binding "$GOOGLE_ADS_KMS_KEY_ID"',
        '"$PUBLIC_RUN_SERVICE_ACCOUNT" "$CHAT_RUN_SERVICE_ACCOUNT"; do '
        'member="serviceAccount:${service_account}"',
        '"$CONSOLE_RUN_SERVICE_ACCOUNT" "$WEBHOOKS_RUN_SERVICE_ACCOUNT" '
        '"$INTERNAL_RUN_SERVICE_ACCOUNT"; do',
        '"roles/spanner.databaseReader"',
        '"roles/spanner.databaseUser"',
        '"roles/bigtable.reader"',
        '"roles/bigtable.user"',
        '--member="serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" '
        '--role="roles/cloudkms.cryptoKeyEncrypterDecrypter"',
        '--member="serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" '
        '--role="roles/cloudkms.cryptoKeyDecrypter"',
    )
    forbidden = (
        'ensure_project_role "$member" "roles/spanner.',
        'ensure_project_role "$member" "roles/bigtable.',
        'ensure_project_role "$member" "roles/cloudkms.',
        'ensure_project_role "$member" "roles/secretmanager.secretAccessor"',
        'gc projects add-iam-policy-binding "$PROJECT_ID" --member="$member" '
        '--role="roles/spanner.',
        'gc projects add-iam-policy-binding "$PROJECT_ID" --member="$member" '
        '--role="roles/bigtable.',
    )
    return all(item in words for item in required) and not any(
        item in words for item in forbidden
    )


def _anti_reuse_contract_is_complete(source: str) -> bool:
    words = _shell_words(source)
    checks = (
        'case "$_internal_stripe_key" in rk_live_?*)',
        '[ "$_internal_stripe_key" = "$_console_stripe_key" ]',
        '[ "$_internal_ses_access_key_id" = "$_shared_ses_access_key_id" ]',
        '[ "$_internal_ses_secret_access_key" = "$_shared_ses_secret_access_key" ]',
        '[ "$_observer_token_check" = "$_gateway_token_check" ]',
        '[ "$_attribution_token_check" = "$_gateway_token_check" ]',
        '[ "$_attribution_token_check" = "$_observer_token_check" ]',
        '[ "$_monitor_token_check" = "$_observer_token_check" ]',
        '[ "$_monitor_token_check" = "$_gateway_token_check" ]',
        '[ "$_monitor_token_check" = "$_attribution_token_check" ]',
    )
    secret_values = (
        "$_internal_stripe_key",
        "$_console_stripe_key",
        "$_internal_ses_access_key_id",
        "$_internal_ses_secret_access_key",
        "$_shared_ses_access_key_id",
        "$_shared_ses_secret_access_key",
        "$_observer_token_check",
        "$_gateway_token_check",
        "$_attribution_token_check",
        "$_monitor_token_check",
    )
    logged_lines = "\n".join(
        line for line in source.splitlines() if "echo " in line or "log " in line
    )
    return all(check in words for check in checks) and not any(
        value in logged_lines for value in secret_values
    )


def _desired_runtime_grants_precede_obsolete_removals(source: str) -> bool:
    words = _shell_words(source)
    marker = "granting and verifying desired runtime IAM before obsolete-role cleanup"
    if marker not in words:
        return False
    tail = words[words.index(marker) :]
    contracts = (
        (
            (
                'gc kms keys add-iam-policy-binding "$GOOGLE_ADS_KMS_KEY_ID"',
            ),
            'verify_resource_role_present "Google Ads KMS key"',
            'remove_kms_role_if_present "$member" "$role" "$GOOGLE_ADS_KMS_KEY_ID"',
            'verify_only_resource_role "Google Ads KMS key"',
        ),
        (
            (
                'ensure_project_role "$member" "roles/serviceusage.serviceUsageConsumer"',
            ),
            'verify_resource_role_present "project" "$member" '
            '"roles/serviceusage.serviceUsageConsumer"',
            'remove_project_role_if_present "$member" '
            '"roles/serviceusage.serviceUsageConsumer"',
            'verify_only_resource_role "project" "$member" "$expected_project_role"',
        ),
        (
            (
                'gc spanner databases add-iam-policy-binding "$SPANNER_DATABASE_ID" '
                '--instance="$SPANNER_INSTANCE_ID" --member="$member" '
                '--role="roles/spanner.databaseReader"',
                'gc spanner databases add-iam-policy-binding "$SPANNER_DATABASE_ID" '
                '--instance="$SPANNER_INSTANCE_ID" --member="$member" '
                '--role="roles/spanner.databaseUser"',
            ),
            'verify_resource_role_present "Spanner database"',
            'remove_spanner_role_if_present "$member"',
            'verify_only_resource_role "Spanner database" "$member" '
            '"$expected_spanner_role"',
        ),
        (
            (
                'gc bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE_ID" '
                '--member="$member" --role="roles/bigtable.reader"',
                'gc bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE_ID" '
                '--member="$member" --role="roles/bigtable.user"',
            ),
            'verify_resource_role_present "Bigtable instance"',
            'remove_bigtable_role_if_present "$member"',
            'verify_only_resource_role "Bigtable instance" "$member" '
            '"$expected_bigtable_role"',
        ),
        (
            (
                'gc kms keys add-iam-policy-binding "$BYOK_KMS_KEY_ID" '
                '--keyring="$KMS_KEYRING_ID" --location="$REGION" '
                '--member="serviceAccount:${CONSOLE_RUN_SERVICE_ACCOUNT}" '
                '--role="roles/cloudkms.cryptoKeyEncrypterDecrypter"',
                'gc kms keys add-iam-policy-binding "$BYOK_KMS_KEY_ID" '
                '--keyring="$KMS_KEYRING_ID" --location="$REGION" '
                '--member="serviceAccount:${INTERNAL_RUN_SERVICE_ACCOUNT}" '
                '--role="roles/cloudkms.cryptoKeyDecrypter"',
            ),
            'verify_resource_role_present "BYOK KMS key"',
            'remove_kms_role_if_present "$member" "$role"',
            'verify_only_resource_role "BYOK KMS key"',
        ),
    )
    for grants, presence, removal, postverify in contracts:
        if any(grant not in tail for grant in grants):
            return False
        if presence not in tail or removal not in tail or postverify not in tail:
            return False
        removal_position = tail.index(removal)
        if any(tail.index(grant) >= removal_position for grant in grants):
            return False
        presence_position = tail.index(presence)
        if presence_position >= removal_position:
            return False
        if tail.index(postverify, removal_position) <= removal_position:
            return False
    return True


def test_six_runtime_service_accounts_are_distinct_and_never_legacy() -> None:
    array = re.search(r"RUNTIME_SERVICE_ACCOUNTS=\(\n(?P<body>.*?)\n\)", LIB, re.S)
    assert array is not None
    assert tuple(re.findall(r'"\$(\w+_RUN_SERVICE_ACCOUNT)"', array["body"])) == (
        RUNTIME_VARIABLES
    )
    assert "validate_runtime_service_accounts" in INFRA
    assert "validate_runtime_service_accounts" in SECRETS
    assert 'if [ "$service_account" = "$RUN_SERVICE_ACCOUNT" ]' in LIB
    assert "runtime service accounts must be distinct" in LIB
    assert "deploy service account must not be a runtime identity" in LIB
    expected_ids = {
        "PUBLIC": "tr-public",
        "ACTIONS": "tr-actions",
        "CONSOLE": "tr-console",
        "CHAT": "tr-chat",
        "WEBHOOKS": "tr-webhooks",
        "INTERNAL": "tr-internal",
    }
    for surface, account_id in expected_ids.items():
        assignment = (
            f'{surface}_RUN_SERVICE_ACCOUNT="'
            + "${TR_"
            + surface
            + "_RUN_SERVICE_ACCOUNT:-"
            + account_id
            + "@${PROJECT_ID}.iam.gserviceaccount.com}"
            + '"'
        )
        assert assignment in LIB
    assert (
        'DEPLOY_SERVICE_ACCOUNT="${TR_DEPLOY_SERVICE_ACCOUNT:-'
        'tr-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"' in LIB
    )
    assert "deploy service account must belong to ${PROJECT_ID}" in LIB


def test_deploy_actas_is_bound_to_the_six_runtime_accounts() -> None:
    words = _shell_words(INFRA)
    assert (
        'for service_account in "${RUNTIME_SERVICE_ACCOUNTS[@]}"; do gc iam '
        in words
    )
    assert (
        'service-accounts add-iam-policy-binding "$service_account" '
        '--member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" '
        '--role="roles/iam.serviceAccountUser"' in words
    )
    assert (
        'verify_only_resource_role "runtime service-account actAs" '
        '"serviceAccount:${DEPLOY_SERVICE_ACCOUNT}" '
        '"roles/iam.serviceAccountUser"' in words
    )
    # Non-surface actAs targets are the conversion job and dedicated synthetic Job.
    assert words.count('--role="roles/iam.serviceAccountUser"') == 3
    assert '"$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT"' in words
    assert '"$SYNTHETIC_RUN_SERVICE_ACCOUNT"' in words


def test_runtime_data_and_kms_roles_are_resource_scoped() -> None:
    assert _infra_is_resource_scoped(INFRA)


def test_desired_runtime_iam_is_granted_and_verified_before_cleanup() -> None:
    assert _desired_runtime_grants_precede_obsolete_removals(INFRA)


@pytest.mark.parametrize(
    "premature_removal",
    (
        'remove_kms_role_if_present "$member" "$role" "$GOOGLE_ADS_KMS_KEY_ID"',
        'remove_project_role_if_present "$member" '
        '"roles/serviceusage.serviceUsageConsumer"',
        'remove_spanner_role_if_present "$member" "roles/spanner.databaseReader"',
        'remove_bigtable_role_if_present "$member" "roles/bigtable.reader"',
        'remove_kms_role_if_present "$member" "$role"',
    ),
)
def test_remove_before_add_mutations_break_the_runtime_iam_contract(
    premature_removal: str,
) -> None:
    marker = 'log "granting and verifying desired runtime IAM before obsolete-role cleanup"'
    assert marker in INFRA
    mutated = INFRA.replace(marker, f"{marker}\n{premature_removal}", 1)
    assert not _desired_runtime_grants_precede_obsolete_removals(mutated)


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            'gc spanner databases add-iam-policy-binding "$SPANNER_DATABASE_ID"',
            'gc projects add-iam-policy-binding "$PROJECT_ID"',
        ),
        (
            'gc bigtable instances add-iam-policy-binding "$BIGTABLE_INSTANCE_ID"',
            'gc projects add-iam-policy-binding "$PROJECT_ID"',
        ),
        (
            'gc kms keys add-iam-policy-binding "$BYOK_KMS_KEY_ID"',
            'gc projects add-iam-policy-binding "$PROJECT_ID"',
        ),
        (
            '"$PUBLIC_RUN_SERVICE_ACCOUNT" "$CHAT_RUN_SERVICE_ACCOUNT"',
            '"$PUBLIC_RUN_SERVICE_ACCOUNT" "$ACTIONS_RUN_SERVICE_ACCOUNT"',
        ),
    ),
)
def test_resource_scope_mutations_break_the_contract(old: str, new: str) -> None:
    assert old in INFRA
    assert not _infra_is_resource_scoped(INFRA.replace(old, new))


def test_project_roles_are_complete_and_actions_has_none() -> None:
    words = _shell_words(INFRA)
    assert (
        '"$PUBLIC_RUN_SERVICE_ACCOUNT" "$CONSOLE_RUN_SERVICE_ACCOUNT" '
        '"$CHAT_RUN_SERVICE_ACCOUNT" "$WEBHOOKS_RUN_SERVICE_ACCOUNT" '
        '"$INTERNAL_RUN_SERVICE_ACCOUNT"; do ensure_project_role '
        '"serviceAccount:${service_account}" '
        '"roles/serviceusage.serviceUsageConsumer"' in words
    )
    assert (
        '"project" "serviceAccount:${ACTIONS_RUN_SERVICE_ACCOUNT}" "" '
        'gc projects get-iam-policy "$PROJECT_ID"' in words
    )
    for role in ("roles/editor", "roles/owner", "roles/run.developer"):
        assert f'remove_project_role_if_present "$member" "{role}"' in INFRA
    deploy_preflight = _function_body(
        INFRA,
        "verify_deploy_identity_has_no_project_data_roles",
        "preflight_identity_iam_removal_targets",
    )
    for role in (
        "roles/secretmanager.secretAccessor",
        "roles/secretmanager.admin",
        "roles/spanner.databaseReader",
        "roles/spanner.databaseUser",
        "roles/bigtable.reader",
        "roles/bigtable.user",
        "roles/cloudkms.cryptoKeyEncrypterDecrypter",
        "roles/iam.serviceAccountUser",
        "roles/iam.serviceAccountTokenCreator",
        "roles/editor",
        "roles/owner",
    ):
        assert role in deploy_preflight
    assert "remove-iam-policy-binding" not in deploy_preflight
    assert "describe_iam_role_definition" in deploy_preflight
    for permission in (
        "iam.serviceAccounts.actAs",
        "iam.serviceAccounts.getAccessToken",
        "secretmanager.versions.access",
        "cloudkms.cryptoKeyVersions.useToDecrypt",
    ):
        assert permission in deploy_preflight


def test_runtime_secret_reconciler_is_preflighted_targeted_and_exact() -> None:
    helper = _function_body(
        LIB,
        "secret_iam_policy_contract_json",
        "validate_nonempty_region_list",
    )
    body = _function_body(
        SECRETS,
        "reconcile_declared_secret_iam",
        "verify_runtime_secret_access",
    )
    assert "roles/secretmanager.secretAccessor" in helper
    assert "allUsers" in helper and "allAuthenticatedUsers" in helper
    assert "unapproved unrelated secret principal" in helper
    assert "TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON" in helper
    assert "secret_iam_policy_contract_json plan" in body
    assert "unknown secret ${secret_name}" in body
    assert "add-iam-policy-binding" in body
    assert "remove-iam-policy-binding" in body
    assert "set-iam-policy" not in body
    first_mutation = min(
        body.index("gc secrets add-iam-policy-binding"),
        body.index("gc secrets remove-iam-policy-binding"),
    )
    assert body.index('done <<<"$secret_names"') < first_mutation

    required_bindings = (
        "trustedrouter-attribution-cookie-secret",
        "trustedrouter-sentry-dsn",
        "trustedrouter-stripe-secret-key",
        "trustedrouter-stripe-webhook-secret",
        "trustedrouter-internal-stripe-payment-intents-key",
        "trustedrouter-aws-access-key-id",
        "trustedrouter-aws-secret-access-key",
        "trustedrouter-internal-ses-access-key-id",
        "trustedrouter-internal-ses-secret-access-key",
        "trustedrouter-internal-gateway-token",
        "trustedrouter-observer-internal-token",
        "trustedrouter-synthetic-monitor-api-key",
    )
    verification_tail = LIB[LIB.index("secret_expected_surfaces()") :]
    for secret_name in required_bindings:
        assert secret_name in verification_tail


@pytest.mark.parametrize(
    ("bindings", "error_fragment"),
    (
        (
            [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": [
                        "serviceAccount:tr-public@quill-cloud-proxy.iam.gserviceaccount.com"
                    ],
                    "condition": {"title": "expired", "expression": "false"},
                }
            ],
            "noncanonical role or condition",
        ),
        (
            [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": [
                        "serviceAccount:tr-public@quill-cloud-proxy.iam.gserviceaccount.com"
                    ],
                },
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": [
                        "serviceAccount:tr-actions@quill-cloud-proxy.iam.gserviceaccount.com"
                    ],
                    "condition": {"title": "temporary", "expression": "true"},
                },
            ],
            "noncanonical role or condition",
        ),
    ),
    ids=("conditional-owner", "conditional-non-owner"),
)
def test_runtime_secret_verifier_rejects_conditional_owner_and_nonowner_bindings(
    tmp_path: Path,
    bindings: list[dict[str, object]],
    error_fragment: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *\"projects describe\"* ]]; then\n"
        "  printf '123456789\\n'\n"
        "else\n"
        "  exit 2\n"
        "fi\n"
    )
    fake_gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_SECRET_POLICY": json.dumps(
            {"bindings": bindings}, separators=(",", ":")
        ),
    }
    run = subprocess.run(  # noqa: S603 - fixed shell and repo-local helpers
        [
            "/bin/bash",
            "-c",
            'source "$1"; printf "%s" "$FAKE_SECRET_POLICY" | '
            'secret_iam_policy_contract_json plan test-secret "public"',
            "bash",
            str(ROOT / "scripts/deploy/_lib.sh"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        check=False,
    )

    assert run.returncode != 0
    assert error_fragment in run.stderr


def _run_secret_iam_reconciler(
    tmp_path: Path,
    secrets: dict[str, dict[str, object]],
    *,
    preserved: dict[str, list[str]] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object], list[str]]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_path = tmp_path / "secret-state.json"
    events_path = tmp_path / "events.log"
    state_path.write_text(json.dumps(secrets, sort_keys=True), encoding="utf-8")
    events_path.write_text("", encoding="utf-8")
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["--project"]:
    args = args[2:]
state_path = Path(os.environ["FAKE_SECRET_STATE"])
events_path = Path(os.environ["FAKE_SECRET_EVENTS"])
with events_path.open("a", encoding="utf-8") as output:
    output.write(" ".join(args) + "\n")

def option(name):
    for index, value in enumerate(args):
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
        if value == name and index + 1 < len(args):
            return args[index + 1]
    return ""

if args[:2] == ["projects", "describe"]:
    print("123456789")
    raise SystemExit(0)
state = json.loads(state_path.read_text(encoding="utf-8"))
if args[:2] == ["secrets", "list"]:
    print(json.dumps([
        {"name": f"projects/quill-cloud-proxy/secrets/{name}"}
        for name in sorted(state)
    ], separators=(",", ":")))
    raise SystemExit(0)
if args[:2] == ["secrets", "get-iam-policy"]:
    print(json.dumps(state[args[2]], separators=(",", ":")))
    raise SystemExit(0)
if args[:2] in (
    ["secrets", "add-iam-policy-binding"],
    ["secrets", "remove-iam-policy-binding"],
):
    action = args[1].split("-", 1)[0]
    secret = args[2]
    member = option("--member")
    role = option("--role")
    if option("--condition") != "None":
        raise SystemExit(88)
    bindings = state[secret].setdefault("bindings", [])
    matches = [
        binding for binding in bindings
        if binding.get("role") == role and binding.get("condition") is None
    ]
    if action == "add":
        binding = matches[0] if matches else {"role": role, "members": []}
        if not matches:
            bindings.append(binding)
        if member in binding["members"]:
            raise SystemExit(89)
        binding["members"].append(member)
    else:
        found = False
        for binding in matches:
            if member in binding.get("members", []):
                binding["members"].remove(member)
                found = True
        if not found:
            raise SystemExit(90)
        state[secret]["bindings"] = [
            binding for binding in bindings if binding.get("members")
        ]
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    raise SystemExit(0)
print("unexpected fake gcloud call: " + " ".join(args), file=sys.stderr)
raise SystemExit(87)
''',
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    helper = tmp_path / "secret-reconciler.sh"
    helper.write_text(
        _function_body(
            SECRETS,
            "reconcile_declared_secret_iam",
            "verify_runtime_secret_access",
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_SECRET_STATE": str(state_path),
        "FAKE_SECRET_EVENTS": str(events_path),
        "TR_SECRET_IAM_PRESERVED_ACCESSORS_JSON": json.dumps(preserved or {}),
    }
    run = subprocess.run(  # noqa: S603 - isolated fake gcloud and repo helper
        [
            "/bin/bash",
            "-c",
            'source "$1"; source "$2"; reconcile_declared_secret_iam',
            "bash",
            str(ROOT / "scripts/deploy/_lib.sh"),
            str(helper),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        check=False,
    )
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    events = events_path.read_text(encoding="utf-8").splitlines()
    return run, final_state, events


def _secret_accessor_members(policy: dict[str, object]) -> set[str]:
    return {
        member
        for binding in policy.get("bindings", [])
        if binding.get("role") == "roles/secretmanager.secretAccessor"
        and binding.get("condition") is None
        for member in binding.get("members", [])
    }


def test_secret_iam_reconciler_mutates_only_declared_owner_bindings(
    tmp_path: Path,
) -> None:
    preserved_member = "group:billing-auditors@example.com"
    secrets = {
        "trustedrouter-attribution-cookie-secret": {
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": [
                        "serviceAccount:tr-actions@quill-cloud-proxy.iam.gserviceaccount.com",
                        preserved_member,
                    ],
                },
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": ["allUsers"],
                },
            ]
        },
        "trustedrouter-openai-api-key": {
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": [
                        "serviceAccount:tr-chat@quill-cloud-proxy.iam.gserviceaccount.com"
                    ],
                }
            ]
        },
        "trustedrouter-observer-internal-token": {
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": [
                        "serviceAccount:tr-internal@quill-cloud-proxy.iam.gserviceaccount.com"
                    ],
                }
            ]
        },
        "owner-managed-existing-secret": {"bindings": []},
    }

    run, final_state, events = _run_secret_iam_reconciler(
        tmp_path,
        secrets,
        preserved={
            "trustedrouter-attribution-cookie-secret": [preserved_member]
        },
    )

    assert run.returncode == 0, run.stderr
    assert _secret_accessor_members(
        final_state["trustedrouter-attribution-cookie-secret"]
    ) == {
        "serviceAccount:tr-public@quill-cloud-proxy.iam.gserviceaccount.com",
        "serviceAccount:tr-console@quill-cloud-proxy.iam.gserviceaccount.com",
        preserved_member,
    }
    assert _secret_accessor_members(final_state["trustedrouter-openai-api-key"]) == {
        "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
    }
    assert _secret_accessor_members(
        final_state["trustedrouter-observer-internal-token"]
    ) == {
        "serviceAccount:tr-internal@quill-cloud-proxy.iam.gserviceaccount.com",
        "serviceAccount:tr-synthetic@quill-cloud-proxy.iam.gserviceaccount.com",
    }
    assert final_state["owner-managed-existing-secret"] == {"bindings": []}
    mutations = [
        event
        for event in events
        if "add-iam-policy-binding" in event or "remove-iam-policy-binding" in event
    ]
    assert mutations
    assert all("set-iam-policy" not in event for event in events)
    assert not any("owner-managed-existing-secret" in event for event in mutations)


def test_secret_iam_reconciler_fails_before_mutation_on_unknown_managed_grant(
    tmp_path: Path,
) -> None:
    secrets = {
        "trustedrouter-attribution-cookie-secret": {"bindings": []},
        "owner-managed-existing-secret": {
            "bindings": [
                {
                    "role": "roles/secretmanager.secretAccessor",
                    "members": [
                        "serviceAccount:tr-public@quill-cloud-proxy.iam.gserviceaccount.com"
                    ],
                }
            ]
        },
    }

    run, final_state, events = _run_secret_iam_reconciler(tmp_path, secrets)

    assert run.returncode != 0
    assert final_state == secrets
    assert not any("iam-policy-binding" in event for event in events)


def test_deploy_secret_accessor_residual_is_exact_and_never_project_wide() -> None:
    allowlist = _function_body(
        LIB,
        "deploy_service_account_owns_secret",
        "synthetic_service_account_owns_secret",
    )
    grants = set(re.findall(r"trustedrouter-[a-z0-9-]+", allowlist))
    workflow_reads = set(re.findall(r"trustedrouter-[a-z0-9-]+", PRICE_REFRESH))
    workflow_reads.discard("trustedrouter-pricing-bot")
    assert grants == workflow_reads
    assert "grant_tr_deploy_secret_access" not in SECRETS
    assert (
        'ensure_project_role "$member" "roles/secretmanager.secretAccessor"'
        not in INFRA
    )
    assert 'TR_DEPLOY_SA="$DEPLOY_SERVICE_ACCOUNT"' in SECRETS
    assert "TR_DEPLOY_SA must match TR_DEPLOY_SERVICE_ACCOUNT" in SECRETS


def test_split_runtimes_cannot_read_upstream_model_provider_secrets() -> None:
    provider_creation_block = SECRETS[
        SECRETS.index('ensure_secret_from_env_file "ANTHROPIC_API_KEY"') :
        SECRETS.index("# Dedicated read-only ClickHouse credential")
    ]
    provisioned = set(
        re.findall(r"trustedrouter-[a-z0-9-]+", provider_creation_block)
    )
    provisioned -= {
        "trustedrouter-veriff-api-key",
        "trustedrouter-veriff-shared-secret-key",
    }
    manifest = re.search(
        r"DETACHED_PROVIDER_SECRET_NAMES=\(\n(?P<body>.*?)\n\)",
        SECRETS,
        re.S,
    )
    assert manifest is not None
    detached_secrets = set(
        re.findall(r"trustedrouter-[a-z0-9-]+", manifest["body"])
    )
    assert detached_secrets - {"trustedrouter-athena-worker-prompt-v1"} == provisioned
    ownership_loop = SECRETS[
        manifest.end() : SECRETS.index(
            "# Runtime-SA project-level IAM bindings", manifest.end()
        )
    ]
    assert '"$CHAT_RUN_SERVICE_ACCOUNT"' not in ownership_loop
    assert (
        'verify_runtime_secret_access optional "$secret_name"\n'
        '  fi' in ownership_loop
    )
    assert (
        'verify_runtime_secret_access optional "$secret_name" \\\n'
        '      "$CONSOLE_RUN_SERVICE_ACCOUNT"' in ownership_loop
    )


def test_axiom_token_is_detached_from_every_split_runtime() -> None:
    axiom_call = SECRETS[
        SECRETS.index(
            "verify_runtime_secret_access optional trustedrouter-axiom-api-token"
        ) :
        SECRETS.index("for secret_name in \\\n  trustedrouter-federation-peer-token")
    ]
    assert "RUN_SERVICE_ACCOUNT" not in axiom_call


def test_actions_has_no_data_or_kms_capability() -> None:
    words = _shell_words(INFRA)
    assert (
        '"Spanner database" "serviceAccount:${ACTIONS_RUN_SERVICE_ACCOUNT}" ""'
        in words
    )
    assert (
        '"$ACTIONS_RUN_SERVICE_ACCOUNT" "$CHAT_RUN_SERVICE_ACCOUNT" '
        '"$WEBHOOKS_RUN_SERVICE_ACCOUNT"; do verify_only_resource_role '
        '"Bigtable instance"' in words
    )
    assert (
        '"$PUBLIC_RUN_SERVICE_ACCOUNT" "$ACTIONS_RUN_SERVICE_ACCOUNT" '
        '"$CHAT_RUN_SERVICE_ACCOUNT" "$WEBHOOKS_RUN_SERVICE_ACCOUNT"; do '
        'verify_only_resource_role "BYOK KMS key"' in words
    )
    assert (
        '"$PUBLIC_RUN_SERVICE_ACCOUNT" "$ACTIONS_RUN_SERVICE_ACCOUNT" '
        '"$CHAT_RUN_SERVICE_ACCOUNT" "$WEBHOOKS_RUN_SERVICE_ACCOUNT" '
        '"$INTERNAL_RUN_SERVICE_ACCOUNT"; do verify_only_resource_role '
        '"Google Ads KMS key"' in words
    )


def test_restricted_provider_credentials_have_mutation_sensitive_anti_reuse_checks() -> None:
    assert _anti_reuse_contract_is_complete(SECRETS)


@pytest.mark.parametrize(
    "needle",
    (
        '[ "$_internal_stripe_key" = "$_console_stripe_key" ]',
        '[ "$_internal_ses_access_key_id" = "$_shared_ses_access_key_id" ]',
        '[ "$_internal_ses_secret_access_key" = "$_shared_ses_secret_access_key" ]',
        '[ "$_attribution_token_check" = "$_observer_token_check" ]',
        '[ "$_monitor_token_check" = "$_gateway_token_check" ]',
    ),
)
def test_removing_a_reuse_guard_breaks_the_contract(needle: str) -> None:
    assert needle in SECRETS
    assert not _anti_reuse_contract_is_complete(SECRETS.replace(needle, "[ 1 = 2 ]", 1))


def test_synthetic_job_identity_has_only_direct_job_and_secret_contracts() -> None:
    words = _shell_words(SYNTHETIC)
    secret_envs = re.search(r"SECRET_ENVS=\(\n(?P<body>.*?)\n\)", SYNTHETIC, re.S)
    assert secret_envs is not None
    assert set(re.findall(r"trustedrouter-[a-z0-9-]+", secret_envs["body"])) == {
        "trustedrouter-observer-internal-token",
        "trustedrouter-synthetic-monitor-api-key",
    }
    assert "roles/run.developer" not in SYNTHETIC
    assert "ensure_project_role" not in SYNTHETIC
    assert 'gc run jobs add-iam-policy-binding "$job_name"' in words
    assert '--role="roles/run.invoker"' in words
    assert "--condition=None" in words
    assert "verify_synthetic_secret_access" in SYNTHETIC
    assert "verify_existing_synthetic_job_invoker_or_absent" in SYNTHETIC
    assert "verify_exact_unconditional_roles" in SYNTHETIC
    assert "--flatten='bindings[].members'" not in SYNTHETIC
    assert words.count('ensure_synthetic_job_invoker "$') == 4


def test_legacy_retirement_is_guarded_and_covers_data_and_both_kms_keys() -> None:
    words = _shell_words(INFRA)
    assert 'if [ "$TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM" = "1" ]; then' in INFRA
    assert "verify_legacy_runtime_retirement_ready" in INFRA
    assert "verify_legacy_synthetic_secret_access_ready" in INFRA
    assert "verify_legacy_cloud_run_service_inventory" in INFRA
    assert "verify_legacy_synthetic_jobs_ready" in INFRA
    assert "cloud_run_inventory_lines services" in INFRA
    assert "cloud_run_inventory_lines jobs" in INFRA
    assert '"roles/run.invoker"' in INFRA
    assert 'oauth.get("serviceAccountEmail") != expected_identity' in INFRA
    assert 'for key_id in "$BYOK_KMS_KEY_ID" "$GOOGLE_ADS_KMS_KEY_ID"' in words
    assert '"retired project" "$member" "" gc projects get-iam-policy' in words
    assert "sole-revision 100%" in RUNBOOK
    assert "TR_RETIRE_LEGACY_RUN_SERVICE_ACCOUNT_IAM=1" in RUNBOOK


def test_provider_side_restrictions_are_explicitly_outside_secret_manager() -> None:
    assert "Payment Intents" in RUNBOOK
    assert "ses:SendEmail" in RUNBOOK
    assert "ses:SendRawEmail" in RUNBOOK
    assert "Secret Manager" in RUNBOOK
    assert "cannot prove Stripe dashboard permissions or an AWS IAM policy" in RUNBOOK


def test_iam_queries_preserve_conditions_and_cover_direct_resource_ancestors() -> None:
    helper = _function_body(
        LIB,
        "iam_direct_binding_tokens_for_member",
        "iam_member_has_unconditional_role",
    )
    assert "--format=json" in helper
    assert '"condition" in binding' in helper
    assert 'prefix = "conditional:"' in helper
    assert "--flatten" not in helper

    preflight = _function_body(
        INFRA,
        "preflight_identity_iam_removal_targets",
        "verify_identity_ancestor_scopes_empty",
    )
    for command in (
        'gc projects get-iam-policy "$PROJECT_ID"',
        'gc spanner instances get-iam-policy "$SPANNER_INSTANCE_ID"',
        'gc spanner databases get-iam-policy "$SPANNER_DATABASE_ID"',
        'gc bigtable instances get-iam-policy "$BIGTABLE_INSTANCE_ID"',
        'gc bigtable tables get-iam-policy "$BIGTABLE_GENERATION_TABLE"',
        'gc kms keyrings get-iam-policy "$KMS_KEYRING_ID"',
        'gc kms keys get-iam-policy "$key_id"',
    ):
        assert command in preflight
    for roles in ("project_roles", "database_roles", "bigtable_roles", "kms_roles"):
        assert f'"${roles}"' in preflight


@pytest.mark.parametrize(
    "command",
    (
        'gc spanner instances get-iam-policy "$SPANNER_INSTANCE_ID"',
        'gc bigtable tables get-iam-policy "$BIGTABLE_GENERATION_TABLE"',
        'gc kms keyrings get-iam-policy "$KMS_KEYRING_ID"',
    ),
)
def test_removing_an_ancestor_read_breaks_the_preflight_contract(command: str) -> None:
    preflight = _function_body(
        INFRA,
        "preflight_identity_iam_removal_targets",
        "verify_identity_ancestor_scopes_empty",
    )
    assert command in preflight
    assert command not in preflight.replace(command, "gc false", 1)


def test_every_removal_target_is_preflighted_before_the_first_runtime_removal() -> None:
    preflight_call = INFRA.index(
        'log "preflighting all runtime IAM removal targets and direct ancestor policies"'
    )
    first_runtime_removal = INFRA.index(
        'remove_kms_role_if_present "$member" "$role" "$GOOGLE_ADS_KMS_KEY_ID"',
        preflight_call,
    )
    assert preflight_call < first_runtime_removal
    for call in (
        'preflight_identity_iam_removal_targets "split runtime" "$service_account"',
        "verify_legacy_runtime_retirement_ready",
        "verify_legacy_cloud_run_service_inventory",
        "verify_legacy_synthetic_secret_access_ready",
        "verify_legacy_synthetic_jobs_ready",
        'preflight_identity_iam_removal_targets "legacy runtime" "$RUN_SERVICE_ACCOUNT"',
    ):
        assert preflight_call < INFRA.index(call, preflight_call) < first_runtime_removal


def test_deploy_actas_audit_is_exact_read_only_and_project_aware() -> None:
    start = INFRA.index("verify_deploy_actas_inventory() {")
    end = INFRA.index('\n}\n\nlog "enabling required GCP APIs"', start) + 3
    body = INFRA[start:end]
    assert "service-accounts list --format=json" in body
    assert "verify_exact_unconditional_roles" in body
    assert "roles/iam.serviceAccountUser" in body
    assert "verify_deploy_identity_has_no_project_data_roles" in body
    assert "remove-iam-policy-binding" not in body
    preflight = (
        'verify_deploy_actas_inventory '
        '"$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" preflight'
    )
    postverify = (
        'verify_deploy_actas_inventory '
        '"$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT" post'
    )
    assert preflight in INFRA
    assert postverify in INFRA
    assert INFRA.index(preflight) < INFRA.index(
        'remove_kms_role_if_present "$member" "$role" "$GOOGLE_ADS_KMS_KEY_ID"',
        INFRA.index(preflight),
    )
    assert INFRA.index(postverify) > INFRA.index(
        'gc iam service-accounts add-iam-policy-binding \\\n  "$GOOGLE_DATA_MANAGER_SERVICE_ACCOUNT"'
    )


def test_regions_and_internal_billing_alias_are_validated_fail_closed() -> None:
    body = _function_body(LIB, "validate_runtime_service_accounts", "read_key_file_var")
    assert 'if [ "$TR_BILLING_SERVICE" != "$INTERNAL_SERVICE" ]' in body
    assert "validate_nonempty_region_list TR_CONTROL_PLANE_REGIONS" in body
    for variable in (
        "TR_SYNTHETIC_MONITOR_REGIONS",
        "TR_SYNTHETIC_THROUGHPUT_REGION",
        "TR_SYNTHETIC_IMAGE_REGION",
        "TR_SYNTHETIC_VIDEO_REGION",
    ):
        assert f"validate_nonempty_region_list {variable}" in body


def test_synthetic_live_contract_checks_one_full_traffic_revision_and_runtime_sa() -> None:
    body = _function_body(
        SYNTHETIC,
        "verify_synthetic_ingest_service_contract",
        "ensure_synthetic_job_invoker",
    )
    assert "len(traffic) != 1" in body
    assert "!= 100" in body
    assert 'gc run revisions describe "$revision_name"' in body
    assert '"$INTERNAL_RUN_SERVICE_ACCOUNT"' in body
    assert "serving revision identity" in body


def test_condition_aware_iam_helper_rejects_state_change_between_reads(
    tmp_path: Path,
) -> None:
    member = "serviceAccount:test@quill-cloud-proxy.iam.gserviceaccount.com"
    valid = json.dumps(
        {
            "bindings": [
                {"role": "roles/run.invoker", "members": [member]}
            ]
        },
        separators=(",", ":"),
    )
    conditional = json.dumps(
        {
            "bindings": [
                {
                    "role": "roles/run.invoker",
                    "members": [member],
                    "condition": {"title": "temporary", "expression": "true"},
                }
            ]
        },
        separators=(",", ":"),
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *\"projects describe\"* ]]; then\n"
        "  printf '123456789\\n'\n"
        "  exit 0\n"
        "fi\n"
        "if [ ! -f \"$FAKE_IAM_STATE\" ]; then\n"
        "  : > \"$FAKE_IAM_STATE\"\n"
        f"  printf '%s\\n' '{valid}'\n"
        "else\n"
        f"  printf '%s\\n' '{conditional}'\n"
        "fi\n"
    )
    fake_gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_IAM_STATE": str(tmp_path / "iam-state"),
    }
    run = subprocess.run(  # noqa: S603 - fixed shell and repo-local helper
        [
            "/bin/bash",
            "-c",
            'source "$1"; '
            'verify_exact_unconditional_roles first "$2" roles/run.invoker '
            'gc projects get-iam-policy "$PROJECT_ID"; '
            'if verify_exact_unconditional_roles second "$2" roles/run.invoker '
            'gc projects get-iam-policy "$PROJECT_ID"; then exit 91; fi',
            "bash",
            str(ROOT / "scripts/deploy/_lib.sh"),
            member,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        check=False,
    )

    assert run.returncode == 0
    assert "conditional:roles/run.invoker" in run.stderr


def _run_runtime_service_account_policy_contract(
    tmp_path: Path,
    policy: dict[str, object],
    *,
    phase: str = "post",
    operator_members: str = "",
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *\"projects describe\"* ]]; then\n"
        "  printf '123456789\\n'\n"
        "else\n"
        "  printf 'unexpected gcloud call: %s\\n' \"$*\" >&2\n"
        "  exit 2\n"
        "fi\n"
    )
    fake_gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "RUNTIME_POLICY": json.dumps(policy, separators=(",", ":")),
        "POLICY_PHASE": phase,
        "TR_RUNTIME_SERVICE_ACCOUNT_OPERATOR_MEMBERS": operator_members,
    }
    return subprocess.run(  # noqa: S603 - fixed shell and repo-local helper
        [
            "/bin/bash",
            "-c",
            'source "$1"; printf "%s" "$RUNTIME_POLICY" | '
            "verify_runtime_service_account_policy_json "
            '"$PUBLIC_RUN_SERVICE_ACCOUNT" "$POLICY_PHASE"',
            "bash",
            str(ROOT / "scripts/deploy/_lib.sh"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        check=False,
    )


def test_runtime_service_account_policy_allows_deploy_and_explicit_operator(
    tmp_path: Path,
) -> None:
    deploy = "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
    operator = "user:operator@example.com"
    run = _run_runtime_service_account_policy_contract(
        tmp_path,
        {
            "bindings": [
                {"role": "roles/iam.serviceAccountUser", "members": [deploy]},
                {"role": "roles/iam.serviceAccountUser", "members": [operator]},
            ]
        },
        operator_members=operator,
    )

    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize(
    "role",
    (
        "roles/iam.serviceAccountUser",
        "roles/iam.serviceAccountTokenCreator",
        "roles/viewer",
    ),
)
def test_runtime_service_account_policy_rejects_every_cross_runtime_role(
    tmp_path: Path,
    role: str,
) -> None:
    deploy = "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
    other_runtime = (
        "serviceAccount:tr-actions@quill-cloud-proxy.iam.gserviceaccount.com"
    )
    run = _run_runtime_service_account_policy_contract(
        tmp_path,
        {
            "bindings": [
                {"role": "roles/iam.serviceAccountUser", "members": [deploy]},
                {"role": role, "members": [other_runtime]},
            ]
        },
    )

    assert run.returncode != 0
    assert "split runtime principal" in run.stderr


@pytest.mark.parametrize(
    ("binding", "operator_members", "error_fragment"),
    (
        (
            {
                "role": "roles/iam.serviceAccountUser",
                "members": [
                    "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
                ],
                "condition": {"title": "temporary", "expression": "true"},
            },
            "",
            "deploy principal bindings",
        ),
        (
            {
                "role": "roles/iam.serviceAccountTokenCreator",
                "members": [
                    "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
                ],
            },
            "",
            "deploy principal bindings",
        ),
        (
            {
                "role": "roles/iam.serviceAccountUser",
                "members": ["user:unlisted@example.com"],
            },
            "",
            "unapproved principal",
        ),
        (
            {
                "role": "roles/iam.serviceAccountTokenCreator",
                "members": ["user:operator@example.com"],
            },
            "user:operator@example.com",
            "must have only unconditional serviceAccountUser",
        ),
    ),
    ids=(
        "conditional-deploy",
        "deploy-token-creator",
        "unknown-principal",
        "operator-token-creator",
    ),
)
def test_runtime_service_account_policy_rejects_noncanonical_direct_bindings(
    tmp_path: Path,
    binding: dict[str, object],
    operator_members: str,
    error_fragment: str,
) -> None:
    deploy = "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
    bindings: list[dict[str, object]] = []
    if deploy not in binding.get("members", []):
        bindings.append(
            {"role": "roles/iam.serviceAccountUser", "members": [deploy]}
        )
    bindings.append(binding)
    run = _run_runtime_service_account_policy_contract(
        tmp_path,
        {"bindings": bindings},
        operator_members=operator_members,
    )

    assert run.returncode != 0
    assert error_fragment in run.stderr


def test_runtime_policy_contract_is_mutation_sensitive_to_cross_runtime_drift(
    tmp_path: Path,
) -> None:
    deploy = "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
    valid = {"bindings": [{"role": "roles/iam.serviceAccountUser", "members": [deploy]}]}
    mutated = json.loads(json.dumps(valid))
    mutated["bindings"].append(
        {
            "role": "roles/iam.serviceAccountUser",
            "members": [
                "serviceAccount:tr-chat@quill-cloud-proxy.iam.gserviceaccount.com"
            ],
        }
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        "#!/usr/bin/env bash\n"
        "printf '123456789\\n'\n"
    )
    fake_gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "VALID_POLICY": json.dumps(valid, separators=(",", ":")),
        "MUTATED_POLICY": json.dumps(mutated, separators=(",", ":")),
    }
    run = subprocess.run(  # noqa: S603 - fixed shell and repo-local helper
        [
            "/bin/bash",
            "-c",
            'source "$1"; '
            'printf "%s" "$VALID_POLICY" | '
            'verify_runtime_service_account_policy_json "$PUBLIC_RUN_SERVICE_ACCOUNT" post; '
            'if printf "%s" "$MUTATED_POLICY" | '
            'verify_runtime_service_account_policy_json "$PUBLIC_RUN_SERVICE_ACCOUNT" post; '
            "then exit 91; fi",
            "bash",
            str(ROOT / "scripts/deploy/_lib.sh"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        check=False,
    )

    assert run.returncode == 0
    assert "split runtime principal" in run.stderr


def test_runtime_policy_shared_contract_is_used_by_infra_pre_and_post_audits() -> None:
    helper = _function_body(
        LIB,
        "verify_runtime_service_account_policy_json",
        "validate_runtime_service_accounts",
    )
    assert "roles/iam.serviceAccountTokenCreator" not in helper
    assert "split runtime principal" in helper
    assert "unapproved principal" in helper
    assert "operator" in helper
    audit_start = INFRA.index("verify_deploy_actas_inventory() {")
    audit_end = INFRA.index('\n}\n\nlog "enabling required GCP APIs"', audit_start)
    audit = INFRA[audit_start:audit_end]
    assert "verify_runtime_service_account_policy_json" in audit
    assert '"$service_account" "$phase"' in audit
