"""Run a real deploy script to completion without touching anything real.

WHY THIS EXISTS
---------------
The question "does this bring-up script actually run the completeness gate?"
was answered, for one commit, by a regex: does the string
``verify_cloud_complete.sh <cloud>`` appear in the script's last N lines? Three
reviewers killed it for the same reason, and it is the reason this whole change
exists: that predicate is satisfied by a heredoc body, by a printed
instruction, and by a commented-out line. Printing the step is not doing the
step. No amount of hardening the regex fixes a proof-by-text — the next
careless edit wins, and the check goes on reporting success.

So the script is EXECUTED, and two properties are asserted about what it did:

  1. the gate was CALLED, with this cloud;
  2. when the gate FAILS, the script exits non-zero.

A printed instruction fails both by construction. So does a commented-out call,
a heredoc, and a call whose exit status is swallowed.

HOW ISOLATED IT IS, EXACTLY
---------------------------
Stated precisely rather than flatteringly, because "hermetic" was claimed here
before and overstated three separate things.

``PATH`` is one directory, built here, containing:

* a recording stub for each name in :data:`STUBBED_COMMANDS` — the commands
  these scripts use to leave the machine or change something outside it
  (``aws``, ``az``, ``gcloud``, ``docker``, ``curl``, ``ssh``, ``systemctl``,
  ``journalctl``, ``clickhouse-client``, ``sleep``, ...). Each writes its argv
  to a shared ordered log and exits 0;
* a symlink to **every other entry of /bin and /usr/bin**. Not a curated list of
  text utilities: all of them, so nothing has to be re-implemented.

So the isolation is *by name*, and its boundary is :data:`STUBBED_COMMANDS`. A
script that reached the network through some tool nobody listed — ``ftp``,
``telnet``, a language runtime — would reach it. What the harness does
guarantee is what the two assertions need: the scripts under test call the
cloud CLIs and ``curl``, all of which are stubs, and the ``bash`` they run is
this machine's.

``$HOME`` and ``$TMPDIR`` point inside the temp directory, and the repository
the scripts see is a COPY of ``clickhouse/``, ``src/`` and ``scripts/``: they
read the SQL schemas and the package for real, and writes land in the copy
rather than in the checkout the suite is running from. That is a copy, not a
sandbox — an absolute path would still escape it — but no script here uses one.
``verify_cloud_complete.sh`` in that copy is replaced by a stub that records the
call and exits with ``HARNESS_VERIFIER_RC``; ``cloud_complete_gate.sh`` is the
real one, because its behaviour is part of what is being proven, and it resolves
its verifier relative to itself, which is how the stub gets found.

WHAT THE STUB RESPONSES ARE, AND WHAT THEY ARE NOT
--------------------------------------------------
Some scripts check their own work — ``aws_eu_control_plane.sh`` refuses to
continue unless App Runner reports it is serving the digest that was just
pushed. A stub that answers ``stub-output`` to everything fails that check, and
correctly. :data:`SCRIPT_FIXTURES` therefore gives a few argv patterns a
plausible answer, so the script can reach its own end.

That is a fixture, and it is worth being precise about what it can and cannot
launder: the two properties above are about the script's CONTROL FLOW at the
gate, and no answer a stub gives can make a swallowed exit status non-zero or
conjure a call that is not there. What a wrong fixture does is stop the script
early — a loud failure in this harness, never a false pass. A script whose
middle cannot be answered without asserting the answer it wants is recorded as
NOT_PROVEN instead; ``aws_eu_clickhouse_drain_install.sh`` is that script.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Commands replaced by a recording stub: anything that reaches the network, a
#: cloud API, another host, or this machine's state. ``sleep`` is here so a
#: script's waiter loops do not make the suite take twenty minutes.
STUBBED_COMMANDS: tuple[str, ...] = (
    "aws",
    "az",
    "gcloud",
    "gc",
    "gsutil",
    "bq",
    "docker",
    "podman",
    "curl",
    "wget",
    "ssh",
    "scp",
    "rsync",
    "systemctl",
    "journalctl",
    "clickhouse-client",
    "psql",
    "openssl",
    "apt-get",
    "useradd",
    "terraform",
    "gh",
    "kubectl",
    "helm",
    "uv",
    "sudo",
    "nc",
    "ping",
    "dig",
    "host",
    "sleep",
)

#: Directories whose real contents the scripts read (SQL schemas, the package
#: they tar up). Copied rather than symlinked so nothing a script does can
#: reach the checkout the test suite is running from.
MIRRORED = ("clickhouse", "src", "scripts")

#: ~10MB of web assets no deploy script reads; the drain installer excludes the
#: same three by name when it builds its payload.
_IGNORED = shutil.ignore_patterns(
    "__pycache__", "static", "templates", "content", ".git", "*.pyc", "node_modules"
)

_STUB = r"""#!/usr/bin/env bash
# Recording stub. Writes one tab-separated line per invocation to the shared
# ordered log, answers from the fixture table if anything matches, and exits 0.
{ printf '%s' "${0##*/}"; for a in "$@"; do
    recorded="${a//$'\n'/\\n}"
    recorded="${recorded//$'\t'/\\t}"
    printf '\t%s' "$recorded"
  done
  printf '\n'
} \
  >> "$HARNESS_ARGV_LOG"
