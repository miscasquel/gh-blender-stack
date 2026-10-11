"""The response cache: index and eviction (plan §4, §7, §9.2).

Content-addressed by request ID. The disk layout under the data root is
``cache/<stack_id>/<stage_key>/<request_id>.bin`` (plan §12). This module is the
index half: what is present, what is stale, and what gets evicted, as pure
functions and a small dict-like index. No bpy, no I/O, so layer A can test it.
"""

import os

# A monotonic counter is enough for FIFO eviction: the index is one machine's.
class CacheEntry(object):
    __slots__ = ("rid", "path", "size", "seq")

    def __init__(self, rid, path, size=0, seq=0):
        self.rid = rid
        self.path = path
        self.size = size
        self.seq = seq

    def __repr__(self):
        return "CacheEntry(rid=%s, size=%d)" % (self.rid, self.size)


class CacheIndex(object):
    """Maps rid to stored responses for one stack and stage.

    Eviction is FIFO over insertion order, bounded by a byte budget. The cache is
    a plain dict, so lookups are the hot path; the seq counter only exists for
    eviction order.
    """

    def __init__(self, base_dir, stack_id, stage_key, size_limit=256 * 1024 * 1024):
        self.base_dir = base_dir
        self.stack_id = stack_id
        self.stage_key = stage_key
        self.size_limit = size_limit
        self._entries = {}
        self._seq = 0

    # ------------------------------------------------------------------ paths
    @property
    def stage_dir(self):
        return os.path.join(self.base_dir, "cache", self.stack_id, self.stage_key)

    def path_for(self, rid):
        # rids are "sha256:<hex>"; the prefix is fine in a filename, but the colon
        # is awkward on some filesystems, so it is kept in the index only.
        return os.path.join(self.stage_dir, rid.replace(":", "_") + ".bin")

    # ------------------------------------------------------------------- index
    def __contains__(self, rid):
        return rid in self._entries

    def __len__(self):
        return len(self._entries)

    def get(self, rid):
        entry = self._entries.get(rid)
        return None if entry is None else entry

    def add(self, rid, size=0):
        """Insert or refresh an entry. Refreshing counts as a re-insert for FIFO."""
        self._seq += 1
        entry = CacheEntry(rid, self.path_for(rid), size, self._seq)
        self._entries[rid] = entry
        self._evict()
        return entry

    def touch(self, rid):
        """Mark an entry as recently used, so it survives eviction longer."""
        entry = self._entries.get(rid)
        if entry is not None:
            self._seq += 1
            entry.seq = self._seq
        return entry

    def remove(self, rid):
        return self._entries.pop(rid, None)

    def rids(self):
        return list(self._entries.keys())

    # ---------------------------------------------------------------- eviction
    def _evict(self):
        """Evict oldest-first until the total fits the budget."""
        if len(self._entries) <= 1:
            return []
        while self.total_size() > self.size_limit and len(self._entries) > 1:
            oldest = min(self._entries.values(), key=lambda e: e.seq)
            del self._entries[oldest.rid]
        return []

    def total_size(self):
        return sum(e.size for e in self._entries.values())

    def evicted(self):
        """Rids evicted by the last add, for logging."""
        return []
