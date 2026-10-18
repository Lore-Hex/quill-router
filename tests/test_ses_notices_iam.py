"""The dedicated notices sender must be deployable without broad SES access."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_notices_sender_is_exactly_scoped_and_matches_rollout() -> None:
    policy = json.loads((ROOT / "scripts/ops/policies/ses-notices-sender.json").read_text())
    assert policy["Version"] == "2012-10-17"
    assert policy["Statement"] == [{
        "Sid": "SendProductNotices",
        "Effect": "Allow",
        "Action": "ses:SendEmail",
        "Resource": [
            "arn:aws:ses:us-east-1:330422590279:identity/notices.trustedrouter.com",
            "arn:aws:ses:us-east-1:330422590279:configuration-set/trustedrouter-notices",
        ],
        "Condition": {"StringEquals": {
            "ses:FromAddress": "notices@notices.trustedrouter.com",
        }},
    }]
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    assert "TR_SES_NOTICES_FROM_EMAIL=notices@notices.trustedrouter.com" in rollout
    assert "TR_SES_NOTICES_CONFIGURATION_SET=trustedrouter-notices" in rollout


def test_notices_sender_runbook_preserves_the_existing_policies() -> None:
    source = (ROOT / "docs/runbooks/ses-notices.md").read_text()
    assert "--policy-name TrustedRouterSesNoticesSendOnly" in source
    assert "--user-name trustedrouter-ses-sender" in source
    assert "file://scripts/ops/policies/ses-notices-sender.json" in source
    assert "Do not replace `TrustedRouterSesSendOnly`" in source
