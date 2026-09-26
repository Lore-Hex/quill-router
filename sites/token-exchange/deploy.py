#!/usr/bin/env python3
"""Deploy only the exchange static surface; never changes inference backends.

DNS delegation is deliberately manual at the registrar, after reviewing the
saved record inventory. This script preserves existing URL-map host rules.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from build import load_markets

PROJECT = "quill-cloud-proxy"
BUCKET = "quill-token-exchange-public"
BACKEND = "token-exchange-static"
MAP = "trusted-router-control-map"
PROXY = "trusted-router-control-https-proxy"
IP = "35.241.14.18"


def gcloud(*args: str, read: bool = False):
    executable = shutil.which("gcloud")
    if executable is None:
        raise RuntimeError("Install the Google Cloud CLI before deployment")
    command = [executable, *args, f"--project={PROJECT}"]
    if read:
        # All command arguments are internal constants or the reviewed domain manifest.
        return json.loads(subprocess.check_output([*command, "--format=json"], text=True))  # noqa: S603
    subprocess.run([*command, "--quiet"], check=True)  # noqa: S603
    return None


def domains() -> list[str]:
    return [d for m in load_markets() for d in [m["domain"], *m["aliases"]]]


def certificate_requests(attached: list[dict], hosts: list[str]) -> list[tuple[str, list[str]]]:
    """Certificates to create so every host is in an attached certificate's managed domains.

    Hosts in the managed domains of an attached certificate are skipped. The rest are
    requested in certificates of at most 16 hosts. Each name is derived from its
    host list, so the same missing hosts always map to the same names.
    """
    covered = {d for cert in attached for d in cert.get("managed", {}).get("domains", [])}
    missing = [h for h in hosts if h not in covered]
    requests = []
    for i in range(0, len(missing), 16):
        batch = missing[i : i + 16]
        digest = hashlib.sha256(",".join(batch).encode()).hexdigest()[:12]
        requests.append((f"token-exchange-{digest}", batch))
    return requests


def url_map(existing: dict) -> dict:
    result = copy.deepcopy(existing)
    wanted_hosts = {h for domain in domains() for h in (domain, "www." + domain)}
    for rule in result.get("hostRules", []):
        if not rule["pathMatcher"].startswith("exchange-") and wanted_hosts.intersection(
            rule["hosts"]
        ):
            raise ValueError("Exchange hostname already routed by an unrelated matcher")
    result["hostRules"] = [
        r for r in result.get("hostRules", []) if not r["pathMatcher"].startswith("exchange-")
    ]
    result["pathMatchers"] = [
        r for r in result.get("pathMatchers", []) if not r["name"].startswith("exchange-")
    ]
    service = (
        f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/backendBuckets/{BACKEND}"
    )
    for market in load_markets():
        name = "exchange-" + market["slug"]
        result["hostRules"].append({"hosts": [market["domain"]], "pathMatcher": name})
        rules = []
        for path, file in (
            ("/", "index.html"),
            ("/index.html", "index.html"),
            ("/robots.txt", "robots.txt"),
            ("/sitemap.xml", "sitemap.xml"),
        ):
            rules.append(
                {
                    "paths": [path],
                    "service": service,
                    "routeAction": {
                        "urlRewrite": {"pathPrefixRewrite": f"/{market['slug']}/{file}"}
                    },
                }
            )
        result["pathMatchers"].append({"name": name, "defaultService": service, "pathRules": rules})
        aliases = [
            "www." + market["domain"],
            *market["aliases"],
            *["www." + alias for alias in market["aliases"]],
        ]
        result["hostRules"].append({"hosts": aliases, "pathMatcher": name + "-aliases"})
        result["pathMatchers"].append(
            {
                "name": name + "-aliases",
                "defaultUrlRedirect": {
                    "hostRedirect": market["domain"],
                    "httpsRedirect": True,
                    "redirectResponseCode": "MOVED_PERMANENTLY_DEFAULT",
                    "stripQuery": False,
                },
            }
        )
    for field in ("kind", "id", "creationTimestamp", "selfLink"):
        result.pop(field, None)
    return result


def inventory(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if (output / "dns-before.json").exists():
        raise ValueError(
            "Keep the original DNS backup; use a new state directory for a new inventory"
        )
    data = {}
    for domain in domains():
        records = {}
        for label, kind in [(domain, t) for t in ("NS", "A", "AAAA", "MX", "TXT", "CAA", "DS")] + [
            ("www." + domain, "CNAME"),
            ("_dmarc." + domain, "TXT"),
        ]:
            value = subprocess.check_output(  # noqa: S603
                ["/usr/bin/dig", "+short", "+time=5", "+tries=2", label, kind], text=True
            ).strip()
            records[f"{label} {kind}"] = value
        data[domain] = records
        print(domain, "NS:", records[f"{domain} NS"].replace("\n", ","), flush=True)
    (output / "dns-before.json").write_text(json.dumps(data, indent=2) + "\n")


def provision_dns(state: Path) -> None:
    inventory_data = json.loads((state / "dns-before.json").read_text())
    zones = {z["dnsName"]: z for z in gcloud("dns", "managed-zones", "list", read=True)}
    manifest = {}
    for domain in domains():
        records = inventory_data[domain]
        if records.get(f"{domain} DS"):
            raise ValueError(f"DNSSEC delegation requires a staged migration: {domain}")
        zone = zones.get(domain + ".")
        if not zone:
            name = "exchange-" + domain.replace(".", "-")
            gcloud(
                "dns",
                "managed-zones",
                "create",
                name,
                f"--dns-name={domain}.",
                "--description=TrustedRouter regional Token Exchange",
            )
            zone = gcloud("dns", "managed-zones", "describe", name, read=True)
        name = zone["name"]
        present = {
            (r["name"], r["type"]): r
            for r in gcloud("dns", "record-sets", "list", f"--zone={name}", read=True)
        }
        desired = [(domain + ".", "A", [IP]), ("www." + domain + ".", "CNAME", [domain + "."])]
        for key, value in records.items():
            label, kind = key.split()
            if value and kind in ("MX", "TXT", "CAA"):
                desired.append((label + ".", kind, value.splitlines()))
        for label, kind, values in desired:
            old = present.get((label, kind))
            if old:
                if sorted(old["rrdatas"]) != sorted(values):
                    raise ValueError(
                        f"Existing Cloud DNS record differs, review before replacing: {label} {kind}"
                    )
                continue
            # Alternate list separator preserves commas inside TXT strings.
            gcloud(
                "dns",
                "record-sets",
                "create",
                label,
                f"--zone={name}",
                f"--type={kind}",
                "--ttl=300",
                "--rrdatas=^|^" + "|".join(values),
            )
        manifest[domain] = {"zone": name, "nameservers": zone["nameServers"]}
        (state / "dns-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(domain, ",".join(zone["nameServers"]), flush=True)


def publish(output: Path, state: Path) -> None:
    required = [output / m["slug"] / "index.html" for m in load_markets()]
    required += [output / "assets" / f"og-{m['slug']}.png" for m in load_markets()]
    if not all(p.is_file() for p in required):
        raise ValueError("Build and verify all market pages and OG images before publishing")
    # A proxy with a certificate map serves the map's certificates
    # (infra/control_lb_certificate_map.tf) and ignores its classic ones. Then
    # publish creates and attaches none, and changes nothing until every host
    # has an ACTIVE map entry with an ACTIVE certificate.
    certificate_map = gcloud(
        "compute", "target-https-proxies", "describe", PROXY, "--global", read=True
    ).get("certificateMap")
    if certificate_map:
        gaps = certificate_map_gaps(certificate_map, served_hosts())
        if gaps:
            raise RuntimeError(
                "No ACTIVE entry with an ACTIVE certificate in the HTTPS proxy's certificate map for "
                + ", ".join(gaps)
                + ". Add the domain to infra/token_exchange_certificate_domains.json "
                "and apply infra first."
            )
    buckets = gcloud("storage", "buckets", "list", read=True)
    if not any(b.get("name") == BUCKET for b in buckets):
        gcloud(
            "storage",
            "buckets",
            "create",
            f"gs://{BUCKET}",
            "--location=US",
            "--uniform-bucket-level-access",
        )
        gcloud(
            "storage",
            "buckets",
            "add-iam-policy-binding",
            f"gs://{BUCKET}",
            "--member=allUsers",
            "--role=roles/storage.objectViewer",
        )
    gcloud(
        "storage",
        "rsync",
        str(output),
        f"gs://{BUCKET}",
        "--recursive",
        "--cache-control=public,max-age=300",
    )
    backends = gcloud("compute", "backend-buckets", "list", read=True)
    if not any(b["name"] == BACKEND for b in backends):
        gcloud(
            "compute",
            "backend-buckets",
            "create",
            BACKEND,
            f"--gcs-bucket-name={BUCKET}",
            "--enable-cdn",
        )
    gcloud(
        "compute",
        "backend-buckets",
        "update",
        BACKEND,
        "--compression-mode=AUTOMATIC",
        "--custom-response-header=X-Content-Type-Options:nosniff",
        "--custom-response-header=X-Frame-Options:DENY",
        "--custom-response-header=Referrer-Policy:strict-origin-when-cross-origin",
    )
    current = gcloud("compute", "url-maps", "describe", MAP, "--global", read=True)
    state.mkdir(parents=True, exist_ok=True)
    backup = state / "urlmap-before.json"
    if not backup.exists():
        backup.write_text(json.dumps(current, indent=2) + "\n")
    proposed = state / "urlmap-proposed.json"
    proposed.write_text(json.dumps(url_map(current), indent=2) + "\n")
    gcloud("compute", "url-maps", "validate", f"--source={proposed}", "--global")
    fresh = gcloud("compute", "url-maps", "describe", MAP, "--global", read=True)
    if fresh["fingerprint"] != current["fingerprint"]:
        raise RuntimeError("URL map changed concurrently. Rerun to merge against the latest map.")
    gcloud("compute", "url-maps", "import", MAP, f"--source={proposed}", "--global")
    if not certificate_map:
        publish_certificates()


def served_hosts() -> list[str]:
    return [h for d in domains() for h in (d, "www." + d)]


def certificate_map_gaps(certificate_map: str, hosts: list[str]) -> list[str]:
    """Hosts the certificate map does not serve with an ACTIVE entry and certificate.

    Certificate Manager serves a host from the entry for that exact name, or
    else from the entry for *.<its parent domain>. An entry is PENDING while it
    propagates to the load balancer's frontends.
    """
    entries = {
        entry["hostname"]: entry
        for entry in gcloud(
            "certificate-manager",
            "maps",
            "entries",
            "list",
            "--map=" + certificate_map.rsplit("/", 1)[-1],
            "--location=global",
            read=True,
        )
        if entry.get("hostname")
    }
    active = {
        certificate["name"]
        for certificate in gcloud(
            "certificate-manager", "certificates", "list", "--location=global", read=True
        )
        if certificate.get("managed", {}).get("state") == "ACTIVE"
    }
    gaps = []
    for host in hosts:
        wildcard = "*." + host.split(".", 1)[1]
        entry = entries[host] if host in entries else entries.get(wildcard, {})
        if entry.get("state") != "ACTIVE" or not any(
            c in active for c in entry.get("certificates", [])
        ):
            gaps.append(host)
    return gaps


def attached_certificates() -> list[str]:
    proxy = gcloud("compute", "target-https-proxies", "describe", PROXY, "--global", read=True)
    return [c.rsplit("/", 1)[-1] for c in proxy.get("sslCertificates", [])]


def certificate_inventory() -> dict[str, dict]:
    listed = gcloud("compute", "ssl-certificates", "list", "--global", read=True)
    return {c["name"]: c for c in listed}


def publish_certificates() -> None:
    """Create and attach the certificates that certificate_requests() names.

    Planning reads the proxy's certificate list, then the certificate inventory.
    Requested certificates that do not exist are created. Then both are read
    again, whether or not anything was requested. The proxy is updated only if
    its list is unchanged, planning on the fresh reads gives the same requests,
    and every requested name exists with exactly that request's hosts as its
    managed domains; otherwise publish raises without updating and must be rerun.
    """
    hosts = served_hosts()
    attached = attached_certificates()
    existing = certificate_inventory()
    requests = certificate_requests([existing[n] for n in attached if n in existing], hosts)
    names = [name for name, _ in requests]
    if len(dict.fromkeys(attached + names)) > 15:
        raise RuntimeError("Certificate limit reached; existing certificates were not removed")
    for name, batch in requests:
        print(f"Requesting certificate {name} for {', '.join(batch)}")
        if name not in existing:
            gcloud(
                "compute",
                "ssl-certificates",
                "create",
                name,
                "--global",
                "--domains=" + ",".join(batch),
            )
    if attached_certificates() != attached:
        raise RuntimeError("HTTPS proxy certificates changed during publish. Rerun to plan again.")
    current = certificate_inventory()
    if certificate_requests([current[n] for n in attached if n in current], hosts) != requests:
        raise RuntimeError("Attached certificates changed during publish. Rerun to plan again.")
    for name, batch in requests:
        found = current.get(name, {}).get("managed", {}).get("domains")
        if sorted(found or []) != sorted(batch):
            raise RuntimeError(f"Certificate {name} exists for other domains: {found}")
    if names:
        gcloud(
            "compute",
            "target-https-proxies",
            "update",
            PROXY,
            "--global",
            "--ssl-certificates=" + ",".join(dict.fromkeys(attached + names)),
        )

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("inventory", "dns", "publish"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "inventory":
        inventory(args.state)
    elif args.action == "dns":
        provision_dns(args.state)
    else:
        if args.output is None:
            parser.error("publish requires --output")
        publish(args.output, args.state)