# Drain stdin before answering. A stub that exits without reading closes the
# pipe under its upstream, and `aws ecr get-login-password | docker login
# --password-stdin` then dies of SIGPIPE -- 141 through `set -o pipefail`, in
# about one run in eighty. That is the harness inventing a failure in the
# script it is measuring, which would be a flake in the one suite that must not
# have any. The run's stdin is /dev/null, so this returns immediately when the
# stub is not in a pipeline.
cat >/dev/null 2>&1 || true
joined="${0##*/} $*"

if [ "${HARNESS_PUBLIC_SURFACE_SMOKE:-0}" = "1" ]; then
  region=""
  previous=""
  output_file=""
  ingress=""
  for argument in "$@"; do
    case "$argument" in
      --region=*) region="${argument#--region=}" ;;
      --region) previous="region"; continue ;;
      --ingress=*) ingress="${argument#--ingress=}" ;;
      --ingress) previous="ingress"; continue ;;
      -o) previous="output"; continue ;;
    esac
    case "$previous" in
      region) region="$argument" ;;
      ingress) ingress="$argument" ;;
      output) output_file="$argument" ;;
    esac
    previous=""
  done
  if [[ " $* " == *" run deploy trusted-router-public "* ]] \
      && [[ " $* " == *"status.latestCreatedRevisionName"* ]]; then
    python3 - "$HARNESS_PUBLIC_INGRESS_STATE" "$region" "$ingress" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
state = json.loads(path.read_text())
state[sys.argv[2]] = sys.argv[3]
path.write_text(json.dumps(state, sort_keys=True) + "\n")
PY
    printf 'trusted-router-public-candidate-%s\n' "$region"
    exit 0
  fi
  if [[ " $* " == *" run services update trusted-router-public "* ]] \
      && [ -n "$ingress" ]; then
    python3 - "$HARNESS_PUBLIC_INGRESS_STATE" "$region" "$ingress" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
state = json.loads(path.read_text())
state[sys.argv[2]] = sys.argv[3]
path.write_text(json.dumps(state, sort_keys=True) + "\n")
PY
    exit 0
  fi
  if [[ " $* " == *" run services update-traffic trusted-router-public "* ]] \
      && [[ " $* " == *" --update-tags="* ]]; then
    tag_assignment=""
    for argument in "$@"; do
      case "$argument" in --update-tags=*) tag_assignment="${argument#--update-tags=}" ;; esac
    done
    printf '%s\n' "${tag_assignment#*=}" >"${HARNESS_PROBE_TAG_STATE_DIR}/${region}"
    exit 0
  fi
  if [[ " $* " == *" run services update-traffic trusted-router-public "* ]] \
      && [[ " $* " == *" --remove-tags="* ]]; then
    remaining="$(cat "$HARNESS_PROBE_TAG_REMOVE_FAILURES_STATE")"
    if [ "${HARNESS_PROBE_TAG_REMOVE_ALWAYS_FAIL:-0}" = "1" ] || [ "$remaining" -gt 0 ]; then
      if [ "$remaining" -gt 0 ]; then
        printf '%s\n' "$((remaining - 1))" >"$HARNESS_PROBE_TAG_REMOVE_FAILURES_STATE"
      fi
      exit 1
    fi
    rm -f "${HARNESS_PROBE_TAG_STATE_DIR}/${region}"
    exit 0
  fi
  if [[ " $* " == *" run services update-traffic trusted-router-public "* ]] \
      && [[ " $* " == *"--to-revisions=trusted-router-public-candidate-${region}=100"* ]] \
      && [ "${HARNESS_PUBLIC_TERM_DURING_PROMOTE_REGION:-}" = "$region" ]; then
    kill -TERM "$PPID"
    exit 143
  fi
  if [[ " $* " == *" run services describe trusted-router-public "* ]] \
      && [[ " $* " == *" --format=json "* ]]; then
    python3 - "$HARNESS_PUBLIC_INGRESS_STATE" "$region" <<'PY'
import json
import pathlib
import sys

ingress = json.loads(pathlib.Path(sys.argv[1]).read_text())[sys.argv[2]]
print(
    json.dumps(
        {
            "metadata": {
                "annotations": {"run.googleapis.com/ingress": ingress}
            },
            "status": {
                "traffic": [
                    {
                        "percent": 100,
                        "revisionName": "trusted-router-public-active",
                    }
                ]
            },
        },
        separators=(",", ":"),
    )
)
PY
    exit 0
  fi
  if [[ " $* " == *" run services describe trusted-router-public "* ]] \
      && [[ " $* " == *"status.traffic"* ]]; then
    if [ -f "${HARNESS_PROBE_TAG_STATE_DIR}/${region}" ]; then
      cat "${HARNESS_PROBE_TAG_STATE_DIR}/${region}"
    fi
    exit 0
  fi
  if [[ " $* " == *" run services describe trusted-router-public "* ]] \
      && [[ " $* " == *"status.url"* ]]; then
    printf 'https://trusted-router-public-%s.a.run.app\n' "$region"
    exit 0
  fi
  if [ "${0##*/}" = "curl" ]; then
    url="${*: -1}"
    path="/${url#*://*/}"
    [ "$path" = "//" ] && path="/"
    direct_ingress="$(python3 - "$HARNESS_PUBLIC_INGRESS_STATE" "$url" <<'PY'
