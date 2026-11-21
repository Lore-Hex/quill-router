#!/usr/bin/env bash
# Refresh from committed gateway source only. No provider credentials or jobs.
set -euo pipefail
repo=${1:?usage: refresh.sh PATH_TO_GATEWAY GIT_REF}
rev=$(git -C "$repo" rev-parse --verify "${2:?gateway ref}^{commit}")
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git -C "$repo" archive "$rev" enclave-go | tar -x -C "$tmp"
mkdir "$tmp/enclave-go/cmd/media-export"
cp "$here/export.go" "$tmp/enclave-go/cmd/media-export/main.go"
(cd "$tmp/enclave-go" && go run -mod=readonly ./cmd/media-export "$rev") > "$tmp/models.json"
cp "$tmp/models.json" "$here/../../src/trusted_router/data/video_discovery.json"
