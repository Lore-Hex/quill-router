#!/usr/bin/env python3
"""Deploy only the exchange static surface; never changes inference backends.

DNS delegation is deliberately manual at the registrar, after reviewing the
saved record inventory. This script preserves existing URL-map host rules.
"""

from __future__ import annotations

import argparse
import copy
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
    existing = gcloud("compute", "ssl-certificates", "list", "--global", read=True)
    names = {c["name"] for c in existing}
    hosts = [h for d in domains() for h in (d, "www." + d)]
    certs = []
    for i in range(0, len(hosts), 16):
        name = f"token-exchange-20260919-{i // 16 + 1}"
        certs.append(name)
        if name not in names:
            gcloud(
                "compute",
                "ssl-certificates",
                "create",
                name,
                "--global",
                "--domains=" + ",".join(hosts[i : i + 16]),
            )
    proxy = gcloud("compute", "target-https-proxies", "describe", PROXY, "--global", read=True)
    all_certs = list(
        dict.fromkeys([c.rsplit("/", 1)[-1] for c in proxy.get("sslCertificates", [])] + certs)
    )
    if len(all_certs) > 15:
        raise RuntimeError("Certificate limit reached; existing certificates were not removed")
    gcloud(
        "compute",
        "target-https-proxies",
        "update",
        PROXY,
        "--global",
        "--ssl-certificates=" + ",".join(all_certs),
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
