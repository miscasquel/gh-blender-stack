"""Protocol v2 hashing: geometry hashes, request IDs, output hashes.

All three are SHA-256, prefixed ``sha256:`` and hex-encoded, per protocol §6.
This module is pure Python. The plan says the geometry hash is computed with numpy
on the Blender side; nothing here needs numpy, because the hash is over bytes the
caller already has (``section_bytes`` in :mod:`codec`).
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


def ordered_names(names):
    """The canonical §6.1 order for a set of section names.

    Base sections follow the §5 table order; `idef.<i>.*` groups follow, one
    definition after the other in index order, each definition ordered the same
    way; anything else is last, sorted, so an unknown name stays deterministic.
    """
    base = []
    groups = {}
    extra = []
    for name in names:
        parts = name.split(".", 2)
        if len(parts) == 3 and parts[0] == "idef" and parts[1].isdigit():
            groups.setdefault(int(parts[1]), []).append(parts[2])
        elif name in HASH_SECTION_ORDER:
            base.append(name)
        else:
            extra.append(name)
    ordered = [name for name in HASH_SECTION_ORDER if name in base]
    for index in sorted(groups):
        for inner in ordered_names(groups[index]):
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
