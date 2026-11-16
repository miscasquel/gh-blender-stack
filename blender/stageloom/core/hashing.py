"""Protocol v2 hashing: geometry hashes, request IDs, output hashes.

All three are SHA-256, prefixed ``sha256:`` and hex-encoded, per protocol §6.
This module is pure Python: the hash is over bytes the caller already has
(``section_bytes`` in :mod:`codec`), so no numpy is needed here.
"""

import hashlib

B_PREFIX = b"sha256:"

# The order the protocol fixes for the geometry hash (protocol §5 table order).
# Sections not present are skipped, in this order.
HASH_SECTION_ORDER = (
    "mesh.verts",
    "mesh.face_counts",
    "mesh.face_indices",
    "mesh.colors",
    "mesh.crease_edges",
    "mesh.crease_values",
    "subd.verts",
    "subd.face_counts",
    "subd.face_indices",
    "subd.crease_edges",
    "subd.crease_values",
    "crv.points",
    "crv.counts",
    "crv.types",
    "crv.orders",
    "crv.knots_mode",
    "crv.cyclic",
    "pts.positions",
    "pts.radii",
    "pts.colors",
    "inst.ref_def",
    "inst.transforms",
)

# Historical hash domain: saved stage records carry request IDs derived from
# this string. Keep it across the rebrand so existing .blend records stay coherent.
RID_DOMAIN = b"gh-stack/rid/v2"


def _prefixed(digest):
    return "sha256:" + digest.hexdigest()


def _update_section(digest, name, raw):
    digest.update(name.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(raw)


# Definition and part indices are decimal integers. C# parses them with
# int.TryParse(NumberStyles.None, InvariantCulture); matching that here keeps
# the two hashers on one grammar. A token outside this range is not an index.
_MAX_INDEX = 2**31 - 1


def _parse_index(token):
    """The int a decimal index token names, or None.

    ASCII digits only: ``str.isdigit`` also accepts non-ASCII digits that the
    C# parser does not, which would give the two sides different hash orders.
    """
    if not token or not token.isascii() or not token.isdigit():
        return None
    value = int(token)
    return value if value <= _MAX_INDEX else None


def _split_part(rest):
    """``(part, inner)`` for a multi-part name fragment, else ``(None, rest)``.

    ``rest`` is what follows ``idef.<i>.``. A definition-level fragment starts
    with a section kind (``mesh``, ``inst``, ``attr``, ...); a multi-part one
    starts with a part number, so only a leading decimal component is a part.
    """
    head, separator, inner = rest.partition(".")
    if not separator or not inner:
        return None, rest
    part = _parse_index(head)
    return (part, inner) if part is not None else (None, rest)


def ordered_names(names):
    """The canonical §6.1 order for a set of section names.

    Base sections follow the §5 table order. `idef.<i>.*` groups follow, one
    definition after the other in numeric index order. Within a definition,
    multi-part sections come first in numeric part order, each part ordered the
    same way; the definition-level sections (nested `inst.*` rows) follow after
    all parts, so a definition's rows stay last as they always have. Anything
    else is last, sorted, so an unknown name stays deterministic.

    Single-part definitions have no numeric part fragment, so their order is
    unchanged: the §5 table order over the whole definition.
    """
    base = []
    groups = {}
    extra = []
    for name in names:
        parts = name.split(".", 2)
        index = _parse_index(parts[1]) if len(parts) == 3 and parts[0] == "idef" else None
        if index is not None:
            entry = groups.setdefault(index, {"base": [], "parts": {}})
            part, inner = _split_part(parts[2])
            if part is None:
                entry["base"].append(inner)
            else:
                entry["parts"].setdefault(part, []).append(inner)
        elif name in HASH_SECTION_ORDER:
            base.append(name)
        else:
            extra.append(name)
    ordered = [name for name in HASH_SECTION_ORDER if name in base]
    for index in sorted(groups):
        entry = groups[index]
        for part in sorted(entry["parts"]):
            for inner in ordered_names(entry["parts"][part]):
                ordered.append("idef.%d.%d.%s" % (index, part, inner))
        for inner in ordered_names(entry["base"]):
            ordered.append("idef.%d.%s" % (index, inner))
    ordered += sorted(extra)
    return ordered


def geometry_hash(section_renderers):
    """SHA-256 over the canonical section list.

    ``section_renderers`` maps a section name to any ``len()``able sequence of raw
    bytes (a numpy ``ndarray`` works, via ``bytes()``). Sections are hashed in
    :func:`ordered_names` order.
    """
    digest = hashlib.sha256()
    by_name = dict(section_renderers)
    for name in ordered_names(by_name):
        _update_section(digest, name, bytes(by_name[name]))
    return _prefixed(digest)


def output_hash(sections_in_header_order):
    """SHA-256 over the response sections in header order (protocol §6.3).

    ``sections_in_header_order`` is a list of ``(name, bytes_like)`` pairs.
    """
    digest = hashlib.sha256()
    for name, raw in sections_in_header_order:
        _update_section(digest, name, bytes(raw))
    return _prefixed(digest)


def request_id(stage_key, params_json, geom_hash, units, space, nonce, domain=RID_DOMAIN):
    """SHA-256 over the request fields, separated by 0x00 (protocol §6.2).

    ``params_json`` is the canonical parameter JSON as a string. ``nonce`` is the
    request counter; Push increments it.
    """
    digest = hashlib.sha256()
    for part in (
        domain,
        stage_key.encode("utf-8"),
        params_json.encode("utf-8"),
        geom_hash.encode("utf-8"),
        units.encode("utf-8"),
        space.encode("utf-8"),
        str(int(nonce)).encode("ascii"),
    ):
        digest.update(part)
        digest.update(b"\x00")
    return _prefixed(digest)
