from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "scripts" / "deploy" / "rollout_frontend_attest.py"
SMOKE = REPOSITORY / "scripts" / "deploy" / "rollout_smoke.sh"
PROJECT = "attest-prod1"
FORWARDING_RULE = "tr-public-https"
HTTPS_PROXY = "tr-public-proxy"
URL_MAP = "tr-six-surface-map"
CERTIFICATE_MAP = "tr-public-cert-map"
VIP = "34.111.20.30"
HOSTS = [
    "trustedrouter.com",
    "www.trustedrouter.com",
    "status.trustedrouter.com",
    "eu.trustedrouter.com",
    "status-us.trustedrouter.com",
    "status-eu.trustedrouter.com",
    "allyrouter.com",
    "www.allyrouter.com",
    "status.allyrouter.com",
    "trust.allyrouter.com",
    "uptimerouter.com",
    "www.uptimerouter.com",
    "status.uptimerouter.com",
    "trust.uptimerouter.com",
]
SECRET_SENTINEL = "DO-NOT-LOG-OR-PERSIST-PRIVATE-KEY"  # noqa: S105


def _compute_url(collection: str, name: str) -> str:
    return f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/{collection}/{name}"


def _cm_resource(collection: str, name: str) -> str:
    return f"projects/{PROJECT}/locations/global/{collection}/{name}"


def _map_entry_resource(name: str) -> str:
    return (
        f"projects/{PROJECT}/locations/global/certificateMaps/{CERTIFICATE_MAP}/"
        f"certificateMapEntries/{name}"
    )


def _default_state() -> dict[str, Any]:
    certificates: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for index, host in enumerate(HOSTS, start=1):
        certificate_name = f"tr-host-{index}"
        certificate_resource = _cm_resource("certificates", certificate_name)
        certificates[certificate_name] = {
            "name": certificate_resource,
            "managed": {"state": "ACTIVE", "domains": [host]},
            "sanDnsnames": [host],
            # This simulates unrelated/sensitive provider response fields.  The
            # attestation artifact and errors must never reproduce them.
            "privateKey": SECRET_SENTINEL,
        }
        entries.append(
            {
                "name": _map_entry_resource(f"host-{index}"),
                "state": "ACTIVE",
                "hostname": host,
                "certificates": [certificate_resource],
            }
        )
    return {
        "forwarding_rule": {
            "name": FORWARDING_RULE,
            "selfLink": _compute_url("forwardingRules", FORWARDING_RULE),
            "IPAddress": VIP,
            "IPProtocol": "TCP",
            "portRange": "443-443",
            "networkTier": "PREMIUM",
            "loadBalancingScheme": "EXTERNAL_MANAGED",
            "target": _compute_url("targetHttpsProxies", HTTPS_PROXY),
        },
        "https_proxy": {
            "name": HTTPS_PROXY,
            "selfLink": _compute_url("targetHttpsProxies", HTTPS_PROXY),
            "urlMap": _compute_url("urlMaps", URL_MAP),
            "certificateMap": (
                "//certificatemanager.googleapis.com/"
                + _cm_resource("certificateMaps", CERTIFICATE_MAP)
            ),
        },
        "url_map": {
            "name": URL_MAP,
            "selfLink": _compute_url("urlMaps", URL_MAP),
        },
        "certificate_map": {
            "name": _cm_resource("certificateMaps", CERTIFICATE_MAP),
        },
        "certificate_map_entries": entries,
        "certificates": certificates,
        "compute_certificates": {},
        "dns": {host: {"A": [VIP], "AAAA": []} for host in HOSTS},
    }


GCLOUD_FAKE = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = json.loads(Path(os.environ["FAKE_FRONTEND_STATE"]).read_text())
if state.get("fail_gcloud"):
    sys.stderr.write(state.get("provider_secret", "provider failure"))
    raise SystemExit(23)
args = sys.argv[1:]
key = None
value = None
if args[:3] == ["compute", "forwarding-rules", "describe"]:
    key = "forwarding_rule"
    value = state[key]
elif args[:3] == ["compute", "target-https-proxies", "describe"]:
    key = "https_proxy"
    value = state[key]
elif args[:3] == ["compute", "url-maps", "describe"]:
    key = "url_map"
    value = state[key]
elif args[:3] == ["compute", "ssl-certificates", "describe"]:
    key = "compute_certificate"
    value = state["compute_certificates"][args[3]]
elif args[:3] == ["certificate-manager", "maps", "describe"]:
    key = "certificate_map"
    value = state[key]
elif args[:4] == ["certificate-manager", "maps", "entries", "list"]:
    key = "certificate_map_entries"
    value = state[key]
