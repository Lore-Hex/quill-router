import json
import subprocess

import pytest

from scripts.deploy import reconcile_cloud_releases as r


@pytest.fixture
def api(monkeypatch):
    state = {"calls": [], "jobs": {j: "success" for j in r.REQUIRED_JOBS},
             "runs": [], "comparison": "ahead"}

    def gh(*args):
        state["calls"].append(args)
        if args[:2] == ("run", "list"):
            if "deploy.yml" in args:
                return [{"databaseId": 1, "headSha": "a" * 40}]
            return state["runs"]
        if args[:2] == ("run", "view"):
            return {"jobs": [{"name": k, "conclusion": v} for k, v in state["jobs"].items()]}
        if args[0] == "api":
            if args[1].endswith("/compare/" + "a" * 40 + "..." + "a" * 40):
                return {"status": state.get("gcp_comparison", "identical")}
            return {"status": "ahead" if args[1].endswith("...main") else state["comparison"]}
        return None

    monkeypatch.setattr(r, "gh", gh)
    monkeypatch.setattr(r, "probe_cloud", lambda cloud: ("a" if cloud == "gcp" else "b") * 40)
    return state


def test_dispatches_both_secondary_clouds_with_exact_release(api):
    assert len(r.reconcile()) == 2
    dispatches = [c for c in api["calls"] if c[:2] == ("workflow", "run")]
    assert len(dispatches) == 2
    assert all("release_sha=" + "a" * 40 in c for c in dispatches)
    assert all("promotion_only=true" in c for c in dispatches)
    assert all(c[c.index("--ref") + 1] == "main" for c in dispatches)


@pytest.mark.parametrize("job", sorted(r.REQUIRED_JOBS))
@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_requires_all_completion_gates(api, job, result):
    api["jobs"][job] = result
    with pytest.raises(r.Refused, match="fully verified"):
        r.reconcile()
    assert not any(c[0] == "workflow" for c in api["calls"])


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
def test_does_not_duplicate_pending_deploys(api, status):
    api["runs"] = [{"status": status}]
    assert all(row["action"] == "already queued or running" for row in r.reconcile())


@pytest.mark.parametrize("comparison", ["identical", "behind"])
def test_never_downgrades_or_redeploys_current_release(api, comparison):
    api["comparison"] = comparison
    assert all(row["action"] == "current or newer" for row in r.reconcile())


def test_diverged_release_refused(api):
    api["comparison"] = "diverged"
    with pytest.raises(r.Refused, match="diverges"):
        r.reconcile()


def test_unknown_health_never_dispatches(api, monkeypatch):
    def sick(_):
        raise r.Refused("unknown health")
    monkeypatch.setattr(r, "probe_cloud", sick)
    with pytest.raises(r.Refused, match="unknown health"):
        r.reconcile()
    assert not any(c[0] == "workflow" for c in api["calls"])


def test_rolled_back_gcp_candidate_is_not_promoted_elsewhere(api):
    api["gcp_comparison"] = "ahead"
    with pytest.raises(r.Refused, match="no longer serves"):
        r.reconcile()
    assert not any(c[0] == "workflow" for c in api["calls"])


def test_queued_promotion_refuses_a_diverged_target(api):
    api["comparison"] = "diverged"
    with pytest.raises(r.Refused, match="queued promotion"):
        r.verify_promotion("aws", "a" * 40)


@pytest.mark.parametrize("comparison", ["ahead", "identical", "behind"])
def test_queued_promotion_only_changes_an_older_release(api, comparison):
    api["comparison"] = comparison
    assert r.verify_promotion("azure", "a" * 40) == (comparison == "ahead")


@pytest.mark.parametrize("acknowledgment", [
    "", "Created workflow_dispatch event for deploy-aws-control-plane.yml at main\n",
    "https://github.com/Lore-Hex/quill-router/actions/runs/36933182051\n",
])
def test_cli_acknowledgment_does_not_stop_second_cloud(monkeypatch, acknowledgment):
    calls = []

    def run(command, **kwargs):
        assert kwargs["check"] is True and kwargs["timeout"] == 60
        calls.append(command)
        args = command[1:]
        if args[:2] == ["workflow", "run"]:
            output = acknowledgment
        elif args[:2] == ["run", "list"]:
            output = json.dumps(
                [{"databaseId": 1, "headSha": "a" * 40}] if "deploy.yml" in args else []
            )
        elif args[:2] == ["run", "view"]:
            output = json.dumps({"jobs": [
                {"name": name, "conclusion": "success"} for name in r.REQUIRED_JOBS
            ]})
        else:
            assert args[0] == "api"
            identical = args[1].endswith("/compare/" + "a" * 40 + "..." + "a" * 40)
            output = json.dumps({"status": "identical" if identical else "ahead"})
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(r.subprocess, "run", run)
    monkeypatch.setattr(r, "probe_cloud", lambda cloud: ("a" if cloud == "gcp" else "b") * 40)
    assert [item["cloud"] for item in r.reconcile()] == ["aws", "azure"]
    dispatches = [c for c in calls if c[1:3] == ["workflow", "run"]]
    assert len(dispatches) == 2
    assert all("release_sha=" + "a" * 40 in command for command in dispatches)


def test_cli_failed_dispatch_still_raises(monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="dispatch refused")

    monkeypatch.setattr(r.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        r.gh("workflow", "run", "deploy-azure-control-plane.yml")


def test_cli_malformed_read_response_still_raises(monkeypatch):
    monkeypatch.setattr(r.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, stdout="not JSON", stderr=""))
    with pytest.raises(json.JSONDecodeError):
        r.gh("api", "repos/Lore-Hex/quill-router/compare/main...main")
