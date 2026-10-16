"""Read the parent descriptor once, or resolve the reviewed instance mapping."""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import select
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from trusted_router import launch_descriptor_state
from trusted_router.services.async_settle_shadow_startup_records import (
    canonical,
    closed,
    identifier,
    require,
    revision,
)


@dataclass(frozen=True)
class Descriptor:
    value: dict[str, Any]
    mode: str
    original: bytes
    source_coordinate: bytes | None = None


def parse(raw: bytes, mode: str = "fd3") -> Descriptor:
    require(0 < len(raw) <= 2048)
    value = json.loads(raw)
    closed(value, "instance launch role revision region")
    require(canonical(value) == raw and value["role"] == "router")
    for name in ("instance", "launch", "region"):
        identifier(value[name])
    revision(value["revision"])
    return Descriptor(value, mode, raw)


def read_fd(fd: int | None = None, timeout: float = .25) -> Descriptor | None:
    if fd is not None:
        return _read_fd(fd, timeout)
    try:
        owned = launch_descriptor_state.take()
        value = _read_fd(owned, timeout)
        require(owned is None or value is not None)
        return value
    finally:
        launch_descriptor_state.close()


def _read_fd(fd: int | None, timeout: float) -> Descriptor | None:
    """None means absent; any present but unfinished input raises ValueError."""
    if fd is None:
        return None
    require(fd != -1)
    try:
        os.fstat(fd)
    except OSError as error:
        if error.errno == errno.EBADF:
            return None
        raise ValueError("launch_descriptor") from error
    require(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
    deadline = time.monotonic() + timeout
    raw = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0 and bool(select.select([fd], [], [], remaining)[0]))
        part = os.read(fd, 2049-len(raw))
        if not part:
            return parse(bytes(raw))
        raw.extend(part)
        require(len(raw) <= 2048)


def metadata_instance() -> str:
    from trusted_router.storage_gcp_shadow_observation import startup_instance

    value = startup_instance()
    identifier(value)
    return value


def acquire(mapping: dict[str, Any], *, fd: int | None = None,
            environ: Mapping[str, str] | None = None,
            instance_reader: Callable[[], str] = metadata_instance) -> Descriptor:
    inherited = read_fd(fd)
    if inherited is not None:
        return inherited
    return instance_descriptor(mapping, environ=environ, instance_reader=instance_reader)


def instance_descriptor(mapping: dict[str, Any], *, environ: Mapping[str, str] | None = None,
                        instance_reader: Callable[[], str] = metadata_instance) -> Descriptor:
    env = os.environ if environ is None else environ
    require(mapping["mode"] == "instance_as_launch")
    platform_revision = env.get("K_REVISION", "")
    identifier(platform_revision)
    released = mapping["revisions"][platform_revision]
    revision(released)
    coordinate = dict(project=mapping["project"], location=mapping["region"],
                      service=mapping["service"], revision_name=platform_revision,
                      instance_id=instance_reader())
    for value in coordinate.values():
        identifier(value)
    raw = canonical(dict(source="cloud_run_instance", coordinate=coordinate))
    digest = hashlib.sha256(raw).hexdigest()
    descriptor = parse(canonical(dict(instance="instance:"+digest, launch="launch:"+digest,
        role="router", revision=released, region=mapping["region"])), "instance_as_launch")
    return Descriptor(descriptor.value, descriptor.mode, descriptor.original, raw)
