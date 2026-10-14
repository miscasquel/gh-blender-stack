"""Layer B, the prefix memo: it must see every input the evaluated prefix reads.

Run with:

    scripts/test-blender

The revision memo in `engine._prefix_revision` is trusted to describe everything
a prefix depends on. This file pins the two inputs that are easiest to leave out,
because each one cost a release:

1. A Geometry Nodes modifier's group inputs live in its stored input values
   (`modifier.properties.inputs`), which are ID properties, not RNA properties:
   the `bl_rna` walk in `_modifier_descriptor` cannot see them, and `keys()`
   raises on a Blender 5.2 modifier. Dragging one of those sliders left the
   revision - and so the memoized sections, the geometry hash, the request id
   and the frame cache - exactly where they were: the stage answered the same
   rid with the frame from before the edit and never asked Grasshopper anything.
2. A cache reset (Push) must retire the memoized prefix as well as the frame
   store, or the "recovery" re-sends the same geometry.

Retry, which asks again without discarding the frame cache, is covered here too.
"""

import os
import subprocess
import sys
import tempfile
import time
import json
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)

PORT = 9147
SCALE_PARAM = "00000000-0000-4000-8000-0000000000aa"


def pump(seconds=10.0, until=None):
    """Drive the engine timer directly: bpy.app.timers is inert under -b (S2)."""
    import gh_stack.blender.engine as engine

    deadline = time.time() + seconds
    while time.time() < deadline:
        engine._timer()
        if until is not None and until():
            return True
        time.sleep(0.005)
    return until is None


