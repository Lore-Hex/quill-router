"""Keep large released file originals in bounded parts until the day call."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import Schema, require


@dataclass
class DayFiles(PackFiles):
    large: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put(self, path: str, raw: bytes, schema: Schema | None = None) -> dict[str, Any]:
        if path not in RELEASE_FILES or len(raw) <= 262144:
            return super().put(path, raw, schema)
        import hashlib
        parts = [super(DayFiles, self).put(f'released/{path}/{i:05d}', raw[at:at + 262144])
                 for i, at in enumerate(range(0, len(raw), 262144))]
        descriptor = dict(parts=parts, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        require(path not in self.large or self.large[path] == descriptor, 'artifact_gap')
        self.large[path] = descriptor
        self.obj('released/' + path + '/original.json', descriptor)
        return descriptor

    def original(self, descriptor: dict[str, Any]) -> bytes:
        if 'parts' not in descriptor:
            return self.read(descriptor)
        import hashlib
        require(bool(descriptor['parts']), 'artifact_gap')
        raw = b''.join(self.read(ref) for ref in descriptor['parts'])
        require(len(raw) == descriptor['bytes'] and hashlib.sha256(raw).hexdigest() == descriptor['sha256'],
                'artifact_gap')
        return raw

    def originals(self) -> dict[str, bytes]:
        return {**super().originals(), **{path: self.original(ref) for path, ref in self.large.items()}}


@dataclass
class ScopedOutputs(PackFiles):
    """Write new graph records under a scope; read original paths unchanged."""
    prefix: str = ''

    def put(self, path: str, raw: bytes, schema: Schema | None = None) -> dict[str, Any]:
        # Retained captures are content-addressed lookup records in the existing loader.
        return super().put(path if path.startswith('loading/retained-output/') else self.prefix + path, raw, schema)


def release_dependency(path: str) -> bool:
    """Fixed checkout originals and their storage parts do not enter input leaves."""
    return path in RELEASE_FILES or any(path.startswith('released/' + name + '/') for name in RELEASE_FILES)
