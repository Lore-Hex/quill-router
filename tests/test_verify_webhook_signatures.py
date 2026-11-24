"""scripts/verify_webhook_signatures.py, proven against the real app.

Two properties:

* With every verifier configured as in production, every case gets its
  signature-rejection answer. The script's expectations track the routes.
* With any single verifier switched off, the script reports that case as
  ACCEPTED, not "rejected". Without this check, a probe whose bodies fail
  some later validation would stay green forever.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from scripts import verify_webhook_signatures as probe
from tests.route_inventory import route_methods
from tests.test_adyen_billing import HMAC_KEY as ADYEN_TEST_HMAC_KEY
from tests.test_adyen_billing import REFERENCE_KEY as ADYEN_TEST_REFERENCE_KEY
from trusted_router.config import Settings
from trusted_router.main import create_app

_PROD_LIKE: dict[str, Any] = {
    # "test" keeps app startup offline. Every verifier below is configured, so
    # none of the local/test bypasses can apply. That is the production shape.
    "environment": "test",
    "sentry_dsn": None,
    "stripe_webhook_secret": "whsec_signature_check_fixture",
    "adyen_enabled": True,
    "adyen_api_key": "AQE_TEST_API_KEY",
    "adyen_client_key": "test_CLIENT_KEY",
    "adyen_hmac_key": ADYEN_TEST_HMAC_KEY,
    "adyen_reference_key": ADYEN_TEST_REFERENCE_KEY,
    "adyen_merchant_account": "TrustedRouterUS",
    "adyen_environment": "test",
    "paypal_enabled": True,
    "paypal_client_id": "paypal-client",
    "paypal_client_secret": "paypal-secret",
    "paypal_webhook_id": "WEBHOOKID",
    "routable_enabled": True,
    "routable_api_token": "routable-" + "token",
    "routable_webhook_secret": "routable-" + "webhook-secret",
    "routable_company_id": "company-trustedrouter",
    "routable_team_member_id": "member-operator",
    "routable_withdraw_from_account_id": "account-operating",
    "veriff_enabled": True,
    "veriff_api_key": "api-key",
    "veriff_shared_secret_key": "veriff-webhook-test-secret",
}


@pytest.fixture(autouse=True)
def paypal_answers_failure() -> Iterator[None]:
    """PayPal's verify API, offline: it rejects the probe's forged signature.

    Without this the PayPal case would make a real network call. Controls
    that force SUCCESS patch over it.
    """
    with patch(
        "trusted_router.services.paypal_billing._paypal_post",
        return_value={"verification_status": "FAILURE"},
    ):
        yield


def _client(**overrides: Any) -> TestClient:
    settings = Settings(**{**_PROD_LIKE, **overrides})
    return TestClient(create_app(settings, init_observability=False))


def _poster(client: TestClient) -> probe.Poster:
    # TestClient is an httpx.Client, so this is the script's real poster.
    return probe.http_poster(client, "http://testserver")


def _results(client: TestClient) -> dict[str, probe.CaseResult]:
    return {r.name: r for r in probe.run_cases(_poster(client), retries=0)}


def test_cases_cover_every_inbound_webhook_route() -> None:
    app = create_app(Settings(**_PROD_LIKE), init_observability=False)
    webhook_paths = {
        path
        for path, method in route_methods(app)
        if method == "POST"
        and path.startswith("/internal/")
        and ("webhook" in path or path == "/internal/ses/notifications")
    }
    assert webhook_paths == {case.path for case in probe.CASES}


def test_production_shaped_app_rejects_every_unsigned_delivery() -> None:
    results = _results(_client())
    assert {name: r.verdict for name, r in results.items()} == {
        case.name: "rejected" for case in probe.CASES
    }
    out = io.StringIO()
    assert probe.report(list(results.values()), out=out) == 0
    assert "All 6 webhooks rejected" in out.getvalue()


def test_unconfigured_verifiers_fail_closed_and_count_as_rejected() -> None:
    # A deployed environment with no Veriff or PayPal webhook secret must
    # refuse deliveries, not take the local/test bypass. (Stripe can't be
    # shown here: the deployed-config validator won't boot without its
    # webhook secret, a stronger fail-closed than the route's own 503.)
    client = _client(
        environment="staging",
        allow_deployed_combined_surface=True,
        rate_limit_enabled=False,
        internal_gateway_token="internal-gateway-token-fixture",  # noqa: S106 - test fixture
        stripe_secret_key="sk_test_signature_check_fixture",  # noqa: S106 - test fixture
        veriff_enabled=False,
        veriff_shared_secret_key=None,
        paypal_enabled=False,
        paypal_client_id=None,
        paypal_client_secret=None,
        paypal_webhook_id=None,
    )
    results = _results(client)
    for name in ("veriff", "paypal"):
        assert results[name].verdict == "rejected", results[name]


@contextmanager
def _comparison_forced(name: str) -> Iterator[dict[str, Any]]:
    """Make one route's signature comparison succeed, as a regression would.

    Patched at the comparison (not the whole verifier) where possible, so the
    control also proves the probe's timestamps and header shapes get that far.
    """
    if name == "stripe":
        with patch("stripe._webhook.secure_compare", return_value=True):
            yield {}
    elif name == "adyen":
        with patch("trusted_router.services.adyen_billing.hmac.compare_digest", return_value=True):
            yield {}
    elif name == "paypal":
        with patch(
            "trusted_router.services.paypal_billing._paypal_post",
            return_value={"verification_status": "SUCCESS"},
        ):
            yield {}
    elif name == "routable":
        # Production's company id is unknown to the probe (documented in the
        # script), so this control uses the probe's value to show the HMAC and
        # timestamp are the only things standing in the way.
        with patch("trusted_router.services.routable_payouts.hmac.compare_digest", return_value=True):
            yield {"routable_company_id": "tr-signature-check"}
    elif name == "veriff":
        with patch("trusted_router.routes.internal.veriff.verify_veriff_signature"):
            yield {}
    elif name == "ses":
        # RSA isn't reachable from outside (documented in the script); this
        # control proves the route-level check is what the probe sees.
        with patch("trusted_router.routes.ses_notifications.verify_sns_message"):
            yield {}
    else:  # pragma: no cover - a new case must add its positive control here
        raise AssertionError(f"no positive control for {name}")


@pytest.mark.parametrize("name", [case.name for case in probe.CASES])
def test_a_failed_open_comparison_is_reported(name: str) -> None:
    case = next(c for c in probe.CASES if c.name == name)
    with _comparison_forced(name) as overrides:
        (result,) = probe.run_cases(_poster(_client(**overrides)), cases=(case,), retries=0)
    # Adyen re-checks the merchant account after the signature, which the
    # probe cannot know, so a failed-open Adyen check lands on "merchant
    # mismatch". That is still a failure (UNEXPECTED), just not a 2xx.
    expected = "UNEXPECTED" if name == "adyen" else "ACCEPTED"
    assert result.verdict == expected, result
    out = io.StringIO()
    assert probe.report([result], out=out) == 1
    if expected == "ACCEPTED":
        assert f"signature verification is OFF for: {name}" in out.getvalue()


@pytest.mark.parametrize("name", ["stripe", "veriff", "paypal"])
def test_the_local_bypass_is_reported(name: str) -> None:
    """The env-gated bypasses, reached by a dropped secret plus a local env."""
    overrides: dict[str, Any] = {"environment": "local"}
    overrides[{"stripe": "stripe_webhook_secret", "veriff": "veriff_shared_secret_key",
               "paypal": "paypal_webhook_id"}[name]] = None
    case = next(c for c in probe.CASES if c.name == name)
    (result,) = probe.run_cases(_poster(_client(**overrides)), cases=(case,), retries=0)
    assert result.verdict == "ACCEPTED", result


def test_a_shape_rejection_is_not_a_signature_rejection() -> None:
    # PayPal's cheap header check shares a prefix with its signature failure;
    # exact matching keeps "our probe headers went stale" from reading green.
    case = next(c for c in probe.CASES if c.name == "paypal")
    body = '{"error":{"message":"Invalid PayPal webhook headers"}}'
    assert probe.classify(case, 400, body).verdict == "UNEXPECTED"


def test_a_rejection_for_another_reason_is_not_a_pass() -> None:
    case = next(c for c in probe.CASES if c.name == "adyen")
    result = probe.classify(case, 400, '{"error":{"message":"Adyen webhook merchant mismatch"}}')
    assert result.verdict == "UNEXPECTED"
    assert not result.ok
    assert probe.classify(case, 404, "Not Found").verdict == "UNEXPECTED"
    assert probe.classify(case, 500, "boom").verdict == "UNEXPECTED"


def test_transport_failure_is_a_failure_not_a_pass() -> None:
    def down(_case: probe.WebhookCase) -> httpx.Response:
        raise httpx.ConnectError("refused")

    results = probe.run_cases(down, retries=0)
    assert all(r.verdict == "UNEXPECTED" and r.status is None for r in results)
    assert probe.report(results, out=io.StringIO()) == 1


def test_workflow_runs_the_script_daily_and_after_deploys() -> None:
    from pathlib import Path

    workflow = Path(".github/workflows/webhook-signature-check.yml").read_text()
    assert "scripts/verify_webhook_signatures.py" in workflow
    assert "schedule:" in workflow
    assert 'workflows: ["Deploy TR control plane"]' in workflow
