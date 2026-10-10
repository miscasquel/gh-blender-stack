import bpy, os, traceback
dg = lambda: bpy.context.evaluated_depsgraph_get()

def nverts(obj):
    return len(obj.evaluated_get(dg()).data.vertices)

def set_input(mod, ident, value):
    mod.properties.bl_system_properties_get()["inputs"][ident] = value

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
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name = "GH_Helper"
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object; res.name = "Result"

try:
    ng = bpy.data.node_groups.new("GH_Stage_Test", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
    gi = ng.nodes.new("NodeGroupInput"); go = ng.nodes.new("NodeGroupOutput")
    oi = ng.nodes.new("GeometryNodeObjectInfo")
    ng.links.new(gi.outputs["Source"], oi.inputs["Object"])
    ng.links.new(oi.outputs["Geometry"], go.inputs["Geometry"])
    src_id = [s.identifier for s in ng.interface.items_tree if getattr(s, "in_out", "") == "INPUT"][0]
    mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
    set_input(mod, src_id, helper)
    print("T1 objinfo initial verts:", nverts(res), "(expect 8)")
    helper.data.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)], [], [(0, 1, 2)])
    helper.data.update()
    print("T2 after in-place mesh edit verts:", nverts(res), "(expect 3 if live)")
    print("T3 modifier still attached:", res.modifiers.get("GH") is not None, "| helper mesh:", helper.data.name)
    helper.hide_viewport = True
    helper.data.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    helper.data.update()
    print("T4 hidden helper edit verts:", nverts(res))
except Exception:
    traceback.print_exc()

try:
    ng2 = bpy.data.node_groups.new("PLY_Stage_Test", "GeometryNodeTree")
    ng2.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    ng2.interface.new_socket("Path", in_out="INPUT", socket_type="NodeSocketString")
    gi2 = ng2.nodes.new("NodeGroupInput"); go2 = ng2.nodes.new("NodeGroupOutput")
    imp = ng2.nodes.new("GeometryNodeImportPLY")
    ng2.links.new(gi2.outputs["Path"], imp.inputs["Path"])
    ng2.links.new(imp.outputs["Mesh"], go2.inputs["Geometry"])
    path_id = [s.identifier for s in ng2.interface.items_tree if getattr(s, "in_out", "") == "INPUT"][0]

    ply = "/tmp/opencode/probe/stage.ply"
    write_ply(ply, cube_v, cube_f)
    res2 = bpy.data.objects.new("PLYProbe", bpy.data.meshes.new("PLYProbe"))
    bpy.context.collection.objects.link(res2)
    m2 = res2.modifiers.new("GHply", "NODES"); m2.node_group = ng2
    set_input(m2, path_id, ply)
    print("T5 PLY initial verts:", nverts(res2), "(expect 8)")

    write_ply(ply, cube_v[:4], [(0, 1, 2)])
    set_input(m2, path_id, ply)
    print("T6 PLY same path rewritten + re-set:", nverts(res2), "(expect 3 if re-read)")

    ply2 = "/tmp/opencode/probe/stage_r2.ply"
    write_ply(ply2, cube_v[:4], [(0, 1, 2)])
    set_input(m2, path_id, ply2)
    print("T7 PLY new path:", nverts(res2), "(expect 3)")
    write_ply(ply2, cube_v, cube_f)
    print("T8 PLY same new path, file rewritten, no re-set:", nverts(res2), "(stale=3, live=8)")
except Exception:
    traceback.print_exc()