import json
import pathlib
import sys

state = json.loads(pathlib.Path(sys.argv[1]).read_text())
url = sys.argv[2]
print(next((ingress for region, ingress in state.items() if region in url), "all"))
PY
)"
    if [ "$direct_ingress" != "all" ]; then
      printf '403'
      exit 0
    fi
    if [ "${HARNESS_PUBLIC_SMOKE_TRANSPORT_PATH:-}" = "$path" ]; then
      printf '000'
      exit 7
    fi
    code="200"
    if [ -n "${HARNESS_PUBLIC_SMOKE_FAIL_REGION:-}" ] \
        && [[ "$url" == *"${HARNESS_PUBLIC_SMOKE_FAIL_REGION}"* ]] \
        && [ "${HARNESS_PUBLIC_SMOKE_FAIL_PATH:-}" = "$path" ]; then
      code="500"
    fi
    if [ -n "$output_file" ]; then
      printf 'harness response body\n' >"$output_file"
    fi
    printf '%s' "$code"
    exit 0
  fi
fi

if [ -n "${HARNESS_FAILURES:-}" ] && [ -f "$HARNESS_FAILURES" ]; then
  while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    if printf '%s' "$joined" | grep -Eq -- "$pattern"; then
      exit 1
    fi
  done < "$HARNESS_FAILURES"
fi

# URL-map validation is part of the safety gate under test. Do not let the
# generic success fallback launder a malformed candidate.
if [ "${0##*/}" = "gcloud" ] || [ "${0##*/}" = "gc" ]; then
  source_path=""
  for argument in "$@"; do
    case "$argument" in
      --source=*) source_path="${argument#--source=}" ;;
    esac
  done
  if [[ " $* " == *" compute url-maps validate "* ]]; then
    python3 - "$source_path" "${HARNESS_URL_MAP_NAME:-trusted-router-control-map}" <<'PY'
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
expected_name = sys.argv[2]
try:
    candidate = json.loads(path.read_text())
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid URL-map candidate: {exc}")
if not isinstance(candidate, dict) or candidate.get("name") != expected_name:
    raise SystemExit("URL-map candidate has the wrong or missing name")
rejected = {"creationTimestamp", "id", "kind", "selfLink"}
def rejected_fields(value):
    if isinstance(value, dict):
        return rejected.intersection(value).union(
            *(rejected_fields(item) for item in value.values())
        )
    if isinstance(value, list):
        return set().union(*(rejected_fields(item) for item in value))
    return set()
present = sorted(rejected_fields(candidate))
if present:
    raise SystemExit(f"URL-map candidate has output-only fields: {present}")

services = []
def collect(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"defaultService", "service"} and isinstance(item, str):
                services.append(item)
            collect(item)
    elif isinstance(value, list):
        for item in value:
            collect(item)
collect(candidate)
if not services or any(
    re.search(r"/global/backendServices/[^/]+$", service) is None
    for service in services
):
    raise SystemExit("URL-map candidate references an invalid backend")

if path.name.endswith(".public-candidate.json"):
    backend_names = {service.rsplit("/", 1)[-1] for service in services}
    expected = {
        "trusted-router-control-backend",
        "trusted-router-public-backend",
    }
    if not expected <= backend_names:
        raise SystemExit("public candidate does not reference both expected backends")
    matchers = candidate.get("pathMatchers") or []
    if not any(
        matcher.get("name") == "trusted-router-service-surfaces"
        and matcher.get("pathRules")
        for matcher in matchers
    ):
        raise SystemExit("public candidate has no service-surface path matcher")
PY
    exit $?
  fi
  if [[ " $* " == *" compute url-maps import "* ]]; then
    python3 - "$source_path" "$HARNESS_URL_MAP_STATE" <<'PY'
import hashlib
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
state = pathlib.Path(sys.argv[2])
document = json.loads(source.read_text())
if not isinstance(document, dict) or not document.get("name"):
    raise SystemExit("refusing malformed URL-map import")
rejected = {"creationTimestamp", "id", "kind", "selfLink"}
def rejected_fields(value):
    if isinstance(value, dict):
        return rejected.intersection(value).union(
            *(rejected_fields(item) for item in value.values())
        )
    if isinstance(value, list):
        return set().union(*(rejected_fields(item) for item in value))
    return set()
present = sorted(rejected_fields(document))
if present:
    raise SystemExit(f"refusing URL-map import with output-only fields: {present}")
