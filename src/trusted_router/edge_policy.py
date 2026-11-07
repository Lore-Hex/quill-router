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
  4.1). A second optional member, ``admission_excluded_workspaces``: the
  fast-path pilot workspaces outside ``async_settle_fastpath_overlap_workspaces``
  that admission would otherwise cover (all of them with no
  ``admission_workspaces``, else the ones it names), sorted, present only
  with admission on and at least one, and within ``admission_workspaces``
  when that is present: async settle takes no request the fast path takes
  (section 4.5). Go reads a workspace as covered when admission is on, the
  first member is absent or names it, and the second does not name it.
  ``null`` and an empty array are refused for both.

Five fields are optional:

- ``fastpath_pilot_key_windows``: ``"alert"`` or ``"enforce"``, the setting of
  that name (docs/design/fast-admission-e2-key-windows.md section 5): which
  keys with spend windows the pilot covers. Absent when the setting is
  ``off``, so the default body is byte-identical to one from before the field
  existed, and Go reads absent as ``off``. ``null`` and ``"off"`` are refused,
  so each mode has exactly one encoding.
- ``fastpath_edge_streams``: ``true`` when the settings
  ``fastpath_edge_streams``, ``fastpath_pilot_streams`` and
  ``reap_snapshot_booking_enabled`` are all on
  (docs/design/fast-admission-streams.md section 5.4): the edge may decide
  streams. Absent otherwise, so the default body is byte-identical to one
  from before the field existed, and Go reads absent as off; ``null`` and
  ``false`` are refused.
- ``fastpath_pilot_all`` and ``fastpath_pilot_exclude``
  (docs/design/fast-admission-widening.md sections 1 and 6.3): ``true`` and
  the sorted, unique workspaces the pilot leaves out (0 to 64), when the
  pilot covers every workspace but those instead of the explicit
  ``fastpath_pilot_workspaces``, which is then empty. Both absent
  otherwise, so the default body is byte-identical to one from before the
  fields existed, and Go reads absent as the explicit list; ``null`` and
  ``false`` are refused, and the exclude list is present exactly with
  ``fastpath_pilot_all``. Go reads them first (PR 3 of the widening); the
  publisher writes them from the settings ``fastpath_pilot_all`` and
  ``fastpath_pilot_exclude`` (PR 5), only while the first is on.
- ``fastpath_stream_ceiling``: ``true`` when the setting of that name is on
  (docs/design/fast-admission-streams.md section 12.4, PR 4): the edge
  admits a stream whose reservation is below its estimate with the estimate
  as the admission's Ceiling, where it otherwise passes as ``stream.hold``.
  Absent otherwise, so the default body is byte-identical to one from before
  the field existed, and Go reads absent as off; ``null`` and ``false`` are
  refused. Go reads it in the same release; the setting moves only once
  every node runs that release.
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
# fastpath_pilot_exclude's bound (Go: routingsnapshot.maxPilotExclude).
MAX_PILOT_EXCLUDE: Final = 64

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
    # Optional: the fast-path workspaces admission leaves out although
    # admission_workspaces (or its absence) would cover them, present only
    # with admission on and at least one (fast-admission-e2-g15-async.md
    # section 4.5). The canonical bytes omit it when absent.
    admission_excluded_workspaces: tuple[WorkspaceId, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def admission_set(self) -> Self:
        for name, workspaces in (("admission_workspaces", self.admission_workspaces),
                                 ("admission_excluded_workspaces", self.admission_excluded_workspaces)):
            if workspaces is None:
                if name in self.model_fields_set:
                    raise ValueError(f"{name} is absent when it names none, never null")
                continue
            if not self.admission_enabled:
                raise ValueError(f"{name} requires admission_enabled")
            if not 1 <= len(workspaces) <= MAX_ADMISSION_WORKSPACES:
                raise ValueError(f"{name} must name 1 to 32 workspaces")
            _sorted_unique(workspaces, name)
        # One encoding per answer: an excluded workspace is one the pilot set
        # would cover (every workspace when it is absent).
        excluded, covered = self.admission_excluded_workspaces, self.admission_workspaces
        if excluded is not None and covered is not None and not set(excluded) <= set(covered):
            raise ValueError("admission_excluded_workspaces must be within admission_workspaces")
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
    # Optional: absent is off, and the canonical bytes omit it then.
    fastpath_edge_streams: Flag | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    # Optional, together: absent is the explicit list, and the canonical
    # bytes omit both then.
    fastpath_pilot_all: Flag | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    fastpath_pilot_exclude: tuple[WorkspaceId, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    # Optional: absent is off, and the canonical bytes omit it then.
    fastpath_stream_ceiling: Flag | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.fastpath_pilot_key_windows is None and "fastpath_pilot_key_windows" in self.model_fields_set:
            raise ValueError("fastpath_pilot_key_windows is absent when off, never null")
        if self.fastpath_edge_streams is not True and "fastpath_edge_streams" in self.model_fields_set:
            raise ValueError("fastpath_edge_streams is absent when off, never null or false")
        if self.fastpath_stream_ceiling is not True and "fastpath_stream_ceiling" in self.model_fields_set:
            raise ValueError("fastpath_stream_ceiling is absent when off, never null or false")
        self._pilot_all_consistent()
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

    def _pilot_all_consistent(self) -> None:
        # One encoding per answer, as Go's validatePilotAll holds it.
        pilot_all, exclude = self.fastpath_pilot_all, self.fastpath_pilot_exclude
        if pilot_all is not True and "fastpath_pilot_all" in self.model_fields_set:
            raise ValueError("fastpath_pilot_all is absent when off, never null or false")
        if exclude is None and "fastpath_pilot_exclude" in self.model_fields_set:
            raise ValueError("fastpath_pilot_exclude is absent with the explicit list, never null")
        if pilot_all is None:
            if exclude is not None:
                raise ValueError("fastpath_pilot_exclude requires fastpath_pilot_all")
            return
        if exclude is None:
            raise ValueError("fastpath_pilot_all requires fastpath_pilot_exclude")
        if self.fastpath_pilot_workspaces:
            raise ValueError("fastpath_pilot_workspaces must be empty with fastpath_pilot_all")
        if len(exclude) > MAX_PILOT_EXCLUDE:
            raise ValueError(f"fastpath_pilot_exclude must name at most {MAX_PILOT_EXCLUDE} workspaces")
        _sorted_unique(exclude, "fastpath_pilot_exclude")


def canonical_edge_policy_bytes(policy: EdgePolicyV1) -> bytes:
    """ASCII JSON, lexical keys, compact separators, no trailing newline."""
    return canonical_bytes(policy)


def parse_edge_policy(raw: bytes | str) -> EdgePolicyV1:
    """Strict: duplicate keys, unknown fields, floats and coercions are refused."""
    return EdgePolicyV1.model_validate_json(raw)
