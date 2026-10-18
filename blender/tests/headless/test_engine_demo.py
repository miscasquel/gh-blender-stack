"""Layer B, engine demo: the Blender side against tools/fake_gh.

Run with:

    scripts/test-blender

What it proves, headless:
1. The add-on registers and starts its server.
2. Add Stage creates a stage, a hidden helper, and a GN modifier.
3. A request goes out with full geometry; the response is applied to the helper.
4. The stack object's evaluated mesh shows the GH output through the modifier.
5. Changing a parameter sends a `ref` request (no geometry) and the result
   updates; the fake's report shows full=1 and ref>=1.
6. The modifier's own visibility toggles are the control: the engine reads them
   and never writes them back.
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

PORT = 9141


def pump(seconds=8.0, until=None):
    """Drive the engine timer directly: bpy.app.timers is inert under -b (S2)."""
    import gh_stack.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until is None


class EngineDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Clear the file first: read_factory_settings also resets preferences,
        # which would disable the add-on if it ran after addon_enable.
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        bpy.context.preferences.addons["gh_stack"].preferences.data_root = tempfile.mkdtemp(
            prefix="gh-stack-test-"
        )
        import gh_stack.blender.engine as engine
        import gh_stack.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.report_path = os.path.join(tempfile.gettempdir(), "fake_gh_report.json")
        if os.path.exists(cls.report_path):
            os.remove(cls.report_path)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Demo"

        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.gh_stack.add_stage(label="Demo")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.param_name = "gh/%s/%s" % (cls.stage.key, "00000000-0000-4000-8000-0000000000aa")

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
                    [os.path.join(REPO, "blender", "gh_stack", "vendor"), REPO]
                ),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # Wait for the connection and the schema, then for the first response.
        def ready():
            return cls.stage.state in ("ok", "error")

        pump(until=ready)
        cls.engine.push(cls.obj)
        pump(until=ready)

    @classmethod
    def tearDownClass(cls):
        cls.fake.terminate()
        try:
            cls.fake.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.fake.kill()
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def helper(self):
        import gh_stack.blender.helpers as helpers

        return helpers.ensure_helper(self.obj, self.stage.key)

    def helper_coords(self):
        import numpy as np

        mesh = self.helper().data
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        return co.reshape(-1, 3)

    def test_01_schema_created_the_param(self):
        self.assertIn(self.param_name, self.obj)
        self.assertEqual(len(self.stage.params), 3)
        self.assertEqual(self.stage.params[0].label, "Scale")
        # The vector parameter arrives through the same path and becomes a 3-array.
        vector_name = "gh/%s/%s" % (self.stage.key, "00000000-0000-4000-8000-0000000000bb")
        self.assertIn(vector_name, self.obj)
        self.assertEqual(len(self.obj[vector_name]), 3)
        self.assertAlmostEqual(self.obj[vector_name][2], 2.0, places=5)

    def test_01b_value_list_param_and_dropdown(self):
        """A choice parameter arrives with its labels, and picking from the
        dropdown writes the custom property and marks the object dirty (a raw
        Python write does not tag the depsgraph)."""
        choice_name = "gh/%s/%s" % (self.stage.key, "00000000-0000-4000-8000-0000000000cc")
        self.assertIn(choice_name, self.obj)
        entry = None
        for e in self.stage.params:
            if e.param_id == "00000000-0000-4000-8000-0000000000cc":
                entry = e
        self.assertIsNotNone(entry)
        self.assertEqual(entry.type, "value_list")
        self.assertEqual(entry.values, "Ribbon\nPanel\nGrid")
        self.assertEqual(entry.group, "Shape")

        self.engine._dirty.discard(self.obj.name)
        entry.value_index = "2"
        self.assertEqual(self.obj[choice_name], 2)
        self.assertIn(self.obj.name, self.engine._dirty, "dropdown pick did not mark the object dirty")

    def test_02_first_response_applied(self):
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        # Default scale is 1.0: the helper holds the cube unchanged.
        coords = self.helper_coords()
        self.assertEqual(len(coords), 8)
        self.assertAlmostEqual(float(abs(coords).max()), 1.0, places=5)
        self.assertEqual(self.stage.applied_out_hash[:7], "sha256:")

    def test_03_parameter_change_uses_ref_and_updates(self):
        self.obj[self.param_name] = 2.5
        self.engine.push(self.obj)

        def updated():
            return self.stage.state == "ok" and abs(float(abs(self.helper_coords()).max()) - 2.5) < 1e-3

        self.assertTrue(pump(until=updated), "helper did not reach the new scale")
        # Give the report a moment to include the last request.
        time.sleep(0.3)
        if os.path.exists(self.report_path):
            report = json.load(open(self.report_path))
            self.assertGreaterEqual(report["full"], 1)
            self.assertGreaterEqual(report["ref"], 1)

    def test_04_evaluated_object_shows_gh_output(self):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = self.obj.evaluated_get(depsgraph)
        mesh = evaluated.data
        import numpy as np

        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        # Through the modifier stack, the cube is scaled by the parameter.
        self.assertAlmostEqual(float(abs(co).max()), 2.5, places=4)

    def test_06_geometry_edit_updates_without_a_push(self):
        """Editing the base mesh must flow through without pressing Push."""
        mesh = self.obj.data
        before = self.helper_coords().copy()
        mesh.vertices[0].co.x += 1.0
        mesh.update()
        # The GUI gets this from the depsgraph when an edit ends; headless has to
        # ask for it explicitly.
        bpy.context.view_layer.update()

        def updated():
            coords = self.helper_coords()
            return abs(float(coords[0][0] - before[0][0])) > 0.5

        self.assertTrue(pump(until=updated), "geometry edit did not reach the helper")
        # The edit was scaled by the current parameter (2.5 from test_03).
        coords = self.helper_coords()
        self.assertAlmostEqual(float(coords[0][0] - before[0][0]), 2.5, places=3)

    def test_05_modifier_toggles_are_the_switch(self):
        """The three context toggles in the modifiers panel are the control, and
        the engine never writes them back (the old bug: every pass re-enabled
        realtime and render)."""
        import gh_stack.blender.nodes as nodes

        modifier = nodes.modifier_of(self.obj, self.stage)
        self.assertIsNotNone(modifier)
        # Muting one context keeps the stage active: render is still on.
        modifier.show_viewport = False
        self.engine._mark_object_dirty(self.obj)
        self.engine._timer()
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        self.assertTrue(self.stage.enabled, "one muted context disabled the stage")
        self.assertFalse(modifier.show_viewport, "the engine re-enabled the toggle")
        # All three off: disabled, and the toggles stay where they were put.
        modifier.show_render = False
        modifier.show_in_editmode = False
        self.engine._mark_object_dirty(self.obj)
        self.engine._timer()
        self.assertEqual(self.stage.state, "disabled")
        self.assertFalse(self.stage.enabled)
        self.assertFalse(modifier.show_render)
        self.assertFalse(modifier.show_in_editmode)
        # Back on: the cached frame replays.
        modifier.show_viewport = True
        modifier.show_render = True
        modifier.show_in_editmode = True
        self.engine._mark_object_dirty(self.obj)
        self.assertTrue(pump(until=lambda: self.stage.state == "ok"))


if __name__ == "__main__":
    unittest.main()
