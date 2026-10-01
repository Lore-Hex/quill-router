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
