#!/usr/bin/env python3
"""Build the six-service HTTPS URL map without disturbing unrelated hosts.

The public service is the default failure domain.  Only the explicit patterns
below can reach anonymous actions, account/console, chat, signed webhooks, or
internal billing services. Keep this module importable: route-totality tests
compare every registered FastAPI route with this exact deployment contract.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

Surface = Literal["public", "actions", "console", "chat", "webhooks", "internal"]


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

PUBLIC_PATH_PATTERNS = (
    "/analytics/events",
    "/bedrock-group-buy",
    "/bedrock-group-buy/",
    "/notify/texml",
    "/v1/health",
    "/v1/ready",
    "/v1/coverage/openrouter",
    "/v1/analytics/events",
    "/v1/bedrock-group-buy",
    "/v1/embeddings/models",
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
    "/trustedos/inquiry",
)

CONSOLE_PATH_PATTERNS = (
    "/v1/*",
    "/models/user",
    "/v1/models/user",
    "/bedrock-group-buy/*",
    "/google_oauth_callback",
    "/github_oauth_callback",
    "/console",
    "/console/*",
    "/provider",
    "/provider/*",
    "/mcp",
    "/auth",
    "/auth/*",
    "/byok",
    "/byok/*",
    "/credits",
    "/credits/*",
    "/billing/*",
    "/broadcast/*",
    "/custom-models",
    "/custom-models/*",
    "/user-models",
    "/user-models/*",
    "/key",
    "/keys",
    "/keys/*",
    "/activity",
    "/generation",
    "/generation/*",
    "/client-events",
    "/workspaces",
    "/workspaces/*",
    "/organization/*",
    "/chat/completions",
    "/messages",
    "/embeddings",
    "/responses",
    "/audio/*",
    "/rerank",
    "/videos",
    "/videos/*",
    "/private/*",
    "/guardrails",
    "/guardrails/*",
    "/analytics/*",
    "/classifications/*",
    "/datasets/*",
    "/files",
    "/files/*",
    "/images",
    "/images/*",
    "/model/*",
    "/observability/*",
    "/presets",
    "/presets/*",
    "/scim/*",
    "/signup",
    "/notify",
    "/notify/*",
    "/internal/chat/issue-browser-key",
    "/v1/internal/chat/issue-browser-key",
)

CHAT_PATH_PATTERNS = (
    "/chat-proxy/*",
)

WEBHOOK_PATH_PATTERNS = (
    "/internal/stripe/webhook",
    "/internal/paypal/webhook",
    "/internal/adyen/webhook",
    "/internal/veriff/webhook",
    "/internal/ses/notifications",
    "/v1/internal/stripe/webhook",
    "/v1/internal/paypal/webhook",
    "/v1/internal/adyen/webhook",
    "/v1/internal/veriff/webhook",
    "/v1/internal/ses/notifications",
)

INTERNAL_PATH_PATTERNS = (
    "/internal/*",
    "/v1/internal/*",
)

_PATTERNS: dict[Surface, tuple[str, ...]] = {
    "public": PUBLIC_PATH_PATTERNS,
    "actions": ACTIONS_PATH_PATTERNS,
    "console": CONSOLE_PATH_PATTERNS,
    "chat": CHAT_PATH_PATTERNS,
    "webhooks": WEBHOOK_PATH_PATTERNS,
    "internal": INTERNAL_PATH_PATTERNS,
}
_MATCHER_NAME = "trusted-router-service-surfaces"
_OUTPUT_ONLY_KEYS = {
    "creationTimestamp",
    "fingerprint",
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
    console_backend: str,
    chat_backend: str,
    webhooks_backend: str,
    internal_backend: str,
    domains: list[str] | tuple[str, ...],
    preserved_hosts: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Return an importable URL map with a first-party six-way bulkhead."""
    if existing.get("defaultUrlRedirect") is not None:
        raise ValueError("cannot convert a redirect-only URL map into the HTTPS service map")
    if existing.get("defaultRouteAction") is not None:
        raise ValueError("refusing a top-level default route action on the managed URL map")
    if existing.get("headerAction") is not None:
        raise ValueError(
            "refusing a top-level header action that could inject credentials into managed hosts"
        )
    if not existing.get("defaultService"):
        raise ValueError("refusing to rewrite a URL map without an existing default service")
    domain_set = {domain.strip().lower().rstrip(".") for domain in domains if domain.strip()}
    if not domain_set:
        raise ValueError("at least one first-party domain is required")
    managed_hosts = set(first_party_hosts(tuple(domain_set)))
    for rule in existing.get("hostRules", []):
        hosts = list(rule.get("hosts") or [])
        if "*" in hosts:
            raise ValueError("refusing to rewrite a URL map with a catch-all host rule")
        if any(
            str(host).strip().lower().rstrip(".") == f"*.{domain}"
            for host in hosts
            for domain in domain_set
        ):
            raise ValueError(
                "refusing to rewrite a wildcard first-party host rule; split it into "
                "explicit attested, regional, and public hosts first"
            )
    preserved_host_set = {
        host.strip().lower().rstrip(".") for host in preserved_hosts if host.strip()
    }
    if not preserved_host_set:
        raise ValueError("an explicit preserved-host inventory is required")
    managed_inventory_overlap = sorted(preserved_host_set & managed_hosts)
    if managed_inventory_overlap:
        raise ValueError(
            "preserved-host inventory must exclude managed public web host(s): "
            + ", ".join(managed_inventory_overlap)
        )
    explicitly_routed_hosts = {
        str(host).strip().lower().rstrip(".")
        for rule in existing.get("hostRules", [])
        for host in (rule.get("hosts") or [])
        if host != "*"
    }
    missing_host_rules = sorted(preserved_host_set - explicitly_routed_hosts)
    if missing_host_rules:
        raise ValueError(
            "preserved host(s) rely on the top-level default instead of an explicit "
            "hostRule: "
            + ", ".join(missing_host_rules)
        )

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
        "console": console_backend,
        "chat": chat_backend,
        "webhooks": webhooks_backend,
        "internal": internal_backend,
    }
    surface_order: tuple[Surface, ...] = (
        "public",
        "actions",
        "console",
        "chat",
        "webhooks",
        "internal",
    )
    path_matchers.append(
        {
            "name": _MATCHER_NAME,
            "defaultService": public_backend,
            "pathRules": [
                {"paths": list(_PATTERNS[surface]), "service": backend_for[surface]}
                for surface in surface_order
            ],
        }
    )
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
            test["service"] = backend_for[route_surface(str(test.get("path") or "/"))]
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-backend", required=True)
    parser.add_argument("--actions-backend", required=True)
    parser.add_argument("--console-backend", required=True)
    parser.add_argument("--chat-backend", required=True)
    parser.add_argument("--webhooks-backend", required=True)
    parser.add_argument("--internal-backend", required=True)
    parser.add_argument("--domains", required=True)
    parser.add_argument("--preserved-hosts", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    existing = json.loads(args.input.read_text(encoding="utf-8"))
    rewritten = rewrite_url_map(
        existing,
        args.public_backend,
        args.actions_backend,
        args.console_backend,
        args.chat_backend,
        args.webhooks_backend,
        args.internal_backend,
        args.domains.split(","),
        args.preserved_hosts.split(","),
    )
    _atomic_write_json(args.output, rewritten)


if __name__ == "__main__":
    main()
