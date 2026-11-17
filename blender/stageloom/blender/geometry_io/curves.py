"""Curve read/write: Curve datablocks, Curves components, bezier nets and the
poly/NURBS/bezier spline builders.
"""

import numpy as np

from ...core import attributes as attr_mod
from .. import prefs
from .attributes import _attribute_values, _emit_mapped
from .common import (GeometryIOError, _count_note, _note, crv_sections,
                     sections_by_name)

CURVE_POLY, CURVE_BEZIER, CURVE_NURBS = 0, 1, 2

# The Curves component's own channels are the geometry, not user attributes.
# They are already carried by crv.* and must not be duplicated as generic ones.
CURVE_INTERNAL_ATTRIBUTES = (
    "curve_type", "nurbs_weight", "nurbs_order", "knots_mode", "cyclic",
    "handle_left", "handle_right",
)

KNOTS_NORMAL, KNOTS_ENDPOINT, KNOTS_BEZIER = 0, 1, 2

# Blender's CurvesType enum, as stored in the curve_type attribute.
CURVE_TYPE_CATMULL_ROM, CURVE_TYPE_POLY, CURVE_TYPE_BEZIER, CURVE_TYPE_NURBS = 0, 1, 2, 3


class _CurveBatch:
    """One curve read's parallel wire lists and append rule."""

    __slots__ = ("points", "counts", "types", "orders", "modes", "cyclic",
                 "curve_indices", "point_indices", "has_bezier")

    def __init__(self):
        self.points = []
        self.counts = []
        self.types = []
        self.orders = []
        self.modes = []
        self.cyclic = []
        self.curve_indices = []
        self.point_indices = []
        self.has_bezier = False

    def add(self, points, curve_type, order, mode, cyclic, curve_index=None,
            point_indices=()):
        self.points.extend(points)
        self.counts.append(len(points))
        self.types.append(curve_type)
        self.orders.append(order)
        self.modes.append(mode)
        self.cyclic.append(cyclic)
        if curve_index is not None:
            self.curve_indices.append(curve_index)
            self.point_indices.extend(point_indices)

    def sections(self, prefix):
        if not self.counts:
            return []
        flat = np.asarray(self.points, dtype=np.float32).reshape(-1, 4)
        return crv_sections(prefix, flat, self.counts, self.types,
                            self.orders, self.modes, self.cyclic)


def curve_to_sections(curve, prefix="", notes=None):
    """Read a Blender Curve datablock into ``crv.*`` sections (protocol §5).

    A Bezier spline travels as a degree-3 NURBS in Bezier knot mode, using the
    control net co0, handle_right0, handle_left1, co1, ... — the representation
    the GH side rebuilds exactly. A cyclic Bezier spline wraps the closing
    segment, so its net ends where it starts instead of carrying the cyclic flag.
    ``prefix`` namespaces a definition's sections (§5.2). ``notes`` collects
    splines this wire form cannot carry.
    """
    batch = _CurveBatch()
    empty_bezier = 0
    short_splines = 0
    bad_nurbs = 0

    for spline in curve.splines:
        if spline.type == "BEZIER":
            net = _bezier_net(spline)
            if net is None:
                empty_bezier += 1
                continue
            batch.add(net, CURVE_NURBS, 4, KNOTS_BEZIER, 0)
            continue
        if spline.type == "NURBS":
            p = [(pt.co[0], pt.co[1], pt.co[2], float(pt.weight)) for pt in spline.points]
            order = int(spline.order_u)
            weights = [point[3] for point in p]
            if (
                len(p) < 2
                or order < 2
                or order > 6
                or order > len(p)
                or any(not np.isfinite(weight) or weight <= 0.0 for weight in weights)
            ):
                bad_nurbs += 1
                continue
            batch.add(
                p, CURVE_NURBS, order,
                KNOTS_ENDPOINT if spline.use_endpoint_u else KNOTS_NORMAL,
                1 if spline.use_cyclic_u else 0,
            )
            continue
        # POLY and anything else Blender has: a polyline through the points.
        p = [(pt.co[0], pt.co[1], pt.co[2], 1.0) for pt in spline.points]
        if len(p) < 2:
            short_splines += 1
            continue
        batch.add(p, CURVE_POLY, 0, 0, 1 if spline.use_cyclic_u else 0)

    _count_note(notes, empty_bezier,
                "Bezier spline had no span and was skipped",
                "Bezier splines had no span and were skipped")
    _count_note(notes, short_splines,
                "spline had fewer than two points and was skipped",
                "splines had fewer than two points and were skipped")
    _count_note(notes, bad_nurbs,
                "NURBS spline has an unsupported order or weight and was skipped",
                "NURBS splines have an unsupported order or weight and were skipped")

    return batch.sections(prefix)


