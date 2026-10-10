import bpy
bpy.ops.mesh.primitive_plane_add(size=2); res = bpy.context.active_object
ng = bpy.data.node_groups.new("G", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
ng.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
mod = res.modifiers.new("GH", "NODES"); mod.node_group = ng
print("DBG props:", sorted(p.identifier for p in mod.bl_rna.properties if p.identifier not in ('rna_type',)))
print("DBG funcs:", [a for a in dir(mod) if not a.startswith('_')][:80])
print("DBG bl_rna:", mod.bl_rna.identifier, "has __getitem__:", hasattr(mod,'__getitem__'))
