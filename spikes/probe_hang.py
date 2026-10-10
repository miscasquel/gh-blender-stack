import bpy, sys, time
case = sys.argv[sys.argv.index("--")+1]
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name = "GH_Helper"
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object; res.name = "Result"
ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gi = ng.nodes.new("NodeGroupInput"); go = ng.nodes.new("NodeGroupOutput")
if case == "passthrough":
    ng.links.new(gi.outputs[0], go.inputs[0]) if len(gi.outputs) else None
else:
    ng.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
    oi = ng.nodes.new("GeometryNodeObjectInfo")
    ng.links.new(gi.outputs["Source"], oi.inputs["Object"])
    ng.links.new(oi.outputs["Geometry"], go.inputs["Geometry"])
mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
if case != "passthrough":
    sid = [s.identifier for s in ng.interface.items_tree if getattr(s,"in_out","")=="INPUT"][0]
    mod.properties.bl_system_properties_get()["inputs"][sid] = helper
print("START", case, flush=True)
t=time.time()
d = bpy.context.evaluated_depsgraph_get()
print("DEPSGRAPH", round(time.time()-t,2), flush=True)
n = len(res.evaluated_get(d).data.vertices)
print("VERTS", case, n, round(time.time()-t,2), flush=True)