def _bezier_net(spline):
    """The NURBS control net of a Bezier spline, or None when it has no span."""
    bp = list(spline.bezier_points)
    n = len(bp)
    if n < 2:
        return None

    def p(v):
        return (float(v[0]), float(v[1]), float(v[2]), 1.0)

    net = [p(bp[0].co)]
    for i in range(n - 1):
        net.append(p(bp[i].handle_right))
        net.append(p(bp[i + 1].handle_left))
        net.append(p(bp[i + 1].co))
    if spline.use_cyclic_u:
        net.append(p(bp[n - 1].handle_right))
        net.append(p(bp[0].handle_left))
        net.append(p(bp[0].co))
    return net


def curves_component_to_sections(curves, index0=0, prefix="", definition=None,
                                 part=None, notes=None):
    """Read a GeometrySet's curve component into ``crv.*`` sections and attributes.

    The component is the new Curves type: per-curve attributes carry the kind
    (``curve_type``), the NURBS order and knots mode, and the cyclic flag;
    per-point attributes carry the position, the NURBS weight, and the Bezier
    handles. Those internal channels stay in ``crv.*``; user attributes are
    reindexed onto the curves that actually reach the wire, with per-curve
    values in domain ``CURVE`` and per-point values in domain ``POINT``. This is
    the same attribute logic the loose-edge exporter uses.

    ``prefix`` namespaces a definition's sections (§5.2). Returns
    ``(sections, attributes)``.

    Catmull-Rom components have no wire representation (our protocol has poly,
    Bezier, and NURBS); they are skipped and ``notes`` names the count. A Bezier
    curve's wire points are its control net, not its original points, so
    per-point attributes are skipped for the whole component when a Bezier curve
    is present; a 1:1 point mapping does not exist.
    """
    attrs = curves.attributes
    positions = attrs["position"].data
    weights = attrs.get("nurbs_weight")
    cyclic_attr = attrs.get("cyclic")
    type_attr = attrs.get("curve_type")
    order_attr = attrs.get("nurbs_order")
    knots_attr = attrs.get("knots_mode")
    handle_left = attrs.get("handle_left")
    handle_right = attrs.get("handle_right")

    batch = _CurveBatch()
    catmull = 0
    missing_handles = 0
    short_curves = 0
    unsupported_nurbs = 0
    unknown_types = 0

    for curve in curves.curves:
        first = curve.first_point_index
        length = curve.points_length
        index = curve.index
        if length < 2:
            short_curves += 1
            continue
        ctype = int(type_attr.data[index].value) if type_attr is not None else CURVE_TYPE_POLY
        is_cyclic = bool(cyclic_attr.data[index].value) if cyclic_attr is not None else False

        if ctype == CURVE_TYPE_CATMULL_ROM:
            catmull += 1
            prefs.log(
                "[stageloom] Catmull-Rom curve component skipped: no wire representation",
                flush=True,
            )
            continue

        if ctype == CURVE_TYPE_BEZIER:
            net = _component_bezier_net(positions, handle_left, handle_right, first, length, is_cyclic)
            if net is None:
                missing_handles += 1
                prefs.log(
                    "[stageloom] Bezier curve component skipped: no handles",
                    flush=True,
                )
                continue
            batch.add(net, CURVE_NURBS, 4, KNOTS_BEZIER, 0, curve_index=index)
            batch.has_bezier = True
            continue

        chunk = []
        for i in range(first, first + length):
            p = positions[i].vector
            w = float(weights.data[i].value) if weights is not None else 1.0
            chunk.append((p[0], p[1], p[2], w))

        if ctype == CURVE_TYPE_NURBS:
            order = int(order_attr.data[index].value) if order_attr is not None else 4
            mode = int(knots_attr.data[index].value) if knots_attr is not None else KNOTS_NORMAL
            weights_valid = all(
                np.isfinite(point[3]) and point[3] > 0.0 for point in chunk
            )
            if order < 2 or order > 6 or order > length or mode > 2 or not weights_valid:
                unsupported_nurbs += 1
                prefs.log(
                    "[stageloom] NURBS curve component skipped: order %d, mode %d, "
                    "or a non-positive weight is not representable" % (order, mode),
                    flush=True,
                )
                continue
            batch.add(
                chunk, CURVE_NURBS, order, mode, 1 if is_cyclic else 0,
                curve_index=index, point_indices=range(first, first + length),
            )
        elif ctype == CURVE_TYPE_POLY:
            batch.add(
                chunk, CURVE_POLY, 0, 0, 1 if is_cyclic else 0,
                curve_index=index, point_indices=range(first, first + length),
            )
        else:
            unknown_types += 1
            prefs.log(
                "[stageloom] curve component type %d skipped: not a wire curve type" % ctype,
                flush=True,
            )
            continue

    _count_note(notes, catmull,
                "Catmull-Rom curve component has no wire representation and was skipped",
                "Catmull-Rom curve components have no wire representation and were skipped")
    _count_note(notes, missing_handles,
                "Bezier curve component had no handles and was skipped",
                "Bezier curve components had no handles and were skipped")
    _count_note(notes, short_curves,
                "curve component had fewer than two points and was skipped",
                "curve components had fewer than two points and were skipped")
    _count_note(notes, unsupported_nurbs,
                "NURBS curve component had an unsupported order, mode or weight and was skipped",
                "NURBS curve components had an unsupported order, mode or weight and were skipped")
    _count_note(notes, unknown_types,
                "curve component had an unknown type and was skipped",
                "curve components had an unknown type and were skipped")

    sections = batch.sections(prefix)
    attribute_sections, attributes = curves_component_attributes(
        curves, batch.curve_indices, batch.point_indices, batch.has_bezier,
        index0=index0, prefix=prefix, definition=definition, part=part,
        notes=notes,
    )
    return sections + attribute_sections, attributes


