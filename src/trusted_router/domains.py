"""First-party hostname policy.

All request-derived absolute URLs must pass through this module. Reflecting an
arbitrary Host header into OAuth, billing, or wallet flows would create an open
redirect and SIWE-domain confusion. Only the canonical domain and explicitly
configured aliases are accepted; everything else falls back to the canonical
domain.
"""

from __future__ import annotations

import re

from fastapi import Request

from trusted_router.config import Settings

_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


def configured_control_domains(settings: Settings) -> tuple[str, ...]:
    """Return the canonical control domain followed by unique aliases."""
    values = [settings.trusted_domain, *settings.trusted_domain_aliases.split(",")]
    domains: list[str] = []
    seen: set[str] = set()
    for value in values:
        domain = _normalized_domain(value)
        if domain is None or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    canonical = _normalized_domain(settings.trusted_domain)
    if canonical is None:
        raise ValueError("TR_TRUSTED_DOMAIN must be a valid DNS domain")
    if not domains or domains[0] != canonical:
        domains.insert(0, canonical)
    return tuple(domains)


def request_hostname(request: Request) -> str:
    """Return a normalized DNS hostname, or an empty string if invalid."""
    host = request.headers.get("host", "").strip()
    # Product domains are DNS names, never IPv6 literals. Splitting on the
    # first colon safely removes the optional HTTP port for this allowlist.
    hostname = host.split(":", 1)[0]
    return _normalized_domain(hostname) or ""


def control_domain_for_hostname(settings: Settings, hostname: str) -> str:
    """Map an apex/www/status/trust hostname to its allowed base domain."""
    normalized = _normalized_domain(hostname)
    domains = configured_control_domains(settings)
    if normalized:
        for domain in domains:
            if normalized == domain or normalized in {
                f"www.{domain}",
                f"status.{domain}",
                f"trust.{domain}",
            }:
                return domain
    return domains[0]


def request_control_domain(request: Request, settings: Settings) -> str:
    return control_domain_for_hostname(settings, request_hostname(request))


def request_control_origin(request: Request, settings: Settings) -> str:
    return f"https://{request_control_domain(request, settings)}"


def api_base_url_for_domain(settings: Settings, domain: str) -> str:
    canonical = configured_control_domains(settings)[0]
    normalized = _normalized_domain(domain)
    if normalized == canonical:
        return settings.api_base_url.rstrip("/")
    if normalized in configured_control_domains(settings):
        return f"https://api.{normalized}/v1"
    return settings.api_base_url.rstrip("/")


def request_api_base_url(request: Request, settings: Settings) -> str:
    return api_base_url_for_domain(settings, request_control_domain(request, settings))


def is_www_hostname(settings: Settings, hostname: str) -> bool:
    return any(hostname == f"www.{domain}" for domain in configured_control_domains(settings))


def is_status_hostname(settings: Settings, hostname: str) -> bool:
    return any(hostname == f"status.{domain}" for domain in configured_control_domains(settings))


def is_trust_hostname(settings: Settings, hostname: str) -> bool:
    return any(hostname == f"trust.{domain}" for domain in configured_control_domains(settings))


def status_hostname_for_domain(domain: str) -> str:
    return f"status.{domain}"


def trust_hostname_for_domain(domain: str) -> str:
    return f"trust.{domain}"


def _normalized_domain(value: str) -> str | None:
    domain = value.strip().lower().rstrip(".")
    if not domain or not _DOMAIN_RE.fullmatch(domain):
        return None
    return domain
