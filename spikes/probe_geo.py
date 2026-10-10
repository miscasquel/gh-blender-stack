import bpy
print("G1 Object.evaluated_geometry:", hasattr(bpy.types.Object, "evaluated_geometry"), flush=True)
print("G2 SeparateComponents node:", hasattr(bpy.types, "GeometryNodeSeparateComponents"), flush=True)
print("G3 PointCloud data:", hasattr(bpy.types, "PointCloud"), flush=True)
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1); ob = bpy.context.active_object
ng = bpy.data.node_groups.new("MixedTest", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gi = ng.nodes.new("NodeGroupInput"); go = ng.nodes.new("NodeGroupOutput")
cc = ng.nodes.new("GeometryNodeCurvePrimitiveCircle"); cc.inputs["Radius"].default_value = 2.0
j = ng.nodes.new("GeometryNodeJoinGeometry")
ng.links.new(gi.outputs[0], j.inputs[0]) if False else None
ng.links.new(cc.outputs["Curve"], j.inputs[0])
ng.links.new(j.outputs["Geometry"], go.inputs["Geometry"])
m = ob.modifiers.new("Mixed", "NODES"); m.node_group = ng
dg = bpy.context.evaluated_depsgraph_get()
ev = ob.evaluated_get(dg)
print("G4 mixed output: evaluated type =", ev.type, "data type =", type(ev.data).__name__, flush=True)
if hasattr(ev, "evaluated_geometry"):
    try:
        gs = ev.evaluated_geometry()
        print("G5 geometry set components:", [c for c in dir(gs) if "has_" in c or "get_" in c][:10], flush=True)
    except Exception as e: print("G5 err", type(e).__name__, e, flush=True)
gs = ev.evaluated_geometry()
print("G6 GeometrySet type:", type(gs).__name__, flush=True)
print("G6 members:", [a for a in dir(gs) if not a.startswith("_")], flush=True)
cv = gs.curves
print("G7 curves component:", type(cv).__name__ if cv is not None else None, flush=True)
if cv is not None:
    try:
        print("G7 curve_type enum:", list(cv.bl_rna.properties["curve_type"].enum_items.keys()) if "curve_type" in cv.bl_rna.properties else "n/a", flush=True)
    except Exception as e: print("G7 enum err", e, flush=True)
    print("G7 curves props:", sorted(p.identifier for p in cv.bl_rna.properties if not p.identifier.startswith("rna")) [:40], flush=True)
print("G8 Curves types exist:", hasattr(bpy.types, "Curves"), hasattr(bpy.types, "CurvesGeometry"), flush=True)
print("G9 curve item attrs:", [a for a in dir(cv.curves[0]) if not a.startswith("_") and not a.startswith("bl_")][:40] if len(cv.curves) else "empty", flush=True)
print("G9 curves attribute names:", [a.name for a in cv.attributes], flush=True)
print("G9 curves points:", len(cv.points), "curves:", len(cv.curves), flush=True)
