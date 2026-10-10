import bpy, traceback, math
print("V", bpy.app.version_string, flush=True)
# 1 spline API
cu = bpy.data.curves.new("C", "CURVE")
sp = cu.splines.new("NURBS")
print("S1 nurbs spline attrs:", [a for a in ("order_u","use_endpoint_u","use_bezier_u","use_cyclic_u","knot_mode_u","weight_softness") if hasattr(sp,a)], flush=True)
print("S1 point attrs:", [a for a in ("co","weight","radius","tilt") if hasattr(sp.points[0] if len(sp.points) else sp,a)], flush=True)
bz = cu.splines.new("BEZIER"); print("S1 bezier point attrs:", [a for a in ("co","handle_left","handle_right","handle_left_type") if hasattr(bz.bezier_points[0] if len(bz.bezier_points) else bz,a)] if True else "", flush=True)
try:
    print("S1 curve prop enum:", [i for i in cu.bl_rna.properties["dimensions"].enum_items.keys()], flush=True)
except Exception as e: print("S1 dims err", e, flush=True)
# 2 crease attributes + subsurf
me = bpy.data.meshes.new("M"); me.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)], [], [(0,1,2,3)])
ob = bpy.data.objects.new("M", me); bpy.context.collection.objects.link(ob)
for name in ("crease_edge","crease_vert","sharp_edge"):
    try:
        a = me.attributes.new(name, "FLOAT" if "crease" in name else "BOOLEAN", "EDGE" if "edge" in name else "POINT")
        print("S2 attribute ok:", name, a.domain, flush=True)
    except Exception as e: print("S2 attribute err", name, e, flush=True)
ss = ob.modifiers.new("Sub","SUBSURF"); ss.levels=2; ss.render_levels=2
print("S2 subsurf verts:", len(ob.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices), flush=True)
print("S2 GN subd node:", hasattr(bpy.types, "GeometryNodeSubdivisionSurface"), flush=True)
# 3 keyframe + driver on custom property
cube = bpy.data.objects.new("Stack", bpy.data.meshes.new("S")); bpy.context.collection.objects.link(cube)
cube["gh/panelize-3f9a/Radius"] = 1.0
cube.id_properties_ui("gh/panelize-3f9a/Radius").update(min=0.0, max=10.0, soft_min=0.0, soft_max=5.0, description="radius")
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 10
cube["gh/panelize-3f9a/Radius"] = 1.0; cube.keyframe_insert('["gh/panelize-3f9a/Radius"]', frame=1)
cube["gh/panelize-3f9a/Radius"] = 5.0; cube.keyframe_insert('["gh/panelize-3f9a/Radius"]', frame=10)
for f in (1, 5, 10):
    scene.frame_set(f)
    print("K frame", f, "value", round(cube["gh/panelize-3f9a/Radius"],3), flush=True)
fc = cube.animation_data.action.fcurves[0]; print("K fcurves", len(cube.animation_data.action.fcurves), flush=True)
print("K fcurve data_path:", fc.data_path, flush=True)
drv = cube.driver_add('["gh/panelize-3f9a/Radius"]')
d = drv.driver; d.type = "SCRIPTED"; d.expression = "frame * 0.5"
scene.frame_set(4); print("D driver value at frame 4 (expect 2.0 if driver wins):", round(cube["gh/panelize-3f9a/Radius"],3), flush=True)
