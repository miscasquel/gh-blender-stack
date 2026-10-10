import bpy
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object
ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
s = ng.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
print("DBG identifiers:", [(i.item_type, getattr(i,'in_out',None), getattr(i,'identifier',None), getattr(i,'name',None)) for i in ng.interface.items_tree])
mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
print("DBG mod type", type(mod).__name__, "keys:", list(mod.keys()))
for ident in [i.identifier for i in ng.interface.items_tree if getattr(i,'in_out',None)=="INPUT"]:
    try:
        mod[ident] = bpy.data.objects.get("Cube") or res
        print("DBG set ok", ident, mod[ident])
    except Exception as e:
        print("DBG set fail", ident, e)
print("DBG has __setitem__ on Object?", hasattr(res, "__setitem__"))
