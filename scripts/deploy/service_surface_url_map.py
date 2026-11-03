#!/usr/bin/env python3
"""Build the four-service HTTPS URL map without disturbing unrelated hosts.

The public service is the default failure domain.  Only the explicit patterns
below can reach the authenticated control service or the internal billing
service.  Keep this module importable: route-totality tests compare every
registered FastAPI route with this exact deployment contract.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any, Literal

Surface = Literal["public", "actions", "control", "internal"]

PUBLIC_PATH_PATTERNS = (
    "/analytics/events",
    "/notify/texml",
    "/v1/health",
    "/v1/ready",
    "/v1/coverage/openrouter",
    "/v1/analytics/events",
    "/v1/embeddings/models",
    # Anonymous image-catalog reads (#693). Exact-and-under patterns beat the
    # authenticated /images/* control wildcard by specificity, exactly as
    # /v1/models/* beats /v1/*; image GENERATION stays on control.
    "/images/models",
    "/images/models/*",
    "/v1/images/models",
    "/v1/images/models/*",
    "/v1/models",
    "/v1/models/count",
    "/v1/models/picker",
    "/v1/models/*",
    "/v1/notify/texml",
    "/v1/endpoints/zdr",
    "/v1/regions",
    "/v1/providers",
)

ACTIONS_PATH_PATTERNS = (
    "/support/inquiry",
    "/token-exchange/brief",
    "/token-exchange/events",
    "/token-exchange/reserve/checkout",
    "/token-exchange/reserve/quote",
    "/trustedos/inquiry",
)

CONTROL_PATH_PATTERNS = (
    "/token-exchange/reservations",
    "/v1",
    "/v1/*",
    "/models/user",
    "/v1/models/user",
    "/bedrock-group-buy",
    "/bedrock-group-buy/*",
    "/google_oauth_callback",
    "/github_oauth_callback",
    "/console",
    "/console/*",
    "/provider",
    "/provider/*",
    "/mcp",
    "/chat-proxy",
    "/chat-proxy/*",
    "/auth",
    # Conformant OAuth consent + token exchange need the session cookie,
    # Stripe and key minting, so they belong beside /auth on control. The
    # /v1 twins already match the /v1/* control rule; these unversioned
    # aliases would otherwise fall through to the public default.
    "/oauth/authorize",
    "/oauth/token",
    "/auth/*",
    "/oauth/apps",
    "/oauth/apps/*",
    "/oauth/authorized-apps",
    "/oauth/authorized-apps/*",
    "/v1/oauth/apps",
    "/v1/oauth/apps/*",
    "/v1/oauth/authorized-apps",
    "/v1/oauth/authorized-apps/*",
    "/byok",
    "/byok/*",
    "/credits",
    "/credits/*",
    "/billing",
    "/billing/*",
    # Retired endpoint: keep the old edge assignment recognizable during
    # rolling URL-map validation. No application route is mounted anymore.
    "/lightning/feedback",
    "/broadcast",
    "/broadcast/*",
    "/payouts",
    "/payouts/*",
    "/custom-models",
    "/custom-models/*",
    "/user-models",
    "/user-models/*",
    "/key",
    "/keys",
    "/keys/*",
    "/activity",
    "/settlements",
    "/settlements/*",
    "/generation",
    "/generation/*",
    "/client-events",
    "/workspaces",
    "/workspaces/*",
    "/organization",
    "/organization/*",
    "/chat/completions",
    "/messages",
    "/embeddings",
    "/decide",
    "/evaluate",
    "/responses",
    "/tools/fetch",
    "/audio",
    "/audio/*",
    "/rerank",
    "/videos",
    "/videos/*",
    "/private",
    "/private/*",
    "/guardrails",
    "/guardrails/*",
    "/analytics",
    "/analytics/*",
    "/classifications",
    "/classifications/*",
    "/datasets",
    "/datasets/*",
    "/files",
    "/files/*",
    "/images",
    "/images/*",
    "/model",
    "/model/*",
    "/observability",
    "/observability/*",
    "/presets",
    "/presets/*",
    "/scim",
    "/scim/*",
    "/signup",
    "/notify",
    "/notify/*",
    "/internal/stripe/webhook",
    "/internal/paypal/webhook",
    "/internal/adyen/webhook",
    "/internal/veriff/webhook",
    "/internal/routable/webhook",
    "/internal/ses/notifications",
    "/internal/chat/issue-browser-key",
    "/internal/broadcast/drain",
    "/v1/internal/stripe/webhook",
    "/v1/internal/paypal/webhook",
    "/v1/internal/adyen/webhook",
    "/v1/internal/veriff/webhook",
    "/v1/internal/routable/webhook",
    "/v1/internal/ses/notifications",
    "/v1/internal/chat/issue-browser-key",
    "/v1/internal/broadcast/drain",
)

GATEWAY_PATH_PATTERNS = (
    "/internal/gateway",
    "/internal/gateway/*",
    "/v1/internal/gateway",
    "/v1/internal/gateway/*",
)

INTERNAL_PATH_PATTERNS = (
    "/internal",
    "/internal/*",
    "/v1/internal",
    "/v1/internal/*",
)

_PATTERNS: dict[Surface, tuple[str, ...]] = {
    "public": PUBLIC_PATH_PATTERNS,
    "actions": ACTIONS_PATH_PATTERNS,
    "control": CONTROL_PATH_PATTERNS,
    "internal": INTERNAL_PATH_PATTERNS,
}
# The enclave's terminal POSTs. The disposition GET stays on /internal/*: it is
# a recovery read, and pathRules only support a trailing /*, which would also
# capture the by-gateway-request-id lookup.
TERMINAL_PATH_PATTERNS = (
    "/internal/gateway/settle",
    "/internal/gateway/heartbeat",
    "/internal/gateway/refund",
    "/v1/internal/gateway/settle",
    "/v1/internal/gateway/heartbeat",
    "/v1/internal/gateway/refund",
)
TERMINAL_BACKEND_NAME = "trusted-router-control-terminal-c1-backend"


def terminal_route_weight() -> int:
    """One switch for Terraform and every renderer caller; fail closed on drift."""
    source = (Path(__file__).resolve().parents[2] / "infra/control_terminal_backend.tf").read_text()
    weights = re.findall(r"^\s*terminal_c1_route_weight\s*=\s*(\d+)\s*$", source, re.MULTILINE)
    if len(weights) != 1 or not 0 <= int(weights[0]) <= 100:
        raise ValueError("expected one terminal_c1_route_weight integer in [0, 100]")
    return int(weights[0])


def billing_override_rules(existing: dict[str, Any]) -> list[dict[str, Any]]:
    """Recognize the narrow overrides without confusing them with gateway/*."""
    found = []
    seen: set[str] = set()
    for matcher in existing.get("pathMatchers", []):
        if matcher.get("name") != _MATCHER_NAME:
            continue
        for rule in matcher.get("pathRules", []):
            paths = set(rule.get("paths", []))
            if paths & (set(TERMINAL_PATH_PATTERNS) | {"/internal/gateway/authorize"}):
                kind = "authorize" if paths == {"/internal/gateway/authorize"} else "terminals"
                expected = {"/internal/gateway/authorize"} if kind == "authorize" else set(TERMINAL_PATH_PATTERNS)
                if paths != expected or kind in seen or not rule.get("routeAction"):
                    raise ValueError("ambiguous billing override; refusing to discard live routing")
                seen.add(kind)
                found.append(rule)
    return found


_MATCHER_NAME = "trusted-router-service-surfaces"
_OUTPUT_ONLY_KEYS = {
    "creationTimestamp",
    "id",
    "kind",
    "selfLink",
}


def _match_specificity(path: str, pattern: str) -> tuple[int, int] | None:
    if pattern.endswith("/*"):
        prefix = pattern[:-1]
        if path.startswith(prefix):
            return (len(prefix), 0)
        return None
    if path == pattern:
        return (len(pattern), 1)
    return None


def route_surface(path: str) -> Surface:
    """Return the backend selected by GCP's longest-path-match semantics."""
    matches: list[tuple[tuple[int, int], Surface, str]] = []
    for surface, patterns in _PATTERNS.items():
        for pattern in patterns:
            specificity = _match_specificity(path, pattern)
            if specificity is not None:
                matches.append((specificity, surface, pattern))
    if not matches:
        return "public"
    best = max(specificity for specificity, _, _ in matches)
    winners = [(surface, pattern) for specificity, surface, pattern in matches if specificity == best]
    owners = {surface for surface, _ in winners}
    if len(owners) != 1:
        raise ValueError(f"ambiguous URL-map ownership for {path!r}: {winners!r}")
    return winners[0][0]


def is_gateway_path(path: str) -> bool:
    return any(_match_specificity(path, pattern) is not None for pattern in GATEWAY_PATH_PATTERNS)


def existing_gateway_backend(existing: dict[str, Any]) -> str | None:
    """Preserve an installed override; refuse partial/ambiguous gateway rules."""
    overrides = billing_override_rules(existing)
    rules = [
        rule
        for matcher in existing.get("pathMatchers", [])
        if matcher.get("name") == _MATCHER_NAME
        for rule in matcher.get("pathRules", [])
        if rule not in overrides
        and any(is_gateway_path(path.removesuffix("*")) for path in rule.get("paths", []))
    ]
    if not rules:
        return None
    if (len(rules) != 1 or set(rules[0].get("paths", [])) != set(GATEWAY_PATH_PATTERNS)
            or not rules[0].get("service")):
        raise ValueError("ambiguous gateway backend; refusing to drop or replace live rules")
    return str(rules[0]["service"])


def first_party_hosts(domains: list[str] | tuple[str, ...]) -> list[str]:
    normalized = sorted({domain.strip().lower().rstrip(".") for domain in domains if domain.strip()})
    hosts = {
        host
        for domain in normalized
        for host in (domain, f"www.{domain}", f"status.{domain}", f"trust.{domain}")
    }
    if "trustedrouter.com" in normalized:
        hosts.update(
            {
                "eu.trustedrouter.com",
                "status-us.trustedrouter.com",
                "status-eu.trustedrouter.com",
            }
        )
    return sorted(hosts)


def _is_first_party_host(host: str, managed_hosts: set[str]) -> bool:
    normalized = host.strip().lower().rstrip(".")
    return normalized in managed_hosts


def _strip_output_only(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_output_only(item)
            for key, item in value.items()
            if key not in _OUTPUT_ONLY_KEYS
        }
    if isinstance(value, list):
        return [_strip_output_only(item) for item in value]
    return value


def rewrite_url_map(
    existing: dict[str, Any],
    public_backend: str,
    actions_backend: str,
    control_backend: str,
    internal_backend: str,
    domains: list[str] | tuple[str, ...],
    gateway_backend: str | None = None,
) -> dict[str, Any]:
    """Render four surfaces, optionally overriding gateway backend placement.

    Terminal routing always uses the shared Terraform switch. An existing
    authorize override is retained verbatim. CLI callers can also preserve an
    installed gateway wildcard override with --preserve-gateway-backend.
    """
    if existing.get("defaultUrlRedirect") is not None:
        raise ValueError("cannot convert a redirect-only URL map into the HTTPS service map")
    if not existing.get("defaultService"):
        raise ValueError("refusing to rewrite a URL map without an existing default service")
    domain_set = {domain.strip().lower().rstrip(".") for domain in domains if domain.strip()}
    if not domain_set:
        raise ValueError("at least one first-party domain is required")
    managed_hosts = set(first_party_hosts(tuple(domain_set)))

    result = _strip_output_only(copy.deepcopy(existing))
    host_rules: list[dict[str, Any]] = []
    reserved_matcher_is_retained = False
    for rule in result.get("hostRules", []):
        hosts = list(rule.get("hosts") or [])
        if "*" in hosts:
            raise ValueError("refusing to rewrite a URL map with a catch-all host rule")
        if any(
            host.strip().lower().rstrip(".") == f"*.{domain}"
            for host in hosts
            for domain in domain_set
        ):
            raise ValueError(
                "refusing to rewrite a wildcard first-party host rule; split it into "
                "explicit attested, regional, and public hosts first"
            )
        retained = [host for host in hosts if not _is_first_party_host(host, managed_hosts)]
        if retained:
            rewritten = dict(rule)
            rewritten["hosts"] = retained
            host_rules.append(rewritten)
            if rewritten.get("pathMatcher") == _MATCHER_NAME:
                reserved_matcher_is_retained = True

    if reserved_matcher_is_retained:
        raise ValueError(
            f"refusing to replace reserved path matcher {_MATCHER_NAME!r} while "
            "an unrelated host still references it"
        )

    path_matchers = [
        matcher
        for matcher in result.get("pathMatchers", [])
        if matcher.get("name") != _MATCHER_NAME
    ]
    backend_for: dict[Surface, str] = {
        "public": public_backend,
        "actions": actions_backend,
        "control": control_backend,
        "internal": internal_backend,
    }
    path_matchers.append(
        {
            "name": _MATCHER_NAME,
            "defaultService": public_backend,
            "pathRules": [
                {"paths": list(_PATTERNS[surface]), "service": backend_for[surface]}
                for surface in ("public", "actions", "control", "internal")
            ],
        }
    )
    if gateway_backend:
        path_matchers[-1]["pathRules"].append(
            {"paths": list(GATEWAY_PATH_PATTERNS), "service": gateway_backend}
        )
    # Exact terminals beat both /internal/* and an optional gateway/* override.
    # Use the control backend, not an optional internal/gateway split backend.
    weight = terminal_route_weight()
    terminal_backend = control_backend.rsplit("/", 1)[0] + "/" + TERMINAL_BACKEND_NAME if "/" in control_backend else TERMINAL_BACKEND_NAME
    terminal_rule = {
        "paths": list(TERMINAL_PATH_PATTERNS),
        "routeAction": {
            "timeout": {"seconds": 30},
            "weightedBackendServices": [
                {"backendService": control_backend, "weight": 100 - weight},
                {"backendService": terminal_backend, "weight": weight},
            ],
        },
    }
    path_matchers[-1]["pathRules"].append(terminal_rule)
    for override in billing_override_rules(existing):
        if override["paths"] == ["/internal/gateway/authorize"]:
            path_matchers[-1]["pathRules"].append(copy.deepcopy(override))
    host_rules.append(
        {
            "hosts": first_party_hosts(tuple(domain_set)),
            "pathMatcher": _MATCHER_NAME,
        }
    )

    # Unknown Host values must not fall through to the legacy/control backend.
    # Explicit unrelated hostRules and their matchers remain untouched, while
    # the unmatched top-level default deliberately becomes the protected public
    # backend.  Rollout preflight must inventory every legitimate hostname
    # before making this fail-closed compatibility change.
    result["defaultService"] = public_backend
    result["hostRules"] = host_rules
    result["pathMatchers"] = path_matchers

    for test in result.get("tests", []):
        host = str(test.get("host") or "")
        if _is_first_party_host(host, managed_hosts):
            path = str(test.get("path") or "/")
            if path in TERMINAL_PATH_PATTERNS:
                test["service"] = terminal_backend if weight == 100 else control_backend
            elif path == "/internal/gateway/authorize" and any(
                rule["paths"] == [path] for rule in billing_override_rules(existing)
            ):
                # The authorize rule and its live test assertions stay unchanged.
                continue
            else:
                test["service"] = (
                    gateway_backend if gateway_backend and is_gateway_path(path)
                    else backend_for[route_surface(path)]
                )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-backend", required=True)
    parser.add_argument("--actions-backend", required=True)
    parser.add_argument("--control-backend", required=True)
    parser.add_argument("--internal-backend", required=True)
    parser.add_argument("--domains", required=True)
    parser.add_argument("--gateway-backend")
    parser.add_argument("--preserve-gateway-backend", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    existing = json.loads(args.input.read_text(encoding="utf-8"))
    rewritten = rewrite_url_map(
        existing,
        args.public_backend,
        args.actions_backend,
        args.control_backend,
        args.internal_backend,
        args.domains.split(","),
        args.gateway_backend or (
            existing_gateway_backend(existing) if args.preserve_gateway_backend else None
        ),
    )
    args.output.write_text(json.dumps(rewritten, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
