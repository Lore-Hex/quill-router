"""Service-account key controls (infra/service_account_key_policy.tf,
infra/laptop_key_use_alert.tf).

No new keys may be minted in the project, and a call made with either laptop
key from an address that is not on the known list pages. The known addresses
are an operator's network addresses, so they must never be written into this
public repository.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = (ROOT / "infra/service_account_key_policy.tf").read_text()
ALERT = (ROOT / "infra/laptop_key_use_alert.tf").read_text()


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_key_creation_is_blocked_for_the_project_and_adopted_by_import() -> None:
    code = _code(POLICY)
    assert 'constraint = "constraints/iam.disableServiceAccountKeyCreation"' in code
    assert re.search(r"boolean_policy \{\s*enforced = true\s*\}", code)
    # The deploy identity cannot set an organization policy, so the operator
    # sets it and the unattended apply only adopts it. The provider's import
    # id separates the project and the constraint with a colon.
    assert re.search(
        r"import \{\s*to = google_project_organization_policy\.no_new_service_account_keys\s*"
        r'id = "\$\{local\.gcp_project_id\}:constraints/iam\.disableServiceAccountKeyCreation"',
        code,
    )


def test_the_alert_matches_only_key_authenticated_calls_of_the_two_laptop_accounts() -> None:
    code = _code(ALERT)
    accounts = re.search(r"laptop_key_accounts = \[(.*?)\]", code, re.S)
    assert accounts
    assert re.findall(r'"([a-z-]+)@', accounts.group(1)) == ["tr-deploy", "tr-ops-local"]
    # Federated CI calls present no key name, so they never match.
    assert 'protoPayload.authenticationInfo.serviceAccountKeyName:\\"/keys/\\"' in code
    assert "protoPayload.authenticationInfo.principalEmail=(" in code
    assert "protoPayload.requestMetadata.callerIp:*" in code
    assert 'NOT ip_in_net(protoPayload.requestMetadata.callerIp, \\"${cidr}\\")' in code
    assert "filter = local.laptop_key_used_elsewhere_filter" in code
    # Every clause must hold at once: joined any other way, one known address
    # would be enough to match everything.
    assert 'laptop_key_used_elsewhere_filter = join(" AND ", concat(' in code


def test_cloud_schedulers_copy_of_a_call_does_not_page() -> None:
    # Cloud Scheduler writes a second entry for a call it receives, with the
    # literal address "private". It is not an address, so it is in no range
    # on the list, and without this clause the operator's own Scheduler
    # commands from the known address would page. A call from a VM (the
    # literal "gce-internal-ip") must still page.
    code = _code(ALERT)
    assert 'NOT protoPayload.requestMetadata.callerIp=\\"private\\"' in code
    assert "gce-internal-ip" not in code


def test_an_empty_address_list_cannot_be_applied() -> None:
    # With no known address the policy would page on every call made with a
    # laptop key, hundreds a day.
    assert re.search(r"precondition \{\s*condition\s*= length\(local\.laptop_key_known_addresses\) > 0", _code(ALERT))


def test_a_malformed_or_too_wide_entry_stops_the_plan_with_a_fixed_message() -> None:
    # The alerting API refuses a bad range at apply and quotes it, and the
    # apply log is public; a range as wide as everything would switch the
    # alert off without an error. Both stop at plan, and the message must not
    # interpolate anything.
    code = _code(ALERT)
    check = re.search(
        r"precondition \{\s*condition = alltrue\(\[\s*for cidr in local\.laptop_key_known_addresses :\s*"
        r'can\(cidrhost\(cidr, 0\)\) && try\(tonumber\(split\("/", cidr\)\[1\]\), 0\) >= 16\s*\]\)\s*'
        r'error_message = "([^"]*)"',
        code,
    )
    assert check
    assert "${" not in check.group(1)
    for message in re.findall(r'error_message\s*= "([^"]*)"', code):
        assert "${" not in message


def test_the_known_addresses_are_read_from_a_secret_and_never_written_here() -> None:
    assert 'secret  = "tr-laptop-key-known-addresses"' in ALERT
    assert "data.google_secret_manager_secret_version.laptop_key_known_addresses.secret_data" in ALERT
    for text in (ALERT, POLICY, Path(__file__).read_text()):
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text), "an address is written in the repository"


def test_the_policy_is_rate_limited_and_notifies_the_security_channel() -> None:
    code = _code(ALERT)
    assert re.search(r'notification_rate_limit \{\s*period = "3600s"\s*\}', code)
    assert 'auto_close = "86400s"' in code
    assert re.search(
        r'data "google_monitoring_notification_channel" "security_alerts" \{\s*project\s*= local\.gcp_project_id\s*'
        r'display_name = "TrustedRouter security alerts"',
        code,
    )
    assert "notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]" in code
    assert 'severity     = "CRITICAL"' in code
