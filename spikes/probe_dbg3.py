import bpy
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name="H"
ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
ng.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
p = mod.properties
print("DBG type", type(p).__name__, [a for a in dir(p) if not a.startswith('_')])
try:
    print("DBG items", [(k, type(v).__name__) for k, v in p.items()][:5] if hasattr(p,'items') else 'noitems')
except Exception as e: print("DBG items err", e)
try:
    print("DBG get", p.get("Socket_1"))
except Exception as e: print("DBG get err", e)
try:
    p["Socket_1"] = helper
    print("DBG set via properties ok", p["Socket_1"])
except Exception as e: print("DBG set err", type(e).__name__, e)
