#!/usr/bin/env python3
"""Check staged host routing via an already-valid LB hostname, or live TLS.

--staged checks content and redirects only, not new-domain TLS or delegation.
After nameserver propagation, rerun without the flag to validate public HTTPS.
"""

from __future__ import annotations

import argparse
import http.client
import ssl

from build import load_markets


def get(host: str, path: str, staged: bool) -> tuple[int, dict, bytes]:
    connection = http.client.HTTPSConnection(
        "trustedrouter.com" if staged else host,
        timeout=30,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def smoke(staged: bool) -> None:
    for market in load_markets():
        host = market["domain"]
        for path, expected in (
            ("/", market["name"].encode()),
            ("/robots.txt", f"https://{host}/sitemap.xml".encode()),
            ("/sitemap.xml", f"https://{host}/".encode()),
            (f"/assets/og-{market['slug']}.png", b"\x89PNG"),
        ):
            status, _, body = get(host, path, staged)
            if status != 200 or expected not in body:
                raise AssertionError(f"{host}{path}: unexpected status/content {status}")
        for alias in [
            "www." + host,
            *market["aliases"],
            *["www." + name for name in market["aliases"]],
        ]:
            status, headers, _ = get(alias, "/?utm_source=smoke", staged)
            location = next((v for k, v in headers.items() if k.lower() == "location"), "")
            if status != 301 or location != f"https://{host}/?utm_source=smoke":
                raise AssertionError(f"{alias}: unexpected redirect {status} {location}")
        print(f"PASS {host}: content, robots, sitemap, OG, aliases", flush=True)
    for host in ("trustedrouter.com", "trust.trustedrouter.com", "status.trustedrouter.com"):
        status, _, _ = get(host, "/", False)
        if status != 200:
            raise AssertionError(f"Existing production surface {host} returned {status}")
        print(f"PASS existing surface {host}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true")
    smoke(parser.parse_args().staged)
