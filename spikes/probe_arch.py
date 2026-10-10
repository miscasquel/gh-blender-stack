import bpy, bmesh, math, traceback
dg = lambda: bpy.context.evaluated_depsgraph_get()
def local_coords(obj):
    me = obj.evaluated_get(dg()).data
    return sorted(tuple(round(c, 4) for c in v.co) for v in me.vertices)
def set_mesh(obj, verts, faces):
    bm = bmesh.new()
    vs = [bm.verts.new(v) for v in verts]
    for f in faces: bm.faces.new([vs[i] for i in f])
    bm.to_mesh(obj.data); bm.free(); obj.data.update()

bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1); stack = bpy.context.active_object; stack.name = "Stack"
stack.rotation_euler = (0, 0, math.radians(90)); stack.location = (5, 0, 0)
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name = "GH_Helper"
helper.rotation_euler = (0, 0, 0); helper.location = (0, 0, 0); helper.scale = (1, 1, 1)
tri = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
set_mesh(helper, tri, [(0, 1, 2)])
print("P0 helper local:", sorted(tuple(round(c,4) for c in v.co) for v in helper.data.vertices), flush=True)

ng = bpy.data.node_groups.new("GH_Stage", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gi = ng.nodes.new("NodeGroupInput"); go = ng.nodes.new("NodeGroupOutput")
oi = ng.nodes.new("GeometryNodeObjectInfo")
oi.inputs[0].default_value = helper
print("P1 ObjectInfo props:", [p.identifier for p in oi.bl_rna.properties if 'transform' in p.identifier or 'space' in p.identifier], flush=True)
ng.links.new(oi.outputs["Geometry"], go.inputs["Geometry"])
m = stack.modifiers.new("GH", "NODES"); m.node_group = ng
print("P2 persistent_uid set:", bool(m.persistent_uid), m.persistent_uid, flush=True)
for mode in ("RELATIVE", "ORIGINAL"):
    try:
        oi.transform_space = mode
        print("P3", mode, "stack-local coords of output:", local_coords(stack), flush=True)
    except Exception as e:
        print("P3", mode, "ERR", e, flush=True)
oi.transform_space = "ORIGINAL"
print("P4 expected stack-local for helper data:", sorted(tuple(round(c,4) for c in v) for v in tri), flush=True)

bpy.ops.mesh.primitive_plane_add(size=1); extra = bpy.context.active_object; extra.name = "Extra"
m2 = stack.modifiers.new("Extra", "BEVEL")
uid_before = {x.name: x.persistent_uid for x in stack.modifiers}
with bpy.context.temp_override(object=stack, active_object=stack):
    bpy.ops.object.modifier_move_to_index(modifier="GH", index=len(stack.modifiers)-1)
uid_after = {x.name: x.persistent_uid for x in stack.modifiers}
print("P5 order after move:", [x.name for x in stack.modifiers], "uid stable on reorder:", uid_before["GH"]==uid_after["GH"], flush=True)
ghmods = [x for x in stack.modifiers if x.type=="NODES"]
print("P6 modifiers:", [(x.name, x.type) for x in stack.modifiers], flush=True)

clone = stack.copy()
bpy.context.scene.collection.objects.link(clone)
print("P7 clone modifiers:", [(x.name, x.persistent_uid==uid_after.get(x.name)) for x in clone.modifiers], flush=True)
print("P7b clone shares node group:", clone.modifiers["GH"].node_group == ng, flush=True)
clone.modifiers.remove(clone.modifiers["Extra"])
print("P8 clone prefix eval verts:", len(clone.evaluated_get(dg()).data.vertices), flush=True)
bpy.data.objects.remove(clone, do_unlink=True)

m3 = stack.modifiers.copy() if hasattr(stack.modifiers, "copy") else None
with bpy.context.temp_override(object=stack, active_object=stack):
    try:
        bpy.ops.object.modifier_copy(modifier="GH")
    except Exception as e:
        print("P9 modifier_copy ERR", e, flush=True)
dups = [x for x in stack.modifiers if x.name.startswith("GH")]
print("P9 dups:", [(x.name, x.persistent_uid != uid_after["GH"], x.node_group.name if x.node_group else None) for x in dups], flush=True)

helper.hide_viewport = True
oi.transform_space = "ORIGINAL"
print("P10 hidden helper stack verts:", len(stack.evaluated_get(dg()).data.vertices), local_coords(stack), flush=True)
coll = bpy.data.collections.new("StackHelpers"); bpy.context.scene.collection.children.link(coll)
coll.objects.link(helper); bpy.context.scene.collection.objects.unlink(helper)
lc = bpy.context.view_layer.layer_collection.children["StackHelpers"]; lc.exclude = True
print("P11 excluded-collection helper stack verts:", len(stack.evaluated_get(dg()).data.vertices), flush=True)
