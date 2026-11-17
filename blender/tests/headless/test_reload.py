"""Layer B: file-load lifecycle and plumbing repair."""

import os
import tempfile
import time
import unittest

import bpy

from harness import (
    free_port,
    isolate_data_root,
    pump,
    report_of,
    start_fake,
    stop_fake,
)
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import triggers as engine_triggers


class ReopenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.bridge as bridge
        import stageloom.blender.engine as engine

        cls.engine = engine
        cls.bridge = bridge

        cls.enterClassContext(isolate_data_root("stageloom-reload-test-"))
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = "Reload"
        bpy.ops.stageloom.add_stage(label="Reload")
        stage = obj.gh_stack.stages[0]
        cls.stage_key = stage.key
        # Save the file exactly as a save taken mid-request looks.
        stage.state = "waiting"
        cls.blend = os.path.join(tempfile.gettempdir(), "stageloom_reload_test.blend")
        bpy.ops.wm.save_as_mainfile(filepath=cls.blend)

        # Grasshopper is already connected before the file opens, so no new
        # hello arrives after the load: the ordering that exposed the bug.
        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_reload_report.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(cls.stage_key, cls.report, port=cls.port)
        deadline = time.time() + 5.0
        while time.time() < deadline and not bridge.state["connected"]:
            time.sleep(0.05)
        cls.connected = bridge.state["connected"]

        # A decoded frame from the old file's session sits in the queue when
        # the new file opens. It must neither be applied nor leak the schema
        # of the previous document into the new one.
        cls.stale_queued = 2
        bridge.post_incoming(
            {
                "type": "schema",
                "stage": cls.stage_key,
                "params": [
                    {
                        "id": "00000000-0000-4000-8000-0000000000aa",
                        "label": "Stale",
                        "type": "float",
                        "default": 1.0,
                    }
                ],
            },
            {},
        )
        bridge.post_incoming(
            {
                "type": "response",
                "stage": cls.stage_key,
                "rid": "sha256:stale-before-load",
                "status": "ok",
                "out_hash": "stale-before-load",
            },
            {},
        )
        bpy.ops.wm.open_mainfile(filepath=cls.blend)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_00_the_load_dropped_the_queued_frames(self):
        obj = bpy.data.objects.get("Reload")
        stage = obj.gh_stack.stages[0]
        self.assertEqual(
            self.bridge.INCOMING.qsize(),
            0,
            "the load kept %d old-session frame(s) queued" % self.stale_queued,
        )
        self.assertEqual(len(stage.params), 0, "a schema from the old file was applied")
        self.assertEqual(stage.applied_out_hash, "", "a response from the old file was applied")

    def test_01_engine_hooks_survive_the_load(self):
        self.assertTrue(
            bpy.app.timers.is_registered(engine_triggers._timer),
            "the engine timer was cleared by the file load",
        )
        self.assertIn(engine_triggers._on_depsgraph, bpy.app.handlers.depsgraph_update_post)
        self.assertIn(engine_triggers._on_frame_change, bpy.app.handlers.frame_change_post)
        self.assertIn(engine_triggers._on_load, bpy.app.handlers.load_post)

    def test_02_load_resets_waiting_and_marks_the_stack_dirty(self):
        obj = bpy.data.objects.get("Reload")
        self.assertIsNotNone(obj, "the saved object did not survive the reload")
        stage = obj.gh_stack.stages[0]
        self.assertNotEqual(stage.state, "waiting", "a mid-request save was not reset on load")
        self.assertIn("Reload", ENGINE.dirty, "the loaded stack was not marked dirty")

    def test_03_reopened_stage_talks_to_grasshopper(self):
        self.assertTrue(self.connected, "the fake never connected before the load")
        obj = bpy.data.objects.get("Reload")
        stage = obj.gh_stack.stages[0]
        self.assertTrue(
            pump(seconds=10.0, until=lambda: stage.state in ("ok", "error")),
            "the reopened stage never reached a result",
        )
        self.assertEqual(stage.state, "ok", stage.error)
        report = report_of(self.report)
        self.assertGreaterEqual(report.get("full", 0) + report.get("ref", 0), 1, report)


class RepairTests(unittest.TestCase):
    """Plumbing deleted before a save is rebuilt on load, and the stage is marked
    unbound until an answer arrives. Deleting the *modifier* is not
    repaired — that is removal, covered by test_stage_controls."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.bridge as bridge
        import stageloom.blender.engine as engine
        import stageloom.blender.helpers as helpers
        import stageloom.blender.nodes as nodes

        cls.engine = engine
        cls.bridge = bridge
        cls.helpers = helpers
        cls.nodes = nodes

        cls.enterClassContext(isolate_data_root("stageloom-repair-test-"))
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = "Repair"
        bpy.ops.stageloom.add_stage(label="Repair")
        cls.stage_key = obj.gh_stack.stages[0].key
        # A saved token is a claim about helper data from the old session. The
        # load and the plumbing repair both invalidate it.
        obj.gh_stack.stages[0].applied_topology = "sha256:" + "d" * 64

        # The file this test opens: helpers and the node group already gone.
        for kind in helpers.KINDS:
            helper = helpers.helper_for(obj, cls.stage_key, kind)
            if helper is not None:
                bpy.data.objects.remove(helper, do_unlink=True)
        group = bpy.data.node_groups.get(nodes.group_name(cls.stage_key))
        if group is not None:
            bpy.data.node_groups.remove(group)

        cls.blend = os.path.join(tempfile.gettempdir(), "stageloom_repair_test.blend")
        bpy.ops.wm.save_as_mainfile(filepath=cls.blend)

        # Grasshopper stays connected across the load, like the reopen test.
        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_repair_report.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(cls.stage_key, cls.report, port=cls.port)
        deadline = time.time() + 5.0
        while time.time() < deadline and not bridge.state["connected"]:
            time.sleep(0.05)
        cls.connected = bridge.state["connected"]

        bpy.ops.wm.open_mainfile(filepath=cls.blend)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_lost_plumbing_rebuilt_and_unbound(self):
        obj = bpy.data.objects.get("Repair")
        self.assertIsNotNone(obj, "the saved object did not survive the reload")
        stage = obj.gh_stack.stages[0]
        self.assertEqual(stage.state, "unbound", "a rebuilt stage was not marked unbound")
        self.assertEqual(
            stage.applied_topology, "", "a loaded/rebuilt helper kept its old token"
        )
        for kind in self.helpers.KINDS:
            self.assertIsNotNone(
                self.helpers.helper_for(obj, self.stage_key, kind),
                "helper %s was not rebuilt on load" % kind,
            )
        modifier = self.nodes.modifier_of(obj, stage)
        self.assertIsNotNone(modifier)
        self.assertIsNotNone(modifier.node_group, "the node group was not rebuilt on load")

    def test_02_an_answer_returns_it_to_ok(self):
        obj = bpy.data.objects.get("Repair")
        stage = obj.gh_stack.stages[0]
        self.assertTrue(self.connected, "the fake never connected before the load")
        self.assertTrue(
            pump(seconds=10.0, until=lambda: stage.state in ("ok", "error")),
            "the rebuilt stage never reached a result",
        )
        self.assertEqual(stage.state, "ok", stage.error)


if __name__ == "__main__":
    unittest.main()
