"""Probe the Blender 5.2 APIs the S3 section extraction needs.

    blender -b --factory-startup --python spikes/probe_sections.py

Checks and reports, without asserting:
- mesh.vertices/loops/polygons foreach_get shapes and dtypes
- colour attributes: domains, data types, and which read paths work
- crease_edge attribute: domain, dtype, foreach_get
- curve splines: types, points, bezier_points, order/knot flags
- point clouds: attributes and foreach_get
- evaluated_geometry() components
"""

import sys

import bpy
import numpy as np


def out(msg):
    print("[probe] " + msg, flush=True)


def probe_mesh():
    mesh = bpy.data.meshes.new("probe")
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [],
        [(0, 1, 2, 3)],
    )
    mesh.update()
    out("mesh: %d verts, %d loops, %d polys, %d edges"
        % (len(mesh.vertices), len(mesh.loops), len(mesh.polygons), len(mesh.edges)))

    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    out("verts foreach_get f32 ok, first=%s" % (co[:3].tolist(),))

    counts = np.empty(len(mesh.polygons), dtype=np.uint32)
    mesh.polygons.foreach_get("loop_total", counts)
    out("polygons.loop_total u32 ok: %s" % (counts.tolist(),))

    starts = np.empty(len(mesh.polygons), dtype=np.uint32)
    mesh.polygons.foreach_get("loop_start", starts)
    out("polygons.loop_start: %s" % (starts.tolist(),))

    idx = np.empty(len(mesh.loops), dtype=np.uint32)
    mesh.loops.foreach_get("vertex_index", idx)
    out("loops.vertex_index: %s" % (idx.tolist(),))

    ev = np.empty(len(mesh.edges) * 2, dtype=np.uint32)
    mesh.edges.foreach_get("vertices", ev)
    out("edges.vertices: %s" % (ev.tolist(),))

    out("colour attributes: %s" % ([a.name for a in mesh.color_attributes],))
    out("attributes: %s" % ([(a.name, a.domain, a.data_type) for a in mesh.attributes],))

    # Make a byte colour attribute on POINT and on CORNER, and read both.
    try:
        attr = mesh.color_attributes.new("ColPoint", "BYTE_COLOR", "POINT")
        vals = np.zeros(len(mesh.vertices) * 4, dtype=np.uint8)
        vals[0:4] = [255, 128, 0, 255]
        attr.data.foreach_set("color_srgb", vals.astype(np.float32) / 255.0)
        back = np.empty(len(mesh.vertices) * 4, dtype=np.float32)
        attr.data.foreach_get("color_srgb", back)
        out("POINT BYTE_COLOR color_srgb float read ok: %s" % (back[:4].tolist(),))
        try:
            back_u8 = np.empty(len(mesh.vertices) * 4, dtype=np.uint8)
            attr.data.foreach_get("color_srgb", back_u8)
            out("POINT BYTE_COLOR color_srgb u8 read ok: %s" % (back_u8[:4].tolist(),))
        except Exception as ex:
            out("POINT BYTE_COLOR color_srgb u8 read FAILED: %r" % (ex,))
    except Exception as ex:
        out("POINT BYTE_COLOR creation FAILED: %r" % (ex,))

    try:
        attr2 = mesh.color_attributes.new("ColCorner", "BYTE_COLOR", "CORNER")
        vals = np.zeros(len(mesh.loops) * 4, dtype=np.float32)
        attr2.data.foreach_set("color_srgb", vals)
        out("CORNER BYTE_COLOR on %d loops ok" % (len(mesh.loops),))
    except Exception as ex:
        out("CORNER BYTE_COLOR FAILED: %r" % (ex,))

    # Crease attribute.
    try:
        cr = mesh.attributes.new("crease_edge", "FLOAT", "EDGE")
        out("crease_edge created; domain=%s dtype=%s" % (cr.domain, cr.data_type))
        vals = np.zeros(len(mesh.edges), dtype=np.float32)
        vals[0] = 0.75
        cr.data.foreach_set("value", vals)
        back = np.empty(len(mesh.edges), dtype=np.float32)
        cr.data.foreach_get("value", back)
        out("crease_edge values: %s" % (back.tolist(),))
    except Exception as ex:
        out("crease_edge FAILED: %r" % (ex,))


def probe_curve():
    cu = bpy.data.curves.new("probe-curve", "CURVE")
    cu.dimensions = "3D"
    poly = cu.splines.new("POLY")
    poly.points.add(2)
    for i, p in enumerate(poly.points):
        p.co = (float(i), 0.0, 0.0, 1.0)
    nurb = cu.splines.new("NURBS")
    nurb.points.add(3)
    for i, p in enumerate(nurb.points):
        p.co = (0.0, float(i), 0.0, 1.0)
    nurb.order_u = 3
    bez = cu.splines.new("BEZIER")
    bez.bezier_points.add(1)
    for i, bp in enumerate(bez.bezier_points):
        bp.co = (0.0, 0.0, float(i))
        bp.handle_left = (0.0, 0.0, float(i) - 0.5)
        bp.handle_right = (0.0, 0.0, float(i) + 0.5)

    for s in cu.splines:
        out("spline type=%s points=%d cyclic=%s" % (s.type, len(s.points), s.use_cyclic_u))
        if s.type == "NURBS":
            out("  NURBS order_u=%s use_endpoint_u=%s use_bezier_u=%s"
                % (s.order_u, s.use_endpoint_u, getattr(s, "use_bezier_u", "n/a")))

    obj = bpy.data.objects.new("probe-curve", cu)
    bpy.context.scene.collection.objects.link(obj)
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    geo = ev.evaluated_geometry()
    out("curve evaluated_geometry(): %s" % (geo,))
    out("curve components: %s" % ([(c.type, len(c.data)) for c in geo.curve_components()],))


def probe_pointcloud():
    pc = bpy.data.pointclouds.new("probe-points")
    pc.points.add(2)
    pts = np.empty(len(pc.points) * 3, dtype=np.float32)
    pc.points.foreach_get("co", pts)
    out("pointcloud co: %s" % (pts.tolist(),))
    obj = bpy.data.objects.new("probe-points", pc)
    bpy.context.scene.collection.objects.link(obj)
    dg = bpy.context.evaluated_depsgraph_get()
    geo = obj.evaluated_get(dg).evaluated_geometry()
    out("pointcloud evaluated_geometry(): %s" % (geo,))
    out("point components: %s" % ([(c.type, len(c.data)) for c in geo.pointcloud_components()],))


def main():
    out("Blender %s, numpy %s" % (bpy.app.version_string, np.__version__))
    probe_mesh()
    probe_curve()
    probe_pointcloud()


main()
