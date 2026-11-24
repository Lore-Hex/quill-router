#!/usr/bin/env python3
"""Check that every inbound webhook rejects an unsigned delivery.

Every money and identity webhook the control plane exposes (Stripe, Adyen,
PayPal, Routable, Veriff, SES/SNS) trusts its body only after checking the
sender's signature. Those checks are tested offline, but nothing re-checks
them in production. A config change can switch one off without anyone
noticing, for example a webhook secret dropped from a deploy or an environment
renamed so that a local-only bypass applies. Once that happens, anyone can
post a forged "payment succeeded" event.

This script sends each endpoint a delivery with a missing or wrong signature
and requires the endpoint's own signature-rejection answer.

    uv run python scripts/verify_webhook_signatures.py
    uv run python scripts/verify_webhook_signatures.py --base-url https://…

WHAT COUNTS AS A PASS
    The status code AND the exact error message must match what that route
    returns when it rejects a signature (or when signature checking is not
    configured, which also fails closed). Any other rejection does not count.
    A 400 for "merchant mismatch" means the request got past the signature
    check, so it is reported as a failure, not as "it was rejected anyway".
    Timestamps are fresh and header shapes are valid so that, where the route
    allows it, the request reaches the signature comparison itself rather
    than an earlier freshness or format check.

WHAT IT DOES NOT PROVE
    Its job is catching CONFIGURATION regressions: a missing secret, a
    renamed environment, a disabled provider flag. Library or code bugs in
    the comparison are the offline tests' job. Two routes can't be pushed all
    the way to the comparison from outside:
      * Routable answers a bare 401 for a bad HMAC, a stale timestamp and a
        foreign company id alike, and the probe doesn't know our company id.
      * SES/SNS needs a real AWS signing-cert URL to reach RSA verification.
        The probe uses an untrusted host, so it proves the route verifies at
        all and refuses outside certificates, not that RSA verification
        works.

WHY THE BODIES ARE SAFE TO SEND
    Each body does nothing that moves money, changes identity or blocks
    email, even if verification has failed open: an unknown event type, an
    unknown notification kind, a non-payable resource, an empty Veriff
    payload. At most a failed-open route records a replay-dedupe row.
    tests/test_verify_webhook_signatures.py runs each case against the real
    app with its comparison forced to succeed. Nothing is billed. PayPal's
    case makes one verify call to PayPal, which answers FAILURE.

Exit status: 0 only if every endpoint gave its signature-rejection answer.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

DEFAULT_BASE_URL = "https://trustedrouter.com"
_BAD_HEX_SIG = "0" * 64


@dataclass(frozen=True)
class WebhookCase:
    name: str
    path: str
    body: dict[str, object]
    # Built at send time so timestamps are fresh: an expired timestamp is
    # rejected before the signature comparison on Stripe and Routable.
    headers: Callable[[], dict[str, str]] = field(default=lambda: {})
    # (status, exact error.message) pairs that mean "rejected for signature
    # reasons". None matches any body: Routable's 401 has no body.
    rejections: tuple[tuple[int, str | None], ...] = ()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


CASES: tuple[WebhookCase, ...] = (
    WebhookCase(
        name="stripe",
        path="/internal/stripe/webhook",
        body={
            "id": "evt_tr_signature_check",
            "object": "event",
            "type": "tr.signature_check",
            "data": {"object": {"object": "tr_signature_check", "metadata": {}}},
        },
        headers=lambda: {"stripe-signature": f"t={int(time.time())},v1={_BAD_HEX_SIG}"},
        rejections=(
            (400, "Invalid Stripe webhook"),
            (400, "Missing Stripe signature"),
            (503, "Stripe webhook verification is not configured"),
        ),
    ),
    WebhookCase(
        name="adyen",
        path="/internal/adyen/webhook",
        body={
            "live": "false",
            "notificationItems": [
                {
                    "NotificationRequestItem": {
                        "eventCode": "TR_SIGNATURE_CHECK",
                        "pspReference": "tr-signature-check",
                        "merchantAccountCode": "tr-signature-check",
                        "success": "false",
                        "additionalData": {"hmacSignature": "AAAA"},
                    }
                }
            ],
        },
        rejections=(
            (400, "Invalid Adyen webhook signature"),
            (503, "Adyen webhook verification is not configured"),
        ),
    ),
    WebhookCase(
        name="paypal",
        path="/internal/paypal/webhook",
        body={"id": "WH-TR-SIGNATURE-CHECK", "event_type": "TR.SIGNATURE_CHECK", "resource": {}},
        # Well-formed headers so the delivery passes the local shape check and
        # reaches PayPal's verify API, which answers FAILURE for this signature.
        headers=lambda: {
            "paypal-transmission-id": str(uuid.uuid4()),
            "paypal-transmission-time": _now_iso(),
            "paypal-cert-url": "https://api.paypal.com/v1/notifications/certs/CERT-tr-signature-check",
            "paypal-auth-algo": "SHA256withRSA",
            "paypal-transmission-sig": "AAAA",
        },
        rejections=(
            (400, "Invalid PayPal webhook"),
            (400, "PayPal webhook verification is not configured"),
            (400, "PayPal webhook is not configured"),
        ),
    ),
    WebhookCase(
        name="routable",
        path="/internal/routable/webhook",
        body={
            "event_name": "tr.signature_check",
            "event_resource": "tr_signature_check",
            "company_id": "tr-signature-check",
            "object_id": "tr-signature-check",
        },
        headers=lambda: {
            "Routable-Signature": _BAD_HEX_SIG,
            "Routable-Signature-Timestamp": _now_iso(),
        },
        rejections=((401, None),),
    ),
    WebhookCase(
        name="veriff",
        path="/internal/veriff/webhook",
        body={"tr_signature_check": True},
        headers=lambda: {"x-hmac-signature": _BAD_HEX_SIG},
        rejections=(
            (403, "Veriff signature verification failed"),
            (403, "Veriff webhook is not configured"),
        ),
    ),
    WebhookCase(
        name="ses",
        path="/internal/ses/notifications",
        body={
            "Type": "Notification",
            "MessageId": "tr-signature-check",
            "TopicArn": "arn:aws:sns:us-east-1:000000000000:tr-signature-check",
            "Message": json.dumps({"notificationType": "TR_SIGNATURE_CHECK"}),
            "Timestamp": "2026-01-01T00:00:00.000Z",
            "SignatureVersion": "1",
            "Signature": "AAAA",
            # Not an AWS host, so the verifier rejects it before fetching
            # anything. The check is still the production verifier.
            "SigningCertURL": "https://signature-check.invalid/cert.pem",
        },
        rejections=((403, "SNS signature verification failed"),),
    ),
)


@dataclass(frozen=True)
class CaseResult:
    name: str
    verdict: str  # "rejected" | "ACCEPTED" | "UNEXPECTED"
    status: int | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.verdict == "rejected"


def _error_message(body: str) -> str | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else None


def classify(case: WebhookCase, status: int, body: str) -> CaseResult:
    message = _error_message(body)
    for expected_status, expected_message in case.rejections:
        if status != expected_status:
            continue
        if expected_message is None or message == expected_message:
            return CaseResult(case.name, "rejected", status, expected_message or f"HTTP {status}")
    snippet = " ".join(body.split())[:160]
    if 200 <= status < 400:
        return CaseResult(
            case.name,
            "ACCEPTED",
            status,
            f"an unsigned delivery was accepted: {snippet}",
        )
    return CaseResult(
        case.name,
        "UNEXPECTED",
        status,
        f"not a signature rejection (route moved, crashed, or checked something else first): {snippet}",
    )


Poster = Callable[[WebhookCase], httpx.Response]


def run_cases(
    post: Poster,
    *,
    cases: tuple[WebhookCase, ...] = CASES,
    retries: int = 1,
    retry_delay: float = 2.0,
) -> list[CaseResult]:
    results = []
    for case in cases:
        response: httpx.Response | None = None
        error = ""
        for attempt in range(retries + 1):
            try:
                response = post(case)
                break
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt < retries:
                    time.sleep(retry_delay)
        if response is None:
            results.append(CaseResult(case.name, "UNEXPECTED", None, f"no response: {error}"))
            continue
        results.append(classify(case, response.status_code, response.text))
    return results


def http_poster(client: httpx.Client, base_url: str) -> Poster:
    base = base_url.rstrip("/")

    def post(case: WebhookCase) -> httpx.Response:
        return client.post(
            base + case.path,
            content=json.dumps(case.body).encode(),
            headers={"content-type": "application/json", **case.headers()},
        )

    return post


def report(results: list[CaseResult], out=sys.stdout) -> int:
    for result in results:
        status = "-" if result.status is None else str(result.status)
        mark = "ok  " if result.ok else "FAIL"
        print(f"[{mark}] {result.name:<9} {result.verdict:<10} HTTP {status:<4} {result.detail}", file=out)
    failed = [r for r in results if not r.ok]
    if not failed:
        print(f"All {len(results)} webhooks rejected an unsigned delivery.", file=out)
        return 0
    accepted = [r.name for r in failed if r.verdict == "ACCEPTED"]
    if accepted:
        print(f"CRITICAL: signature verification is OFF for: {', '.join(accepted)}", file=out)
    print(f"{len(failed)} of {len(results)} webhooks did not give a signature rejection.", file=out)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        results = run_cases(http_poster(client, args.base_url))
    return report(results)


if __name__ == "__main__":
    sys.exit(main())
