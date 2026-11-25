import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from scripts.deploy import public_reuse as site
from scripts.deploy import schema_presence as schema
from scripts.deploy.release_train import Refused
from tests.test_public_rollout_report import DIGEST, SHA, resources


def test_schema_batch_is_scoped_metadata_and_never_proves_absence(monkeypatch):
    def run(args, **kwargs):
        sql = args[args.index("--sql") + 1]
        assert "FROM INFORMATION_SCHEMA.COLUMNS" in sql and "JOIN INFORMATION_SCHEMA.TABLES" in sql
        assert "c.table_name IN ('tr_lease_hold')" in sql and "spanner_state='COMMITTED'" in sql
        assert kwargs["timeout"] <= 15
        return SimpleNamespace(stdout=b'{"rows":[["tr_lease_hold","ceiling"]]}')
    monkeypatch.setattr(schema.subprocess, "run", run)
    assert schema.snapshot("db", "inst", "project", ["tr_lease_hold"]) == "|tr_lease_hold|tr_lease_hold.ceiling|"
    with pytest.raises(ValueError):
        schema.snapshot("db", "inst", "project", ["invalid'"])
    monkeypatch.setattr(schema.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=b'{"rows":[["other","a"]]}'))
    with pytest.raises(ValueError):
        schema.snapshot("db", "inst", "project", ["tr_lease_hold"])


@pytest.mark.parametrize(("path", "mode", "eligible"), [
    ("src/trusted_router/trust_reconcile_cli.py", "M", True),
    ("src/trusted_router/trust_reconcile_cli.py", "D", False),
    ("src/trusted_router/storage_gcp.py", "M", False),
    ("scripts/deploy/public_surface.sh", "M", False),
    ("Dockerfile", "M", False),
    ("src/trusted_router/templates/public/about.html", "M", False),
    ("tests/test_example.py", "M", False),
])
def test_only_modified_standalone_workers_qualify(monkeypatch, path, mode, eligible):
    monkeypatch.setattr(site.docs, "commit", lambda x: x)
    monkeypatch.setattr(site.docs, "git", lambda *a: SHA)
    monkeypatch.setattr(site.docs, "command", lambda *a: f":100644 100644 abc def {mode}\0{path}\0")
    assert site.worker_delta(SHA, "b" * 40) is eligible


def test_image_gate_checks_actual_dependencies_not_source_labels():
    layers = ["sha256:" + x * 64 for x in "abc"]
    config = {"history": [{"created_by": "FROM base"}, {"created_by": "COPY src ./src # buildkit"},
                          {"created_by": "compile"}], "rootfs": {"diff_ids": layers}, "config": {"User": "1000"}}
    before = site.runtime_layers(config)
    layers[0] = layers[1]
    assert site.runtime_layers(config) != before
    config["history"][1]["created_by"] = "unrecognized"
    with pytest.raises(Refused):
        site.runtime_layers(config)


@pytest.mark.parametrize("damage", ["none", "rotated", "disabled", "iam", "old", "image", "bindings"])
def test_website_receipt_requires_all_regional_evidence(monkeypatch, damage):
    now = datetime.now(UTC)
    created = now - timedelta(hours=25 if damage == "old" else 1)
    def active(region, name):
        service, revision = resources(name + "-one")
        revision["metadata"]["creationTimestamp"] = created.isoformat()
        if name == site.docs.SERVICES[1] and damage != "bindings":
            revision["spec"]["containers"][0]["env"].extend([
                {"name": "var_" + str(i), "valueFrom": {"secretKeyRef": {"name": s, "key": "latest"}}}
                for i, s in enumerate(sorted(site.SECRETS))])
        return service, revision
    def cloud(*args):
        if args[0] == "secrets":
            return {"name": "versions/1", "state": "DISABLED" if damage == "disabled" else "ENABLED",
                    "createTime": (now if damage == "rotated" else created - timedelta(days=1)).isoformat()}
        return {"bindings": [] if damage == "iam" else [{"members": ["allUsers"], "role": "roles/run.invoker"}]}
    monkeypatch.setattr(site, "active", active)
    monkeypatch.setattr(site.docs, "cloud", cloud)
    monkeypatch.setattr(site.docs, "commit", lambda x: x)
    monkeypatch.setattr("scripts.deploy.public_rollout_report.commit", lambda x: x)
    if damage != "none":
        with pytest.raises(Refused):
            site.inventory(SHA, "different" if damage == "image" else DIGEST)
    else:
        result = site.inventory(SHA, DIGEST)
        assert set(result["fleet"]) == set(site.docs.REGIONS)
        assert "sensitive-value" not in json.dumps(result)


def test_stale_api_evidence_forces_full_website_deploy(monkeypatch):
    observed = {"fleet": {"region": {"image": DIGEST}}}
    monkeypatch.setattr(site, "worker_delta", lambda *a: True)
    monkeypatch.setattr(site, "baseline", lambda *a: observed)
    monkeypatch.setattr(site, "inventory", lambda *a: observed)
    monkeypatch.setattr(site, "runtime", lambda *a: ([], {}))
    monkeypatch.setattr(site.docs, "commit", lambda x: x)
    monkeypatch.setattr(site.docs, "git", lambda *a: SHA)
    for sha in ["a" * 40, "b" * 40]:
        result = json.dumps({"plane": "trustedrouter.com", "release": sha})
        monkeypatch.setattr(site.docs, "command", lambda *a, result=result, **k: result)
        assert site.worker_only(SHA, DIGEST) is (sha == SHA)
