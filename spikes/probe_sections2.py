"""Follow-up probe: GeometrySet accessors and curve spline details."""

import bpy
import numpy as np


def out(msg):
    print("[probe2] " + msg, flush=True)


def main():
    # GeometrySet API surface.
    mesh = bpy.data.meshes.new("m")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new("m", mesh)
    bpy.context.scene.collection.objects.link(obj)
    cu = bpy.data.curves.new("c", "CURVE")
    cu.dimensions = "3D"
    poly = cu.splines.new("POLY")
    poly.points.add(2)
    for i, p in enumerate(poly.points):
        p.co = (float(i), 0.0, 0.0, 1.0)
    cob = bpy.data.objects.new("c", cu)
    bpy.context.scene.collection.objects.link(cob)
    pc = bpy.data.pointclouds.new("p")
    try:
        pc.points.add(2)
    except Exception as ex:
        out("pointcloud points.add failed: %r" % (ex,))
        out("pointcloud attrs: %s" % ([d for d in dir(pc) if not d.startswith("_")],))
    pob = bpy.data.objects.new("p", pc)
    bpy.context.scene.collection.objects.link(pob)

    dg = bpy.context.evaluated_depsgraph_get()
    geo = obj.evaluated_get(dg).evaluated_geometry()
    out("GeometrySet dir: %s" % [d for d in dir(geo) if not d.startswith("_")])
    comp = None
    for name in ("mesh_components", "meshes", "mesh", "components"):
        if hasattr(geo, name):
            try:
                v = getattr(geo, name)
                out("geo.%s -> %r" % (name, v))
            except Exception as ex:
                out("geo.%s raised %r" % (name, ex))

    try:
        ms = list(geo.mesh_components())
        out("mesh_components: %d, first has %d verts" % (len(ms), len(ms[0].data.vertices) if ms else -1))
    except Exception as ex:
        out("mesh_components failed: %r" % (ex,))

    cgeo = cob.evaluated_get(dg).evaluated_geometry()
    for name in ("curve_components", "curves"):
        try:
            v = getattr(cgeo, name)
            out("curve geo.%s -> %r" % (name, v))
            if callable(v):
                cs = list(v())
                out("  %d component(s), first %d splines" % (len(cs), len(cs[0].data.splines) if cs else -1))
        except Exception as ex:
            out("curve geo.%s failed: %r" % (name, ex))

    pgeo = pob.evaluated_get(dg).evaluated_geometry()
    for name in ("pointcloud_components", "pointclouds"):
        try:
            v = getattr(pgeo, name)
            out("point geo.%s -> %r" % (name, v))
            if callable(v):
                ps = list(v())
                out("  %d component(s), first %d points" % (len(ps), len(ps[0].data.points) if ps else -1))
        except Exception as ex:
            out("point geo.%s failed: %r" % (name, ex))

    # Spline details for the wire: everything the curve sections need.
    cu2 = bpy.data.curves.new("c2", "CURVE")
    cu2.dimensions = "3D"
    bez = cu2.splines.new("BEZIER")
    bez.bezier_points.add(1)
    for i, bp in enumerate(bez.bezier_points):
        bp.co = (0.0, 0.0, float(i))
    out("BEZIER: %d bezier_points, %d points, order_u=%s" % (len(bez.bezier_points), len(bez.points), bez.order_u))
    nur = cu2.splines.new("NURBS")
    nur.points.add(3)
    out("NURBS: %d points, order_u=%s, use_endpoint_u=%s, use_bezier_u=%s"
        % (len(nur.points), nur.order_u, nur.use_endpoint_u, nur.use_bezier_u))
    # weights
    w = np.array([p.co.w for p in nur.points])
    out("NURBS weights: %s" % (w.tolist(),))

    # FLOAT_COLOR read path
    m2 = bpy.data.meshes.new("m2")
    m2.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    m2.update()
    a = m2.color_attributes.new("F", "FLOAT_COLOR", "POINT")
    a.data.foreach_set("color", np.array([0.5, 0.25, 0.125, 1.0], dtype=np.float32))
    back = np.empty(4, dtype=np.float32)
    a.data.foreach_get("color", back)
    out("FLOAT_COLOR color read: %s" % (back.tolist(),))
    srgb = np.empty(4, dtype=np.float32)
    a.data.foreach_get("color_srgb", srgb)
    out("FLOAT_COLOR color_srgb read: %s" % (srgb.tolist(),))


main()
