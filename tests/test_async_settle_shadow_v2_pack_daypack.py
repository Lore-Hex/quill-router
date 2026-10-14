"""Acyclic file construction through the frozen parsers and real loader.

Small empty populations below test the file grammar only. They are explicitly
incomplete and are never substituted for a native producer in a stage trial.
"""
from __future__ import annotations

import copy

import pytest

from tests.test_async_settle_shadow_v2_pack import DAY, REV, START, peaks, review
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS
from trusted_router.async_settle_shadow_v2_gate_loader import load
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_pack_daypack import assemble_day
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import sha256
from trusted_router.detached_jws import canonical


def grammar_inputs(path):
    files = PackFiles(path)
    blank = files.obj('grammar/payload', {})
    source = files.put('grammar/source', b'original source')
    output = files.obj('grammar/output', {})
    empty = files.detail('grammar/empty', [], 'files')
    limits = files.obj('grammar/limits', dict(regions=[], fleet=peaks()))
    limit_review = review(files, limits, 'grammar/limits-review')
    payload_review = review(files, blank, 'grammar/payload-review')
    pre = dict(**TAGS, stage='pre', retained=dict(**TAGS, entry_point='pilot_enablement.fleet_budgets_pre',
        payload=blank, dependencies=empty, source=source), output=output,
        pre_envelope_sha256=None, approval=payload_review,
        v2_load=dict(**TAGS, interval=dict(start_us=START, end_us=START + 1),
            segments=empty, operations=empty, limits=limits, approval=limit_review))
    post = copy.deepcopy(pre)
    post.update(stage='post', pre_envelope_sha256=sha256(pre))
    post['retained']['entry_point'] = 'pilot_enablement.fleet_budgets'
    files.obj('proof.pre_budget_sha256', pre, a2.BUDGET_ENVELOPE)
    files.obj('proof.post_budget_sha256', post, a2.BUDGET_ENVELOPE)
    serving = files.obj('grammar/serving', [])
    deployment = files.obj('grammar/deployment', dict(serving=[]))
    inventory = dict(**TAGS, day=DAY, serving=serving, deployment=deployment, complete=False,
                     approval=review(files, deployment, 'grammar/deployment-review'))
    files.obj('manifest.independent_inventory_sha256', inventory, a2.INDEPENDENT_INVENTORY)
    measurements = files.obj('grammar/measurements', [])
    files.obj('proof.shutdown_inventory_sha256', dict(**TAGS, writers=empty, measurements=measurements,
        approval=review(files, measurements, 'grammar/shutdown-review'), complete=True), a2.SHUTDOWN_INVENTORY)
    configuration = files.obj('grammar/cohort', dict(workspace_count=1, eligible_only=True,
        supported_candidates=1, fallbacks=False, routes=['chat.completions', 'responses']))
    files.obj('proof.cohort_seed_policy_sha256', dict(**TAGS, workspace_fingerprints=['a' * 64],
        regional_seed_policy='each_traffic_region', required_regions=[], primary_region='region', maximum_publishers=1,
        configuration=configuration, approval=review(files, configuration, 'grammar/cohort-review')), a2.COHORT_SEED_POLICY)
    files.obj('proof.external_proofs_sha256', dict(**TAGS, requirements=[], pilot=None), a2.EXTERNAL_PROOFS)
    files.obj('proof.resolution_dag_sha256', dict(**TAGS, retained=dict(**TAGS,
        entry_point='shadow_report.collect_rollbacks', payload=blank, dependencies=empty, source=source),
        history=empty, approvals=[]), a2.RESOLUTION_DAG)
    for name in contract.FILE_PREIMAGES:
        files.put(name, b'original ' + name.encode())
    proof = dict(v=2, policy_version='shadow-v2', contract_revision='a1', source_shas=dict(router=REV, enclave='b' * 40),
        rosters_by_day={DAY: dict(router={}, enclave={})}, instance_regions_by_day={DAY: {}},
        attachments=[], explicit_keys=[], complete=False)
    manifest = dict(v=2, policy_version='shadow-v2', contract_revision='a1', day=DAY,
        router_writers={}, enclave_writers={}, revisions=dict(router=[], enclave=[]),
        first_evidence_at_us=None, disabled_flag_intervals=[], gaps=[], complete=False)
    preimages = {name: [] for name in (*contract.ENTRY_PREIMAGES, *contract.COVERAGE_PREIMAGES, *contract.CROSS_DAY_PREIMAGES)}
    return files, manifest, proof, files.originals(), preimages


def assemble(files, manifest, proof, artifacts, preimages, **kwargs):
    return assemble_day(files, manifest, proof, artifacts=artifacts, preimages=preimages,
                        expected_closed={'native_population': False}, horizon_ends=[START + 172800000000],
                        complete_through_us=START, **kwargs)


