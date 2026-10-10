import bpy
scene = bpy.context.scene; scene.frame_start, scene.frame_end = 1, 10
cube = bpy.data.objects.new("Stack", bpy.data.meshes.new("S")); bpy.context.collection.objects.link(cube)
cube["gh/p/Radius"] = 1.0
cube.keyframe_insert('["gh/p/Radius"]', frame=1)
cube["gh/p/Radius"] = 5.0; cube.keyframe_insert('["gh/p/Radius"]', frame=10)
act = cube.animation_data.action
print("A action attrs:", [a for a in ("fcurves","layers","slots") if hasattr(act,a)], flush=True)
try:
    print("A layers:", len(act.layers), "strips:", len(act.layers[0].strips), flush=True)
    cb = act.layers[0].strips[0].channelbags[0]
    print("A channelbag fcurves:", [f.data_path for f in cb.fcurves], flush=True)
except Exception as e: print("A layered API err", type(e).__name__, e, flush=True)
d = cube.driver_add('["gh/p/Radius"]')
print("D driver added", type(d).__name__, flush=True)
d.driver.type = "SCRIPTED"; d.driver.expression = "frame * 0.5"
cube.animation_data.action = None
scene.frame_set(4)
print("D driver value frame4 (2.0 if driver drives):", round(cube["gh/p/Radius"], 3), flush=True)
