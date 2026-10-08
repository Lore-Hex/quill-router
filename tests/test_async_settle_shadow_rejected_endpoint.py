# ruff: noqa: F811 - imported pytest fixture
from __future__ import annotations

import copy
import json

from tests.test_async_settle_handler import env, prepare, row_for  # noqa: F401
from tests.test_async_settle_oracle import endpoint_from_candidate
from tests.test_settle_outbox_drain import _client, _typed_credit, _typed_key
from trusted_router.catalog_data import Model
from trusted_router.routes.internal import gateway
from trusted_router.services.async_settle_shadow import Runtime


def test_rejected_replay_endpoint_is_not_persisted(env, monkeypatch):
    go_amount = 2
    import hashlib
    from dataclasses import asdict

    from tests.test_async_settle_shadow import NOW, signer, wire
    from trusted_router.async_settle_shadow_compare import Booking
    from trusted_router.async_settle_shadow_evidence import validate_sample
    from trusted_router.detached_jws import canonical
    from trusted_router.storage_models import generation_id_for_authorization

    body, auth, key = prepare(env)
    store, db, rt, cfg = env
    repair = json.loads(row_for(env, body).settle_body)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg.release = "a" * 40
    cfg._async_settle_shadow_workspace_ids = frozenset({"ws-v1"})
    # Persist the real server nonce as authorize does (prepare's PR-C helper
    # only attaches its nonce to the returned detached authorization).
    payload = json.loads(db.gateway_authorizations[auth.id]["payload"])
    payload["invocation_nonce"] = auth.invocation_nonce
    db.gateway_authorizations[auth.id]["payload"] = json.dumps(payload)
    endpoints = {
        c["endpoint_id"]: endpoint_from_candidate(c) for c in body["billing_snapshot"]["candidates"]
    }
    for endpoint in endpoints.values():
        monkeypatch.setitem(
            gateway.MODELS,
            endpoint.model_id,
            Model(
                id=endpoint.model_id,
                name="shadow",
                provider=endpoint.provider,
                context_length=1000000,
                prepaid_available=True,
            ),
        )
    monkeypatch.setattr(gateway, "endpoint_for_id", endpoints.get)
    from tests.test_async_settle_shadow import FIXTURE

    envelope = copy.deepcopy(FIXTURE)
    envelope["billing_snapshot"] = body["billing_snapshot"]
    envelope["terminal"] = copy.deepcopy(body["terminal"])
    envelope["terminal"]["charge_micro"] = go_amount
    from trusted_router.async_settle_shadow_projection import project
    from trusted_router.billing_snapshot import canonical_hash

    snapshot = project(tuple(endpoints.values()), auth.created_at)
    envelope["billing_snapshot"] = snapshot.model_dump(mode="json")
    body["terminal"]["snapshot_hash"] = canonical_hash(snapshot)
    envelope["terminal"]["snapshot_hash"] = canonical_hash(snapshot)
    claims = dict(
        authorization_id=auth.id,
        generation_id=generation_id_for_authorization(auth.id),
        workspace_id=auth.workspace_id,
        key_id=auth.key_hash,
        invocation_nonce=auth.invocation_nonce,
        reservation_id=auth.credit_reservation_id,
        billing_authority="local",
        journal_region=rt.region,
        epoch=rt.epoch,
        route_type="chat.completions",
        streamed=False,
        settle_origin="typed",
        snapshot_version=1,
        snapshot_hash=body["terminal"]["snapshot_hash"],
        async_eligible=False,
        iss="router-fixture",
        aud="router-shadow",
        iat=NOW,
        exp=NOW + 172800,
    )
    envelope["billing_shadow_binding"] = signer().sign(claims, NOW)
    envelope["payload_hash"] = hashlib.sha256(canonical(envelope["terminal"])).hexdigest()
    del envelope["billing_snapshot"]
    from tests.test_async_settle_shadow_accounting import Database
    from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore

    evidence_store = EvidenceStore(Database())
    evidence = []
    persistence = []
    evidence_commit_counts = []
    booking_observations = []
    sample_commit_counts = []
    commits_before = db.commits

    class DetachedStore:
        def booking(self, identity, deadline):
            booking_observations.append((db.commits, db.gateway_authorizations[identity]["settled"]))
            record = db.gateway_authorizations[identity]
            return Booking(
                record["finalized_cost_microdollars"], record["finalization_outcome"], True
            )

        def reserve(self, day, deadline):
            evidence_commit_counts.append(db.commits)
            return 100

        def insert_sample(self, identity, row, deadline):
            sample_commit_counts.append(db.commits)
            validate_sample(row, identity)
            if not evidence:
                evidence.append(row)
                raise RuntimeError("lost first post-money evidence write")
            evidence.append(row)
            outcome = evidence_store.insert_sample(identity, row, deadline)
            persistence.append(outcome)
            return outcome

        def flush(self, *args):
            pass

    shadow = Runtime(cfg, rt, DetachedStore())
    shadow.signer = signer()
    client = _client(cfg)
    client.app.state.async_settle_shadow = shadow
    before_auth = asdict(auth)
    try:
        first = client.post(
            "/v1/internal/gateway/settle",
            json=repair,
            headers={"X-TR-Settlement-Shadow": wire(envelope)[0]},
        )
        repair["selected_endpoint"] = "PRIVATE_REPLAY_SENTINEL"
        result = client.post(
            "/v1/internal/gateway/settle",
            json=repair,
            headers={"X-TR-Settlement-Shadow": wire(envelope)[0]},
        )
    except RuntimeError as error:
        result = error
    finally:
        shadow.executor.shutdown()
    assert not isinstance(result, RuntimeError), "shadow changed the money response"
    assert all(count > commits_before and settled for count, settled in booking_observations)
    assert all(count > commits_before for count in sample_commit_counts)
    assert all(count > commits_before for count in evidence_commit_counts), (
        "evidence preceded money outcome"
    )
    assert result.status_code == 200 and result.json()["data"]["cost_microdollars"] == 2
    assert _typed_credit(db, "ws-v1")["total_usage"] == _typed_key(db, key.hash)["usage"] == 2
    assert db.reservations[auth.credit_reservation_id]["actual_micro"] == 2
    assert auth.workspace_id == before_auth["workspace_id"]
    assert first.status_code == 200 and first.json()["data"]["cost_microdollars"] == 2
    shadow.executor.shutdown()
    print(
        "replay evidence:", [(r["classification"], r["endpoint_id"]) for r in evidence], flush=True
    )
    assert len(evidence) == 2
    assert evidence[-1]["classification"] == "identity"
    from trusted_router.async_settle_shadow_evidence import SAMPLE

    persisted = [
        json.loads(v) for (kind, _), v in evidence_store.database.rows.items() if kind == SAMPLE
    ]
    print(
        "durable evidence:",
        [(r["classification"], r["endpoint_id"]) for r in persisted],
        flush=True,
    )
    assert len(persisted) == 1

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    rejected = {repair["selected_endpoint"]}
    assert rejected.isdisjoint(strings(persisted))
    assert persisted[0]["endpoint_id"] is None, (
        "rejected client endpoint is persisted in content-free evidence"
    )


def test_sample_uses_server_identities_even_for_rejected_context():
    from dataclasses import replace

    from tests.test_async_settle_shadow import NOW, context
    from trusted_router.async_settle_shadow_compare import Comparison
    from trusted_router.async_settle_shadow_evidence import sample

    rejected = {'PRIVATE_ENDPOINT', 'PRIVATE_MODEL', 'PRIVATE_ROUTE', 'PRIVATE_AUTH'}
    ctx = context()
    ctx.body.selected_endpoint = 'PRIVATE_ENDPOINT'
    ctx.body.route_type = 'PRIVATE_ROUTE'
    ctx.body.authorization_id = 'PRIVATE_AUTH'
    ctx = replace(ctx, selected_endpoint='PRIVATE_ENDPOINT')
    comparison = Comparison(model_id='PRIVATE_MODEL', classification='identity', reasons={'identity'})
    row = sample(ctx, comparison, observed_us=NOW*1000000, router_us=1,
                 comparator_us=1, booking_us=None, instance='server', revision='a'*40)
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
    assert rejected.isdisjoint(strings(row))
    assert row['authorization_id'] == ctx.authorization.id
    assert row['model_id'] == ctx.authorization.model_id
    assert row['endpoint_id'] is None
