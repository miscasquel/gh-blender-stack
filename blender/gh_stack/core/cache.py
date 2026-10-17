"""The response cache: index, disk store, and eviction (plan §4, §7, §9.2).

Content-addressed by request ID. The disk layout under the data root is
``cache/<stack_id>/<stage_key>/<request_id>.bin`` (plan §12), with a small JSON
sidecar next to each frame holding the metadata that decides whether a stored
frame still describes the current definition: the stage's generation counter and
the definition fingerprint (protocol version + schema signature, computed by the
Blender layer).

:class:`CacheIndex` is the pure in-memory index: what is present, what is stale,
and what gets evicted, with no I/O, so layer A can test it anywhere.
:class:`ResponseStore` adds the disk half: scan, load, store, prune, and file
deletion on eviction. The engine keeps its own decoded-memory dict as the hot
path; this store is the backing store and the cross-session cache.
"""

import heapq
import json
import os

DEFAULT_SIZE_LIMIT = 256 * 1024 * 1024


class CacheEntry(object):
    __slots__ = ("rid", "path", "size", "seq", "generation", "definition", "out_hash")

    def __init__(self, rid, path, size=0, seq=0, generation=0, definition="", out_hash=""):
        self.rid = rid
        self.path = path
        self.size = size
        self.seq = seq
        self.generation = generation
        self.definition = definition
        self.out_hash = out_hash

    def __repr__(self):
        return "CacheEntry(rid=%s, size=%d, gen=%d)" % (self.rid, self.size, self.generation)


class CacheIndex(object):
    """Maps rid to stored responses for one stack and stage.

    Eviction is FIFO over insertion order, bounded by a byte budget. The cache is
    a plain dict, so lookups are the hot path; a lazily-pruned heap over
    ``(seq, rid)`` keeps eviction O(log n) per insert (the scan-min-per-removal
    version was quadratic), and the running byte total keeps ``total_size``
    O(1).
    """

    def __init__(self, base_dir, stack_id, stage_key, size_limit=DEFAULT_SIZE_LIMIT):
        self.base_dir = base_dir
        self.stack_id = stack_id
        self.stage_key = stage_key
        self.size_limit = size_limit
        self._entries = {}
        self._seq = 0
        self._total = 0
        self._order = []  # (seq, rid); refreshed or removed pairs go stale
        self._evicted = []

    # ------------------------------------------------------------------ paths
    @property
    def stage_dir(self):
        return os.path.join(self.base_dir, "cache", self.stack_id, self.stage_key)

    def path_for(self, rid):
        # rids are "sha256:<hex>"; the prefix is fine in a filename, but the colon
        # is awkward on some filesystems, so it is kept in the index only.
        return os.path.join(self.stage_dir, rid.replace(":", "_") + ".bin")

    def meta_path_for(self, rid):
        """The JSON sidecar holding generation, definition fingerprint and out hash."""
        return os.path.join(self.stage_dir, rid.replace(":", "_") + ".json")

    # ------------------------------------------------------------------- index
    def __contains__(self, rid):
        return rid in self._entries

    def __len__(self):
        return len(self._entries)

    def get(self, rid):
        entry = self._entries.get(rid)
        return None if entry is None else entry

    def add(self, rid, size=0, generation=0, definition="", out_hash=""):
        """Insert or refresh an entry. Refreshing counts as a re-insert for FIFO."""
        self._seq += 1
        existing = self._entries.get(rid)
        if existing is not None:
            self._total -= existing.size
        entry = CacheEntry(rid, self.path_for(rid), size, self._seq, generation, definition, out_hash)
        self._entries[rid] = entry
        self._total += size
        heapq.heappush(self._order, (self._seq, rid))
        self._evict()
        return entry

    def touch(self, rid):
        """Mark an entry as recently used, so it survives eviction longer."""
        entry = self._entries.get(rid)
        if entry is not None:
            self._seq += 1
            entry.seq = self._seq
            heapq.heappush(self._order, (self._seq, rid))
        return entry

    def remove(self, rid):
        entry = self._entries.pop(rid, None)
        if entry is not None:
            self._total -= entry.size
        return entry

    def rids(self):
        return list(self._entries.keys())

    # ---------------------------------------------------------------- eviction
    def _evict(self):
        """Evict oldest-first until the total fits the budget. Returns the entries
        dropped, so a disk store can delete their files."""
        dropped = []
        while len(self._entries) > 1 and self._total > self.size_limit:
            seq, rid = heapq.heappop(self._order)
            entry = self._entries.get(rid)
            if entry is None or entry.seq != seq:
                continue  # a refreshed or removed entry's old pair
            del self._entries[rid]
            self._total -= entry.size
            dropped.append(entry)
        self._evicted = dropped
        return dropped

    def total_size(self):
        return self._total

    def evicted(self):
        """Entries dropped by the last add, for logging."""
        return list(self._evicted)


