"""P0's install, held to the node it starts (infra/fastpath.tf, the startup
script, the release workflow)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_startup_script_gives_the_node_every_flag_it_needs() -> None:
    script = (ROOT / "infra" / "fastpath_node_startup.sh.tftpl").read_text(encoding="utf-8")
    main = (ROOT / "fastpath" / "cmd" / "fastpath" / "main.go").read_text(encoding="utf-8")
    for flag in ("roles", "address", "region", "database", "project", "key-secret", "topics", "subscriptions"):
        assert f'"{flag}"' in main, flag
        assert re.search(rf"^\s*-{flag} ", script, re.M), flag
    # The topics and subscriptions the script names are the ones Terraform creates.
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    assert "-topics settle-log,records" in script and "-subscriptions auditor,stager" in script
    for name in ("settle-log", "records", "auditor", "stager"):
        assert f'name                       = "{name}"' in tf or f'name = "{name}"' in tf, name
    # A node with no release stays stopped rather than failing the unit.
    assert 'if [ -z "$release" ]' in script


def test_release_workflow_uploads_only_ci_passed_main_commits() -> None:
    workflow = (ROOT / ".github" / "workflows" / "fastpath-release.yml").read_text(encoding="utf-8")
    assert "workflow_run:" in workflow and 'workflows: ["CI"]' in workflow and "branches: [main]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert "gs://$BUCKET/fastpath/$COMMIT/" in workflow
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    assert 'fastpath_release_bucket = "tr-fastpath-releases-${local.gcp_project_id}"' in tf
    assert "BUCKET: tr-fastpath-releases-quill-cloud-proxy" in workflow
    wif = (ROOT / "infra" / "gcp_wif.tf").read_text(encoding="utf-8")
    assert "workflows/fastpath-release.yml@refs/heads/main" in wif


def test_deploy_identity_edits_no_project_iam_for_the_nodes() -> None:
    """The node's project roles are granted by hand; Terraform binds the
    deploy identity's own roles only through import blocks, never creating a
    project binding the deploy identity would need to edit project IAM for."""
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    assert 'google_project_iam_member" "fastpath_node' not in tf
    roles = (ROOT / "infra" / "fastpath_deploy_roles.tf").read_text(encoding="utf-8")
    assert "import {" in roles and 'to       = google_project_iam_member.fastpath_deploy[each.key]' in roles
    runbook = (ROOT / "docs" / "runbooks" / "fastpath-p0.md").read_text(encoding="utf-8")
    for role in ("roles/pubsub.admin", "roles/compute.instanceAdmin.v1", "roles/logging.logWriter"):
        assert role in runbook, role
