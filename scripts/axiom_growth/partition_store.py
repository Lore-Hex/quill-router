"""Copy-on-write growth checkpoint. No source credentials, delivery, or serving-store access."""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import io
import json
import re
from collections import OrderedDict, defaultdict

SCHEMA = 1
PAGE_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 8
MAX_OPS = 2048
MAX_IO_BYTES = 96 * 1024 * 1024
PACK_BYTES = 1024 * 1024
CACHE_BYTES = 12 * PACK_BYTES
KINDS = {'fact', 'usage', 'projection', 'dependency'}
ID = re.compile('[0-9a-f]{64}')


def encode(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    if len(raw) > PAGE_BYTES:
        raise ValueError('partition page exceeds raw budget')
    return gzip.compress(raw, compresslevel=1, mtime=0)


def decode(blob):
    if len(blob) > PAGE_BYTES + 65536:
        raise ValueError('partition page exceeds wire budget')
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        raw = stream.read(PAGE_BYTES + 1)
    if len(raw) > PAGE_BYTES:
        raise ValueError('partition page exceeds raw budget')
    return json.loads(raw)


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def empty():
    return {'schema': SCHEMA, 'days': {}, 'watermarks': {}}


def slot_for(day, kind, key):
    # Directory partitions already consume the first two hex digits. Using
    # the first digit again would funnel an entire directory into one page.
    return kind + ':' + key[2 if day.startswith('index-') else 0]


class Transaction:
    """Objects are immutable. Only a successful root CAS grants visibility.

    backend: head() -> (generation, bytes or None), get(digest), put(digest, bytes),
    swap(expected_generation, bytes). A transaction that raised must be discarded.
    """

    def __init__(self, backend):
        self.backend = backend
        self.version, blob = backend.head()
        self.head = decode(blob) if blob is not None else empty()
        if self.head['schema'] != SCHEMA:
            raise ValueError('unknown partition schema')
        self.original = copy.deepcopy(self.head)
        self.ops = 1
        self.transferred = len(blob or b'')
        self.dirty_days = {}
        self.failed = False
        self.pending = bytearray()
        self.refs = []
        self.cache = OrderedDict()
        self.cache_size = 0

    def charge(self, size=0, ops=1):
        self.ops += ops
        self.transferred += size
        if self.ops > MAX_OPS or self.transferred > MAX_IO_BYTES:
            raise ValueError('partition transaction exceeds IO budget')

    def blob(self, ref):
        if not ID.fullmatch(ref):
            raise ValueError('invalid partition reference')
        if ref in self.cache:
            self.cache.move_to_end(ref)
            return self.cache[ref]
        self.charge()
        blob = self.backend.get(ref)
        self.charge(len(blob), 0)
        if digest(blob) != ref:
            raise ValueError('partition content digest mismatch')
        if len(blob) <= CACHE_BYTES:
            while self.cache_size + len(blob) > CACHE_BYTES:
                _, old = self.cache.popitem(last=False)
                self.cache_size -= len(old)
            self.cache[ref] = blob
            self.cache_size += len(blob)
        return blob

    def read(self, ref):
        if isinstance(ref, str):
            return decode(self.blob(ref))
        pack, start, length, checksum = ref
        data = self.pending if pack is None else self.blob(pack)
        if start < 0 or length <= 0 or start + length > len(data):
            raise ValueError('invalid packed page range')
        piece = data[start:start+length]
        if digest(piece) != checksum:
            raise ValueError('packed page digest mismatch')
        return decode(piece)

    def flush_pack(self):
        """Resolve all pending page references before serializing their parent."""
        if not self.refs:
            return
        blob = bytes(self.pending)
        key = digest(blob)
        self.charge(len(blob))
        self.backend.put(key, blob)
        for ref in self.refs:
            ref[0] = key
        self.refs.clear()
        self.pending.clear()

    def put(self, value):
        blob = encode(value)
        if len(self.pending) + len(blob) > PACK_BYTES:
            self.flush_pack()
        ref = [None, len(self.pending), len(blob), digest(blob)]
        self.pending.extend(blob)
        self.refs.append(ref)
        return ref

    def day(self, day):
        if not re.fullmatch('index-[0-9a-f]{2}', day) and dt.date.fromisoformat(day).isoformat() != day:
            raise ValueError('invalid partition day')
        if day in self.dirty_days:
            return self.dirty_days[day]
        ref = self.head['days'].get(day)
        return self.read(ref) if ref else {}

    def fold(self, chain):
        if len(chain) > MAX_DEPTH:
            raise ValueError('partition depth exceeded')
        values = {}
        for ref in chain:
            for key, value in self.read(ref).items():
                if value is None:
                    values.pop(key, None)
                else:
                    values[key] = value
        return values

    def apply(self, day, kind, changes):
        """Patch stable IDs. None deletes a row. Only compact the touched key ranges."""
        if self.failed:
            raise ValueError('transaction already closed')
        self.failed = True
        if kind not in KINDS:
            raise ValueError('unknown partition kind')
        groups = defaultdict(dict)
        for key, value in changes.items():
            if not ID.fullmatch(key):
                raise ValueError('invalid partition key')
            groups[slot_for(day, kind, key)][key] = value
        pages = self.day(day)
        for slot, patch in groups.items():
            refs = pages.get(slot, [])
            if len(refs) == MAX_DEPTH:
                compact = self.fold(refs)
                for key, value in patch.items():
                    if value is None:
                        compact.pop(key, None)
                    else:
                        compact[key] = value
                pages[slot] = [self.put(compact)] if compact else []
            else:
                pages[slot] = [*refs, self.put(patch)]
        self.dirty_days[day] = pages
        self.failed = False

    def remove_day(self, day):
        if self.failed:
            raise ValueError('transaction already closed')
        self.dirty_days.pop(day, None)
        self.head['days'].pop(day, None)

    def stage_delivery(self, batches):
        """Only committed refs can be delivered. Keep a failed acknowledgement replayable."""
        if self.failed or self.head.get('outbox'):
            raise ValueError('pending or closed transaction')
        self.failed = True
        self.head['outbox'] = [self.put(batch) for batch in batches]
        self.failed = False

    def drain(self, send):
        # Never send from a candidate transaction, even if its uploads succeeded.
        if self.failed or self.dirty_days or self.head != self.original:
            raise ValueError('delivery requires a fresh committed root')
        refs = self.head.get('outbox', [])
        if not refs:
            return 0
        self.failed = True
        for ref in refs:
            send(self.read(ref))
        self.head['outbox'] = []
        body = encode(self.head)
        self.charge(len(body))
        self.backend.swap(self.version, body)
        return len(refs)

    def commit(self, watermarks):
        if self.failed:
            raise ValueError('transaction already closed')
        self.failed = True
        if self.original.get('outbox'):
            raise ValueError('drain committed delivery before advancing')
        for key, stamp in watermarks.items():
            new = dt.datetime.fromisoformat(stamp)
            old = self.head['watermarks'].get(key)
            if new.tzinfo is None or (old and new < dt.datetime.fromisoformat(old)):
                raise ValueError('invalid or regressing partition watermark')
        self.flush_pack()
        for day, pages in self.dirty_days.items():
            self.head['days'][day] = self.put(pages)
        self.flush_pack()
        self.head['watermarks'].update(watermarks)
        body = encode(self.head)
        self.charge(len(body))
        return self.backend.swap(self.version, body)
