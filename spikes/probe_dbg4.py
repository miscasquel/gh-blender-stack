import bpy
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name="H"
ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
ng.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
ng.interface.new_socket("Path", in_out="INPUT", socket_type="NodeSocketString")
mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
inp = mod.properties.inputs
print("DBG inputs type", type(inp).__name__, [a for a in dir(inp) if not a.startswith('_')][:30])
try:
    print("DBG inputs len", len(inp), [ (x.identifier if hasattr(x,'identifier') else x) for x in inp][:10])
except Exception as e: print("DBG iter err", e)
try:
    print("DBG src attrs", [a for a in dir(inp[0]) if not a.startswith('_')][:30])
except Exception as e: print("DBG idx err", e)
sysp = mod.properties.bl_system_properties_get()
print("DBG sys", type(sysp).__name__, list(sysp.keys())[:10] if hasattr(sysp,'keys') else sysp)
