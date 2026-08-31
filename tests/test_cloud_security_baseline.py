"""The cloud security baseline checkers must stay read-only, and must keep the
traps that make them correct.

Context, because these assertions look arbitrary without it. On 2026-08-17 an
audit found that 43 of 53 SOC 2 hardening changes across AWS, Azure and GCP
would silently revert if their resource were rebuilt, because every one existed
only as live cloud state. The obvious fix -- a replayable hardening script per
cloud -- was abandoned after capturing ground truth, for a reason worth keeping:

    soc2/gcp-hardening-2026-08-15.sh says in its own header that none of its
    work has been applied. That was true when written and false now. Re-running
    it today would stop a running production ClickHouse node to swap an identity
    it already has, rewrite two CIS metrics into semantically different filters,
    mint a duplicate notification channel on every invocation, re-introduce a
    ~$236/month logging cost regression, and revert a threshold recalibration
    made two days later.

A one-shot hardening script becomes a hazard the moment its work is done,
because it carries no memory of having run. So the Azure and GCP baselines read
and report; they never converge. These tests keep them that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GCP = ROOT / "scripts/deploy/gcp_security_baseline_check.sh"
AZURE = ROOT / "scripts/deploy/azure_security_baseline_check.sh"
WORKFLOW = ROOT / ".github/workflows/cloud-security-baseline.yml"
CHECK_ONLY = (GCP, AZURE)


def _executable_lines(path: Path) -> str:
    """Script text without comment-only lines.

    Every negative assertion here forbids a verb that the script's own comments
    legitimately discuss -- the headers explain at length why applying is
    dangerous -- so matching raw text would fail on the prose warning against
    the thing being forbidden.
    """
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("path", CHECK_ONLY, ids=lambda p: p.name)
def test_baseline_checkers_never_mutate(path: Path) -> None:
    """No mutating verb may appear in executable code.

    This is the load-bearing test. "check always safe" is only true while it
    stays true, and the cheapest way for it to stop being true is someone
    adding a helpful one-line fix to a script that runs unattended on a
    schedule against production.
    """
    body = _executable_lines(path)
    forbidden = [
        # gcloud
        "gcloud compute firewall-rules create",
        "gcloud compute firewall-rules delete",
        "gcloud compute instances stop",
        "gcloud compute instances set-service-account",
        "gcloud logging metrics create",
        "gcloud logging metrics update",
        "gcloud alpha monitoring channels create",
        "gcloud alpha monitoring policies create",
        "gcloud projects set-iam-policy",
        "add-iam-policy-binding",
        "remove-iam-policy-binding",
        "gcloud essential-contacts create",
        "gcloud kms keyrings create",
        "gcloud kms keys create",
        "gcloud storage buckets update",
        # az
        "az monitor diagnostic-settings create",
        "az monitor log-analytics workspace create",
        "az postgres flexible-server parameter set",
        "az postgres flexible-server firewall-rule create",
        "az keyvault update",
        "az role assignment create",
        "az role assignment delete",
    ]
    for verb in forbidden:
        assert verb not in body, f"{path.name} contains mutating command: {verb}"


@pytest.mark.parametrize("path", CHECK_ONLY, ids=lambda p: p.name)
def test_baseline_checkers_offer_no_apply_flag(path: Path) -> None:
    body = _executable_lines(path)
    assert "--apply" not in body, f"{path.name} must not offer an apply mode"


@pytest.mark.parametrize("path", CHECK_ONLY, ids=lambda p: p.name)
def test_baseline_checkers_exit_nonzero_on_drift(path: Path) -> None:
    """A checker that always exits 0 is decoration: a scheduled run would go
    green forever and the drift it found would scroll past in a log."""
    body = _executable_lines(path)
    assert 'exit "$FAIL"' in body


def test_gcp_pins_cis_filters_verbatim() -> None:
    """Existence is not the control; the filter is.

    A metric that exists but no longer matches yields an alert policy that
    never fires, which is indistinguishable from a quiet account. These are the
    live strings, and they differ from the old hardening script's -- which
    would write an iam.admin/CreateServiceAccount filter for the first, and
    would drop the cloudsql.instances.delete clause from the last.
    """
    body = GCP.read_text()
    assert "policyDelta.auditConfigDeltas" in body
    assert "compute.firewalls.patch" in body
    assert "compute.routes.delete" in body
    assert "cloudsql.instances.delete" in body


def test_gcp_treats_data_read_as_a_cost_regression() -> None:
    """DATA_READ was enabled and removed the same day on measured cost: ~0.07
    -> ~0.80 GiB/hour, about $236/month. Its return means someone re-ran the
    old script."""
    body = GCP.read_text()
    assert "DATA_READ" in body
    assert "236" in body


def test_gcp_detects_duplicate_notification_channels() -> None:
    """`gcloud alpha monitoring channels create` is not idempotent. Each run of
    the old script mints another channel and repoints policies at the new id,
    orphaning the previous one -- so >1 channel is evidence it was re-run."""
    body = GCP.read_text()
    assert "duplicate" in body.lower()


def test_gcp_monitoring_reads_fail_closed_without_inventing_absence() -> None:
    """A missing alpha component or list permission is not evidence that all
    security alerts were deleted. The read must produce valid JSON before any
    presence assertion runs."""
    body = _executable_lines(GCP)
    assert 'read_project_json CHANNELS "Monitoring notification channels"' in body
    assert 'read_project_json POLICIES "Monitoring alert policies"' in body
    assert 'read_project_json EC "effective SECURITY Essential Contacts"' in body
    assert 'drift "could not read $label' in body


def test_gcp_workflow_installs_monitoring_alpha_component() -> None:
    body = WORKFLOW.read_text()
    assert "install_components: alpha" in body


def test_gcp_workflow_does_not_require_optional_security_label() -> None:
    body = WORKFLOW.read_text()
    assert 'LABEL_ARGS=()' in body
    assert 'LABEL_ARGS=(--label security)' in body
    assert 'gh issue create --title "$TITLE" "${LABEL_ARGS[@]}"' in body


def test_gcp_checks_the_iap_path_before_asserting_ssh_is_closed() -> None:
    """default-allow-ssh's deletion is only survivable because
    tr-allow-iap-ssh-all exists. A report saying "ssh is closed" while the IAP
    rule is also gone describes a locked-out project, not a hardened one."""
    body = _executable_lines(GCP)
    assert body.index("tr-allow-iap-ssh-all") < body.index("for r in default-allow-ssh")


def test_gcp_expected_firewall_absence_does_not_emit_not_found_audits() -> None:
    """An expected 404 is still an ERROR audit record. Negative controls read
    the collection once and compare names locally instead of issuing GETs for
    resources that should not exist."""
    body = _executable_lines(GCP)
    assert body.count("compute firewall-rules list --format='value(name)'") == 1
    assert 'firewall-rules describe "$r"' not in body
    assert "firewall-rules describe allow-iap-ssh-tmp" not in body
    assert 'grep -Fxq "$r" <<<"$FIREWALL_NAMES"' in body


def test_gcp_resolves_essential_contacts_by_inheritance() -> None:
    """`essential-contacts list --project=` returns [] here and that does not
    mean absent -- contacts inherit from the org. A check written the obvious
    way reports the control missing; this exact error was written into the
    Statement of Applicability and had to be retracted."""
    body = _executable_lines(GCP)
    assert "essential-contacts compute" in body
    assert "essential-contacts list" not in body


def test_gcp_reads_bucket_config_in_snake_case() -> None:
    """gcloud emits snake_case. A camelCase projection returns null for every
    field, which reads exactly like "no protections exist" -- the mistake that
    nearly put a fabricated critical finding into an audit document."""
    body = _executable_lines(GCP)
    assert "default_kms_key" in body
    assert "soft_delete_policy" in body
    assert "softDeletePolicy" not in body


def test_azure_reads_diagnostic_settings_as_a_bare_array() -> None:
    """`az monitor diagnostic-settings list` returns a bare array; querying
    value[] returns empty, which reads as "no diagnostic settings". The
    subscription-level variant is the one that returns {value:[...]}."""
    body = _executable_lines(AZURE)
    assert "diagnostic-settings list --resource" in body
    assert ".value[]?" in body  # only for the subscription call
    assert "--query 'value[]" not in body


def test_azure_compares_resource_ids_case_insensitively() -> None:
    """ARM lowercases the resource-group segment of ids it returns
    (TR-TEE-DUBAI comes back tr-tee-dubai), so literal equality against how a
    resource is spelled in a script reports permanent false drift."""
    body = _executable_lines(AZURE)
    assert "lc()" in body or "tr '[:upper:]' '[:lower:]'" in body


def test_azure_asserts_all_logs_not_audit_on_postgres() -> None:
    """pg-audit has categoryGroup allLogs ENABLED and audit DISABLED. allLogs is
    a superset containing the audit categories, so asserting audit=true would
    flag a correct control, and converging to audit-only would REDUCE coverage."""
    body = AZURE.read_text()
    assert "pg-audit allLogs" in body
    assert "REDUCE coverage" in body


def test_azure_does_not_compare_activity_log_location() -> None:
    """The hardening script passes --location uaenorth; ARM stores
    location=global. Comparing them reports drift forever."""
    body = AZURE.read_text()
    assert "Do NOT check location" in body
