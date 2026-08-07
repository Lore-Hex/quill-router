from __future__ import annotations

from pathlib import Path

from scripts.deploy.provision_ses_sender_profiles import dns_records, runtime_iam_policy
from trusted_router.config import Settings
from trusted_router.email_profiles import EMAIL_SENDER_PROFILES

ROOT = Path(__file__).resolve().parents[1]


def test_sender_profiles_are_unique_and_use_dedicated_subdomains() -> None:
    assert {profile.name for profile in EMAIL_SENDER_PROFILES} == {
        "auth",
        "onboarding",
        "alerts",
        "support",
        "partners",
    }
    assert len({profile.identity_domain for profile in EMAIL_SENDER_PROFILES}) == 5
    assert len({profile.configuration_set for profile in EMAIL_SENDER_PROFILES}) == 5
    assert len({profile.from_email for profile in EMAIL_SENDER_PROFILES}) == 5
    for profile in EMAIL_SENDER_PROFILES:
        assert profile.identity_domain.endswith(".trustedrouter.com")
        assert profile.identity_domain != "trustedrouter.com"
        assert profile.mail_from_domain == f"mail.{profile.identity_domain}"


def test_settings_defaults_match_the_provisioned_profile_inventory() -> None:
    settings = Settings(environment="test")
    for profile in EMAIL_SENDER_PROFILES:
        assert getattr(settings, f"ses_{profile.settings_name}_from_email") == profile.from_email
        assert getattr(settings, f"ses_{profile.settings_name}_from_name") == profile.from_name
        assert (
            getattr(settings, f"ses_{profile.settings_name}_configuration_set")
            == profile.configuration_set
        )


def test_dns_records_include_dkim_and_fail_closed_mail_from() -> None:
    profile = EMAIL_SENDER_PROFILES[0]
    records = dns_records(
        profile,
        {"DkimAttributes": {"Tokens": ["one", "two", "three"]}},
        region="us-east-1",
    )

    assert records[:3] == [
        {
            "name": f"{token}._domainkey.{profile.identity_domain}.",
            "type": "CNAME",
            "ttl": 300,
            "rrdatas": [f"{token}.dkim.amazonses.com."],
        }
        for token in ("one", "two", "three")
    ]
    assert records[3]["rrdatas"] == ["10 feedback-smtp.us-east-1.amazonses.com."]
    assert records[4]["rrdatas"] == ['"v=spf1 include:amazonses.com -all"']
    assert records[5]["name"] == f"_dmarc.{profile.identity_domain}."
    assert records[5]["rrdatas"] == [
        '"v=DMARC1; p=quarantine; sp=quarantine; adkim=s; aspf=r; pct=100"'
    ]


def test_runtime_iam_policy_has_no_wildcards_or_legacy_sender() -> None:
    policy = runtime_iam_policy(
        aws_account_id="123456789012",
        region="us-east-1",
        include_legacy=False,
    )

    statements = policy["Statement"]
    assert len(statements) == len(EMAIL_SENDER_PROFILES)
    assert all(statement["Action"] == "ses:SendEmail" for statement in statements)
    assert all("*" not in str(statement) for statement in statements)
    assert "noreply@trustedrouter.com" not in str(policy)
    assert {
        statement["Condition"]["StringEquals"]["ses:FromAddress"] for statement in statements
    } == {profile.from_email for profile in EMAIL_SENDER_PROFILES}


def test_rollout_configures_every_sender_profile() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    for profile in EMAIL_SENDER_PROFILES:
        prefix = f"TR_SES_{profile.settings_name.upper()}"
        assert f'"{prefix}_FROM_EMAIL={profile.from_email}"' in rollout
        assert f'"{prefix}_FROM_NAME={profile.from_name}"' in rollout
        assert f'"{prefix}_CONFIGURATION_SET={profile.configuration_set}"' in rollout