def curves_component_attributes(curves, curve_indices, point_indices, has_bezier,
                                index0=0, prefix="", definition=None, part=None,
                                notes=None):
    """Reindex a Curves component's user attributes onto the emitted curves.

    Per-curve values become domain ``CURVE``; per-point values become domain
    ``POINT`` and are flattened in curve order. The internal channels already
    carried by ``crv.*`` are skipped.
    """
    sections = []
    descriptors = []
    if not curve_indices:
        return sections, descriptors

    for attr in curves.attributes:
        name = attr.name
        if (
            name.startswith(".")
            or name in attr_mod.SKIP_NAMES
            or name in CURVE_INTERNAL_ATTRIBUTES
        ):
            continue
        type_name = attr.data_type
        if type_name not in attr_mod.WIRE_TYPES:
            continue
        values = _attribute_values(attr)
        comps = attr_mod.components(type_name)

        if attr.domain not in ("CURVE", "POINT"):
            continue
        if attr.domain == "POINT" and has_bezier:
            prefs.log(
                "[stageloom] curve point attribute %r skipped: a Bezier control "
                "net has no 1:1 point mapping" % name,
                flush=True,
            )
            _note(
                notes,
                "curve point attribute %r was skipped: a Bezier control net "
                "has no 1:1 point mapping" % name,
            )
            continue
        indices = curve_indices if attr.domain == "CURVE" else point_indices
        _emit_mapped(
            sections, descriptors, index0, "crv", name, attr.domain, type_name,
            values, indices, comps,
            prefix=prefix, definition=definition, part=part,
        )

    return sections, descriptors


