import bpy, bmesh
dg = lambda: bpy.context.evaluated_depsgraph_get()
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1); stack = bpy.context.active_object; stack.name = "Stack"
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name = "GH_Helper"
bm = bmesh.new(); vs=[bm.verts.new(v) for v in [(0,0,0),(1,0,0),(0,1,0),(0,0,1)]]; bm.faces.new([vs[0],vs[1],vs[2]]); bm.to_mesh(helper.data); bm.free()
ng = bpy.data.node_groups.new("GH_Stage", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
go = ng.nodes.new("NodeGroupOutput"); oi = ng.nodes.new("GeometryNodeObjectInfo")
oi.inputs[0].default_value = helper; oi.transform_space = "ORIGINAL"
ng.links.new(oi.outputs["Geometry"], go.inputs["Geometry"])
m = stack.modifiers.new("GH", "NODES"); m.node_group = ng
print("Q0 before exclude verts:", len(stack.evaluated_get(dg()).data.vertices), flush=True)
coll = bpy.data.collections.new("StackHelpers"); bpy.context.scene.collection.children.link(coll)
for c in list(helper.users_collection): c.objects.unlink(helper)
coll.objects.link(helper)
bpy.context.view_layer.layer_collection.children["StackHelpers"].exclude = True
print("Q1 helper in view layer:", helper.name in bpy.context.view_layer.objects, flush=True)
print("Q1 excluded-collection helper -> stack verts:", len(stack.evaluated_get(dg()).data.vertices), flush=True)
