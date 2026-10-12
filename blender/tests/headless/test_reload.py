"""Layer B, file-load lifecycle: a reopened .blend keeps the engine alive.

Run with:

    scripts/test-blender

What it proves (the "reopened files do not connect" bug):

1. Blender clears non-persistent app handlers and timers on every file load.
   The engine's timer and handlers are persistent, so they survive and the
   engine is still ticking after `open_mainfile`.
2. A file load behaves like a reconnect: a stage saved mid-request is reset,
   and every loaded stack is marked dirty so it is evaluated against the cache
   and Grasshopper without waiting for an edit.
3. A stage in a reopened file actually talks to Grasshopper when the client was
   already connected before the load (no new `hello` arrives to save the day).
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

PORT = 9147


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


def start_fake(stage_key, report):
    return subprocess.Popen(
        [
            sys.executable,
            os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
            "--port", str(PORT),
            "--stage", stage_key,
            "--report", report,
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


def stop_fake(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def report_of(path):
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path))
    except ValueError:
        return {}


class ReopenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        import gh_stack.blender.bridge as bridge
        import gh_stack.blender.engine as engine

        cls.engine = engine
        cls.bridge = bridge

        prefs = bpy.context.preferences.addons["gh_stack"].preferences
        prefs.data_root = tempfile.mkdtemp(prefix="gh-stack-reload-test-")
        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = "Reload"
        bpy.ops.gh_stack.add_stage(label="Reload")
        stage = obj.gh_stack.stages[0]
        cls.stage_key = stage.key
        # Save the file exactly as a save taken mid-request looks.
        stage.state = "waiting"
        cls.blend = os.path.join(tempfile.gettempdir(), "gh_stack_reload_test.blend")
        bpy.ops.wm.save_as_mainfile(filepath=cls.blend)

        # Grasshopper is already connected before the file opens, so no new
        # hello arrives after the load: the ordering that exposed the bug.
        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_reload_report.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(cls.stage_key, cls.report)
        deadline = time.time() + 5.0
        while time.time() < deadline and not bridge.state["connected"]:
            time.sleep(0.05)
        cls.connected = bridge.state["connected"]

        bpy.ops.wm.open_mainfile(filepath=cls.blend)

    @classmethod
    def tearDownClass(cls):
        stop_fake(getattr(cls, "fake", None))
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_01_engine_hooks_survive_the_load(self):
        self.assertTrue(
            bpy.app.timers.is_registered(self.engine._timer),
            "the engine timer was cleared by the file load",
        )
        self.assertIn(self.engine._on_depsgraph, bpy.app.handlers.depsgraph_update_post)
        self.assertIn(self.engine._on_frame_change, bpy.app.handlers.frame_change_post)
        self.assertIn(self.engine._on_load, bpy.app.handlers.load_post)

    def test_02_load_resets_waiting_and_marks_the_stack_dirty(self):
        obj = bpy.data.objects.get("Reload")
        self.assertIsNotNone(obj, "the saved object did not survive the reload")
        stage = obj.gh_stack.stages[0]
        self.assertNotEqual(stage.state, "waiting", "a mid-request save was not reset on load")
        self.assertIn("Reload", self.engine._dirty, "the loaded stack was not marked dirty")

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


if __name__ == "__main__":
    unittest.main()