canonical = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
document["fingerprint"] = "harness-" + hashlib.sha256(canonical).hexdigest()[:24]
document.update(
    {
        "creationTimestamp": "2026-08-22T12:00:00.000-07:00",
        "id": "1234567890123456789",
        "kind": "compute#urlMap",
        "selfLink": (
            "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
            "global/urlMaps/trusted-router-control-map"
        ),
    }
)
state.write_text(json.dumps(document, separators=(",", ":")) + "\n")
PY
    import_rc=$?
    [ "$import_rc" -eq 0 ] || exit "$import_rc"
    if [ "${HARNESS_URL_MAP_IMPORT_FAIL_AFTER_APPLY:-0}" = "1" ]; then
      exit 1
    fi
    if [ "${HARNESS_URL_MAP_KILL_AFTER_APPLY:-0}" = "1" ]; then
      kill -KILL "$PPID"
      exit 137
    fi
    exit 0
  fi
  if [[ " $* " == *" compute url-maps describe "* ]] \
      && [[ " $* " == *" --format=json "* ]] \
      && [ -s "$HARNESS_URL_MAP_STATE" ]; then
    if [ "${HARNESS_URL_MAP_POST_IMPORT_DESCRIBE_FAIL:-0}" = "1" ]; then
      exit 1
    fi
    cat "$HARNESS_URL_MAP_STATE"
    exit 0
  fi
fi

if [ -n "${HARNESS_FIXTURES:-}" ] && [ -f "$HARNESS_FIXTURES" ]; then
  while IFS=$'\t' read -r pattern encoded_reply; do
    [ -n "$pattern" ] || continue
    if printf '%s' "$joined" | grep -Eq -- "$pattern"; then
      printf '%s' "$encoded_reply" | base64 --decode
      printf '\n'
      exit 0
    fi
  done < "$HARNESS_FIXTURES"
fi
printf '%s\n' "stub-output"
exit 0
"""

#: The stub that stands in for the gate. It records that it was CALLED and with
#: what, and exits with whatever the test told it to. Both assertions read this.
_VERIFIER_STUB = r"""#!/usr/bin/env bash
{ printf 'verify_cloud_complete.sh'; for a in "$@"; do printf '\t%s' "$a"; done; printf '\n'; } \
  >> "$HARNESS_ARGV_LOG"
