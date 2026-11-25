from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.deploy import public_docs as docs
from scripts.deploy.release_train import IMAGE, Refused, Release
from scripts.deploy.superseded_deploy import delivered

ROOT = Path(__file__).resolve().parents[1]
A, B = "a" * 40, "b" * 40
RELEASE = Release(B, B[:9], IMAGE + "b" * 64, 1, 1)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    docs.git("init", "-q")
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
        docs.git("config", key, value)
    for path in (docs.DOC, "src/app.py", "tests/test_docs.py", "docs/notes.md", "Dockerfile"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n")
    save()
    return tmp_path, docs.git("rev-parse", "HEAD")


def save():
    docs.git("add", ".")
    docs.git("commit", "-qm", "test")
    return docs.git("rev-parse", "HEAD")


def test_docs_and_tests_use_docs_lane(repo):
    root, base = repo
    for path in (docs.DOC, "tests/test_docs.py", "docs/notes.md"):
        (root / path).write_text("new\n")
    head = save()
    assert docs.docs_delta(base, head)
    assert docs.proposed_docs_only()
    assert docs.docs_delta(head, head)
    assert not docs.docs_delta(head, base)
    docs.guard_source(base, head)
    with pytest.raises(Refused, match="downgrade"):
        docs.guard_source(head, base)


@pytest.mark.parametrize("path", ["src/app.py", "Dockerfile", "uv.lock", "pyproject.toml",
                                    ".gcloudignore", ".github/workflows/deploy.yml",
                                    "scripts/deploy/public_docs.py", "src/trusted_router/templates/public/base.html"])
def test_any_runtime_or_workflow_change_forces_full_rollout(repo, path):
    root, base = repo
    (root / docs.DOC).write_text("new docs\n")
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    head = save()
    assert not docs.docs_delta(base, head)
    assert not docs.proposed_docs_only()


@pytest.mark.parametrize("kind", ["delete", "rename", "executable", "symlink"])
def test_doc_shape_changes_cannot_use_narrow_lane(repo, kind):
    root, base = repo
    path = root / docs.DOC
    if kind == "delete":
        path.unlink()
    elif kind == "rename":
        path.rename(path.with_suffix(".old"))
    elif kind == "executable":
        path.chmod(0o755)
    else:
        path.unlink()
        path.symlink_to("../base.html")
    assert not docs.docs_delta(base, save())


def test_docs_change_does_not_hide_undelivered_application_change(repo):
    root, base = repo
    (root / "src/app.py").write_text("pending application\n")
    save()
    (root / docs.DOC).write_text("docs\n")
    head = save()
    assert docs.proposed_docs_only()
    assert not docs.docs_delta(base, head)


@pytest.mark.parametrize("path", [*sorted(docs.SITE_FILES), "src/trusted_router/templates/homepage/top.html"])
def test_existing_site_edits_qualify_but_new_inputs_do_not(repo, path):
    root, base = repo
    target = root / path
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n")
        base = save()
        assert not docs.proposed_docs_only()
    target.write_text("new\n")
    assert docs.docs_delta(base, save()) and docs.proposed_docs_only()


@pytest.mark.parametrize("path", ["src/trusted_router/static/homepage/homepage.js",
                                  "src/trusted_router/content/blog.py", "src/trusted_router/dashboard.py",
                                  "src/trusted_router/templates/homepage/nested/page.html",
                                  "src/trusted_router/templates/public/_signin.html"])
def test_site_lookalikes_do_not_qualify(path):
    assert not docs.site_file(path)


def rows():
    return [{"service": service, "region": region, "commit": A, "api_commit": A,
             "revision": service + "-old", "image": IMAGE + "a" * 64, "spec_sha256": "a" * 64}
            for service in docs.SERVICES for region in docs.REGIONS]


def test_all_eight_sources_are_qualified(monkeypatch):
    checked = []
    monkeypatch.setattr(docs, "docs_delta", lambda base, head: checked.append((base, head)) or True)
    fleet = rows()
    fleet[-1]["commit"] = B
    fleet[2]["api_commit"] = "c" * 40
    assert docs.eligible(fleet, RELEASE)
    assert checked == [(A, B), (B, B), ("c" * 40, B)]
    with pytest.raises(Refused, match="incomplete"):
        docs.eligible(rows()[:-1], RELEASE)


def setup_publish(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TR_DEPLOY_MUTEX_OPERATION", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(docs, "validated_release", lambda: RELEASE)
    monkeypatch.setattr(docs, "inventory", rows)
    monkeypatch.setattr(docs, "eligible", lambda *_: True)
    monkeypatch.setattr(docs, "verify", lambda *_: rows())
    commands = []

    def command(*args, **kwargs):
        commands.append((args, kwargs))
        if args[-1] == "acquire":
            assert kwargs["env"]["TR_DEPLOY_COMPONENT"] == "public-surface"
            return ("TR_DEPLOY_MUTEX_OPERATION=own-operation\nTR_DEPLOY_MUTEX_GENERATION=0\n"
                    "TR_DEPLOY_MUTEX_CREATED_AT=now\nTR_DEPLOY_MUTEX_CLOUD=gcp\n")
        return ""

    monkeypatch.setattr(docs, "command", command)
    mutations = []

    def run(args, **kwargs):
        mutations.append(args)
        assert args == ["bash", "scripts/deploy/public_surface.sh", "routed"]
        assert kwargs["env"]["TR_DEPLOY_MUTEX_OPERATION"] == "own-operation"
        assert kwargs["env"]["TR_PUBLIC_DOCS_MANIFEST"] == str(docs.MANIFEST)
        assert json.loads(docs.MANIFEST.read_text())["before"] == rows()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(docs.subprocess, "run", run)
    return commands, mutations


def test_success_publishes_separate_receipt_without_api_delivery(monkeypatch, tmp_path):
    commands, mutations = setup_publish(monkeypatch, tmp_path)
    docs.publish()
    assert len(mutations) == 1
    assert commands[-1][1]["env"]["TR_DEPLOY_OUTCOME"] == "success"
    receipt = json.loads(Path("release.json").read_text())
    assert receipt["surface"] == "public-docs" and receipt["release"] == asdict(RELEASE)
    assert Path("out").read_text() == "full_rollout=false\ndelivered=true\n"
    assert not delivered([{"name": "public-docs / deploy-public-docs", "conclusion": "success"}])


@pytest.mark.parametrize("after_admission", [False, True])
def test_runtime_mismatch_falls_back_without_mutation(monkeypatch, tmp_path, after_admission):
    commands, mutations = setup_publish(monkeypatch, tmp_path)
    values = iter([True, False] if after_admission else [False])
    monkeypatch.setattr(docs, "eligible", lambda *_: next(values))
    docs.publish()
    assert not mutations
    assert Path("out").read_text() == "full_rollout=true\n"
    assert not Path("release.json").exists()
    if after_admission:
        assert commands[-1][1]["env"]["TR_DEPLOY_OUTCOME"] == "success"
    else:
        assert not commands


@pytest.mark.parametrize("failure", ["preflight", "deploy", "verify"])
def test_failure_never_reports_delivery_or_unlocks_mutated_cloud(monkeypatch, tmp_path, failure):
    commands, mutations = setup_publish(monkeypatch, tmp_path)

    def refuse(*_, **__):
        raise Refused("injected failure")

    if failure == "preflight":
        count = iter([True, None])

        def eligible(*_):
            if next(count) is None:
                refuse()
            return True

        monkeypatch.setattr(docs, "eligible", eligible)
    elif failure == "deploy":
        monkeypatch.setattr(docs.subprocess, "run", refuse)
    else:
        monkeypatch.setattr(docs, "verify", refuse)
    with pytest.raises(Refused):
        docs.publish()
    assert not Path("release.json").exists()
    assert not Path("out").exists()
    assert commands[-1][1]["env"]["TR_DEPLOY_OUTCOME"] == ("success" if failure == "preflight" else "failure")


def test_foreign_lease_cannot_be_borrowed(monkeypatch, tmp_path):
    commands, mutations = setup_publish(monkeypatch, tmp_path)
    monkeypatch.setenv("TR_DEPLOY_MUTEX_OPERATION", "foreign")
    with pytest.raises(Refused, match="borrow"):
        docs.publish()
    assert not commands and not mutations


@pytest.mark.parametrize("after_admission", [False, True])
def test_strict_website_only_never_escalates_to_full_deploy(monkeypatch, tmp_path, after_admission):
    commands, mutations = setup_publish(monkeypatch, tmp_path)
    monkeypatch.setenv("TR_WEBSITE_ONLY", "true")
    eligible = iter([True, False] if after_admission else [False])
    monkeypatch.setattr(docs, "eligible", lambda *_: next(eligible))
    with pytest.raises(Refused, match="no API rollout"):
        docs.publish()
    assert not mutations and not Path("out").exists() and not Path("release.json").exists()
    if after_admission:
        assert commands[-1][1]["env"]["TR_DEPLOY_OUTCOME"] == "success"


def test_candidate_rechecks_exact_baseline(monkeypatch, tmp_path, capsys):
    setup_publish(monkeypatch, tmp_path)
    docs.MANIFEST.write_text(json.dumps({"release": asdict(RELEASE), "before": rows()}))
    monkeypatch.setenv("TR_PUBLIC_DOCS_MANIFEST", str(docs.MANIFEST))
    docs.candidate()
    assert capsys.readouterr().out.strip() == f"{B} {RELEASE.image}"
    changed = rows()
    changed[0]["revision"] = "trusted-router-new"
    monkeypatch.setattr(docs, "inventory", lambda: changed)
    with pytest.raises(Refused, match="baseline changed"):
        docs.candidate()


@pytest.mark.parametrize("damage", [None, "api", "public-image", "public-source", "trust-identity"])
def test_postflight_requires_unchanged_api_and_exact_public_image(monkeypatch, damage):
    before = rows()
    after = copy.deepcopy(before)
    for row in after[4:]:
        row.update(commit=B, image=RELEASE.image)
    if damage == "api":
        after[0]["spec_sha256"] = "different"
    elif damage == "public-image":
        after[-1]["image"] = IMAGE + "a" * 64
    elif damage == "public-source":
        after[-1]["commit"] = A
    elif damage == "trust-identity":
        after[-1]["api_commit"] = B
    monkeypatch.setattr(docs, "inventory", lambda: after)
    commands = []
    monkeypatch.setattr(docs, "command", lambda *args, **kw: commands.append(args))
    if damage:
        with pytest.raises(Refused):
            docs.verify(before, RELEASE)
        assert not commands
    else:
        assert docs.verify(before, RELEASE) == after
        assert ("bash", "scripts/deploy/verify_gcp_complete.sh") in commands
        assert ("bash", "scripts/deploy/public_surface_edge.sh", "check") in commands


def test_newer_public_source_blocks_full_companion(monkeypatch):
    monkeypatch.setattr(docs, "commit", lambda value: value)
    monkeypatch.setattr(docs, "git", lambda *_: A)
    with pytest.raises(Refused, match="downgrade"):
        docs.guard_source(B, A)
    docs.guard_source(A, B)


def serving_documents(service=docs.SERVICES[1]):
    current = {"metadata": {"annotations": {"run.googleapis.com/ingress": "internal-and-cloud-load-balancing"}},
               "status": {"traffic": [{"revisionName": service + "-old", "percent": 100}]}}
    revision = {"metadata": {"name": service + "-old"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"containers": [{"image": IMAGE + "a" * 64,
                    "env": [{"name": "TR_RELEASE", "value": A},
                            {"name": "TR_PUBLIC_SOURCE_COMMIT", "value": B}]}]}}
    return current, revision


@pytest.mark.parametrize("damage", [None, "split", "ingress", "unready", "tag", "duplicate-source", "missing-source"])
def test_serving_reader_uses_exact_traffic_revision(monkeypatch, damage):
    current, revision = serving_documents()
    if damage == "split":
        current["status"]["traffic"][0]["percent"] = 50
    elif damage == "ingress":
        current["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
    elif damage == "unready":
        revision["status"]["conditions"][0]["status"] = "False"
    elif damage == "tag":
        revision["spec"]["containers"][0]["image"] = "image:latest"
    elif damage == "duplicate-source":
        revision["spec"]["containers"][0]["env"].append({"name": "TR_RELEASE", "value": B})
    elif damage == "missing-source":
        revision["spec"]["containers"][0]["env"].pop(0)
    calls = []

    def cloud(*args):
        calls.append(args)
        return current if args[1] == "services" else revision

    monkeypatch.setattr(docs, "cloud", cloud)
    monkeypatch.setattr(docs, "commit", lambda value: value)
    if damage:
        with pytest.raises(Refused):
            docs.serving(docs.REGIONS[0], docs.SERVICES[1])
    else:
        result = docs.serving(docs.REGIONS[0], docs.SERVICES[1])
        assert result["commit"] == B and result["api_commit"] == A
        assert calls[1][1:4] == ("revisions", "describe", "trusted-router-public-old")
        assert "spec" not in result


@pytest.mark.parametrize("damage", [None, "red-ci", "removed-main", "wrong-image", "wrong-run-source", "manual"])
@pytest.mark.parametrize("strict", [True, False])
def test_validation_requires_original_build_exact_ci_and_main(monkeypatch, damage, strict):
    env = {"GITHUB_REPOSITORY": "Lore-Hex/quill-router", "GITHUB_REF": "refs/heads/main",
           "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": B, "GITHUB_RUN_ID": "1",
           "PREPARED_TAG": RELEASE.tag, "PREPARED_IMAGE": RELEASE.image}
    if strict:
        env.update(GITHUB_EVENT_NAME="workflow_dispatch", TR_WEBSITE_ONLY="true")
    if damage == "manual":
        env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        env["TR_WEBSITE_ONLY"] = "false"
    if damage == "wrong-image":
        env["PREPARED_IMAGE"] = IMAGE + "a" * 64
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    class API:
        def run(self, run_id):
            assert run_id == 1
            return {}

        def artifact(self, run, kind):
            assert kind == "prepared"
            return {"release": asdict(RELEASE)}

        def ci(self, sha):
            assert sha == B
            if damage == "red-ci":
                raise Refused("red CI")

        def json(self, *_):
            return {"object": {"sha": B}}

        def relation(self, *_):
            return "diverged" if damage == "removed-main" else "identical"

    monkeypatch.setattr(docs, "GitHub", API)
    monkeypatch.setattr(docs, "git", lambda *_: A if damage == "wrong-run-source" else B)
    monkeypatch.setattr(docs, "proposed_docs_only", lambda: True)
    if damage:
        with pytest.raises(Refused):
            docs.validated_release()
    else:
        assert docs.validated_release() == RELEASE


def test_workflow_retains_ci_immutable_build_and_exclusive_ordering():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    jobs = workflow["jobs"]
    assert set(jobs["public-docs"]["needs"]) == {"gate-on-ci", "build-image", "scope"}
    assert "needs.scope.outputs.public_docs == 'true'" in jobs["public-docs"]["if"]
    assert "needs.public-docs.result == 'success'" in jobs["rollout"]["if"]
    assert "needs.public-docs.outputs.full_rollout == 'true'" in jobs["rollout"]["if"]
    assert "inputs.website_only != true" in jobs["rollout"]["if"]
    public = yaml.safe_load((ROOT / ".github/workflows/deploy-public-docs.yml").read_text())
    full = yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())
    assert public["concurrency"] == full["concurrency"]
    steps = public["jobs"]["deploy-public-docs"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert any(s.get("run") == "python3 -m scripts.deploy.public_docs publish" for s in steps)
    assert steps[-1]["with"]["name"] == "public-release-${{ github.run_attempt }}"
    full_steps = full["jobs"]["public-surface-companion"]["steps"]
    assert full_steps[0]["with"]["fetch-depth"] == 0
    observations = [s for s in full_steps if "scripts.deploy.public_rollout_report" in s.get("run", "")]
    assert len(observations) == 2 and all(s["continue-on-error"] and "id" not in s for s in observations)
    mutations = [s for s in full_steps if "bash scripts/deploy/public_surface.sh" in s.get("run", "")]
    assert len(mutations) == 1 and "if" not in mutations[0]
