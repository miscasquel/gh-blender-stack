"""Layer B, the stage index: message paths must not walk the whole blend.

Run with:

    scripts/test-blender

The engine used to scan `bpy.data.objects` on every response, schema heartbeat,
hello and error. The index is built once on register/load and patched as stages
change; these checks pin the lookup shape and the fact that a warm index is not
rebuilt by message handling.
"""

import unittest

import bpy


class StageIndexTests(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        import gh_stack.blender.engine as engine

        self.engine = engine
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        self.obj = bpy.context.object
        self.obj.name = "Indexed"
        bpy.ops.gh_stack.add_stage(label="Indexed")
        self.stage = self.obj.gh_stack.stages[0]

        # Unrelated objects make a full scan expensive, so a hidden rebuild is
        # measurable rather than a rounding error.
        mesh = bpy.data.meshes.new("index-dummy")
        for index in range(30):
            dummy = bpy.data.objects.new("index-dummy-%d" % index, mesh)
            bpy.context.scene.collection.objects.link(dummy)

    def tearDown(self):
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_a_warm_index_is_not_rebuilt_by_messages(self):
        engine = self.engine
        engine._rebuild_index()
        self.assertIn(self.stage.key, engine._stage_index)
        self.assertIn(self.obj, engine._stack_objects)
        before = engine.stats["index_rebuilds"]

        engine._handle_schema({
            "type": "schema",
            "stage": self.stage.key,
            "params": [],
        })
        engine._handle_response({
            "type": "response",
            "stage": self.stage.key,
            "rid": "sha256:" + "0" * 64,
            "status": "error",
            "error": {"code": "test", "message": "no"},
        }, {})
        engine._on_client_hello({
            "type": "hello",
            "doc": "index.gh",
            "stages": [{"key": self.stage.key}],
        })
        engine._last_stall_check = 0.0
        engine._nudge_stalled_waits()

        self.assertEqual(
            engine.stats["index_rebuilds"], before,
            "a message path rebuilt the whole-scene index",
        )

    def test_a_rename_does_not_lose_the_stack(self):
        engine = self.engine
        engine._rebuild_index()
        self.obj.name = "Indexed Renamed"
        engine._dirty.clear()
        engine._mark_dirty_all()
        self.assertIn("Indexed Renamed", engine._dirty)
        self.assertIn(self.obj, engine._stack_objects)

    def test_a_new_object_is_picked_up(self):
        engine = self.engine
        engine._rebuild_index()
        before = engine.stats["index_rebuilds"]
        extra = bpy.data.objects.new("index-new", bpy.data.meshes.new("index-new"))
        bpy.context.scene.collection.objects.link(extra)
        engine._ensure_index()
        self.assertGreater(engine.stats["index_rebuilds"], before)
        self.assertIn(self.obj, engine._stack_objects)


if __name__ == "__main__":
    unittest.main()
