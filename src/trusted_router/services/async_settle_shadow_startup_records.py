"""Closed startup projections. Inputs belong to local record producers."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import sys
import threading
from collections.abc import Callable
from queue import Queue
from typing import Any

INVENTORY: dict[str, Any] = {'fields': {'a3_startup_correlation': 'kind instance launch boot role revision region process_start_us '
                                      'recorded_at_us',
            'a3_boot_check': 'kind boot role revision region boot_at_us criterion completed_at_us checks '
                             'config_sha256 inputs',
            'a3_traffic_ready': 'kind boot at_us',
            'a3_opted_attempt': 'kind boot at_us result receipt',
            'a3_shutdown_span': 'kind boot instance launch revision region day interval_end_us complete '
                                'clock clock_origin spans outcome'},
 'role_fields': {'boot_transport': {'router': 'dependencies clock_basis',
                                    'enclave': 'dependencies clock_basis'},
                 'observer_freshness': {'router': 'health_basis install_elapsed_us contender installed_at_ns '
                                                  'clock_basis',
                                        'enclave': 'router_path configured_router_region '
                                                   'local_transport_ready echo_ready clock_basis'},
                 'marker_keys': {'router': 'effective_pins source_revision authority_epoch '
                                           'public_descriptors policy_sha256 fixture_sha256',
                                 'enclave': 'effective_pins source_revision authority_epoch '
                                            'public_descriptors policy_sha256 fixture_sha256 attested_image '
                                            'attested_revision'}},
 'limits': {'a3_startup_correlation': 2048,
            'a3_boot_check': 16384,
            'a3_traffic_ready': 1024,
            'a3_opted_attempt': 2048,
            'a3_shutdown_span': 4096},
 'checks': {'transport': ['actual_hops', 'both_routes_variants', 'compared_outcome'],
            'size': ['event_snapshot', 'headers_fragments', 'native_memory', 'transactions'],
            'regional_load': ['cpu_reads', 'envelope', 'publisher_export_shutdown'],
            'boot_transport': ['local_dependencies', 'regional_hops'],
            'observer_freshness': ['role_warm_path', 'slot_freshness'],
            'marker_keys': ['deployed_pins', 'public_epoch', 'separation_negatives']},
 'sources': {'transport': ['ci', 'regional_trial'],
             'size': ['ci', 'sizing_trial'],
             'regional_load': ['budget_trial'],
             'boot_transport': ['regional_trial'],
             'observer_freshness': [],
             'marker_keys': ['ci']},
 'attempt_results': ['accepted', 'failed', 'rejected', 'timeout'],
 'channels': {'startup': ['a3_startup_correlation'],
              'readiness': ['a3_boot_check', 'a3_traffic_ready', 'a3_opted_attempt'],
              'shutdown': ['a3_shutdown_span']},
 'emitted_coordinate_fields': 'kind sha256 bytes',
 'emitted_coordinate_types': {'kind': 'field_kind',
                              'sha256': 'lower_hex_64',
                              'bytes': 'positive_integer_to_max_bytes'},
 'emitted_coordinates': {'clock_basis.original': {'kind': 'boot_clock',
                                                  'max_bytes': 262144,
                                                  'producer': 'boot_clock_recorder'},
                         'health_basis': {'kind': 'health_basis',
                                          'max_bytes': 6144,
                                          'producer': 'health_reader'},
                         'claim': {'kind': 'health_claim', 'max_bytes': 1024, 'producer': 'health_publisher'},
                         'publication': {'kind': 'health', 'max_bytes': 4096, 'producer': 'health_publisher'},
                         'public_descriptors': {'kind': 'public_descriptors',
                                                'max_bytes': 16384,
                                                'producer': 'release_loader'},
                         'attested_image': {'kind': 'image_record',
                                            'max_bytes': 16384,
                                            'producer': 'image_record_loader'},
                         'receipt.original': {'kind': 'receipt_page',
                                              'max_bytes': 65536,
                                              'producer': 'receipt_page_writer'},
                         'originals.original_close': {'kind': 'closure',
                                                      'max_bytes': 4096,
                                                      'producer': 'daily_close_writer'},
                         'originals.close_result': {'kind': 'close_result',
                                                    'max_bytes': 4096,
                                                    'producer': 'close_result_writer'}},
 'launch_descriptor_fields': 'instance launch role revision region',
 'launch_coordinate_fields': {'cloud_run_instance': 'project location service revision_name instance_id',
                              'launcher_start': 'project zone instance_id vm_boot_id start_insert_id',
                              'supervisor_start': 'project location instance_id supervisor_boot_id '
                                                  'child_start_sequence'}}

FIELDS = INVENTORY["fields"]
LIMITS = INVENTORY["limits"]
CHECKS = INVENTORY["checks"]
SOURCES = INVENTORY["sources"]
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z", re.ASCII)
DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
REVISION = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
MAX_INT = (1 << 63) - 1
PIN_KEYS = {"TR_ASYNC_SETTLE_ENABLED", "TR_ASYNC_SETTLE_PROTECTION", "TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED"}


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("startup_record")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def closed(value: Any, fields: str | set[str]) -> None:
    require(type(value) is dict and set(value) == (set(fields.split()) if isinstance(fields, str) else fields))


def identifier(value: Any) -> None:
    require(type(value) is str and IDENTIFIER.fullmatch(value) is not None)


def integer(value: Any, minimum: int = 0, maximum: int = MAX_INT) -> None:
    require(type(value) is int and minimum <= value <= maximum)


def digest(value: Any) -> None:
    require(type(value) is str and DIGEST.fullmatch(value) is not None)


def revision(value: Any) -> None:
    require(type(value) is str and REVISION.fullmatch(value) is not None)


def day(value: Any) -> None:
    require(type(value) is str and dt.date.fromisoformat(value).isoformat() == value)


def coordinate(field: str, retained: bytes) -> dict[str, Any]:
    spec = INVENTORY["emitted_coordinates"][field]
    require(type(retained) is bytes and 1 <= len(retained) <= spec["max_bytes"])
    return dict(kind=spec["kind"], sha256=hashlib.sha256(retained).hexdigest(), bytes=len(retained))


def check_coordinate(field: str, value: Any) -> None:
    closed(value, "kind sha256 bytes")
    spec = INVENTORY["emitted_coordinates"][field]
    require(value["kind"] == spec["kind"])
    digest(value["sha256"])
    integer(value["bytes"], 1, spec["max_bytes"])


def clock_basis(value: Any, record: dict[str, Any]) -> None:
    closed(value, "clock boot boot_at_us original")
    integer(value["boot_at_us"])
    require(value["clock"] == "utc_ns" and value["boot"] == record["boot"] and value["boot_at_us"] == record["boot_at_us"])
    check_coordinate("clock_basis.original", value["original"])


def validate(value: dict[str, Any]) -> bytes:
    kind = value["kind"]
    require(kind in FIELDS)
    fields = set(FIELDS[kind].split())
    if kind == "a3_boot_check":
        criterion = value["criterion"]
        require(criterion in CHECKS and value["role"] == "router")
        fields.update(INVENTORY["role_fields"].get(criterion, {}).get("router", "").split())
        if criterion == "observer_freshness" and value.get("contender") is True:
            fields.update("claim publication publication_elapsed_us claim_at_ns published_at_ns".split())
    closed(value, fields)
    for name in ("boot", "instance", "launch", "region"):
        if name in value:
            identifier(value[name])
    if "role" in value:
        require(value["role"] == "router")
    for name in ("revision", "source_revision"):
        if name in value:
            revision(value[name])
    for name in ("process_start_us", "recorded_at_us", "boot_at_us", "completed_at_us", "at_us", "interval_end_us"):
        if name in value:
            integer(value[name])
    if kind == "a3_startup_correlation":
        require(value["process_start_us"] <= value["recorded_at_us"])
    elif kind == "a3_boot_check":
        validate_check(value)
    elif kind == "a3_opted_attempt":
        require(value["result"] in INVENTORY["attempt_results"])
        receipt = value["receipt"]
        closed(receipt, "day boot sequence original")
        require(receipt["boot"] == value["boot"])
        day(receipt["day"])
        integer(receipt["sequence"], 1)
        check_coordinate("receipt.original", receipt["original"])
    elif kind == "a3_shutdown_span":
        validate_span(value)
    raw = canonical(value)
    require(len(raw) <= LIMITS[kind])
    return raw


def validate_check(v: dict[str, Any]) -> None:
    criterion = v["criterion"]
    require(v["boot_at_us"] <= v["completed_at_us"])
    digest(v["config_sha256"])
    closed(v["checks"], set(CHECKS[criterion]))
    require(all(type(x) is str and x in {"pass", "fail"} for x in v["checks"].values()))
    closed(v["inputs"], set(SOURCES[criterion]))
    for artifact in v["inputs"].values():
        identifier(artifact)
    if criterion in {"boot_transport", "observer_freshness"}:
        clock_basis(v["clock_basis"], v)
    if criterion == "boot_transport":
        dependencies = v["dependencies"]
        require(type(dependencies) is list and 1 <= len(dependencies) <= 16)
        seen = set()
        for d in dependencies:
            closed(d, "boot region config_sha256 operation resource attempts marker_matches elapsed_us deadline_us observed_at_us")
            require(d["boot"] == v["boot"] and d["region"] == v["region"] and d["config_sha256"] == v["config_sha256"])
            require(d["operation"] in {"transport", "read"} and d["marker_matches"] is True)
            integer(d["attempts"], 1, 1)
            integer(d["deadline_us"], 0, 10_000_000)
            integer(d["elapsed_us"], 0, d["deadline_us"])
            integer(d["observed_at_us"], v["boot_at_us"], v["completed_at_us"] - d["elapsed_us"])
            identifier(d["resource"])
            key = (d["operation"], d["resource"], d["region"])
            require(key not in seen)
            seen.add(key)
        require({d["operation"] for d in dependencies} == {"transport", "read"})
    elif criterion == "observer_freshness":
        check_coordinate("health_basis", v["health_basis"])
        require(type(v["contender"]) is bool)
        integer(v["install_elapsed_us"], 0, 250_000)
        integer(v["installed_at_ns"], v["boot_at_us"]*1000, v["completed_at_us"]*1000)
        if v["contender"]:
            for field in ("claim", "publication"):
                check_coordinate(field, v[field])
            integer(v["publication_elapsed_us"], 0, 500_000)
            integer(v["claim_at_ns"], v["boot_at_us"]*1000, v["published_at_ns"])
            integer(v["published_at_ns"], v["claim_at_ns"], v["installed_at_ns"])
    elif criterion == "marker_keys":
        closed(v["effective_pins"], PIN_KEYS)
        require(all(type(x) is str and x in {"false", "true", "on", "off"} for x in v["effective_pins"].values()))
        integer(v["authority_epoch"], 1)
        require(v["source_revision"] == v["revision"])
        check_coordinate("public_descriptors", v["public_descriptors"])
        digest(v["policy_sha256"])
        digest(v["fixture_sha256"])


def validate_span(v: dict[str, Any]) -> None:
    day(v["day"])
    require(type(v["complete"]) is bool and v["clock"] == "monotonic_us")
    require(v["outcome"] in {"graceful", "hard_kill"})
    require(v["outcome"] != "hard_kill" or not v["complete"])
    closed(v["clock_origin"], "boot monotonic_at_boot_us")
    require(v["clock_origin"]["boot"] == v["boot"])
    origin = v["clock_origin"]["monotonic_at_boot_us"]
    integer(origin)
    closed(v["spans"], "preparation export_wait final_pass close result")
    for pair in v["spans"].values():
        require(type(pair) is list and len(pair) == 2)
        integer(pair[0], origin)
        integer(pair[1], pair[0])
    p, w, f, c, r = (v["spans"][k] for k in ("preparation", "export_wait", "final_pass", "close", "result"))
    require(p[1] == w[0] and w[1] == f[0])
    require(f[0] <= c[0] <= c[1] <= r[0] <= r[1] <= f[1])


def stdout_writer() -> Callable[[bytes], None]:
    # This base tree's package handler is stderr with a text prefix. Keep the
    # exact JSON line on stdout using the ordinary logging StreamHandler.
    logger = logging.getLogger("trusted_router.startup_records")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    pending: Queue[bytes] = Queue(64)

    def drain() -> None:
        while True:
            raw = pending.get()
            try:
                logger.info("%s", raw.decode("utf-8"))
            finally:
                pending.task_done()

    threading.Thread(target=drain, name="startup-stdout", daemon=True).start()
    return pending.put_nowait


class Emitter:
    def __init__(self, write: Callable[[bytes], None] | None = None) -> None:
        self.write = write
        self.dropped = 0
        self.emitted = 0

    def emit(self, value: dict[str, Any]) -> bytes | None:
        try:
            raw = validate(value)
            if self.write is None:
                self.write = stdout_writer()
            self.write(raw)
            self.emitted += 1
            return raw
        except Exception:
            self.dropped += 1
            return None
