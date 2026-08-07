"""Canonical purpose-specific SES sender profiles.

The same definitions drive runtime defaults and infrastructure provisioning so
the app cannot drift from the verified SES identities or configuration sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SenderProfile = Literal["auth", "onboarding", "alerts", "support", "partners"]


@dataclass(frozen=True)
class EmailSenderProfile:
    name: SenderProfile
    settings_name: str
    identity_domain: str
    local_part: str
    from_name: str
    configuration_set: str

    @property
    def from_email(self) -> str:
        return f"{self.local_part}@{self.identity_domain}"

    @property
    def mail_from_domain(self) -> str:
        return f"mail.{self.identity_domain}"


EMAIL_SENDER_PROFILES: tuple[EmailSenderProfile, ...] = (
    EmailSenderProfile(
        name="auth",
        settings_name="auth",
        identity_domain="auth.trustedrouter.com",
        local_part="accounts",
        from_name="TrustedRouter Accounts",
        configuration_set="trustedrouter-auth",
    ),
    EmailSenderProfile(
        name="onboarding",
        settings_name="onboarding",
        identity_domain="onboarding.trustedrouter.com",
        local_part="hello",
        from_name="TrustedRouter",
        configuration_set="trustedrouter-onboarding",
    ),
    EmailSenderProfile(
        name="alerts",
        settings_name="alert",
        identity_domain="alerts.trustedrouter.com",
        local_part="alerts",
        from_name="TrustedRouter Alerts",
        configuration_set="trustedrouter-alerts",
    ),
    EmailSenderProfile(
        name="support",
        settings_name="support",
        identity_domain="support.trustedrouter.com",
        local_part="support",
        from_name="TrustedRouter Support",
        configuration_set="trustedrouter-support",
    ),
    EmailSenderProfile(
        name="partners",
        settings_name="partner",
        identity_domain="partners.trustedrouter.com",
        local_part="partners",
        from_name="TrustedRouter Partnerships",
        configuration_set="trustedrouter-partners",
    ),
)

EMAIL_SENDER_PROFILE_BY_NAME: dict[SenderProfile, EmailSenderProfile] = {
    profile.name: profile for profile in EMAIL_SENDER_PROFILES
}
