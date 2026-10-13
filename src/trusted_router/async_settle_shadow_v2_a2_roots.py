"""A2 exact-byte digests, sorted entry roots and flat detail chains."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as e
from trusted_router.async_settle_shadow_v2_schema import (
    INT64,
    Schema,
    day,
    identity,
    integer,
    name,
    require,
    revision,
    sequence,
    sha256,
)
from trusted_router.detached_jws import canonical

# The referring field selects a closed stream; schemas are never caller supplied.
ROOT_KEYS = {
    "counter_root": (day, identity), "closure_root": (day, identity),
    "receipts_root": (day, identity, sequence),
    "prerequisite_ledger_root": (e.ROLE, identity, revision, name, integer),
    "ledger_root": (e.ROLE, identity, revision, name, integer),
    "publisher_slot_trace_sha256": (day, identity, integer),
}
DAY_ROOT = Schema({"day": day, "root": e.digest, "count": integer}, 128)
STREAMS = {
    "failures": (e.GATE_FAILURE, e.FAILURE_PAGE, 255, 928),
    "writers": (e.WRITER_REPORT, e.WRITER_REPORT_PAGE, 16, 131072),
    "regions": (e.REGION_REPORT, e.REGION_REPORT_PAGE, 8, 131072),
    "input_inventory": (e.INPUT_INVENTORY_ENTRY, e.ATTACHMENT_PAGE, 64, INT64),
    "reset_history": (e.RESET_ENTRY, e.ATTACHMENT_PAGE, 64, INT64),
    "shutdown_writers": (e.SHUTDOWN_ENTRY, e.ATTACHMENT_PAGE, 64, 131072),
    "segments": (e.LOAD_SEGMENT, e.ATTACHMENT_PAGE, 64, INT64),
    "operations": (e.EXPORT_OPERATION, e.ATTACHMENT_PAGE, 64, INT64),
    "files": (e.FILE_REF, e.ATTACHMENT_PAGE, 128, INT64),
    **dict.fromkeys(ROOT_KEYS, (e.ROOT_ENTRY, e.ROOT_ENTRY_PAGE, 128, INT64)),
    **dict.fromkeys((s + "_root" for s in e.a1.COVERAGE_STREAMS), (DAY_ROOT, None, 32, 32)),
}


def file_digest(raw: bytes) -> str:
    require(type(raw) is bytes, "artifact_gap")
    return hashlib.sha256(raw).hexdigest()


def _root_entry(key: str, bodies: Iterable[bytes]) -> dict[str, Any]:
    """The sole missing/present/conflict construction; coalesce exact bytes only."""
    e.decode_key(key)
    require(isinstance(bodies, Iterable) and not isinstance(bodies, (bytes, str, dict)))
    candidates: dict[str, bytes] = {}
    for body in bodies:
        digest = file_digest(body)
        require(digest not in candidates or candidates[digest] == body, "artifact_gap")
        candidates[digest] = body
    size = sum(len(b) for b in candidates.values())
    integer(size)
    result = {"key": key, "sha256": None if not candidates else next(iter(candidates)) if len(candidates) == 1
        else sha256(sorted(candidates)), "bytes": size,
        "state": "missing" if not candidates else "present" if len(candidates) == 1 else "conflict"}
    e.ROOT_ENTRY(result)
    return result


def _stream(stream: Any) -> tuple[Schema, Schema | None, int, int]:
    require(type(stream) is str and stream in STREAMS)
    return STREAMS[stream]


def _key(entry: dict[str, Any], stream: str) -> Any:
    if stream in ROOT_KEYS:
        parts = e.decode_key(entry["key"])
        checks = ROOT_KEYS[stream]
        require(len(parts) == len(checks))
        values: list[Any] = []
        for part, check in zip(parts, checks, strict=True):
            value: Any = part
            if check is integer:
                require(part.isascii() and part.isdecimal() and str(int(part)) == part)
                value = int(part)
            check(value)
            values.append(value)
        return tuple(values)
    if stream == "failures":
        return canonical([entry["gap"], entry["subject"]])
    if stream == "writers":
        return entry["day"], entry["role"], entry["boot"]
    if stream == "regions":
        return entry["region"]
    if stream == "input_inventory":
        return entry["first_key"]
    if stream == "reset_history":
        return (-1 if entry["at_us"] is None else entry["at_us"], entry["revision"], entry["reason"], entry["support"]["root"])
    if stream == "shutdown_writers":
        return entry["day"], entry["boot"]
    if stream == "segments":
        return entry["day"], entry["boot"], entry["start_us"]
    if stream == "operations":
        return entry["pass"], entry["at_us"], entry["operation"], entry["object"]["path"]
    if stream == "files":
        return entry["path"]
    return entry["day"]


def entry_root(entries: Iterable[dict[str, Any]], *, stream: str) -> str:
    """R: canonical sorted entry array, independent of page partitioning."""
    schema, _, _, bound = _stream(stream)
    require(isinstance(entries, Iterable) and not isinstance(entries, (str, bytes, dict)))
    keyed = []
    for entry in entries:
        schema(entry)
        keyed.append((_key(entry, stream), entry))
        require(len(keyed) <= bound)
    keyed.sort(key=lambda pair: pair[0])
    require(all(a[0] < b[0] for a, b in zip(keyed, keyed[1:], strict=False)))
    if stream == "input_inventory":
        require(all(a[1]["last_key"] < b[1]["first_key"] for a, b in zip(keyed, keyed[1:], strict=False)))
    if stream == "segments":
        nonempty = [pair for pair in keyed if pair[1]["start_us"] < pair[1]["end_us"]]
        require(all(a[0][:2] != b[0][:2] or a[1]["end_us"] <= b[1]["start_us"] for a, b in zip(nonempty, nonempty[1:], strict=False)))
    if stream == "failures":
        counts: dict[str, int] = {}
        for _, entry in keyed:
            counts[entry["gap"]] = counts.get(entry["gap"], 0) + 1
            require(counts[entry["gap"]] <= 16)
    # Stream the canonical array without a second full serialized allocation.
    digest = hashlib.sha256(b"[")
    for i, (_, entry) in enumerate(keyed):
        if i:
            digest.update(b",")
        digest.update(canonical(entry))
    digest.update(b"]")
    return digest.hexdigest()


def validate_detail(detail: dict[str, Any], pages: Iterable[dict[str, Any]], *, stream: str) -> None:
    e.DETAIL(detail)
    _, page_schema, limit, bound = _stream(stream)
    if page_schema is None:
        raise ValueError("evidence_schema_gap")
    require(isinstance(pages, Iterable) and not isinstance(pages, (str, bytes, dict)))
    previous = None
    last_key: Any = None
    entries: list[dict[str, Any]] = []
    page_count = 0
    last_count = limit
    for page in pages:
        require(last_count == limit)
        page_schema(page)
        page_count += 1
        require(int(page["page_seq"]) == page_count and page["previous_sha256"] == previous)
        for entry in page["entries"]:
            STREAMS[stream][0](entry)
            key = _key(entry, stream)
            require(last_key is None or last_key < key)
            last_key = key
            entries.append(entry)
            require(len(entries) <= bound)
        last_count = len(page["entries"])
        require(last_count <= limit)
        previous = sha256(page)
        require(stream != "failures" or page_count <= 4)
    require(detail["count"] == len(entries) and detail["pages"] == page_count and detail["tail_sha256"] == previous)
    require(detail["root"] == entry_root(entries, stream=stream))
