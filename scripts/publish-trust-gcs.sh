#!/usr/bin/env bash
set -euo pipefail

# Publish the machine-readable trust files and a static trust page to a GCS
# bucket named after the hostname. Cloudflare can proxy this hostname because
# it is not the prompt path.

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
REGION="${REGION:-us-central1}"
BUCKET="${TRUST_BUCKET:-trust.trustedrouter.com}"
TRUST_FILE="${TRUST_FILE:-/Users/jperla/claude/quill-cloud-proxy/trust-page/gcp-release.json}"

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }
gc() { gcloud --project "$PROJECT_ID" "$@"; }

if [ ! -f "$TRUST_FILE" ]; then
  echo "ERROR: trust file missing: $TRUST_FILE" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

python3 - "$TRUST_FILE" "$tmpdir" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

trust_file = Path(sys.argv[1])
out = Path(sys.argv[2])
release = json.loads(trust_file.read_text())

(out / "gcp-release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
(out / "image-digest-gcp.txt").write_text(release["image_digest"] + "\n")
(out / "image-reference-gcp.txt").write_text(release["image_reference"] + "\n")
(out / "index.html").write_text(
    f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TrustedRouter Trust</title>
  <style>
    body {{ margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#1b242c; background:#f6f8fa; }}
    main {{ max-width:960px; margin:0 auto; padding:40px 24px; }}
    section {{ background:#fff; border:1px solid #dbe3ea; border-radius:8px; padding:20px; margin-top:16px; }}
    h1 {{ margin:0 0 10px; font-size:32px; letter-spacing:0; }}
    h2 {{ margin:0 0 10px; font-size:18px; letter-spacing:0; }}
    p {{ color:#62717d; line-height:1.55; }}
    code, pre {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
    code {{ background:#edf2f6; border:1px solid #d7e0e7; border-radius:6px; padding:2px 6px; overflow-wrap:anywhere; }}
    pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#101820; color:#eef6ff; border-radius:8px; padding:16px; }}
  </style>
</head>
<body>
  <main>
    <h1>TrustedRouter Trust</h1>
    <p><code>api.quillrouter.com</code> terminates TLS inside the attested Confidential Space workload. Prompt and output content are not stored by the control plane.</p>
    <section><h2>Current GCP workload</h2><p><code>{release["image_reference"]}</code></p><p><code>{release["image_digest"]}</code></p></section>
    <section><h2>Files</h2><p><a href="/gcp-release.json">gcp-release.json</a></p><p><a href="/image-digest-gcp.txt">image-digest-gcp.txt</a></p><p><a href="/image-reference-gcp.txt">image-reference-gcp.txt</a></p></section>
    <section><h2>Release JSON</h2><pre>{json.dumps(release, indent=2, sort_keys=True)}</pre></section>
  </main>
</body>
</html>""",
    encoding="utf-8",
)
PY

gc services enable storage.googleapis.com
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  log "creating gs://${BUCKET}"
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --uniform-bucket-level-access
fi

log "uploading trust files"
gcloud storage cp "$tmpdir/index.html" "gs://${BUCKET}/index.html" --cache-control="max-age=60, public" --content-type="text/html; charset=utf-8"
gcloud storage cp "$tmpdir/gcp-release.json" "gs://${BUCKET}/gcp-release.json" --cache-control="max-age=60, public" --content-type="application/json"
gcloud storage cp "$tmpdir/image-digest-gcp.txt" "gs://${BUCKET}/image-digest-gcp.txt" --cache-control="max-age=60, public" --content-type="text/plain; charset=utf-8"
gcloud storage cp "$tmpdir/image-reference-gcp.txt" "gs://${BUCKET}/image-reference-gcp.txt" --cache-control="max-age=60, public" --content-type="text/plain; charset=utf-8"

cat <<EOF
Trust files uploaded to gs://${BUCKET}.

To make this public, grant objectViewer to allUsers and point Cloudflare:
  trust.trustedrouter.com CNAME c.storage.googleapis.com

Do not point trust.trustedrouter.com at the API enclave IP.
EOF