def test_daypack_frozen_validation_materialize_and_real_loader(tmp_path):
    outputs = []
    for name in ('one', 'two'):
        files, manifest, proof, artifacts, preimages = grammar_inputs(tmp_path / name)
        manifest, proof = assemble(files, manifest, proof, artifacts, preimages)
        assert not manifest['complete'] and not proof['complete']
        assert manifest['proof_manifest_sha256'] == roots.file_digest(a1.PROOF_MANIFEST.serialize(proof))
        artifacts = files.originals()
        contract.validate_preimages(manifest, proof, artifacts=artifacts, roots=preimages)
        assert a1.MANIFEST.parse(a1.MANIFEST.serialize(manifest)) == manifest
        assert a1.PROOF_MANIFEST.parse(artifacts['proof_manifest']) == proof
        graph = PayloadGraph(artifacts)
        graph.materialize({name: schema.parse(artifacts[name]) for name, schema in contract.OBJECT_PREIMAGES.items()})
        assert graph.issues == []
        transport = canonical(dict(policy_version='shadow-v2', contract_revision='a1', days=[DAY], completed_at_us=START,
            rows=[dict(kind=a1.KINDS[5], id=DAY + '/manifest-v2', raw=a1.MANIFEST.serialize(manifest).decode())],
            proof=artifacts['proof_manifest'].decode(), preimages=[dict(name=k, raw=v.decode()) for k, v in artifacts.items()]))
        loaded = load(transport)
        assert loaded.proof == artifacts['proof_manifest']
        assert any(i['gap'] == 'artifact_gap' for i in loaded.a2_streams['loading_issues'])
        outputs.append(artifacts)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('name', [*contract.FILE_PREIMAGES, *contract.OBJECT_PREIMAGES])
def test_each_named_daypack_preimage_required(tmp_path, name):
    files, manifest, proof, artifacts, preimages = grammar_inputs(tmp_path)
    del artifacts[name]
    with pytest.raises((ValueError, KeyError)):
        assemble(files, manifest, proof, artifacts, preimages)


def test_complete_cannot_hide_open_horizon(tmp_path):
    files, manifest, proof, artifacts, preimages = grammar_inputs(tmp_path)
    proof['complete'] = True
    inventory = a2.INDEPENDENT_INVENTORY.parse(artifacts['manifest.independent_inventory_sha256'])
    inventory['complete'] = True
    artifacts['manifest.independent_inventory_sha256'] = a2.INDEPENDENT_INVENTORY.serialize(inventory)
    output = PackFiles(tmp_path / 'assembled')
    with pytest.raises(ValueError):
        assemble_day(output, manifest, proof, artifacts=artifacts, preimages=preimages,
            expected_closed={'native_population': True}, horizon_ends=[START + 172800000000],
            complete_through_us=START)


def test_report_cannot_enter_its_own_upstream_proof(tmp_path):
    files, manifest, proof, artifacts, preimages = grammar_inputs(tmp_path)
    proof['attachments'] = [dict(sha256=roots.file_digest(b'future report'), bytes=len(b'future report'))]
    with pytest.raises(ValueError):
        assemble(files, manifest, proof, artifacts, preimages)


def test_post_links_canonical_pre_not_filename(tmp_path):
    files, manifest, proof, artifacts, preimages = grammar_inputs(tmp_path)
    post = a2.BUDGET_ENVELOPE.parse(artifacts['proof.post_budget_sha256'])
    post['pre_envelope_sha256'] = roots.file_digest(b'proof.pre_budget_sha256')
    artifacts['proof.post_budget_sha256'] = a2.BUDGET_ENVELOPE.serialize(post)
    with pytest.raises(ValueError):
        assemble(files, manifest, proof, artifacts, preimages)


@pytest.mark.parametrize('edge', ['manifest', 'downstream-report'])
def test_frozen_proof_attachment_walk_limitation(tmp_path, edge):
    """The frozen validator does not resolve proof attachments; block that claim.

    The manifest example deliberately leaves an old attachment digest after the
    forward reference changes. It is not a byte-consistent circular digest.
    """
    files, manifest, proof, artifacts, preimages = grammar_inputs(tmp_path)
    manifest, proof = assemble(files, manifest, proof, artifacts, preimages)
    artifacts = files.originals()
    raw = a1.MANIFEST.serialize(manifest) if edge == 'manifest' else b'{"report":"downstream"}'
    proof['attachments'] = [dict(sha256=roots.file_digest(raw), bytes=len(raw))]
    artifacts['proof_manifest'] = a1.PROOF_MANIFEST.serialize(proof)
    manifest['proof_manifest_sha256'] = roots.file_digest(artifacts['proof_manifest'])
    artifacts['back-reference'] = a1.MANIFEST.serialize(manifest) if edge == 'manifest' else raw
    # BASE LIMITATION: acceptance is recorded exactly, never presented as rejection.
    contract.validate_preimages(manifest, proof, artifacts=artifacts, roots=preimages)
