import bpy
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
cd = bpy.data.curves.new("NurbsHelper", "CURVE"); cd.dimensions = "3D"
sp = cd.splines.new("NURBS"); sp.points.add(4)
for i, p in enumerate(sp.points):
    p.co = (i, (i % 2) * 1.0, 0, 1.0)
sp.points[2].weight = 3.0
sp.order_u = 4; sp.use_endpoint_u = True
hob = bpy.data.objects.new("Helper", cd); bpy.context.collection.objects.link(hob)
bpy.ops.mesh.primitive_plane_add(size=1); host = bpy.context.active_object
ng = bpy.data.node_groups.new("NurbsStage", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
go = ng.nodes.new("NodeGroupOutput"); oi = ng.nodes.new("GeometryNodeObjectInfo")
oi.inputs[0].default_value = hob; oi.transform_space = "ORIGINAL"
ng.links.new(oi.outputs["Geometry"], go.inputs["Geometry"])
m = host.modifiers.new("N", "NODES"); m.node_group = ng
dg = bpy.context.evaluated_depsgraph_get()
gs = host.evaluated_get(dg).evaluated_geometry()
cv = gs.curves
print("N1 curves component:", cv is not None, flush=True)
if cv is not None:
    print("N2 attributes:", [a.name for a in cv.attributes], "points:", len(cv.points), flush=True)
    print("N3 curve_type values:", [c for c in cv.attributes["curve_type"].data.values()][:1] if "curve_type" in cv.attributes else None, flush=True)
    print("N4 weight-like attrs:", [a.name for a in cv.attributes if "weight" in a.name.lower() or "knot" in a.name.lower() or "nurb" in a.name.lower()], flush=True)
print("N5 legacy spline weight readback:", round(sp.points[2].weight,3), "order:", sp.order_u, flush=True)