printf 'stub verifier: pretending to check %s (rc=%s)\n' "$*" "${HARNESS_VERIFIER_RC:-0}" >&2
exit "${HARNESS_VERIFIER_RC:-0}"
"""

_ECR = "330422590279.dkr.ecr.eu-west-3.amazonaws.com/trusted-router"
_DIGEST = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
_AWS_SERVICE_ARN = (
    "arn:aws:apprunner:eu-west-3:330422590279:service/tr-eu/harness-service-id"
)
_SYNTHETIC_INGEST_SERVICE_JSON = json.dumps(
    {
        "metadata": {
            "name": "trusted-router-billing",
            "annotations": {
                "run.googleapis.com/ingress": "internal-and-cloud-load-balancing"
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {"name": "TR_SERVICE_SURFACE", "value": "internal"},
                                {
                                    "name": "TR_OBSERVER_INTERNAL_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "trustedrouter-observer-internal-token",
                                            "key": "latest",
                                        }
                                    },
                                },
                                {
                                    "name": "TR_INTERNAL_GATEWAY_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "trustedrouter-internal-gateway-token",
                                            "key": "latest",
                                        }
                                    },
                                },
                            ]
                        }
                    ]
                }
            }
        },
    },
    separators=(",", ":"),
)

_PUBLIC_SURFACE_LEGACY_SERVICE_JSON = json.dumps(
    {
        "status": {
            "traffic": [
                {"percent": 100, "revisionName": "trusted-router-active"}
            ]
        }
    },
    separators=(",", ":"),
)
_PUBLIC_SURFACE_PUBLIC_SERVICE_JSON = json.dumps(
    {
        "metadata": {
            "annotations": {"run.googleapis.com/ingress": "all"}
        },
        "status": {
            "traffic": [
                {"percent": 100, "revisionName": "trusted-router-public-active"}
            ]
        }
    },
    separators=(",", ":"),
)
_PUBLIC_SURFACE_LEGACY_ENV = {
    "TR_RELEASE": "cb16dcc",
    "TR_TRUSTED_DOMAIN": "trustedrouter.com",
    "TR_TRUSTED_DOMAIN_ALIASES": "allyrouter.com,uptimerouter.com",
    "TR_API_BASE_URL": "https://api.trustedrouter.com/v1",
    "TR_SUPPORT_EMAIL": "help@trustedrouter.com",
    "TR_GCP_PROJECT_ID": "quill-cloud-proxy",
    "TR_REGIONS": "us-central1,us-east4,europe-west4,southamerica-east1",
    "TR_PRIMARY_REGION": "us-central1",
    "TR_STORAGE_BACKEND": "spanner-bigtable",
    "TR_SPANNER_INSTANCE_ID": "trusted-router-nam6",
    "TR_SPANNER_DATABASE_ID": "trusted-router",
    "TR_SPANNER_POOL_SIZE": "8",
    "TR_BIGTABLE_INSTANCE_ID": "trusted-router-logs",
    "TR_BIGTABLE_GENERATION_TABLE": "trustedrouter-generations",
    "TR_BIGTABLE_MIRROR_WRITES_ENABLED": "true",
    "TR_ANALYTICS_READ_MODE": "clickhouse",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL": "http://10.128.15.10:8123",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER": "tr_control_read",
    "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE": "tr",
    "TR_TRUST_GCP_SOURCE_COMMIT": "source-commit",
    "TR_TRUST_GCP_IMAGE_REFERENCE": "gcp-image-reference",
    "TR_TRUST_GCP_IMAGE_DIGEST": "sha256:" + "2" * 64,
    "TR_TRUST_GCP_RELEASE_URL": (
        "https://trust.trustedrouter.com/trust/gcp-release.json"
    ),
    "TR_TRUST_GCP_RELEASE_FALLBACK_URLS": (
        "https://raw.githubusercontent.com/Lore-Hex/quill-cloud-proxy/"
        "main/trust-page/gcp-release.json"
    ),
    "TR_TRUST_AWS_RELEASE_URL": (
        "https://trust.trustedrouter.com/trust/aws-release.json"
    ),
    "TR_TRUST_AZURE_RELEASE_URL": (
        "https://trust.trustedrouter.com/trust/azure-release.json"
    ),
}
_PUBLIC_SURFACE_LEGACY_REVISION_JSON = json.dumps(
    {
        "spec": {
            "containers": [
                {
                    "image": (
                        "us-central1-docker.pkg.dev/quill-cloud-proxy/"
                        "trusted-router/trusted-router@sha256:" + "1" * 64
                    ),
                    "env": [
                        *(
                            {"name": name, "value": value}
                            for name, value in _PUBLIC_SURFACE_LEGACY_ENV.items()
                        ),
                        *(
                            {
                                "name": name,
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": secret,
                                        "key": "latest",
                                    }
                                },
                            }
                            for name, secret in (
                                ("TR_GOOGLE_CLIENT_ID", "legacy-google-id"),
                                ("TR_GOOGLE_CLIENT_SECRET", "legacy-google-secret"),
                                ("TR_GITHUB_CLIENT_ID", "legacy-github-id"),
                                ("TR_GITHUB_CLIENT_SECRET", "legacy-github-secret"),
                            )
                        ),
                    ],
                }
            ]
        }
    },
    separators=(",", ":"),
)
_PUBLIC_SURFACE_PUBLIC_REVISION_JSON = json.dumps(
    {"metadata": {"name": "trusted-router-public-active"}},
    separators=(",", ":"),
)
_PUBLIC_EDGE_LIVE_MAP_JSON = json.dumps(
    {
        "creationTimestamp": "2026-08-22T12:00:00.000-07:00",
        "id": "1234567890123456789",
        "kind": "compute#urlMap",
        "name": "trusted-router-control-map",
        "fingerprint": "source-fingerprint",
        "selfLink": (
            "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
            "global/urlMaps/trusted-router-control-map"
        ),
        "defaultService": (
            "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
            "global/backendServices/trusted-router-control-backend"
        ),
    },
    separators=(",", ":"),
)
_PUBLIC_EDGE_ROUTED_SERVICE_JSON = json.dumps(
    {
        "metadata": {
            "annotations": {
                "run.googleapis.com/ingress": "internal-and-cloud-load-balancing"
            }
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {
                                    "name": "TR_RATE_LIMIT_CLIENT_IP_MODE",
                                    "value": "edge_header",
                                }
                            ]
                        }
                    ]
                }
            }
        },
    },
    separators=(",", ":"),
)


@dataclass(frozen=True)
class ScriptFixture:
    """What one script needs in order to reach its own last line under stubs."""

    #: Environment the script legitimately requires from its operator. These are
    #: INPUTS a human types, not state the harness is inventing.
    env: dict[str, str] = field(default_factory=dict)
    #: ``(extended-regex over "<command> <argv...>", stdout)`` in priority order.
    responses: tuple[tuple[str, str], ...] = ()
    #: Extended regexes whose matching command must exit 1 instead of succeeding.
    failures: tuple[str, ...] = ()
    #: Files to create under ``$HOME`` before the run, path -> contents.
    home_files: dict[str, str] = field(default_factory=dict)
    #: Commands legitimately issued AFTER the gate has answered, as regexes over
    #: the joined argv. Only cleanup belongs here — an EXIT trap tearing down
    #: something the script created. Provisioning after the gate means the gate
    #: checked a cloud that did not exist yet, which is what the old "must be in
    #: the last N lines" rule was reaching for.
    cleanup_after_gate: tuple[str, ...] = ()


SCRIPT_FIXTURES: dict[str, ScriptFixture] = {
    "scripts/deploy/public_surface.sh": ScriptFixture(
        env={"HARNESS_PUBLIC_SURFACE_SMOKE": "1"},
        responses=(
            (r"projects describe.*projectNumber", "44325983244"),
            (
                r"run services describe trusted-router .*--format=json",
                _PUBLIC_SURFACE_LEGACY_SERVICE_JSON,
            ),
            (
                r"run revisions describe trusted-router-active .*--format=json",
                _PUBLIC_SURFACE_LEGACY_REVISION_JSON,
            ),
            (
                r"run services describe trusted-router-public .*--format=json",
                _PUBLIC_SURFACE_PUBLIC_SERVICE_JSON,
            ),
            (
                r"run revisions describe trusted-router-public-active .*--format=json",
                _PUBLIC_SURFACE_PUBLIC_REVISION_JSON,
            ),
        ),
    ),
    "scripts/deploy/public_surface_edge.sh": ScriptFixture(
        responses=(
            (r"projects describe.*projectNumber", "44325983244"),
            (
                r"run services describe trusted-router-public .*--format=json",
                _PUBLIC_EDGE_ROUTED_SERVICE_JSON,
            ),
            (
                r"backend-services describe trusted-router-public-backend"
                r" .*securityPolicy[.]basename",
                "trusted-router-public-edge",
            ),
            (
                r"backend-services describe trusted-router-public-backend"
                r" .*value[(]selfLink[)]",
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/backendServices/trusted-router-public-backend",
            ),
            (
                r"backend-services describe trusted-router-control-backend"
                r" .*value[(]selfLink[)]",
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/backendServices/trusted-router-control-backend",
            ),
            (
                r"backend-services describe trusted-router-control-backend"
                r" .*value[(]loadBalancingScheme[)]",
                "EXTERNAL_MANAGED",
            ),
            (
                r"url-maps describe trusted-router-control-map .*--format=json",
                _PUBLIC_EDGE_LIVE_MAP_JSON,
            ),
        ),
    ),
    "scripts/deploy/aws_eu_clickhouse.sh": ScriptFixture(),
    # GCP's out-of-band check needs nothing from an operator: it runs the gate,
    # retries while a Cloud Run revision takes traffic, and returns the gate's
    # status. Listed explicitly rather than falling through to the default so
    # that "GCP is in the harness" is visible here and not only in the registry.
    # `sleep` is stubbed, so its retry loop costs the suite nothing.
    "scripts/deploy/verify_gcp_complete.sh": ScriptFixture(),
    "scripts/deploy/aws_eu_control_plane.sh": ScriptFixture(
        # PCR0 is a required operator input: the script refuses to run without
        # the enclave measurement to pin, which is the point of the probe.
        env={"ATTESTATION_PCR0": "0" * 96},
        responses=(
            # It deploys by DIGEST and then refuses to believe App Runner until
            # the service reports it is serving that exact digest.
            (r"ecr describe-images", _DIGEST),
            (
                r"secretsmanager get-secret-value.*trustedrouter-observer-internal-token",
                "harness-observer-token",
            ),
            (
                r"secretsmanager get-secret-value.*trustedrouter-internal-gateway-token",
                "harness-legacy-gateway-token",
            ),
            (
                r"apprunner describe-auto-scaling-configuration",
                "10\t1\t4\tarn:aws:apprunner:eu-west-3:330422590279:"
                "autoscalingconfiguration/tr-eu-observer-bounded/1/config-id",
            ),
            (
                r"apprunner describe-service.*AutoScalingConfigurationSummary",
                "arn:aws:apprunner:eu-west-3:330422590279:"
                "autoscalingconfiguration/tr-eu-observer-bounded/1/config-id",
            ),
            (r"apprunner describe-service.*HealthCheckConfiguration", "TCP"),
            (
                r"apprunner describe-service.*Service\.ServiceUrl",
                "observer.eu-west-3.awsapprunner.com",
            ),
            (r"apprunner describe-service.*ImageIdentifier", f"{_ECR}@{_DIGEST}"),
            (r"apprunner describe-service.*Service\.Status", "RUNNING"),
            (r"apprunner list-services", "arn:aws:apprunner:eu-west-3:330422590279:service/tr-eu"),
            (r"apprunner create-service", _AWS_SERVICE_ARN),
            (
                r"wafv2 list-web-acls",
                "acl-id\tarn:aws:wafv2:eu-west-3:330422590279:regional/webacl/"
                "trusted-router-app-runner-edge/acl-id",
            ),
            (
                r"wafv2 get-web-acl-for-resource",
                "arn:aws:wafv2:eu-west-3:330422590279:regional/webacl/"
                "trusted-router-app-runner-edge/acl-id",
            ),
            (
                r"wafv2 get-web-acl",
                '{"LockToken":"lock","WebACL":{"Rules":['
                '{"Name":"HighRatePerIpBlock","Action":{"Block":{}},'
                '"Statement":{"RateBasedStatement":{"AggregateKeyType":"IP"}}},'
                '{"Name":"AwsManagedCommon","Statement":'
                '{"ManagedRuleGroupStatement":{"VendorName":"AWS"}}}]}}',
            ),
            # It waits for the EventBridge API-key connection to authorize.
            (r"events describe-connection", "AUTHORIZED"),
        ),
    ),
    "scripts/deploy/aws_eu_north_clickhouse.sh": ScriptFixture(
        env={"TR_STOCKHOLM_REPLICA_WIRED": "1"},
        responses=(
            # It computes a non-overlapping CIDR for the second VPC from the
            # first one's, in real ipaddress arithmetic.
            (r"ec2 describe-vpcs.*CidrBlock", "10.50.0.0/16"),
            (r"ec2 describe-vpcs", "vpc-05b829b9cae6a9cd8"),
            # It refuses to build the inter-region path unless the Paris
            # subnet it found really is in the Paris VPC.
            (r"ec2 describe-subnets.*Subnets\[0\]\.VpcId", "vpc-05b829b9cae6a9cd8"),
            (r"describe-transit-gateway", "available"),
            (r"--query .?State", "available"),
        ),
    ),
    "scripts/deploy/azure_control_plane.sh": ScriptFixture(
        env={},
        home_files={
            # Credential-shaped inputs the script requires. Fake values in a
            # temp $HOME; nothing here is or resembles a real token.
            ".quill-secrets/trustedrouter-observer-internal-token": "harness-fake-observer\n",
            ".quill-secrets/trustedrouter-synthetic-monitor-api-key": "harness-fake-monitor\n",
        },
        responses=(
            (r"acr build|acr import", "harness"),
            (r"--query .?loginServer", "trazureuaenorthacr.azurecr.io"),
            (r"--query .?fullyQualifiedDomainName", "tr-azure-pg.postgres.database.azure.com"),
            (r"activeRevisionsMode", "Single"),
            # Azure owns paid synthetic/remediation loops in-process until an
            # external job is explicitly approved, so it must be a singleton.
            (r"template\.scale\.maxReplicas", "1"),
            (r"concurrentRequests", "10"),
            (r"--query .?properties\.configuration\.ingress\.fqdn", "tr-azure.example.net"),
            # It asserts the COUNT of tr_* tables, not psql's exit code.
            (r"information_schema\.tables", "9"),
        ),
        # The only thing this script does after the gate is the EXIT trap it
        # armed to remove the temporary Postgres firewall rule it opened to
        # apply the schema. Cleanup, not provisioning.
        cleanup_after_gate=(r"firewall-rule delete",),
    ),
    "scripts/deploy/azure_canary_app.sh": ScriptFixture(
        responses=(
            (r"openssl rand -hex 32", "a" * 64),
            (r"secret-name attribution-cookie-secret", "a" * 64),
            (r"TR_GOOGLE_OAUTH_LOGIN_AVAILABLE.*value", "false"),
            (r"TR_GITHUB_OAUTH_LOGIN_AVAILABLE.*value", "false"),
            (r"activeRevisionsMode", "Single"),
            (r"template\.scale\.maxReplicas", "2"),
            (r"concurrentRequests", "10"),
            (
                r"--query .?properties\.configuration\.ingress\.fqdn",
                "tr-canary.example.net",
            ),
        ),
    ),
    "scripts/deploy/synthetic.sh": ScriptFixture(
        env={"TR_BILLING_SERVICE": "trusted-router-billing"},
        responses=(
            (
                r"run services describe trusted-router-billing.*--format=json",
                _SYNTHETIC_INGEST_SERVICE_JSON,
            ),
            (
                r"dns managed-zones describe trusted-router-private-run-app --format=json",
                '{"dnsName":"run.app.","visibility":"private",'
                '"privateVisibilityConfig":{"networks":['
                '{"networkUrl":"projects/quill-cloud-proxy/global/networks/default"}]}}',
            ),
        ),
    ),
}


@dataclass
class HarnessRun:
    """One execution: what it exited with, and every command it ran, in order."""

    returncode: int
    stdout: str
    stderr: str
    calls: list[list[str]]
    public_ingress_state: dict[str, str]

    @property
    def verifier_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "verify_cloud_complete.sh"]

    def gate_ran_for(self, cloud: str) -> bool:
        return any(call[1:] == [cloud] for call in self.verifier_calls)

    def cloud_cli_calls_after_the_gate(self, allowed: tuple[str, ...] = ()) -> list[list[str]]:
        """Provisioning commands issued AFTER the gate answered.

        The old text check enforced "the verifier is in the last N lines" as a
        proxy for "it is the last thing the script does". This is that property
        measured instead of guessed: a gate that passes and is then followed by
        more cloud mutations checked a cloud that did not exist yet.

        ``allowed`` is for EXIT-trap cleanup, which genuinely runs last and is
        not provisioning.
        """
        cloud_clis = {"aws", "az", "gcloud", "gc", "docker", "ssh", "scp", "clickhouse-client"}
        patterns = [re.compile(pattern) for pattern in allowed]
        seen_gate = False
        after: list[list[str]] = []
        for call in self.calls:
            if call[0] == "verify_cloud_complete.sh":
                seen_gate = True
                continue
            if not seen_gate or call[0] not in cloud_clis:
                continue
            joined = " ".join(call)
            if any(pattern.search(joined) for pattern in patterns):
                continue
            after.append(call)
        return after


class DeployScriptHarness:
    """A throwaway checkout, a stub PATH, and one recorded run per invocation."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.mirror = root / "repo"
        self.bin = root / "bin"
        self._runs = 0
        self._build_mirror()
        self._build_bin()

    def _build_mirror(self) -> None:
        self.mirror.mkdir(parents=True)
        for name in MIRRORED:
            shutil.copytree(
                REPO_ROOT / name, self.mirror / name, ignore=_IGNORED, symlinks=True
            )
        for name in ("pyproject.toml",):
            if (REPO_ROOT / name).is_file():
                shutil.copy(REPO_ROOT / name, self.mirror / name)
        verifier = self.mirror / "scripts" / "deploy" / "verify_cloud_complete.sh"
        verifier.write_text(_VERIFIER_STUB)
        verifier.chmod(0o755)

    def _build_bin(self) -> None:
        self.bin.mkdir(parents=True)
        for name in STUBBED_COMMANDS:
            stub = self.bin / name
            stub.write_text(_STUB)
            stub.chmod(0o755)
        for directory in ("/bin", "/usr/bin"):
            source = Path(directory)
            if not source.is_dir():
                continue
            for entry in source.iterdir():
                if entry.name in STUBBED_COMMANDS or (self.bin / entry.name).exists():
                    continue
                try:
                    (self.bin / entry.name).symlink_to(entry)
                except OSError:  # pragma: no cover - unusual filesystems
                    continue

    def write_script(self, relative: str, text: str) -> str:
        """Add a script to the mirrored checkout, for saboteur cases.

        Used to demonstrate that the two assertions catch the shapes the old
        text check let through — a printed instruction, a commented-out call, a
        swallowed exit status — rather than only asserting they would.
        """
        target = self.mirror / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        target.chmod(0o755)
        return relative

    def run(
        self,
        script: str,
        *,
        args: tuple[str, ...] = (),
        extra_env: dict[str, str] | None = None,
        verifier_rc: int = 0,
        timeout: int = 120,
        omit_env: tuple[str, ...] = (),
    ) -> HarnessRun:
        """Run one script. ``omit_env`` drops fixture variables for this run.

        ``omit_env`` exists for one question, and it is a question worth being
        able to ask: a fixture supplies the environment an operator would type,
        so a property that holds only BECAUSE the fixture supplied something is
        a property that does not hold on a first run. See
        ``test_the_gate_status_survives_without_the_operator_attestation``.
        """
        fixture = SCRIPT_FIXTURES.get(script, ScriptFixture())
        self._runs += 1
        run_dir = self.root / f"run-{self._runs:03d}"
        home = run_dir / "home"
        tmp = run_dir / "tmp"
        home.mkdir(parents=True)
        tmp.mkdir(parents=True)
        for relative, contents in fixture.home_files.items():
            target = home / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)

        argv_log = run_dir / "argv.log"
        argv_log.write_text("")
        fixtures_file = run_dir / "fixtures.tsv"
        fixtures_file.write_text(
            "".join(
                f"{pattern}\t{base64.b64encode(reply.encode()).decode('ascii')}\n"
                for pattern, reply in fixture.responses
            )
        )
        failures_file = run_dir / "failures.txt"
        failures_file.write_text("".join(f"{pattern}\n" for pattern in fixture.failures))
        public_ingress_state = run_dir / "public-ingress.json"
        initial_public_ingress = (extra_env or {}).get(
            "HARNESS_PUBLIC_INITIAL_INGRESS", "all"
        )
        public_ingress_state.write_text(
            json.dumps(
                {
                    region: initial_public_ingress
                    for region in (
                        "us-central1",
                        "us-east4",
                        "europe-west4",
                        "southamerica-east1",
                    )
                },
                sort_keys=True,
            )
            + "\n"
        )
        probe_tag_state_dir = run_dir / "probe-tags"
        probe_tag_state_dir.mkdir()
        probe_tag_remove_failures = run_dir / "probe-tag-remove-failures"
        probe_tag_remove_failures.write_text(
            f"{(extra_env or {}).get('HARNESS_PROBE_TAG_REMOVE_FAILURES', '0')}\n"
        )

        env = {
            "PATH": str(self.bin),
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "LANG": "C",
            "HARNESS_ARGV_LOG": str(argv_log),
            "HARNESS_FIXTURES": str(fixtures_file),
            "HARNESS_FAILURES": str(failures_file),
            "HARNESS_URL_MAP_STATE": str(self.root / "url-map-state.json"),
            "HARNESS_URL_MAP_NAME": "trusted-router-control-map",
            "HARNESS_PUBLIC_INGRESS_STATE": str(public_ingress_state),
            "HARNESS_PROBE_TAG_STATE_DIR": str(probe_tag_state_dir),
            "HARNESS_PROBE_TAG_REMOVE_FAILURES_STATE": str(
                probe_tag_remove_failures
            ),
            "HARNESS_VERIFIER_RC": str(verifier_rc),
            **{k: v for k, v in fixture.env.items() if k not in omit_env},
            **(extra_env or {}),
        }

        proc = subprocess.run(  # noqa: S603 - fixed argv, stub PATH, repo-local script
            ["bash", str(self.mirror / script), *args],  # noqa: S607
            capture_output=True,
            text=True,
            env=env,
            cwd=str(self.mirror),
            timeout=timeout,
            # So a stub draining stdin sees EOF at once unless it is genuinely
            # downstream of a pipe.
            stdin=subprocess.DEVNULL,
        )
        calls = [
            line.split("\t")
            for line in argv_log.read_text().splitlines()
            if line.strip()
        ]
        return HarnessRun(
            proc.returncode,
            proc.stdout,
            proc.stderr,
            calls,
            json.loads(public_ingress_state.read_text()),
        )


def summarise(run: HarnessRun) -> str:
    """A failure message that says where the script actually stopped."""
    tail = "\n".join(run.stderr.splitlines()[-12:])
    return (
        f"exit={run.returncode}\n"
        f"commands={json.dumps([c[0] for c in run.calls][-12:])}\n"
        f"stderr tail:\n{tail}"
    )


assert os.name == "posix", "the deploy-script harness needs a POSIX shell"
