"""The edge policy v1 body: the live control-plane facts the fast path's edge
gates on before it answers an authorize itself (E2 P2, dark).

docs/design/routing-snapshot-v1.md, "The edge policy". The routing snapshot
carries the catalog, compiled hourly; this carries the settings and the signed
Stage D image policy a running control plane decides with *now*, refreshed
every 15 s and valid for 60 s. The publisher
(services/edge_policy_publisher.py) builds and signs it; the manifest format
is in routing_snapshot_signing.py (``EDGE_POLICY_TYP``). Imported only when
``edge_policy_publish_enabled`` is on.

Contract: docs/design/fast-admission-e2-decide.md section 4. Byte contract:
the body is ``canonical_edge_policy_bytes``: ASCII JSON, keys sorted at every
level, separators ``,`` and ``:``, no trailing newline, no floats, ``null``
only for ``monitor_lookup_hash``. Sets are sorted, unique arrays; ``configured_regions`` keeps
the configured order (``regions.choose_region`` falls back to its first entry).
Fields, all required:

- ``v``: 1; ``kind``: ``"tr-edge-policy"``.
- ``plane``: ``{"cloud", "region"}``, the publishing control plane's own.
- ``issued_at`` (unix seconds, the publisher's clock), ``refresh_at`` =
  ``issued_at + 15``, ``not_after`` = ``issued_at + 60``.
- ``accepted_image_digests``: ``sha256:<64 hex>`` digests, the verified Stage D
  policy's set plus the audited break-glass override, exactly the set authorize
  checks a boot's image digest against, read right after a synchronous
  resolver refresh.
- ``stage_d``: ``{"eligibility_enabled", "heartbeat_enabled",
  "require_heartbeat_declaration", "pilot_workspaces"}``, the settings
  ``_stage_d_eligibility_reason`` reads (the routing snapshot's ``stage_d``
  shape).
- ``records_durable``: the settle outbox is on and the store builds activity
  rows in the settle commit (the pilot's ``records_durable``).
- ``fastpath_pilot_enabled``, ``fastpath_pilot_workspaces``,
  ``fastpath_pilot_regions``: the pilot's gate.
- ``gateway_authorize_max_in_flight_per_key``: positive integer.
- ``monitor_lookup_hash``: the synthetic monitor key's lookup hash (64 hex),
  or ``null`` when no monitor key is configured.
- ``async_settle_shadow_policy`` (``""`` when unset),
  ``async_settle_shadow_workspaces``.
- ``configured_regions``, ``primary_region`` (one of them),
  ``multi_region_enabled``.
- ``internal_models``: the model ids only an internal probe key may request
  (``internal_model_access.internal_model_ids``: the monitor model and every
  ``internal_only`` catalog model), read from the catalog when signing.
- ``async_settle``: ``{"admission_enabled", "edge_decide", "ticket_authority",
  "typed_records"}``, four booleans the publishing process reads when it signs
  (docs/design/fast-admission-e2-g15-async.md section 3.4): async-settle
  admission (``async_settle_admission_enabled``), whether the edge may decide
  async-v1 authorizes (``fastpath_edge_decide_async``), whether this process's
  async-settle runtime can issue tickets (a signer, an authority epoch of at
  least 1 and a journal region), and whether its store writes typed records
  (the ``typed`` the async-v1 authorize branch passes). Go (G15a) reads it as
  optional; Python always publishes it. Inside it one member is optional:
  ``admission_workspaces``, the sorted async-settle pilot set
  (``async_settle_pilot_workspace_ids``), present only while admission is on
  and the set is non-empty (an empty set admits every workspace, which is the
  member absent). Python answers a workspace the set leaves out as with
  admission off, so the edge composes that answer and passes only the
  workspaces it names (docs/design/fast-admission-e2-g15-async.md section
  4.1). ``null`` and an empty array are refused.

One field is optional:

- ``fastpath_pilot_key_windows``: ``"alert"`` or ``"enforce"``, the setting of
  that name (docs/design/fast-admission-e2-key-windows.md section 5): which
  keys with spend windows the pilot covers. Absent when the setting is
  ``off``, so the default body is byte-identical to one from before the field
  existed, and Go reads absent as ``off``. ``null`` and ``"off"`` are refused,
  so each mode has exactly one encoding.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import Field, model_validator

from trusted_router.billing_snapshot import Digest, Frozen, Identity, UInt, canonical_bytes

KIND: Final = "tr-edge-policy"
# Fixed by the contract: Go checks the same offsets.
REFRESH_SECONDS: Final = 15
LIFETIME_SECONDS: Final = 60
# Settings.parse_pilot_workspaces bounds the async-settle pilot set; Go
# (routingsnapshot.maxAdmissionWorkspaces) bounds admission_workspaces alike.
MAX_ADMISSION_WORKSPACES: Final = 32

Flag = Annotated[bool, Field(strict=True)]
PositiveInt = Annotated[int, Field(strict=True, ge=1, le=(1 << 63) - 1)]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
# The workspace-set settings' own validation (config.parse_fastpath_pilot).
WorkspaceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_./:@+\-]{1,64}$")]
Region = Annotated[str, Field(pattern=r"^[a-z0-9\-]{1,32}$")]
PolicyName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:\-]{0,64}$")]


def _sorted_unique(values: tuple[str, ...], name: str) -> None:
    if list(values) != sorted(set(values)):
        raise ValueError(f"{name} must be sorted and unique")


class EdgePlane(Frozen):
    cloud: Identity
    region: Identity


class EdgeStageD(Frozen):
    eligibility_enabled: Flag
    heartbeat_enabled: Flag
    require_heartbeat_declaration: Flag
    pilot_workspaces: tuple[WorkspaceId, ...]

    @model_validator(mode="after")
    def sorted_pilots(self) -> Self:
        _sorted_unique(self.pilot_workspaces, "pilot_workspaces")
        return self


class EdgeAsyncSettle(Frozen):
    admission_enabled: Flag
    edge_decide: Flag
    ticket_authority: Flag
    typed_records: Flag
    # Optional: the pilot set admission covers, present only with admission
    # on and a non-empty set (an empty set admits every workspace, which is
    # the member absent). The canonical bytes omit it when absent.
    admission_workspaces: tuple[WorkspaceId, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def admission_set(self) -> Self:
        workspaces = self.admission_workspaces
        if workspaces is None:
            if "admission_workspaces" in self.model_fields_set:
                raise ValueError("admission_workspaces is absent for every workspace, never null")
            return self
        if not self.admission_enabled:
            raise ValueError("admission_workspaces requires admission_enabled")
        if not 1 <= len(workspaces) <= MAX_ADMISSION_WORKSPACES:
            raise ValueError("admission_workspaces must name 1 to 32 workspaces")
        _sorted_unique(workspaces, "admission_workspaces")
        return self


class EdgePolicyV1(Frozen):
    v: Literal[1]
    kind: Literal["tr-edge-policy"]
    plane: EdgePlane
    issued_at: UInt
    refresh_at: UInt
    not_after: UInt
    accepted_image_digests: tuple[ImageDigest, ...]
    stage_d: EdgeStageD
    records_durable: Flag
    fastpath_pilot_enabled: Flag
    fastpath_pilot_workspaces: tuple[WorkspaceId, ...]
    fastpath_pilot_regions: tuple[Region, ...]
    gateway_authorize_max_in_flight_per_key: PositiveInt
    monitor_lookup_hash: Digest | None
    async_settle_shadow_policy: PolicyName
    async_settle_shadow_workspaces: tuple[WorkspaceId, ...]
    configured_regions: tuple[Identity, ...] = Field(min_length=1)
    primary_region: Identity
    multi_region_enabled: Flag
    internal_models: tuple[Identity, ...]
    async_settle: EdgeAsyncSettle
    # Optional: absent is "off", and the canonical bytes omit it then.
    fastpath_pilot_key_windows: Literal["alert", "enforce"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.fastpath_pilot_key_windows is None and "fastpath_pilot_key_windows" in self.model_fields_set:
            raise ValueError("fastpath_pilot_key_windows is absent when off, never null")
        if (self.refresh_at != self.issued_at + REFRESH_SECONDS
                or self.not_after != self.issued_at + LIFETIME_SECONDS):
            raise ValueError("edge policy window")
        _sorted_unique(self.accepted_image_digests, "accepted_image_digests")
        _sorted_unique(self.fastpath_pilot_workspaces, "fastpath_pilot_workspaces")
        _sorted_unique(self.fastpath_pilot_regions, "fastpath_pilot_regions")
        _sorted_unique(self.async_settle_shadow_workspaces, "async_settle_shadow_workspaces")
        _sorted_unique(self.internal_models, "internal_models")
        regions = self.configured_regions
        if len(set(regions)) != len(regions) or self.primary_region not in regions:
            raise ValueError("configured_regions must be unique and include primary_region")
        return self


def canonical_edge_policy_bytes(policy: EdgePolicyV1) -> bytes:
    """ASCII JSON, lexical keys, compact separators, no trailing newline."""
    return canonical_bytes(policy)


def parse_edge_policy(raw: bytes | str) -> EdgePolicyV1:
    """Strict: duplicate keys, unknown fields, floats and coercions are refused."""
    return EdgePolicyV1.model_validate_json(raw)
