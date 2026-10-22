"""Layer B, the stage controls: Freeze, Pull, Key all, orphan purge, and the
modifier panel as the lifecycle surface (plan §9.2, §9.7, §9.8, §11).

Run with:

    scripts/test-blender

What it proves, headless:

1. Freeze stops requests and cache replay; the helper keeps its output.
2. Pull refreshes a frozen stage once, and the stage stays frozen.
3. Push skips frozen stages entirely (no generation bump, no request).
4. Key all keys every parameter at the current frame, with constant
   interpolation for int, bool and value list keys.
5. Parameters removed in Grasshopper keep their properties and keyframes until
   Purge orphans is pressed explicitly.
6. Deleting only the stage's node group rebuilds it; deleting its modifier
   removes the stage, its helpers and its parameter properties.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)

PORT = 9152


def pump(seconds=8.0, until=None):
    """Drive the engine timer directly: bpy.app.timers is inert under -b (S2)."""
    import stageloom.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until is None


class StageControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        bpy.context.preferences.addons["stageloom"].preferences.data_root = tempfile.mkdtemp(
            prefix="stageloom-controls-"
        )
        import stageloom.blender.engine as engine
        import stageloom.blender.nodes as nodes

        cls.engine = engine
        cls.nodes = nodes
        cls.report_path = os.path.join(tempfile.gettempdir(), "fake_gh_controls_report.json")
        if os.path.exists(cls.report_path):
            os.remove(cls.report_path)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Controls"

        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.stageloom.add_stage(label="Controls")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.param_name = "gh/%s/%s" % (cls.stage.key, "00000000-0000-4000-8000-0000000000aa")
        cls.choice_name = "gh/%s/%s" % (cls.stage.key, "00000000-0000-4000-8000-0000000000cc")

        cls.fake = subprocess.Popen(
            [
                sys.executable,
                os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
                "--port",
                str(PORT),
                "--stage",
                cls.stage.key,
                "--report",
                cls.report_path,
            ],
            env=dict(
                os.environ,
                PYTHONPATH=os.pathsep.join(
                    [os.path.join(REPO, "blender", "stageloom", "vendor"), REPO]
                ),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        def ready():
            return cls.stage.state in ("ok", "error")

        pump(until=ready)
        engine.push(cls.obj)
        pump(until=ready)

    @classmethod
    def tearDownClass(cls):
        cls.fake.terminate()
        try:
            cls.fake.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.fake.kill()
        bpy.ops.preferences.addon_disable(module="stageloom")

    def report_counts(self):
        time.sleep(0.3)
        if not os.path.exists(self.report_path):
            return (0, 0)
        report = json.load(open(self.report_path))
        return (report.get("full", 0), report.get("ref", 0))

    def helper_coords(self):
        import numpy as np
        import stageloom.blender.helpers as helpers

        mesh = helpers.ensure_helper(self.obj, self.stage.key).data
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        return co.reshape(-1, 3)

    def mark(self):
        self.engine._mark_object_dirty(self.obj)

    def test_01_freeze_stops_updates(self):
        before = self.helper_coords().copy()
        counts = self.report_counts()
        self.assertEqual(bpy.ops.stageloom.freeze(index=0), {"FINISHED"})
        self.assertEqual(self.stage.state, "frozen")

        self.obj[self.param_name] = 3.0
        self.mark()
        pump(seconds=1.0)
        self.assertEqual(self.stage.state, "frozen")
        after = self.helper_coords()
        self.assertAlmostEqual(
            float(abs(after - before).max()), 0.0, places=6, msg="the frozen output changed"
        )
        self.assertEqual(self.report_counts(), counts, "a frozen stage sent a request")

    def test_02_pull_refreshes_while_frozen(self):
        self.assertEqual(bpy.ops.stageloom.pull(index=0), {"FINISHED"})
        self.assertTrue(
            pump(until=lambda: abs(float(abs(self.helper_coords()).max()) - 3.0) < 1e-3),
            "Pull did not refresh the frozen stage",
        )
        self.assertEqual(self.stage.state, "frozen", "Pull unfroze the stage")

    def test_03_push_skips_frozen(self):
        generation = self.stage.generation
        counts = self.report_counts()
        bpy.ops.stageloom.push()
        self.assertEqual(self.stage.generation, generation, "Push touched a frozen stage")
        pump(seconds=0.8)
        self.assertEqual(self.stage.state, "frozen")
        self.assertEqual(self.report_counts(), counts, "Push sent a request for a frozen stage")

    def test_04_unfreeze_resumes(self):
        self.assertEqual(bpy.ops.stageloom.freeze(index=0), {"FINISHED"})  # toggles off
        self.assertTrue(pump(until=lambda: self.stage.state == "ok"), "unfreeze did not resume")
        self.assertAlmostEqual(float(abs(self.helper_coords()).max()), 3.0, places=3)

    def test_05_key_all_keys_every_parameter(self):
        import stageloom.blender.props as props

        bpy.context.scene.frame_set(10)
        self.assertEqual(bpy.ops.stageloom.key_all(index=0), {"FINISHED"})
        scale = '["%s"]' % self.param_name
        choice = '["%s"]' % self.choice_name
        paths = {fcurve.data_path for _bag, fcurve in props._fcurve_slots(self.obj)}
        self.assertIn(scale, paths, "the float parameter was not keyed")
        self.assertIn(choice, paths, "the value list parameter was not keyed")
        for _bag, fcurve in props._fcurve_slots(self.obj):
            if fcurve.data_path == choice:
                for point in fcurve.keyframe_points:
                    self.assertEqual(
                        point.interpolation,
                        "CONSTANT",
                        "a value list key interpolates",
                    )

    def test_06_orphans_are_kept_and_purge_removes_them(self):
        import stageloom.blender.props as props

        name = "gh/%s/00000000-0000-4000-8000-000000000099" % self.stage.key
        self.obj[name] = 1.5
        self.obj.keyframe_insert(data_path='["%s"]' % name, frame=1)
        self.assertIn(name, props.orphans(self.obj, self.stage))
        self.assertEqual(bpy.ops.stageloom.purge_orphans(), {"FINISHED"})
        self.assertNotIn(name, self.obj)
        paths = {fcurve.data_path for _bag, fcurve in props._fcurve_slots(self.obj)}
        self.assertNotIn('["%s"]' % name, paths, "purge left the orphan's keyframes")
        self.assertIn(self.param_name, self.obj, "purge removed a live parameter")

    def test_06b_deleting_only_the_group_rebuilds_it(self):
        group = bpy.data.node_groups.get(self.nodes.group_name(self.stage.key))
        self.assertIsNotNone(group)
        bpy.data.node_groups.remove(group)
        modifier = self.nodes.modifier_of(self.obj, self.stage)
        self.assertIsNone(modifier.node_group)

        self.mark()
        self.assertTrue(pump(until=lambda: modifier.node_group is not None))
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        self.assertEqual(
            modifier.node_group.name,
            self.nodes.group_name(self.stage.key),
        )

    def test_06c_a_pre_uid_record_survives_a_deleted_group(self):
        """A record from before the uid existed, whose group is already gone at
        the first pass: the modifier name is the last link, and the stage must
        be repaired, not removed."""
        stage = self.stage
        stage.modifier_uid = 0
        group = bpy.data.node_groups.get(self.nodes.group_name(stage.key))
        bpy.data.node_groups.remove(group)
        modifier = [m for m in self.obj.modifiers if m.type == "NODES"][0]
        self.assertIsNone(modifier.node_group)

        self.mark()
        self.assertTrue(
            pump(until=lambda: modifier.node_group is not None),
            "the pre-uid stage was not repaired",
        )
        self.assertEqual(len(self.obj.gh_stack.stages), 1, "the stage was removed")
        self.assertEqual(stage.modifier_uid, modifier.persistent_uid, "the uid was not re-adopted")
        self.assertEqual(stage.state, "ok", stage.error)

    def test_07_deleting_the_modifier_removes_the_stage(self):
        import stageloom.blender.helpers as helpers

        record = self.obj.gh_stack
        key = (record.stack_id, self.stage.key)
        # Seed the session maps a live stage can own; removal must clear them
        # rather than leave the cache and timing entries behind.
        self.engine._cache[key] = {"sha256:" + "0" * 64: (b"x", "")}
        self.engine._cache_bytes[key] = 1
        self.engine._state_since[key] = 0.0
        self.engine._timings[key] = {"prefix_ms": 1.0}
        modifier = self.nodes.modifier_of(self.obj, self.stage)
        helper_name = helpers.helper_name(self.obj, self.stage.key)
        self.obj.modifiers.remove(modifier)
        self.mark()
        self.assertTrue(
            pump(until=lambda: len(record.stages) == 0),
            "the stage survived its deleted modifier",
        )
        self.assertIsNone(bpy.data.objects.get(helper_name), "the helper survived removal")
        self.assertNotIn(self.param_name, self.obj, "the parameters survived removal")
        self.assertIsNone(
            bpy.data.node_groups.get(self.nodes.group_name(self.stage.key)),
            "the node group survived removal",
        )
        self.assertNotIn(key, self.engine._cache, "the hot cache entry survived removal")
        self.assertNotIn(key, self.engine._cache_bytes, "the cache accounting survived removal")
        self.assertNotIn(key, self.engine._state_since, "the wait stamp survived removal")
        self.assertNotIn(key, self.engine._timings, "the timings survived removal")


if __name__ == "__main__":
    unittest.main()
