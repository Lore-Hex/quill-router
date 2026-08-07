#!/usr/bin/env python3
"""Provision and verify TrustedRouter's purpose-specific SES sender lanes.

Provisioning and IAM updates are separate on purpose:

1. ``--apply`` creates/updates SES identities and configuration sets.
2. Publish the emitted DNS records and wait for ``--verify`` to pass.
3. ``--update-iam`` grants the runtime user only the verified identities.

The default invocation is read-only and prints the desired profile inventory.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trusted_router.email_profiles import (  # noqa: E402
    EMAIL_SENDER_PROFILES,
    EmailSenderProfile,
)

DEFAULT_REGION = "us-east-1"
DEFAULT_RUNTIME_USER = "trustedrouter-ses-sender"
DEFAULT_POLICY_NAME = "TrustedRouterSesSendOnly"
EVENT_DESTINATION_NAME = "ses-feedback-sns"


class AwsCommandError(RuntimeError):
    pass


def aws_json(arguments: list[str], *, allow_not_found: bool = False) -> dict[str, Any] | None:
    command = ["aws", *arguments, "--output", "json"]
    result = subprocess.run(  # noqa: S603 - fixed executable, argv list, and operator-owned values.
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if allow_not_found and (
            "NotFoundException" in stderr or "ConfigurationSetDoesNotExist" in stderr
        ):
            return None
        raise AwsCommandError(f"{' '.join(command[:4])} failed: {stderr}")
    if not result.stdout.strip():
        return {}
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AwsCommandError(f"{' '.join(command[:4])} returned non-object JSON")
    return payload


def account_id() -> str:
    payload = aws_json(["sts", "get-caller-identity"])
    assert payload is not None
    return str(payload["Account"])


def _ses(region: str, *arguments: str) -> list[str]:
    return ["sesv2", *arguments, "--region", region]


def provision_configuration_set(
    profile: EmailSenderProfile,
    *,
    region: str,
    topic_arn: str,
) -> None:
    existing = aws_json(
        _ses(
            region, "get-configuration-set", "--configuration-set-name", profile.configuration_set
        ),
        allow_not_found=True,
    )
    if existing is None:
        aws_json(
            _ses(
                region,
                "create-configuration-set",
                "--configuration-set-name",
                profile.configuration_set,
            )
        )
    aws_json(
        _ses(
            region,
            "put-configuration-set-delivery-options",
            "--configuration-set-name",
            profile.configuration_set,
            "--tls-policy",
            "REQUIRE",
        )
    )
    aws_json(
        _ses(
            region,
            "put-configuration-set-reputation-options",
            "--configuration-set-name",
            profile.configuration_set,
            "--reputation-metrics-enabled",
        )
    )
    aws_json(
        _ses(
            region,
            "put-configuration-set-suppression-options",
            "--configuration-set-name",
            profile.configuration_set,
            "--suppressed-reasons",
            "BOUNCE",
            "COMPLAINT",
        )
    )
    destination = {
        "Enabled": True,
        "MatchingEventTypes": ["BOUNCE", "COMPLAINT"],
        "SnsDestination": {"TopicArn": topic_arn},
    }
    destinations = aws_json(
        _ses(
            region,
            "get-configuration-set-event-destinations",
            "--configuration-set-name",
            profile.configuration_set,
        )
    )
    assert destinations is not None
    names = {
        row.get("Name")
        for row in destinations.get("EventDestinations", [])
        if isinstance(row, dict)
    }
    action = "update" if EVENT_DESTINATION_NAME in names else "create"
    aws_json(
        _ses(
            region,
            f"{action}-configuration-set-event-destination",
            "--configuration-set-name",
            profile.configuration_set,
            "--event-destination-name",
            EVENT_DESTINATION_NAME,
            "--event-destination",
            json.dumps(destination, separators=(",", ":")),
        )
    )


def provision_identity(profile: EmailSenderProfile, *, region: str) -> dict[str, Any]:
    identity = aws_json(
        _ses(region, "get-email-identity", "--email-identity", profile.identity_domain),
        allow_not_found=True,
    )
    if identity is None:
        aws_json(_ses(region, "create-email-identity", "--email-identity", profile.identity_domain))
    aws_json(
        _ses(
            region,
            "put-email-identity-configuration-set-attributes",
            "--email-identity",
            profile.identity_domain,
            "--configuration-set-name",
            profile.configuration_set,
        )
    )
    aws_json(
        _ses(
            region,
            "put-email-identity-mail-from-attributes",
            "--email-identity",
            profile.identity_domain,
            "--mail-from-domain",
            profile.mail_from_domain,
            "--behavior-on-mx-failure",
            "REJECT_MESSAGE",
        )
    )
    refreshed = aws_json(
        _ses(region, "get-email-identity", "--email-identity", profile.identity_domain)
    )
    assert refreshed is not None
    return refreshed


def dns_records(
    profile: EmailSenderProfile,
    identity: dict[str, Any],
    *,
    region: str,
) -> list[dict[str, Any]]:
    dkim = identity.get("DkimAttributes")
    tokens = dkim.get("Tokens", []) if isinstance(dkim, dict) else []
    records = [
        {
            "name": f"{token}._domainkey.{profile.identity_domain}.",
            "type": "CNAME",
            "ttl": 300,
            "rrdatas": [f"{token}.dkim.amazonses.com."],
        }
        for token in tokens
    ]
    records.extend(
        [
            {
                "name": f"{profile.mail_from_domain}.",
                "type": "MX",
                "ttl": 300,
                "rrdatas": [f"10 feedback-smtp.{region}.amazonses.com."],
            },
            {
                "name": f"{profile.mail_from_domain}.",
                "type": "TXT",
                "ttl": 300,
                "rrdatas": ['"v=spf1 include:amazonses.com -all"'],
            },
            {
                "name": f"_dmarc.{profile.identity_domain}.",
                "type": "TXT",
                "ttl": 300,
                "rrdatas": ['"v=DMARC1; p=quarantine; sp=quarantine; adkim=s; aspf=r; pct=100"'],
            },
        ]
    )
    return records


def verify_profile(
    profile: EmailSenderProfile,
    *,
    region: str,
    topic_arn: str,
) -> list[str]:
    failures: list[str] = []
    identity = aws_json(
        _ses(region, "get-email-identity", "--email-identity", profile.identity_domain),
        allow_not_found=True,
    )
    if identity is None:
        return [f"{profile.name}: identity missing"]
    if identity.get("VerifiedForSendingStatus") is not True:
        failures.append(f"{profile.name}: identity is not verified for sending")
    dkim = identity.get("DkimAttributes")
    if not isinstance(dkim, dict) or dkim.get("Status") != "SUCCESS":
        failures.append(f"{profile.name}: DKIM is not SUCCESS")
    mail_from = identity.get("MailFromAttributes")
    if not isinstance(mail_from, dict) or mail_from.get("MailFromDomainStatus") != "SUCCESS":
        failures.append(f"{profile.name}: custom MAIL FROM is not SUCCESS")
    if identity.get("ConfigurationSetName") != profile.configuration_set:
        failures.append(f"{profile.name}: default configuration set does not match")

    configuration = aws_json(
        _ses(
            region, "get-configuration-set", "--configuration-set-name", profile.configuration_set
        ),
        allow_not_found=True,
    )
    if configuration is None:
        failures.append(f"{profile.name}: configuration set is missing")
    else:
        if configuration.get("DeliveryOptions", {}).get("TlsPolicy") != "REQUIRE":
            failures.append(f"{profile.name}: TLS is not required")
        if configuration.get("ReputationOptions", {}).get("ReputationMetricsEnabled") is not True:
            failures.append(f"{profile.name}: reputation metrics are disabled")
        reasons = set(configuration.get("SuppressionOptions", {}).get("SuppressedReasons", []))
        if reasons != {"BOUNCE", "COMPLAINT"}:
            failures.append(f"{profile.name}: suppression is not fail-closed")
    destinations = aws_json(
        _ses(
            region,
            "get-configuration-set-event-destinations",
            "--configuration-set-name",
            profile.configuration_set,
        ),
        allow_not_found=True,
    )
    rows = destinations.get("EventDestinations", []) if destinations else []
    feedback = next(
        (
            row
            for row in rows
            if isinstance(row, dict) and row.get("Name") == EVENT_DESTINATION_NAME
        ),
        None,
    )
    if (
        feedback is None
        or feedback.get("Enabled") is not True
        or set(feedback.get("MatchingEventTypes", [])) != {"BOUNCE", "COMPLAINT"}
        or feedback.get("SnsDestination", {}).get("TopicArn") != topic_arn
    ):
        failures.append(f"{profile.name}: SNS feedback destination does not match")
    return failures


def runtime_iam_policy(
    *,
    aws_account_id: str,
    region: str,
    include_legacy: bool,
) -> dict[str, Any]:
    statements: list[dict[str, Any]] = []
    for profile in EMAIL_SENDER_PROFILES:
        statements.append(
            {
                "Sid": f"Send{profile.name.title()}Email",
                "Effect": "Allow",
                "Action": "ses:SendEmail",
                "Resource": [
                    f"arn:aws:ses:{region}:{aws_account_id}:identity/{profile.identity_domain}",
                    f"arn:aws:ses:{region}:{aws_account_id}:configuration-set/{profile.configuration_set}",
                ],
                "Condition": {"StringEquals": {"ses:FromAddress": profile.from_email}},
            }
        )
    if include_legacy:
        statements.append(
            {
                "Sid": "LegacyRollbackOnly",
                "Effect": "Allow",
                "Action": "ses:SendEmail",
                "Resource": [
                    f"arn:aws:ses:{region}:{aws_account_id}:identity/trustedrouter.com",
                    f"arn:aws:ses:{region}:{aws_account_id}:configuration-set/trustedrouter-default",
                ],
                "Condition": {"StringEquals": {"ses:FromAddress": "noreply@trustedrouter.com"}},
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--topic-arn")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--dns-output", type=Path)
    parser.add_argument("--update-iam", action="store_true")
    parser.add_argument("--runtime-user", default=DEFAULT_RUNTIME_USER)
    parser.add_argument("--policy-name", default=DEFAULT_POLICY_NAME)
    parser.add_argument("--include-legacy", action="store_true")
    args = parser.parse_args()

    aws_account_id = account_id()
    topic_arn = args.topic_arn or f"arn:aws:sns:{args.region}:{aws_account_id}:ses-feedback"
    identities: dict[str, dict[str, Any]] = {}

    if args.apply:
        for profile in EMAIL_SENDER_PROFILES:
            provision_configuration_set(profile, region=args.region, topic_arn=topic_arn)
            identities[profile.name] = provision_identity(profile, region=args.region)

    failures: list[str] = []
    if args.verify or args.update_iam:
        for profile in EMAIL_SENDER_PROFILES:
            failures.extend(verify_profile(profile, region=args.region, topic_arn=topic_arn))
        if failures:
            for failure in failures:
                print(f"ERROR: {failure}", file=sys.stderr)
            return 1

    if args.dns_output:
        records: list[dict[str, Any]] = []
        for profile in EMAIL_SENDER_PROFILES:
            identity = identities.get(profile.name)
            if identity is None:
                identity = aws_json(
                    _ses(
                        args.region,
                        "get-email-identity",
                        "--email-identity",
                        profile.identity_domain,
                    )
                )
                assert identity is not None
            records.extend(dns_records(profile, identity, region=args.region))
        args.dns_output.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")

    if args.update_iam:
        policy = runtime_iam_policy(
            aws_account_id=aws_account_id,
            region=args.region,
            include_legacy=args.include_legacy,
        )
        aws_json(
            [
                "iam",
                "put-user-policy",
                "--user-name",
                args.runtime_user,
                "--policy-name",
                args.policy_name,
                "--policy-document",
                json.dumps(policy, separators=(",", ":")),
            ]
        )

    print(
        json.dumps(
            {
                "region": args.region,
                "topic_arn": topic_arn,
                "profiles": [
                    {
                        "name": profile.name,
                        "identity": profile.identity_domain,
                        "from_email": profile.from_email,
                        "mail_from": profile.mail_from_domain,
                        "configuration_set": profile.configuration_set,
                    }
                    for profile in EMAIL_SENDER_PROFILES
                ],
                "verified": bool(args.verify or args.update_iam) and not failures,
                "iam_updated": args.update_iam,
                "legacy_allowed": args.include_legacy,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