def start_fake(stage_key, report):
    return subprocess.Popen(
        [
            sys.executable,
            os.path.join(REPO, "tools", "fake_gh", "fake_gh.py"),
            "--port",
            str(PORT),
            "--stage",
            stage_key,
            "--report",
            report,
        ],
        env=dict(
            os.environ,
            PYTHONPATH=os.pathsep.join([os.path.join(REPO, "blender", "gh_stack", "vendor"), REPO]),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def stop_fake(process):
    if process is None or process.poll() is not None:
        return
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



def isolate_data_root():
    prefs = bpy.context.preferences.addons["gh_stack"].preferences
    prefs.data_root = tempfile.mkdtemp(prefix="gh-stack-memo-")


def gn_shift_group(name="MemoShift", shift=0.0, min_value=-20.0, max_value=20.0):
    """A Geometry Nodes modifier group that moves geometry by a slider input."""
    tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    socket = tree.interface.new_socket("Shift", in_out="INPUT", socket_type="NodeSocketFloat")
    socket.default_value = shift
    socket.min_value = min_value
    socket.max_value = max_value
    group_in = tree.nodes.new("NodeGroupInput")
    set_position = tree.nodes.new("GeometryNodeSetPosition")
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    group_out = tree.nodes.new("NodeGroupOutput")
    tree.links.new(group_in.outputs["Geometry"], set_position.inputs["Geometry"])
    tree.links.new(group_in.outputs["Shift"], combine.inputs["X"])
    tree.links.new(combine.outputs["Vector"], set_position.inputs["Offset"])
    tree.links.new(set_position.outputs["Geometry"], group_out.inputs["Geometry"])
    return tree


def set_gn_input(modifier, value, obj=None):
    """Write a stored input value the way the modifier panel does.

    The identifier is looked up by value type: the group input sockets are named
    ``Socket_N`` in storage, and the first one is the geometry input.

    A write into an ID-property group does not tag the depsgraph — the same fact
    `stack.notify_param_changed` works around for a dropdown pick — so the object
    is tagged explicitly here, which is what the UI's RNA write does by itself.

    The update is then evaluated: `-b` has no notifier loop, so without this the
    handler never runs and the stack is never marked dirty (the same reason
    `test_prefix` calls `view_layer.update()` after a mesh edit).
    """
    inputs = modifier.properties.inputs
    for key in inputs.keys():
        entry = inputs[key]
        if "value" not in entry:
            continue
        if isinstance(entry["value"], float) and key != "Socket_0":
            entry["value"] = value
            if obj is not None:
                obj.update_tag()
                bpy.context.view_layer.update()
            return key
    raise AssertionError("no float input stored on the modifier: %s" % (list(inputs.keys()),))


def float_inputs(modifier):
    """``{identifier: value}`` for every non-geometry stored input of a modifier."""
    out = {}
    inputs = modifier.properties.inputs
    for key in inputs.keys():
        entry = inputs[key]
        if "value" in entry and isinstance(entry["value"], float):
            out[key] = float(entry["value"])
    return out


class ModifierInputMemoTests(unittest.TestCase):
    """A slider on a prefix Geometry Nodes modifier must reach Grasshopper.

    The Geometry Nodes modifier is placed *before* the stage, so the prefix has
    to be evaluated (the memo path) rather than read from the base mesh.
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()
        import gh_stack.blender.engine as engine

        cls.engine = engine
        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Memo"

        cls.gn = cls.obj.modifiers.new("GNShift", "NODES")
        cls.gn.node_group = gn_shift_group()
        bpy.context.view_layer.update()

        bpy.ops.gh_stack.add_stage(label="AfterShift")
        cls.stage = cls.obj.gh_stack.stages[0]

        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_memo.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(cls.stage.key, cls.report)

        # Settle: the fake echoes the prefix scaled by the stage's Scale
        # parameter (default 1.0), so a prefix shifted by 10 comes back with a
        # maximum |x| of 11.
        def settled():
            return cls.stage.state == "ok" and report_of(cls.report).get("responses", 0) >= 1

        pump(seconds=15.0, until=settled)

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake)
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_01_the_modifier_input_is_not_rna(self):
        # The premise of the whole file: a stored input is invisible to a
        # `bl_rna` walk, and to `keys()` on the modifier itself.
        identifiers = set(p.identifier for p in self.gn.bl_rna.properties)
        self.assertNotIn("Shift", identifiers)
        self.assertFalse(any(name.startswith("Socket_") for name in identifiers))
        with self.assertRaises(TypeError):
            self.gn.keys()
        self.assertTrue(float_inputs(self.gn), "the modifier stores no float input")

    def test_02_a_stored_input_move_reaches_grasshopper(self):
        before = self.stage.rid
        set_gn_input(self.gn, 10.0, self.obj)

        def followed():
            report = report_of(self.report)
            first = report.get("received_first") or [None]
            return (
                self.stage.rid != before
                and self.stage.state == "ok"
                and first[0] is not None
                and abs(first[0] - 9.0) < 0.05
            )

        self.assertTrue(
            pump(seconds=10.0, until=followed),
            "moving a Geometry Nodes modifier input never reached Grasshopper: "
            "the prefix memo did not see it",
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        report = report_of(self.report)
        self.assertGreaterEqual(report.get("full", 0), 2, "the new geometry was not sent in full")
        # The cube's first vertex is at x = -1; +10 puts it at 9. (`received_max`
        # would read 11 for either direction, so the first vertex is the signal.)
        self.assertAlmostEqual(report.get("received_first", [None])[0], 9.0, places=2)

    def test_03_the_revision_sees_the_input(self):
        import gh_stack.blender.engine as engine

        record = bpy.context.object.gh_stack
        index = engine._modifier_index(self.obj, engine.nodes.modifier_for(self.obj, self.stage.key))
        first = engine._prefix_revision(self.obj, record, self.stage, index)
        set_gn_input(self.gn, -10.0, self.obj)
        second = engine._prefix_revision(self.obj, record, self.stage, index)
        self.assertIsNotNone(first)
        self.assertNotEqual(
            first,
            second,
            "the prefix revision ignored a stored modifier input",
        )
        set_gn_input(self.gn, 10.0, self.obj)
        self.assertEqual(
            engine._prefix_revision(self.obj, record, self.stage, index),
            first,
            "moving the input back did not restore the revision",
        )


class ResetRetiresMemoTests(unittest.TestCase):
    """Push and Retry must both carry the geometry the scene is showing.

    Push resets the frame cache; before it also retired the memoized prefix, a
    stage whose revision was stale could not be recovered by anything the
    add-on offered: the re-request went out with the same geometry and produced
    the same output.
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()
        import gh_stack.blender.engine as engine

        cls.engine = engine
        engine.start_server(PORT)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Reset"
        cls.gn = cls.obj.modifiers.new("GNShift", "NODES")
        cls.gn.node_group = gn_shift_group(name="ResetShift")
        bpy.ops.gh_stack.add_stage(label="Reset")
        cls.stage = cls.obj.gh_stack.stages[0]

        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_reset.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(cls.stage.key, cls.report)
        pump(
            seconds=15.0,
            until=lambda: cls.stage.state == "ok" and report_of(cls.report).get("responses", 0) >= 1,
        )

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake)
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def _seen(self, first_x):
        report = report_of(self.report)
        first = report.get("received_first") or [None]
        return first[0] is not None and abs(first[0] - first_x) < 0.05

    def test_01_push_carries_the_current_geometry(self):
        # A stage whose memoized prefix is stale: nothing else about the scene
        # changed, so before the memo was retired on reset this push re-sent the
        # same geometry and answered with the same output.
        set_gn_input(self.gn, 10.0, self.obj)
        self.engine.push(self.obj)
        self.assertTrue(
            pump(seconds=10.0, until=lambda: self._seen(9.0)),
            "Push did not carry the geometry the scene is showing",
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)

    def test_02_retry_keeps_the_frame_cache(self):
        """Retry is the non-destructive recovery: no frames are dropped."""
        store_before = sorted(
            name for name in os.listdir(_stage_dir(self.obj, self.stage)) if name.endswith(".bin")
        )
        nonce_before = self.stage.nonce
        set_gn_input(self.gn, -10.0, self.obj)
        bpy.ops.gh_stack.retry()
        self.assertGreater(self.stage.nonce, nonce_before, "Retry did not ask again")
        self.assertTrue(
            pump(seconds=10.0, until=lambda: self._seen(-11.0)),
            "Retry did not reach Grasshopper",
        )
        store_after = os.listdir(_stage_dir(self.obj, self.stage))
        for name in store_before:
            self.assertIn(
                name,
                store_after,
                "Retry discarded a cached frame; it must only ask again",
            )


def _stage_dir(obj, stage):
    """The stage's frame-cache directory on disk."""
    import gh_stack.blender.prefs as prefs
    import gh_stack.blender.stack as stack_mod

    record = stack_mod.stack_of(obj)
    return os.path.join(prefs.data_root(), "cache", record.stack_id, stage.key)


if __name__ == "__main__":
    unittest.main()