def _component_bezier_net(positions, handle_left, handle_right, first, length, is_cyclic):
    """The degree-3 Bezier net of a component's Bezier curve, or None."""
    if handle_left is None or handle_right is None:
        return None

    def pos(i):
        v = positions[i].vector
        return (v[0], v[1], v[2], 1.0)

    def left(i):
        v = handle_left.data[i].vector
        return (v[0], v[1], v[2], 1.0)

    def right(i):
        v = handle_right.data[i].vector
        return (v[0], v[1], v[2], 1.0)

    net = [pos(first)]
    for i in range(length - 1):
        net.append(right(first + i))
        net.append(left(first + i + 1))
        net.append(pos(first + i + 1))
    if is_cyclic:
        net.append(right(first + length - 1))
        net.append(left(first))
        net.append(pos(first))
    return net


def sections_to_curve(curve, sections):
    """Rebuild a Blender Curve datablock from ``crv.*`` sections, in place.

    The whole group is validated before ``curve`` is touched: section lengths,
    the points/counts sum, per-curve type, order, knot mode, cyclic flag and
    weight. A malformed group raises ``GeometryIOError``; it is never truncated
    or completed with an invented zero weight.
    """
    by_name = sections_by_name(sections)

    names = (
        "crv.points", "crv.counts", "crv.types", "crv.orders",
        "crv.knots_mode", "crv.cyclic",
    )
    if not any(name in by_name for name in names):
        raise GeometryIOError("curve sections need crv.points and crv.counts")
    if "crv.points" not in by_name or "crv.counts" not in by_name:
        raise GeometryIOError("curve group is incomplete: crv.points/crv.counts missing")

    try:
        points = np.asarray(by_name["crv.points"], dtype=np.float32)
        if points.size % 4:
            raise ValueError
        points = points.reshape(-1, 4)
        counts = np.asarray(by_name["crv.counts"], dtype=np.uint32).reshape(-1)
    except (TypeError, ValueError):
        raise GeometryIOError("crv.points must be N x 4 floats and crv.counts a u32 list")

    n_curves = len(counts)
    arrays = {}
    for name in names[2:]:
        if name not in by_name:
            if n_curves == 0:
                arrays[name] = np.zeros(0, dtype=np.uint8)
                continue
            raise GeometryIOError("%s is missing for %d curves" % (name, n_curves))
        try:
            values = np.asarray(by_name[name]).reshape(-1)
            if len(values) != n_curves:
                raise GeometryIOError(
                    "%s has %d entries for %d curves" % (name, len(values), n_curves)
                )
            if len(values) and (values.min() < 0 or values.max() > 255):
                raise GeometryIOError("%s has a value outside u8 range" % name)
            arrays[name] = values.astype(np.uint8)
        except GeometryIOError:
            raise
        except (TypeError, ValueError) as exc:
            raise GeometryIOError("%s is not a u8 list: %s" % (name, exc))

    types = arrays["crv.types"]
    orders = arrays["crv.orders"]
    modes = arrays["crv.knots_mode"]
    cyclic = arrays["crv.cyclic"]

    total = int(counts.sum())
    if total != len(points):
        raise GeometryIOError(
            "crv.points length %d != sum(crv.counts) %d" % (len(points), total)
        )

    at = 0
    for i in range(n_curves):
        raw_count = int(counts[i])
        if raw_count < 2:
            raise GeometryIOError(
                "curve %d has %d points; at least 2 are required" % (i, raw_count)
            )
        chunk = points[at : at + raw_count]
        at += raw_count
        type_value = int(types[i])
        mode = int(modes[i])
        order = int(orders[i])
        if type_value > CURVE_NURBS:
            raise GeometryIOError("curve %d has type %d" % (i, type_value))
        if mode > KNOTS_BEZIER:
            raise GeometryIOError("curve %d has knot mode %d" % (i, mode))
        if cyclic[i] > 1:
            raise GeometryIOError("curve %d cyclic flag is %d" % (i, cyclic[i]))

        bezier = type_value == CURVE_BEZIER or (
            type_value == CURVE_NURBS and mode == KNOTS_BEZIER
        )
        if bezier:
            if raw_count < 4 or (raw_count - 1) % 3:
                raise GeometryIOError(
                    "curve %d is a Bezier net with %d points; expected 3k+1"
                    % (i, raw_count)
                )
        elif type_value == CURVE_NURBS:
            if order < 2 or order > 6:
                raise GeometryIOError(
                    "curve %d NURBS order is %d; expected 2..6" % (i, order)
                )
            if order > raw_count:
                raise GeometryIOError(
                    "curve %d NURBS order %d exceeds its %d control points"
                    % (i, order, raw_count)
                )

        weights = chunk[:, 3]
        if not np.all(np.isfinite(weights)) or not np.all(weights > 0.0):
            bad = int(np.nonzero(~np.isfinite(weights) | (weights <= 0.0))[0][0])
            raise GeometryIOError(
                "curve %d point %d has weight %r; weights must be finite and positive"
                % (i, bad, float(weights[bad]))
            )

    curve.splines.clear()
    at = 0
    for i in range(n_curves):
        n = int(counts[i])
        chunk = points[at : at + n]
        at += n
        if types[i] == CURVE_POLY:
            _poly_spline(curve, chunk, bool(cyclic[i]))
        elif modes[i] == KNOTS_BEZIER or types[i] == CURVE_BEZIER:
            _bezier_spline(curve, chunk)
        else:
            _nurbs_spline(curve, chunk, int(orders[i]), int(modes[i]), bool(cyclic[i]))
    curve.update_tag()
    return curve


