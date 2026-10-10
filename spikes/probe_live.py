import bpy, bmesh, time, os, traceback
dg = lambda: bpy.context.evaluated_depsgraph_get()
def nv(o): return len(o.evaluated_get(dg()).data.vertices)
def set_mesh(obj, verts, faces):
    bm = bmesh.new()
    vs = [bm.verts.new(v) for v in verts]
    for f in faces:
        bm.faces.new([vs[i] for i in f])
    bm.to_mesh(obj.data); bm.free(); obj.data.update()

bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name = "GH_Helper"
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object; res.name = "Result"
ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gi = ng.nodes.new("NodeGroupInput"); go = ng.nodes.new("NodeGroupOutput")
oi = ng.nodes.new("GeometryNodeObjectInfo"); oi.inputs[0].default_value = helper
ng.links.new(oi.outputs["Geometry"], go.inputs["Geometry"])
mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
print("T1 nodedefault initial:", nv(res), "(expect 8)", flush=True)
set_mesh(helper, [(0,0,0),(1,0,0),(0,1,0),(0,0,1)], [(0,1,2)])
print("T2 bmesh in-place edit:", nv(res), "(expect 3 if live)", flush=True)
print("T3 same mesh datablock:", helper.data.name, "| modifier still on Result:", res.modifiers.get("GH") is not None, flush=True)

cube_v = [(x,y,z) for x in (0,1) for y in (0,1) for z in (0,1)]
cube_f = [(0,1,3,2),(4,6,7,5),(0,4,5,1),(2,3,7,6),(0,2,6,4),(1,5,7,3)]
def write_ply(path, verts, faces):
    with open(path, "w") as f:
        f.write("ply\nformat ascii 1.0\nelement vertex %d\nproperty float x\nproperty float y\nproperty float z\n" % len(verts))
        f.write("element face %d\nproperty list uchar int vertex_indices\nend_header\n" % len(faces))
        for v in verts: f.write("%f %f %f\n" % v)
        for fc in faces: f.write("%d %s\n" % (len(fc), " ".join(map(str, fc))))
try:
    ng2 = bpy.data.node_groups.new("PLY", "GeometryNodeTree")
    ng2.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    ng2.interface.new_socket("Path", in_out="INPUT", socket_type="NodeSocketString")
    gi2 = ng2.nodes.new("NodeGroupInput"); go2 = ng2.nodes.new("NodeGroupOutput")
    imp = ng2.nodes.new("GeometryNodeImportPLY")
    ng2.links.new(gi2.outputs["Path"], imp.inputs["Path"])
    ng2.links.new(imp.outputs["Mesh"], go2.inputs["Geometry"])
    pid = [s.identifier for s in ng2.interface.items_tree if getattr(s,"in_out","")=="INPUT"][0]
    p = "/tmp/opencode/probe/stage.ply"; write_ply(p, cube_v, cube_f)
    o2 = bpy.data.objects.new("PLYProbe", bpy.data.meshes.new("PLYProbe")); bpy.context.collection.objects.link(o2)
    m2 = o2.modifiers.new("GHply", "NODES"); m2.node_group = ng2
    m2.properties.bl_system_properties_get()["inputs"][pid] = p
    print("T4 PLY initial:", nv(o2), "(expect 8)", flush=True)
    write_ply(p, cube_v[:4], [(0,1,2)])
    m2.properties.bl_system_properties_get()["inputs"][pid] = p
    print("T5 PLY same path rewritten + re-set:", nv(o2), "(expect 3 if re-read)", flush=True)
    p2 = "/tmp/opencode/probe/stage_r2.ply"; write_ply(p2, cube_v[:4], [(0,1,2)])
    m2.properties.bl_system_properties_get()["inputs"][pid] = p2
    print("T6 PLY new path:", nv(o2), flush=True)
    write_ply(p2, cube_v, cube_f)
    print("T7 PLY same new path rewritten, no re-set:", nv(o2), "(stale=3, live=8)", flush=True)
except Exception:
    traceback.print_exc()
