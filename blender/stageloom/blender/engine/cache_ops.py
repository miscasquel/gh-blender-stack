"""Hot-cache entries and the disk-backed response store."""

import sys

engine = sys.modules[__package__]


def _store_for(record, stage):
    key = (record.stack_id, stage.key)
    root = engine.prefs.data_root()
    store = engine._stores.get(key)
    limit = engine.prefs.cache_size_bytes()
    if store is None or store.base_dir != root:
        store = engine.frame_cache.ResponseStore(root, record.stack_id, stage.key, limit)
        engine._stores[key] = store
    else:
        store.size_limit = limit
    return store


def _definition(stage):
    return engine.props.definition_fingerprint(stage)


def _bump_generation(obj, record, stage, reason=""):
    """A reset: old frames describe the wrong definition and are deleted."""
    engine._forget_prefix(obj, stage.key)
    stage.generation += 1
    engine._cache.pop((record.stack_id, stage.key), None)
    engine._cache_bytes.pop((record.stack_id, stage.key), None)
    engine._trace.append("%s: reset (%s)" % (stage.key, reason or "definition changed"))
    try:
        removed = engine._store_for(record, stage).prune(generation=stage.generation)
    except OSError as exc:
        removed = []
        engine.prefs.log("[stageloom] cache reset failed for %s: %r" % (stage.key, exc), flush=True)
    if removed:
        engine.prefs.log(
            "[stageloom] %s: cache reset (%s), %d frame(s) dropped"
            % (stage.key, reason or "definition changed", len(removed)),
            flush=True,
        )


def _frame_bytes(header, sections):
    """Encode one decoded frame for storage.

    The socket path normally hands over the exact bytes it received; this is the
    fallback for tests and any in-process caller that only has decoded sections.
    """
    entries = []
    for entry in header.get("sections") or []:
        name = entry.get("name")
        if name in sections:
            entries.append((name, entry.get("dtype"), sections[name]))
    return engine.codec.encode_frame(header, entries)


def _drop_hot(record, stage, rid):
    """Remove one hot entry and keep its running byte total exact."""
    key = (record.stack_id, stage.key)
    cache = engine._cache.get(key)
    if cache is None:
        return
    entry = cache.pop(rid, None)
    if entry is None:
        return
    total = max(0, engine._cache_bytes.get(key, 0) - len(entry[0]) - len(entry[1]))
    if cache:
        engine._cache_bytes[key] = total
    else:
        engine._cache.pop(key, None)
        engine._cache_bytes.pop(key, None)


def _store_frame(record, stage, rid, header, sections, raw=None):
    """Persist a response as the raw protocol frame plus a metadata sidecar."""
    if header.get("status") != "ok":
        return
    if raw is None:
        try:
            raw = engine._frame_bytes(header, sections)
        except (engine.codec.CodecError, KeyError, TypeError) as exc:
            engine.prefs.log(
                "[stageloom] could not store a frame for %s: %r" % (stage.key, exc),
                flush=True,
            )
            return
    try:
        engine._store_for(record, stage).store(
            rid,
            raw,
            generation=stage.generation,
            definition=engine._definition(stage),
            out_hash=header.get("out_hash") or "",
        )
    except OSError as exc:
        engine.prefs.log("[stageloom] cache write failed for %s: %r" % (stage.key, exc), flush=True)


def _remember(record, stage, rid, header, sections, raw=None):
    """Put a response in the hot memory cache, bounded by the same byte budget
    as the disk store (FIFO over insertion order; the disk is the backing store,
    so an evicted frame just loads again).

    The entry holds the wire bytes, not the decoded Python tuples. The tuples
    are several times larger than the frame and were the reason the old
    wire-size accounting could exceed the bound by a wide margin. A replay
    decodes the bytes again; the apply path already dominates that cost.
    """
    if raw is None:
        try:
            raw = engine._frame_bytes(header, sections)
        except (engine.codec.CodecError, KeyError, TypeError) as exc:
            engine.prefs.log(
                "[stageloom] could not cache a frame for %s: %r" % (stage.key, exc),
                flush=True,
            )
            return
    key = (record.stack_id, stage.key)
    cache = engine._cache.setdefault(key, {})
    if key not in engine._cache_bytes:
        total = sum(len(entry[0]) + len(entry[1]) for entry in cache.values())
    else:
        total = engine._cache_bytes[key]
    if not cache:
        total = 0  # a direct test clear resets the accounting with it
    old = cache.get(rid)
    if old is not None:
        total -= len(old[0]) + len(old[1])
    entry = (raw, header.get("out_hash") or "")
    cache[rid] = entry
    total += len(entry[0]) + len(entry[1])
    limit = engine.prefs.cache_size_bytes()
    while len(cache) > 1 and total > limit:
        oldest = next(iter(cache))
        if oldest == rid:
            break
        dropped = cache.pop(oldest)
        total -= len(dropped[0]) + len(dropped[1])
    engine._cache_bytes[key] = total


def _cached_response(record, stage, rid):
    """A stored response for this rid: memory first, then disk. Entries that do
    not match the current generation or definition are dropped on the way."""
    memory = engine._cache.get((record.stack_id, stage.key))
    if memory is not None:
        hit = memory.get(rid)
        if hit is not None:
            try:
                header, sections = engine.codec.decode_frame(hit[0])
            except engine.codec.CodecError as exc:
                engine.prefs.log(
                    "[stageloom] dropping a corrupt hot frame for %s: %r"
                    % (stage.key, exc),
                    flush=True,
                )
                engine._drop_hot(record, stage, rid)
                return None
            if header.get("status") != "ok" or header.get("stage") != stage.key:
                engine._drop_hot(record, stage, rid)
                return None
            return header, sections
    try:
        store = engine._store_for(record, stage)
        entry = store.valid(rid, generation=stage.generation, definition=engine._definition(stage))
        if entry is None:
            return None
        raw = store.load(rid)
    except OSError as exc:
        engine.prefs.log("[stageloom] cache read failed for %s: %r" % (stage.key, exc), flush=True)
        return None
    if raw is None:
        return None
    try:
        header, sections = engine.codec.decode_frame(raw)
    except engine.codec.CodecError as exc:
        engine.prefs.log("[stageloom] dropping a corrupt cached frame for %s: %r" % (stage.key, exc), flush=True)
        store.remove(rid)
        return None
    if header.get("status") != "ok" or header.get("stage") != stage.key:
        store.remove(rid)
        return None
    engine._remember(record, stage, rid, header, sections, raw)
    return header, sections


def _forget_frame(record, stage, rid):
    """Drop one stored frame — hot cache and disk — so it cannot be replayed.

    `_handle_response` stores the payload before it applies it, exactly so a
    stale rid's answer is still cached. When the apply fails, that order means
    the frame is a trap: the next pass for the same rid finds it, applies it and
    raises again, forever. Removing it turns the retry into a real request,
    which is the only thing that can heal a transient apply fault.
    """
    engine._drop_hot(record, stage, rid)
    try:
        engine._store_for(record, stage).remove(rid)
    except OSError as exc:
        engine.prefs.log(
            "[stageloom] could not drop a failed frame for %s: %r" % (stage.key, exc),
            flush=True,
        )