class ResponseStore(object):
    """Disk-backed responses for one stack and stage, over a :class:`CacheIndex`.

    One frame is two files: ``<rid>.bin`` (the raw protocol frame as received)
    and ``<rid>.json`` (the metadata that decides validity). A frame without its
    sidecar is not usable and is removed. Writes go through a temporary file and
    a rename, so a crash never leaves a half-written frame in place.
    """

    def __init__(self, base_dir, stack_id, stage_key, size_limit=DEFAULT_SIZE_LIMIT):
        self.index = CacheIndex(base_dir, stack_id, stage_key, size_limit)
        self._scanned = False

    # ----------------------------------------------------------------- scanning
    def scan(self):
        """Load the index from disk (once). Orphan frame files and stale temp
        files are removed here, so a failed write cannot poison the store."""
        if self._scanned:
            return self
        self._scanned = True
        stage_dir = self.index.stage_dir
        if not os.path.isdir(stage_dir):
            return self
        found = []
        for name in os.listdir(stage_dir):
            path = os.path.join(stage_dir, name)
            if name.endswith(".tmp"):
                _unlink(path)
                continue
            if not name.endswith(".bin"):
                continue
            rid = _rid_from_filename(name)
            meta = _read_meta(self.index.meta_path_for(rid))
            if meta is None:
                _unlink(path)
                continue
            try:
                size = os.path.getsize(path)
                order = os.path.getmtime(path)
            except OSError:
                continue
            found.append((order, rid, size, meta))
        found.sort(key=lambda item: item[0])
        for _, rid, size, meta in found:
            self.index.add(
                rid,
                size=size,
                generation=int(meta.get("generation") or 0),
                definition=meta.get("definition") or "",
                out_hash=meta.get("out_hash") or "",
            )
        return self

    # -------------------------------------------------------------------- io
    def load(self, rid):
        """The raw frame bytes for a rid, or None. Touches the entry."""
        self.scan()
        entry = self.index.get(rid)
        if entry is None:
            return None
        try:
            with open(entry.path, "rb") as handle:
                raw = handle.read()
        except OSError:
            self.remove(rid)
            return None
        self.index.touch(rid)
        return raw

    def store(self, rid, raw, generation=0, definition="", out_hash=""):
        """Write one frame and its sidecar, then refresh the index and evict."""
        self.scan()
        os.makedirs(self.index.stage_dir, exist_ok=True)
        _atomic_write(self.index.path_for(rid), bytes(raw))
        meta = {
            "generation": int(generation),
            "definition": definition or "",
            "out_hash": out_hash or "",
            "size": len(raw),
        }
        _atomic_write(self.index.meta_path_for(rid), json.dumps(meta, sort_keys=True).encode("utf-8"))
        self.index.add(rid, size=len(raw), generation=generation, definition=definition, out_hash=out_hash)
        for gone in self.index.evicted():
            self._delete_files(gone.rid)
        return self.index.get(rid)

    def valid(self, rid, generation=None, definition=None):
        """The entry for a rid if it matches the given generation and
        definition. A stale entry is deleted on the spot, so it can never
        replay; a None answer means "not usable", absent or old alike."""
        self.scan()
        entry = self.index.get(rid)
        if entry is None:
            return None
        if generation is not None and entry.generation != generation:
            self.remove(rid)
            return None
        if definition is not None and entry.definition != definition:
            self.remove(rid)
            return None
        return entry

    def remove(self, rid):
        entry = self.index.remove(rid)
        self._delete_files(rid)
        return entry

    def prune(self, generation=None, definition=None):
        """Delete every entry that fails the given checks. Returns the rids removed."""
        self.scan()
        removed = []
        for rid in self.index.rids():
            entry = self.index.get(rid)
            if generation is not None and entry.generation != generation:
                removed.append(rid)
            elif definition is not None and entry.definition != definition:
                removed.append(rid)
            else:
                continue
            self.index.remove(rid)
            self._delete_files(rid)
        return removed

    def _delete_files(self, rid):
        _unlink(self.index.path_for(rid))
        _unlink(self.index.meta_path_for(rid))

    def rids(self):
        self.scan()
        return self.index.rids()

    def __len__(self):
        return len(self.index)


# ------------------------------------------------------------------- file helpers


def _rid_from_filename(name):
    # "sha256_abc….bin" -> "sha256:abc…"; the colon is the only one in a rid.
    return name[:-4].replace("_", ":", 1)


def _read_meta(path):
    try:
        with open(path, "rb") as handle:
            meta = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _atomic_write(path, raw):
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(raw)
    os.replace(tmp, path)


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass
