"""Secret reads by unexpected accounts (infra/secret_reader_alert.tf).

Every read of a secret's value is recorded (infra/secret_manager_audit.tf).
One policy pages on a read by any account that is not on the readers list,
the other on a read of a signing key by anyone but its signer. The list is
what keeps the first policy quiet, so it is pinned here entry by entry, and
it may hold only whole service-account addresses.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
ALERT = (INFRA / "secret_reader_alert.tf").read_text()
CHANNEL = (INFRA / "laptop_key_use_alert.tf").read_text()

SA = "${local.service_account_domain}"
EXPECTED_READERS = {
    "gateway_runtime": '"${local.gcp_project_number}-compute@developer.gserviceaccount.com"',
    "control_runtime": f'"trusted-router-control-run@{SA}"',
    "enclave_workload": f'"quill-workload@{SA}"',
    "growth_sync": f'"tr-growth-sync@{SA}"',
    "public_runtime": f'"tr-public@{SA}"',
    "deploy": f'"${{local.ci_tier_accounts["deploy"]}}@{SA}"',
    "ops_local": f'"tr-ops-local@{SA}"',
    "fastpath_node": "google_service_account.fastpath_node.email",
    "ci_prices": 'google_service_account.ci_tier["prices"].email',
    "ci_probe": 'google_service_account.ci_tier["probe"].email',
    "internal_runtime": f'"tr-internal@{SA}"',
    "clickhouse_node": 'trimprefix(local.clickhouse_node_member, "serviceAccount:")',
    "aws_enclave": f'"tr-aws-cross-cloud@{SA}"',
}


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _block(opening: str, closing: str = "}") -> str:
    if not opening.endswith(("{", "[")):
        opening += " = {"
    code = _code(ALERT)
    marker = f"\n  {opening}\n"
    assert marker in code, f"{opening} not found"
    return code.split(marker, 1)[1].split(f"\n  {closing}", 1)[0]


def _readers() -> dict[str, str]:
    entries = {}
    for line in _block("secret_readers").splitlines():
        if not line.strip():
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        assert key not in entries, f"{key} appears twice"
        entries[key] = value
    return entries


def _resource(name: str) -> str:
    marker = f'resource "google_monitoring_alert_policy" "{name}" {{'
    code = _code(ALERT)
    assert marker in code
    return code.split(marker, 1)[1].split('\nresource "', 1)[0]


def test_the_readers_list_is_exactly_the_reviewed_accounts() -> None:
    # Adding an account here silences its reads; it is a reviewed change.
    assert _readers() == EXPECTED_READERS


def test_the_readers_list_holds_whole_service_account_addresses_and_no_wildcard() -> None:
    for value in _readers().values():
        assert "*" not in value
        assert " OR " not in value
        assert "user:" not in value and "group:" not in value and "domain:" not in value
        assert value.endswith(('@' + SA + '"', '@developer.gserviceaccount.com"', ".email", '"serviceAccount:")')), value
    # The plan refuses anything else, with a message that quotes nothing.
    check = 'can(regex("^[a-z0-9][a-z0-9.-]*@[a-z0-9][a-z0-9.-]*\\\\.gserviceaccount\\\\.com$", email))'
    assert re.search(r"for email in values\(local\.secret_readers\) :\s*" + re.escape(check), ALERT)
    assert "length(distinct(values(local.secret_readers))) == length(local.secret_readers)" in ALERT
    assert "length(local.secret_readers) > 0" in ALERT


def test_every_secret_accessor_binding_in_terraform_has_its_reader_listed() -> None:
    # A binding without a list entry pages on the account's first read.
    readers = set(_readers().values())
    granted = []
    for path in sorted(INFRA.glob("*.tf")):
        code = _code(path.read_text())
        for block in re.findall(r'resource "google_secret_manager_secret_iam_(?:member|binding)" "[^"]+" \{(.*?)\n\}', code, re.S):
            if not re.search(r'role\s*=\s*"roles/secretmanager\.secretAccessor"', block):
                continue
            granted += re.findall(r"(google_service_account\.[a-z_]+(?:\[\"[a-z]+\"\])?)\.member", block)
            granted += [
                f'"{name}@{SA}"'
                for name in re.findall(r'"serviceAccount:([a-z0-9-]+)@\$\{local\.gcp_project_id\}\.iam\.gserviceaccount\.com"', block)
            ]
    assert len(granted) >= 5
    for member in granted:
        email = member + ".email" if member.startswith("google_service_account.") else member
        assert email in readers, f"{member} can read a secret and is not in secret_readers"


def test_the_first_policy_matches_any_read_of_a_secret_value_by_an_unlisted_account() -> None:
    code = _code(ALERT)
    assert 'log_id(\\"cloudaudit.googleapis.com/data_access\\")' in code
    assert 'protoPayload.serviceName=\\"secretmanager.googleapis.com\\"' in code
    # Any API version: the v1 name is google.cloud.secretmanager.v1.SecretManagerService.AccessSecretVersion.
    assert 'protoPayload.methodName:\\"SecretManagerService.AccessSecretVersion\\"' in code
    assert (
        '"NOT protoPayload.authenticationInfo.principalEmail=(${join(" OR ", '
        'formatlist("\\"%s\\"", local.all_secret_readers))})"'
    ) in code
    # The listed accounts and the confidential ones (infra/confidential_secret_readers.tf).
    assert (
        "all_secret_readers = sort(concat(values(local.secret_readers), local.confidential_secret_readers))"
    ) in code
    # Every clause must hold at once.
    assert 'secret_read_filter = join(" AND ", [' in code
    assert 'unexpected_secret_reader_filter = join(" AND ", [\n    local.secret_read_filter,' in code
    assert '"NOT (${local.operator_login_at_known_address})",\n  ])' in code
    assert "filter = local.unexpected_secret_reader_filter" in _resource("unexpected_secret_reader")
    assert "length(local.unexpected_secret_reader_filter) <= 20000" in code


def test_no_service_account_read_is_left_out_by_its_address() -> None:
    # Cloud Scheduler's copies carry the literal address "private", and so
    # does every read made from inside a Cloud Run service. Scheduler reads no
    # secret, so leaving "private" out would only hide unknown accounts. The
    # caller's address is consulted in one place: a user login's read.
    code = _code(ALERT)
    assert "private" not in code
    assert "callerIp=" not in code
    filters = code.split('\nresource "', 1)[0]
    assert filters.count("callerIp") == 1
    assert "callerIp" in _block("operator_login_at_known_address = join(\" AND \", [", "]")


def test_a_user_login_is_quiet_only_from_the_laptop_alerts_known_addresses() -> None:
    clause = _block("operator_login_at_known_address = join(\" AND \", [", "]")
    assert clause.splitlines()[:2] == [
        '    "protoPayload.authenticationInfo.principalEmail:*",',
        '    "NOT protoPayload.authenticationInfo.principalEmail:\\"gserviceaccount.com\\"",',
    ]
    # One range is enough: the ranges are alternatives, the three clauses
    # must all hold.
    assert (
        '"(${join(" OR ", [\n      for cidr in local.laptop_key_known_addresses :\n'
        '      "ip_in_net(protoPayload.requestMetadata.callerIp, \\"${cidr}\\")"\n    ])})",'
    ) in clause

    # The same list as the laptop alert: one data source for the secret in
    # the whole root, and none here.
    readers_of_the_list = [
        path.name
        for path in INFRA.glob("*.tf")
        if 'secret  = "tr-laptop-key-known-addresses"' in _code(path.read_text())
    ]
    assert readers_of_the_list == ["laptop_key_use_alert.tf"]
    assert 'data "google_secret_manager_secret_version"' not in _code(ALERT)

    # The same two checks on it, word for word.
    def address_checks(text: str) -> list[str]:
        blocks = re.findall(r"\n    precondition \{\n(.*?)\n    \}", _code(text), re.S)
        return [block for block in blocks if "laptop_key_known_addresses" in block]

    laptop = address_checks(CHANNEL)
    assert len(laptop) == 2
    assert address_checks(_resource("unexpected_secret_reader")) == laptop


def test_the_second_policy_pages_on_a_signing_key_read_by_anyone_but_its_signer() -> None:
    entries = [line.split("=", 1) for line in _block("signing_key_readers").splitlines() if line.strip()]
    assert {key.strip(): value.strip() for key, value in entries} == {
        "(google_secret_manager_secret.fastpath_envelope_key.secret_id)": "local.secret_readers.fastpath_node",
        "(google_secret_manager_secret.routing_snapshot_signing_key.secret_id)": "local.secret_readers.control_runtime",
    }
    code = _code(ALERT)
    assert (
        '"(protoPayload.resourceName:\\"/secrets/${secret}/\\" AND '
        'NOT protoPayload.authenticationInfo.principalEmail=\\"${reader}\\")"'
    ) in code
    assert 'signing_key_read_by_other_filter = join(" AND ", [\n    local.secret_read_filter,' in code
    assert "filter = local.signing_key_read_by_other_filter" in _resource("signing_key_read_by_other")


def test_both_policies_are_rate_limited_and_notify_the_security_channel() -> None:
    assert re.search(
        r'data "google_monitoring_notification_channel" "security_alerts" \{\s*project\s*= local\.gcp_project_id\s*'
        r'display_name = "TrustedRouter security alerts"',
        _code(CHANNEL),
    )
    for name in ("unexpected_secret_reader", "signing_key_read_by_other"):
        resource = _resource(name)
        assert "condition_matched_log {" in resource
        assert len(re.findall(r"\n  conditions \{", resource)) == 1
        assert re.search(r'notification_rate_limit \{\s*period = "3600s"\s*\}', resource)
        assert 'auto_close = "86400s"' in resource
        assert "notification_channels = [data.google_monitoring_notification_channel.security_alerts.name]" in resource
        assert 'severity     = "CRITICAL"' in resource


def test_messages_quote_nothing_and_no_address_is_written() -> None:
    messages = re.findall(r'error_message\s*= "([^"]*)"', ALERT)
    assert len(messages) == 9
    for message in messages:
        assert "${" not in message
    for text in (ALERT, Path(__file__).read_text()):
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text), "an address is written in the repository"


CONFIDENTIAL = (INFRA / "confidential_secret_readers.tf").read_text()


def test_confidential_readers_come_only_from_their_secret_and_are_never_written() -> None:
    # Accounts this public repository must not name are read from a secret,
    # like the operator's known addresses, and nowhere else.
    code = _code(CONFIDENTIAL)
    assert 'data "google_secret_manager_secret_version" "confidential_secret_readers" {' in code
    assert 'secret  = "tr-confidential-secret-readers"' in code
    readers_of_the_list = [
        path.name for path in INFRA.glob("*.tf") if 'secret  = "tr-confidential-secret-readers"' in _code(path.read_text())
    ]
    assert readers_of_the_list == ["confidential_secret_readers.tf"]
    assert (
        "for email in split(\",\", data.google_secret_manager_secret_version.confidential_secret_readers.secret_data) :\n"
        "    trimspace(email) if trimspace(email) != \"\""
    ) in code
    # The container is managed (never destroyed) and adopted, not recreated.
    assert 'resource "google_secret_manager_secret" "confidential_secret_readers" {' in code
    assert "prevent_destroy = true" in code
    imports = _code((INFRA / "imports.tf").read_text())
    assert (
        "import {\n  to = google_secret_manager_secret.confidential_secret_readers\n"
        '  id = "projects/quill-cloud-proxy/secrets/tr-confidential-secret-readers"\n}'
    ) in imports
    # No account is written: only the placeholder in the instructions. The
    # detector itself is checked against an ordinary address first.
    account = re.compile(r"[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com")
    assert account.search("# feed-job@quill-cloud-proxy.iam.gserviceaccount.com")
    assert not account.search(CONFIDENTIAL.replace("ACCOUNT@PROJECT", ""))


CONFIDENTIAL_CHECK = (
    'can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\\\.iam\\\\.gserviceaccount\\\\.com$", email))\n'
    '        && endswith(email, "@${local.service_account_domain}")'
)


def test_confidential_readers_must_be_whole_accounts_of_this_project() -> None:
    lifecycle = _resource("unexpected_secret_reader")
    assert "for email in local.confidential_secret_readers :\n        " + CONFIDENTIAL_CHECK in lifecycle
    assert "length(distinct(local.all_secret_readers)) == length(local.all_secret_readers)" in lifecycle
    # Neither check can quote an entry.
    messages = re.findall(r'error_message\s*= "([^"]*)"', lifecycle)
    confidential = [message for message in messages if "tr-confidential-secret-readers" in message]
    assert len(confidential) == 2 and all("${" not in message for message in confidential)


def _confidential_accepts(email: str) -> bool:
    """The precondition's own rule, with its regex read from the Terraform."""
    found = re.search(r'can\(regex\("(\^\[a-z\][^"]*)", email\)\)\n\s*&& endswith', _resource("unexpected_secret_reader"))
    assert found
    pattern = found.group(1).replace("\\\\", "\\")
    return bool(re.fullmatch(pattern, email)) and email.endswith("@quill-cloud-proxy.iam.gserviceaccount.com")


def test_the_confidential_check_admits_real_account_ids_and_refuses_everything_else() -> None:
    domain = "@quill-cloud-proxy.iam.gserviceaccount.com"
    assert _confidential_accepts("feed-job" + domain)
    assert _confidential_accepts("a-reader-1" + domain)
    for refused in (
        "feed-job@other-project.iam.gserviceaccount.com",
        "12345-compute@developer.gserviceaccount.com",
        "service-1@gcp-sa-secretmanager.iam.gserviceaccount.com",
        "*" + domain,
        "short" + domain[:-1],
        "ab" + domain,
        "Upper-case" + domain,
        "user:someone@example.com",
        "-starts-with-hyphen" + domain,
        # Review round 1: entries that would inject alternatives into the
        # Logging filter, or break its syntax, must be refused whole.
        'feed-job@x" OR "service-1@gcp-sa-secretmanager.iam.gserviceaccount.com" OR "finish' + domain,
        'feed-job@"' + domain,
        "feed-job@feed-job" + domain,
        "feed job" + domain,
        "feed-job\\" + domain,
        "feed-job\n" + domain,
        "feed-job" + domain + "\n",
        "feed-job" + domain + ' OR "x' + domain,
        "feed-job@quill-cloud-proxy.iam.gserviceaccount.com.evil",
    ):
        assert not _confidential_accepts(refused), refused
