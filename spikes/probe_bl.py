import sys, bpy
print("PYVER", sys.version.split()[0], "BLVER", bpy.app.version_string)
mt = bpy.types.Modifier.bl_rna.properties["type"].enum_items.keys()
print("MODIFIER TYPES", len(mt), "GN" , "NODES" in " ".join(mt) or "NODES", [m for m in mt if m in ("NODES",)])
for n in ["GeometryNodeObjectInfo","GeometryNodeImportPLY","GeometryNodeImportOBJ","GeometryNodeImportSTL","GeometryNodeImportCSV","GeometryNodeImportAlembic","GeometryNodeImportUSD"]:
    print("NODE", n, hasattr(bpy.types, n))
print("NodesModifier", hasattr(bpy.types, "NodesModifier"))
class CustomMod(bpy.types.Modifier):
    bl_idname = "THREEDM_TEST"
    bl_label = "t"
    bl_rna = None
try:
    bpy.utils.register_class(CustomMod)
    print("CUSTOM MODIFIER REGISTERED (unexpected)")
except Exception as e:
    print("CUSTOM MODIFIER REGISTER FAILED:", type(e).__name__, str(e)[:200])