def _poly_spline(curve, chunk, cyclic):
    spline = curve.splines.new("POLY")
    spline.points.add(len(chunk) - 1)
    spline.points.foreach_set("co", chunk.reshape(-1))
    spline.use_cyclic_u = cyclic


def _nurbs_spline(curve, chunk, order, mode, cyclic):
    """One NURBS spline. ``order`` is validated by ``sections_to_curve``; it is
    deliberately not clamped here, so a bad frame cannot silently change shape."""
    spline = curve.splines.new("NURBS")
    spline.points.add(len(chunk) - 1)
    spline.order_u = int(order)
    spline.use_endpoint_u = mode == KNOTS_ENDPOINT
    spline.use_cyclic_u = cyclic
    spline.points.foreach_set("co", chunk.reshape(-1))


def _bezier_spline(curve, chunk):
    """A Bezier net is ``co0, hr0, hl1, co1, ...``: 3k+1 points for k spans.

    Closed nets (from a cyclic Blender spline or a closed Rhino curve) end where
    they start; the open Bezier spline with coincident ends is the same shape.
    """
    n = len(chunk)
    if n < 4 or (n - 1) % 3 != 0:
        # Not a Bezier net. A NURBS spline keeps the control polygon shape, at
        # the cost of exactness; the GH side is where approximations are warned.
        _nurbs_spline(curve, chunk, 4, KNOTS_ENDPOINT, False)
        return
    k = (n - 1) // 3
    spline = curve.splines.new("BEZIER")
    spline.bezier_points.add(k)
    for i in range(k + 1):
        bp = spline.bezier_points[i]
        co = chunk[3 * i]
        bp.co = (float(co[0]), float(co[1]), float(co[2]))
        bp.handle_left_type = "FREE"
        bp.handle_right_type = "FREE"
        if i > 0:
            hl = chunk[3 * i - 1]
            bp.handle_left = (float(hl[0]), float(hl[1]), float(hl[2]))
        if i < k:
            hr = chunk[3 * i + 1]
            bp.handle_right = (float(hr[0]), float(hr[1]), float(hr[2]))
    spline.use_cyclic_u = False
