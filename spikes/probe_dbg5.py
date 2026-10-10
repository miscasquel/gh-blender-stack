import bpy
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object
bpy.ops.mesh.primitive_cube_add(size=1); helper = bpy.context.active_object; helper.name="H"
ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
ng.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
ng.interface.new_socket("Path", in_out="INPUT", socket_type="NodeSocketString")
mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
sysp = mod.properties.bl_system_properties_get()
try:
    ins = sysp["inputs"]
    print("DBG sysp inputs type", type(ins).__name__, list(ins.keys()))
    ins["Socket_1"] = helper
    print("DBG set socket1 ok ->", ins["Socket_1"])
except Exception as e: print("DBG sysp err", type(e).__name__, e)
try:
    mod.properties.bl_system_properties_get()["inputs"]["Socket_2"] = "/tmp/x.ply"
    print("DBG path ok", mod.properties.bl_system_properties_get()["inputs"]["Socket_2"])
except Exception as e: print("DBG path err", type(e).__name__, e)
