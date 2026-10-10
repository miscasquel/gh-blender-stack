import bpy, sys, time
case = sys.argv[sys.argv.index("--")+1]
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name = "GH_Helper"
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object; res.name = "Result"
ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gi = ng.nodes.new("NodeGroupInput"); go = ng.nodes.new("NodeGroupOutput")
oi = ng.nodes.new("GeometryNodeObjectInfo")
oi.inputs[0].default_value = helper
ng.links.new(oi.outputs["Geometry"], go.inputs["Geometry"])
mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
print("START", case, flush=True)
t=time.time()
d = bpy.context.evaluated_depsgraph_get()
n = len(res.evaluated_get(d).data.vertices)
print("VERTS", case, n, round(time.time()-t,2), flush=True)
helper.data.from_pydata([(0,0,0),(1,0,0),(0,1,0)], [], [(0,1,2)]); helper.data.update()
d = bpy.context.evaluated_depsgraph_get()
print("LIVE_EDIT", len(res.evaluated_get(d).data.vertices), flush=True)
