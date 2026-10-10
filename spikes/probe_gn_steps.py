import bpy, os, traceback
dg = lambda: bpy.context.evaluated_depsgraph_get()

def nverts(obj):
    return len(obj.evaluated_get(dg()).data.vertices)

def set_input(mod, ident, value):
    mod.properties.bl_system_properties_get()["inputs"][ident] = value
    print("STEP 8", flush=True)

def write_ply(path, verts, faces):
    with open(path, "w") as f:
        f.write("ply\nformat ascii 1.0\nelement vertex %d\nproperty float x\nproperty float y\nproperty float z\n" % len(verts))
        f.write("element face %d\nproperty list uchar int vertex_indices\nend_header\n" % len(faces))
        for v in verts:
            f.write("%f %f %f\n" % v)
        for fc in faces:
            f.write("%d %s\n" % (len(fc), " ".join(map(str, fc))))

cube_v = [(x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
cube_f = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]

bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
print("STEP 22", flush=True)
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name = "GH_Helper"
print("STEP 23", flush=True)
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object; res.name = "Result"
print("STEP 24", flush=True)

try:
    ng = bpy.data.node_groups.new("GH_Stage_Test", "GeometryNodeTree")
    print("STEP 27", flush=True)
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    print("STEP 28", flush=True)
    ng.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
    print("STEP 29", flush=True)
    gi = ng.nodes.new("NodeGroupInput"); go = ng.nodes.new("NodeGroupOutput")
    print("STEP 30", flush=True)
    oi = ng.nodes.new("GeometryNodeObjectInfo")
    print("STEP 31", flush=True)
    ng.links.new(gi.outputs["Source"], oi.inputs["Object"])
    print("STEP 32", flush=True)
    ng.links.new(oi.outputs["Geometry"], go.inputs["Geometry"])
    print("STEP 33", flush=True)
    src_id = [s.identifier for s in ng.interface.items_tree if getattr(s, "in_out", "") == "INPUT"][0]
    print("STEP 34", flush=True)
    mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
    print("STEP 35", flush=True)
    set_input(mod, src_id, helper)
    print("STEP 36", flush=True)
    print("T1 objinfo initial verts:", nverts(res), "(expect 8)")
    helper.data.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)], [], [(0, 1, 2)])
    print("STEP 38", flush=True)
    helper.data.update()
    print("STEP 39", flush=True)
    print("T2 after in-place mesh edit verts:", nverts(res), "(expect 3 if live)")
    print("T3 modifier still attached:", res.modifiers.get("GH") is not None, "| helper mesh:", helper.data.name)
    helper.hide_viewport = True
    print("STEP 42", flush=True)
    helper.data.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    print("STEP 43", flush=True)
    helper.data.update()
    print("STEP 44", flush=True)
    print("T4 hidden helper edit verts:", nverts(res))
except Exception:
    traceback.print_exc()
    print("STEP 47", flush=True)

try:
    ng2 = bpy.data.node_groups.new("PLY_Stage_Test", "GeometryNodeTree")
    print("STEP 50", flush=True)
    ng2.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    print("STEP 51", flush=True)
    ng2.interface.new_socket("Path", in_out="INPUT", socket_type="NodeSocketString")
    print("STEP 52", flush=True)
    gi2 = ng2.nodes.new("NodeGroupInput"); go2 = ng2.nodes.new("NodeGroupOutput")
    print("STEP 53", flush=True)
    imp = ng2.nodes.new("GeometryNodeImportPLY")
    print("STEP 54", flush=True)
    ng2.links.new(gi2.outputs["Path"], imp.inputs["Path"])
    print("STEP 55", flush=True)
    ng2.links.new(imp.outputs["Mesh"], go2.inputs["Geometry"])
    print("STEP 56", flush=True)
    path_id = [s.identifier for s in ng2.interface.items_tree if getattr(s, "in_out", "") == "INPUT"][0]
    print("STEP 57", flush=True)

    ply = "/tmp/opencode/probe/stage.ply"
    print("STEP 59", flush=True)
    write_ply(ply, cube_v, cube_f)
    print("STEP 60", flush=True)
    res2 = bpy.data.objects.new("PLYProbe", bpy.data.meshes.new("PLYProbe"))
    print("STEP 61", flush=True)
    bpy.context.collection.objects.link(res2)
    print("STEP 62", flush=True)
    m2 = res2.modifiers.new("GHply", "NODES"); m2.node_group = ng2
    print("STEP 63", flush=True)
    set_input(m2, path_id, ply)
    print("STEP 64", flush=True)
    print("T5 PLY initial verts:", nverts(res2), "(expect 8)")

    write_ply(ply, cube_v[:4], [(0, 1, 2)])
    print("STEP 67", flush=True)
    set_input(m2, path_id, ply)
    print("STEP 68", flush=True)
    print("T6 PLY same path rewritten + re-set:", nverts(res2), "(expect 3 if re-read)")

    ply2 = "/tmp/opencode/probe/stage_r2.ply"
    print("STEP 71", flush=True)
    write_ply(ply2, cube_v[:4], [(0, 1, 2)])
    print("STEP 72", flush=True)
    set_input(m2, path_id, ply2)
    print("STEP 73", flush=True)
    print("T7 PLY new path:", nverts(res2), "(expect 3)")
    write_ply(ply2, cube_v, cube_f)
    print("STEP 75", flush=True)
    print("T8 PLY same new path, file rewritten, no re-set:", nverts(res2), "(stale=3, live=8)")
except Exception:
    traceback.print_exc()
    print("STEP 78", flush=True)
