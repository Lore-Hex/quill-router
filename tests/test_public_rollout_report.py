from __future__ import annotations

import copy
import json
import sys

import pytest

from scripts.deploy import public_rollout_report as report
from scripts.deploy.release_train import IMAGE, Refused

SHA = "a" * 40
DIGEST = IMAGE + "b" * 64


def resources(name):
    revision = {"metadata": {"name": name, "annotations": {"autoscaling.knative.dev/minScale": "1"}},
                "spec": {"serviceAccountName": "public", "containers": [{
                    "image": DIGEST, "env": [{"name": k, "value": SHA} for k in
                                             ("TR_RELEASE", "TR_PUBLIC_SOURCE_COMMIT")] +
                                             [{"name": "SAMPLE", "value": "sensitive-value"}]}]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    service = {"metadata": {"name": report.SERVICE, "annotations": {
        "run.googleapis.com/ingress": "internal-and-cloud-load-balancing"}},
        "status": {"traffic": [{"revisionName": name, "percent": 100}],
                   "conditions": [{"type": "Ready", "status": "True"}]},
        "spec": {"traffic": [{"revisionName": name, "percent": 100}], "template": {
            "metadata": {}, "spec": copy.deepcopy(revision["spec"])}}}
    return service, revision


@pytest.fixture(autouse=True)
def resolve(monkeypatch):
    monkeypatch.setattr(report, "commit", lambda value: value)


def test_equivalence_ignores_revision_names_not_configuration_or_sources():
    before = report.signature(*resources(report.SERVICE + "-one"))
    service, revision = resources(report.SERVICE + "-two")
    after = report.signature(service, revision)
    assert "sensitive-value" not in json.dumps(after)
    assert report.compare(before, after, DIGEST, SHA) == "observed match"
    for mutate in (lambda s, r: r["spec"].update(serviceAccountName="different"),
                   lambda s, r: s["spec"]["template"]["metadata"].update(labels={"pin": "1"}),
                   lambda s, r: r["metadata"]["annotations"].update({"run.googleapis.com/vpc-access-egress": "all"})):
        mutate(service, revision)
        assert report.compare(before, report.signature(service, revision), DIGEST, SHA) == "changed: configuration"
        service, revision = resources(report.SERVICE + "-two")
    assert report.compare({}, after, DIGEST, SHA) == "unknown"
    assert report.compare(before, after, IMAGE + "c" * 64, SHA) == "unknown"
    assert report.compare(before, after, DIGEST, "d" * 40) == "unknown"


@pytest.mark.parametrize("damage", ["split", "tag", "desired", "ingress", "readiness", "image"])
def test_unproven_state_is_never_a_match(damage):
    service, revision = resources(report.SERVICE + "-two")
    if damage == "split":
        service["status"]["traffic"].append({"percent": 1})
    elif damage == "tag":
        service["status"]["traffic"][0]["tag"] = "unexpected"
    elif damage == "desired":
        service["spec"]["traffic"][0]["revisionName"] = "another"
    elif damage == "ingress":
        service["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    elif damage == "readiness":
        revision["status"]["conditions"][0]["status"] = "Unknown"
    else:
        revision["spec"]["containers"][0]["image"] = "mutable:latest"
    with pytest.raises(Refused):
        report.signature(service, revision)


def test_report_records_only_aggregate_decision_with_partial_read(monkeypatch, tmp_path):
    summary = tmp_path / "summary"
    for k, v in {"RUNNER_TEMP": str(tmp_path), "GITHUB_STEP_SUMMARY": str(summary),
                 "EXPECTED_IMAGE": DIGEST, "EXPECTED_COMMIT": SHA}.items():
        monkeypatch.setenv(k, v)
    fingerprint = report.signature(*resources(report.SERVICE + "-one"))
    monkeypatch.setattr(report, "observe", lambda _: fingerprint)
    monkeypatch.setattr(sys, "argv", ["report", "before"])
    report.main()
    monkeypatch.setattr(report, "observe", lambda r: {} if r == report.REGIONS[-1] else fingerprint)
    monkeypatch.setattr(sys, "argv", ["report", "after"])
    report.main()
    assert summary.read_text().count("observed match") == 3
    assert "unknown" in summary.read_text() and "Fleet match: not proven" in summary.read_text()
