"""Operator-only Axiom administration. Never imported by the scheduled runtime."""
import json
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def credentials():
    binary = shutil.which('axiom')
    if not binary:
        raise RuntimeError('Axiom CLI is required for operator administration')
    output = subprocess.check_output([binary, "config", "export", "--force"], text=True, stderr=subprocess.DEVNULL)  # noqa: S603 - fixed operator CLI command.
    values = {}
    for token in shlex.split(output):
        if "=" in token:
            key, value = token.split("=", 1)
            if key.startswith("AXIOM_"):
                values[key] = value.rstrip(";")
    return values


def api(method, path, body=None, *, edge=False):
    config = credentials()
    base = "https://eu-central-1.aws.edge.axiom.co" if edge else config.get("AXIOM_URL", "https://api.axiom.co")
    if base not in {'https://eu-central-1.aws.edge.axiom.co', 'https://api.axiom.co'}:
        raise ValueError('Unexpected Axiom API host')
    url = base.rstrip("/") + path
    headers = {"Authorization": "Bearer " + config["AXIOM_TOKEN"], "Content-Type": "application/json"}
    if config.get("AXIOM_ORG_ID"):
        headers["X-Axiom-Org-Id"] = config["AXIOM_ORG_ID"]
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)  # noqa: S310 - HTTPS allowlisted above.
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - HTTPS allowlisted above.
        payload = response.read()
        return json.loads(payload) if payload else None


def require_complete(result, *, display_limit=False):
    status = result.get("status", {})
    if status.get("isPartial") or status.get("isEstimate"):
        raise ValueError("Partial or estimated Axiom result refused")
    refused = {"default_limit_warning"}
    if not display_limit:
        refused.add("max_limit_warning")
    if any(message.get("code") in refused
           for message in status.get("messages", [])):
        raise ValueError("Implicit Axiom result limit refused")
    return result


def query(apl, start="2026-08-12T00:00:00Z", end=None):
    import datetime as dt
    result = api("POST", "/v1/query/_apl?format=tabular", {
        "apl": apl, "startTime": start,
        "endTime": end or dt.datetime.now(dt.UTC).isoformat(),
    }, edge=True)
    # A final explicit top-N list is intentionally partial presentation, not
    # estimated input to the calculation. Estimates remain forbidden above.
    display_limit=bool(re.search(r"\|\s*(?:take|limit)\s+\d+\s*$",apl))
    return require_complete(result,display_limit=display_limit)


if __name__ == "__main__":
    method, path = sys.argv[1:3]
    body = json.loads(Path(sys.argv[3]).read_text()) if len(sys.argv) > 3 else None
    try:
        print(json.dumps(api(method, path, body), indent=2))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()[:3000]}", file=sys.stderr)
        sys.exit(1)
