import bpy, traceback
dg = lambda: bpy.context.evaluated_depsgraph_get()
def nv(o): return len(o.evaluated_get(dg()).data.vertices)
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
    gi2 = ng2.nodes.new("NodeGroupInput"); go2 = ng2.nodes.new("NodeGroupOutput")
    imp = ng2.nodes.new("GeometryNodeImportPLY")
    ng2.links.new(imp.outputs["Mesh"], go2.inputs["Geometry"])
    p = "/tmp/opencode/probe/stage.ply"; write_ply(p, cube_v, cube_f)
    imp.inputs["Path"].default_value = p
    o2 = bpy.data.objects.new("PLYProbe", bpy.data.meshes.new("PLYProbe")); bpy.context.collection.objects.link(o2)
    m2 = o2.modifiers.new("GHply", "NODES"); m2.node_group = ng2
    print("T4 PLY initial:", nv(o2), "(expect 8)", flush=True)
    write_ply(p, cube_v[:4], [(0,1,2)])
    imp.inputs["Path"].default_value = p
    print("T5 same path rewritten + re-set default:", nv(o2), "(expect 3 if re-read)", flush=True)
    p2 = "/tmp/opencode/probe/stage_r2.ply"; write_ply(p2, cube_v[:4], [(0,1,2)])
    imp.inputs["Path"].default_value = p2
    print("T6 new path:", nv(o2), "(expect 3)", flush=True)
    write_ply(p2, cube_v, cube_f)
    print("T7 same new path rewritten, no re-set:", nv(o2), "(stale=3, live=8)", flush=True)
except Exception:
    traceback.print_exc()