elif args[:3] == ["certificate-manager", "certificates", "describe"]:
    key = "certificate"
    value = state["certificates"][args[3]]
else:
    raise SystemExit(97)
if state.get("malformed_gcloud") == key:
    sys.stdout.write("not-json")
else:
    json.dump(value, sys.stdout)
"""


DIG_FAKE = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = json.loads(Path(os.environ["FAKE_FRONTEND_STATE"]).read_text())
if state.get("fail_dig"):
    sys.stderr.write(state.get("provider_secret", "dns failure"))
    raise SystemExit(29)
host, record_type = sys.argv[-2:]
answers = state["dns"][host][record_type]
for answer in answers:
    print(answer)
"""


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.script = root / "scripts" / "deploy" / SOURCE.name
        self.smoke = root / "scripts" / "deploy" / SMOKE.name
        self.state_path = root / "provider-state.json"
        self.artifact = root / "artifacts" / "frontend-attestation.json"
        self.fake_bin = root / "fake-bin"
        self.fake_bin.mkdir(parents=True)
        self.script.parent.mkdir(parents=True)
        shutil.copy2(SOURCE, self.script)
        shutil.copy2(SMOKE, self.smoke)
        for name, body in (("gcloud", GCLOUD_FAKE), ("dig", DIG_FAKE)):
            executable = self.fake_bin / name
            executable.write_text(body, encoding="utf-8")
            executable.chmod(0o755)
        self.state = _default_state()
        self.write_state()
        self.environment = os.environ.copy()
        self.environment["PATH"] = f"{self.fake_bin}{os.pathsep}{self.environment['PATH']}"
        self.environment["FAKE_FRONTEND_STATE"] = str(self.state_path)

    def write_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def capture(
        self,
        *,
        artifact: Path | None = None,
        hosts: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- fixed local test executable and argv
            [
                sys.executable,
                str(self.script),
                "capture",
                "--project",
                PROJECT,
                "--forwarding-rule",
                FORWARDING_RULE,
                "--https-proxy",
                HTTPS_PROXY,
                "--url-map",
                URL_MAP,
                "--hosts",
                ",".join(hosts or HOSTS),
                "--artifact",
                str(artifact or self.artifact),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )

    def verify(self, *, artifact: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 -- fixed local test executable and argv
            [
                sys.executable,
                str(self.script),
                "verify-artifact",
                str(artifact or self.artifact),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_capture_and_live_verify_certificate_map(harness: Harness) -> None:
    captured = harness.capture()
    assert captured.returncode == 0, captured.stderr
    assert stat.S_IMODE(harness.artifact.stat().st_mode) == 0o600
    artifact_text = harness.artifact.read_text(encoding="utf-8")
    artifact = json.loads(artifact_text)
    assert artifact["schema_version"] == 1
    assert artifact["request"]["hosts"] == HOSTS
    assert artifact["frontend"]["certificate_binding"]["mode"] == "certificate-map"
    assert artifact["frontend"]["forwarding_rule"] == {
        "resource": f"projects/{PROJECT}/global/forwardingRules/{FORWARDING_RULE}",
        "ip_address": VIP,
        "ip_protocol": "TCP",
        "ports": ["443"],
        "network_tier": "PREMIUM",
        "load_balancing_scheme": "EXTERNAL_MANAGED",
        "target_https_proxy": (f"projects/{PROJECT}/global/targetHttpsProxies/{HTTPS_PROXY}"),
    }
    assert SECRET_SENTINEL not in artifact_text
    assert SECRET_SENTINEL not in captured.stdout + captured.stderr

    verified = harness.verify()
    assert verified.returncode == 0, verified.stderr
    assert SECRET_SENTINEL not in verified.stdout + verified.stderr


def test_capture_rejects_partial_managed_host_inventory(harness: Harness) -> None:
    result = harness.capture(hosts=HOSTS[:3])
    assert result.returncode != 0
    assert "managed-domain contract" in result.stderr
    assert not harness.artifact.exists()


def test_capture_and_live_verify_direct_managed_certificates(harness: Harness) -> None:
    certificate_name = "tr-direct-cert"
    certificate_url = _compute_url("sslCertificates", certificate_name)
    harness.state["https_proxy"].pop("certificateMap")
    harness.state["https_proxy"]["sslCertificates"] = [certificate_url]
    harness.state["compute_certificates"][certificate_name] = {
        "name": certificate_name,
        "selfLink": certificate_url,
        "type": "MANAGED",
        "managed": {"status": "ACTIVE", "domains": list(reversed(HOSTS))},
        "subjectAlternativeNames": list(HOSTS),
        "privateKey": SECRET_SENTINEL,
    }
    harness.write_state()

    captured = harness.capture()
    assert captured.returncode == 0, captured.stderr
    artifact = json.loads(harness.artifact.read_text(encoding="utf-8"))
    binding = artifact["frontend"]["certificate_binding"]
    assert binding["mode"] == "direct"
    assert binding["certificate_map"] is None
    assert binding["entries"] == []
    assert binding["certificates"][0]["domains"] == sorted(HOSTS)
    assert harness.verify().returncode == 0


@pytest.mark.parametrize(
    "drift",
    ["wrong-proxy", "wrong-map", "wrong-vip", "wrong-scheme", "extra-ipv6"],
)
def test_capture_rejects_hostile_frontend_contract(harness: Harness, drift: str) -> None:
    if drift == "wrong-proxy":
        harness.state["forwarding_rule"]["target"] = _compute_url(
            "targetHttpsProxies", "attacker-proxy"
        )
    elif drift == "wrong-map":
        harness.state["https_proxy"]["urlMap"] = _compute_url("urlMaps", "attacker-map")
    elif drift == "wrong-vip":
        harness.state["forwarding_rule"]["IPAddress"] = "34.111.20.31"
    elif drift == "wrong-scheme":
        harness.state["forwarding_rule"]["loadBalancingScheme"] = "EXTERNAL"
    else:
        harness.state["dns"][HOSTS[0]]["AAAA"] = ["2001:4860:4860::8888"]
    harness.write_state()

    result = harness.capture()
    assert result.returncode == 1
    assert not harness.artifact.exists()


@pytest.mark.parametrize("drift", ["inactive-certificate", "missing-host"])
def test_capture_rejects_inactive_or_incomplete_certificates(harness: Harness, drift: str) -> None:
    certificate = harness.state["certificates"]["tr-host-2"]
    if drift == "inactive-certificate":
        certificate["managed"]["state"] = "PROVISIONING"
    else:
        certificate["managed"]["domains"] = ["unrelated.example.com"]
        certificate["sanDnsnames"] = ["unrelated.example.com"]
    harness.write_state()

    result = harness.capture()
    assert result.returncode == 1
    assert not harness.artifact.exists()


@pytest.mark.parametrize("shadow", ["exact", "wildcard", "ambiguous-exact"])
def test_certificate_map_enforces_selector_precedence(
    harness: Harness, shadow: str
) -> None:
    host_index = 1 if shadow == "wildcard" else 0
    host = HOSTS[host_index]
    exact_entry = harness.state["certificate_map_entries"][host_index]

    fallback_name = f"fallback-{shadow}"
    fallback_resource = _cm_resource("certificates", fallback_name)
    harness.state["certificates"][fallback_name] = {
        "name": fallback_resource,
        "managed": {"state": "ACTIVE", "domains": [host]},
        "sanDnsnames": [host],
    }
    fallback_entry = {
        "name": _map_entry_resource(f"fallback-{shadow}"),
        "state": "ACTIVE",
        "certificates": [fallback_resource],
    }

    if shadow == "exact":
        fallback_entry["hostname"] = "*.trustedrouter.com"
        bad_certificate = harness.state["certificates"]["tr-host-1"]
        bad_certificate["managed"]["domains"] = ["unrelated.example.com"]
        bad_certificate["sanDnsnames"] = ["unrelated.example.com"]
    elif shadow == "wildcard":
        exact_entry["hostname"] = "unused.trustedrouter.com"
        fallback_entry["matcher"] = "PRIMARY"
        wildcard_resource = _cm_resource("certificates", "bad-wildcard")
        harness.state["certificates"]["bad-wildcard"] = {
            "name": wildcard_resource,
            "managed": {"state": "ACTIVE", "domains": ["*.unrelated.example.com"]},
            "sanDnsnames": ["*.unrelated.example.com"],
        }
        harness.state["certificate_map_entries"].append(
            {
                "name": _map_entry_resource("bad-wildcard"),
                "state": "ACTIVE",
                "hostname": "*.trustedrouter.com",
                "certificates": [wildcard_resource],
            }
        )
    else:
        fallback_entry["hostname"] = host
    harness.state["certificate_map_entries"].append(fallback_entry)
    harness.write_state()

    result = harness.capture()
    assert result.returncode == 1
    assert not harness.artifact.exists()


@pytest.mark.parametrize("drift", ["provider", "dns"])
def test_verify_requeries_live_state_and_rejects_post_capture_drift(
    harness: Harness, drift: str
) -> None:
    assert harness.capture().returncode == 0
    if drift == "provider":
        harness.state["https_proxy"]["urlMap"] = _compute_url("urlMaps", "replacement-map")
    else:
        harness.state["dns"][HOSTS[-1]]["A"] = ["34.111.20.99"]
    harness.write_state()

    verified = harness.verify()
    assert verified.returncode == 1
    assert "verified" not in verified.stderr


def test_verify_rejects_changed_repository_smoke_hash(harness: Harness) -> None:
    assert harness.capture().returncode == 0
    with harness.smoke.open("a", encoding="utf-8") as output:
        output.write("\n# post-capture drift\n")

    verified = harness.verify()
    assert verified.returncode == 1
    assert "hash differs" in verified.stderr


def test_verify_rejects_malformed_extra_fields_and_insecure_mode(harness: Harness) -> None:
    assert harness.capture().returncode == 0
    artifact = json.loads(harness.artifact.read_text(encoding="utf-8"))
    artifact["unexpected"] = True
    harness.artifact.write_text(json.dumps(artifact), encoding="utf-8")
    harness.artifact.chmod(0o600)
    malformed = harness.verify()
    assert malformed.returncode == 1
    assert "fields differ" in malformed.stderr

    del artifact["unexpected"]
    harness.artifact.write_text(json.dumps(artifact), encoding="utf-8")
    harness.artifact.chmod(0o644)
    insecure = harness.verify()
    assert insecure.returncode == 1
    assert "mode-0600" in insecure.stderr


def test_provider_output_failure_is_silent_and_fail_closed(harness: Harness) -> None:
    harness.state["fail_gcloud"] = True
    harness.state["provider_secret"] = SECRET_SENTINEL
    harness.write_state()

    failed = harness.capture()
    assert failed.returncode == 1
    assert SECRET_SENTINEL not in failed.stdout + failed.stderr
    assert not harness.artifact.exists()

    harness.state.pop("fail_gcloud")
    harness.state["malformed_gcloud"] = "forwarding_rule"
    harness.write_state()
    malformed = harness.capture()
    assert malformed.returncode == 1
    assert not harness.artifact.exists()


def test_atomic_output_failure_leaves_no_partial_artifact(harness: Harness) -> None:
    destination = harness.root / "artifact-is-a-directory"
    destination.mkdir()

    failed = harness.capture(artifact=destination)
    assert failed.returncode == 1
    assert destination.is_dir()
    assert list(harness.root.glob(".artifact-is-a-directory.*.tmp")) == []


def test_capture_replaces_existing_artifact_with_mode_0600(harness: Harness) -> None:
    harness.artifact.parent.mkdir(parents=True)
    harness.artifact.write_text("old", encoding="utf-8")
    harness.artifact.chmod(0o666)

    captured = harness.capture()
    assert captured.returncode == 0, captured.stderr
    assert stat.S_IMODE(harness.artifact.stat().st_mode) == 0o600
    assert json.loads(harness.artifact.read_text(encoding="utf-8"))["schema_version"] == 1


def test_capture_accepts_a_cname_that_chains_through_a_managed_host(harness: Harness) -> None:
    """www is a CNAME to the apex by production DNS policy; the apex is attested.

    The chain is recorded in the artifact so verify re-checks the same path.
    """
    harness.state["dns"]["www.trustedrouter.com"]["A"] = ["trustedrouter.com.", VIP]
    harness.state["dns"]["www.trustedrouter.com"]["AAAA"] = ["trustedrouter.com."]
    harness.write_state()

    captured = harness.capture()
    assert captured.returncode == 0, captured.stderr
    artifact = json.loads(harness.artifact.read_text(encoding="utf-8"))
    entries = {entry["host"]: entry for entry in artifact["frontend"]["dns"]}
    assert entries["www.trustedrouter.com"]["cname"] == ["trustedrouter.com"]
    assert entries["www.trustedrouter.com"]["a"] == [VIP]
    assert "cname" not in entries["trustedrouter.com"]

    verified = harness.verify()
    assert verified.returncode == 0, verified.stderr


@pytest.mark.parametrize(
    "answers",
    [
        ["lore-hex.github.io.", "185.199.111.153"],
        ["trustedrouter.com.", "lore-hex.github.io.", VIP],
        [VIP, "trustedrouter.com."],
        ["www.trustedrouter.com.", VIP],
    ],
    ids=["outside-managed", "outside-mid-chain", "cname-after-address", "self-cname"],
)
def test_capture_rejects_cnames_that_leave_the_managed_set(
    harness: Harness, answers: list[str]
) -> None:
    harness.state["dns"]["www.trustedrouter.com"]["A"] = answers
    harness.write_state()

    captured = harness.capture()
    assert captured.returncode != 0
    assert "DNS response" in captured.stderr
    assert not harness.artifact.exists()
