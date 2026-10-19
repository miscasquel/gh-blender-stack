"""Layer B, the prefix memo: it must see every input the evaluated prefix reads.

Run with:

    scripts/test-blender

The memo is only allowed to answer while *nothing* has been tagged since its
sections were read (`engine._prefix_revision` holds the depsgraph generation).
This file pins the inputs that are easiest to believe "Blender wouldn't tag
that", because each one cost a release:

1. A Geometry Nodes modifier's group inputs live in its stored input values
   (`modifier.properties.inputs`), which are ID properties, not RNA properties:
   a `bl_rna` walk cannot see them, and `keys()` raises on a Blender 5.2
   modifier. Dragging one of those sliders left the revision - and so the
   memoized sections, the geometry hash, the request id and the frame cache -
   exactly where they were: the stage answered the same rid with the frame from
   before the edit and never asked Grasshopper anything.
2. A cache reset (Push) must retire the memoized prefix as well as the frame
   store, or the "recovery" re-sends the same geometry.
3. A prefix that reads another object must follow it: Blender tags the object
   that moved, and any tag moves the generation.
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
        first = engine._prefix_revision(self.obj, record)
        set_gn_input(self.gn, -10.0, self.obj)
        second = engine._prefix_revision(self.obj, record)
        self.assertIsNotNone(first)
        self.assertNotEqual(
            first,
            second,
            "the prefix revision ignored a stored modifier input",
        )
        set_gn_input(self.gn, 10.0, self.obj)
        self.assertNotEqual(
            engine._prefix_revision(self.obj, record),
            second,
            "moving the input again did not move the revision: a generation only grows",
        )


class ResetRetiresMemoTests(unittest.TestCase):
    """Push must carry the geometry the scene is showing.

    Push resets the frame cache; before it also retired the memoized prefix, a
    stale stage could not be recovered by anything the add-on offered: the
    re-request went out with the same geometry and produced the same output.
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


class ReferencedObjectTests(unittest.TestCase):
    """A prefix that reads *another object* must follow it.

    The bug this pins: the revision froze an Object reference as its name, so a
    prefix that read another object through a node tree — an Object Info node, or
    a modifier input of type Object or Collection — kept sending the geometry from
    before that object moved. Blender tags the object that moved; the tag moves
    the global depsgraph generation, and the memo moves with it.
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

        # Target: what the prefix reads. Holder: the stack object.
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.target = bpy.context.object
        cls.target.name = "Referenced"
        bpy.ops.mesh.primitive_plane_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Holder"
        bpy.context.view_layer.objects.active = cls.obj

        cls.tree = bpy.data.node_groups.new("Reads", "GeometryNodeTree")
        cls.tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        info = cls.tree.nodes.new("GeometryNodeObjectInfo")
        info.inputs["Object"].default_value = cls.target
        info.transform_space = "ORIGINAL"
        out = cls.tree.nodes.new("NodeGroupOutput")
        cls.tree.links.new(info.outputs["Geometry"], out.inputs["Geometry"])
        modifier = cls.obj.modifiers.new("Reads", "NODES")
        modifier.node_group = cls.tree

        bpy.ops.gh_stack.add_stage(label="Reads")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_referenced.json")
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

    @staticmethod
    def _depsgraph_event():
        """Run one real update through the handler, as a session would.

        `dg.updates` is only valid inside the handler, which is why the counters
        are moved there and not from the value side.
        """
        seen = []

        def capture(scene, depsgraph):
            seen.append(depsgraph)

        bpy.app.handlers.depsgraph_update_post.append(capture)
        try:
            bpy.context.view_layer.update()
        finally:
            bpy.app.handlers.depsgraph_update_post.remove(capture)
        if seen:
            import gh_stack.blender.engine as engine

            engine._bump_epochs(seen[-1])

    def test_01_a_referenced_object_move_reaches_grasshopper(self):
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        before = report_of(self.report).get("received_first") or [None]
        self.assertIsNotNone(before[0])

        # Move the referenced *geometry*, not the holder: this is the case where
        # Blender does not tag the holder at all, so only the reference's own
        # epoch can carry it.
        self.target.data.vertices[0].co.x += 7.0
        self.target.data.update()
        self._depsgraph_event()

        def followed():
            report = report_of(self.report)
            first = report.get("received_first") or [None]
            return (
                self.stage.state == "ok"
                and first[0] is not None
                and abs(first[0] - (before[0] + 7.0)) < 0.05
            )

        self.assertTrue(
            pump(seconds=10.0, until=followed),
            "the prefix kept reading the referenced object where it used to be: "
            "an Object reference must follow the object",
        )
        self.assertGreaterEqual(report_of(self.report).get("full", 0), 2)

    def test_02_the_generation_is_consulted(self):
        import gh_stack.blender.engine as engine

        record = self.obj.gh_stack
        before = engine._prefix_revision(self.obj, record)
        # Any depsgraph update retires every memo: the generation moves and the
        # revision with it, with no list of "what this prefix reads" to keep
        # correct. Blender tags the object that moved; the engine bumps once for
        # the whole graph.
        engine._bump_epochs(None)
        self.assertNotEqual(before, engine._prefix_revision(self.obj, record))


class UpstreamOutputTests(unittest.TestCase):
    """A GH -> GN -> GH chain: when the first stage's output is written, the
    stage after it must re-evaluate. The write happens inside a pass, so the tag
    it produces arrives after the next stage has already looked; the engine
    stamps the helper itself."""

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
        cls.obj.name = "Chain"
        bpy.ops.gh_stack.add_stage(label="First")
        cls.stage_a = cls.obj.gh_stack.stages[0]
        # A native modifier between the two stages, so the second one's prefix is
        # evaluated (the memo path) rather than read from the base.
        bevel = cls.obj.modifiers.new("Mid", "BEVEL")
        bevel.width = 0.1
        cls.obj.modifiers.active = bevel
        bpy.ops.gh_stack.add_stage(label="Second")
        cls.stage_b = cls.obj.gh_stack.stages[1]

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_the_engine_write_moves_the_downstream_revision(self):
        import gh_stack.blender.engine as engine

        record = self.obj.gh_stack
        before = engine._prefix_revision(self.obj, record)
        # What _apply does to a helper: the write, and the stamp that stands in
        # for the tag the depsgraph has not produced yet.
        engine._apply(
            self.obj,
            self.stage_a,
            {
                "mesh.verts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                "mesh.face_counts": [3],
                "mesh.face_indices": [0, 1, 2],
            },
        )
        self.assertNotEqual(
            before,
            engine._prefix_revision(self.obj, record),
            "the downstream stage did not see the upstream write",
        )


class WaitingAgeTests(unittest.TestCase):
    """`waiting_seconds` is what the diagnostics panel shows while a stage is
    outstanding. It has to read 0 when nothing is waiting — otherwise every stage
    would advertise a wait — and grow once the engine has asked for something."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="gh_stack")
        isolate_data_root()

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="gh_stack")

    def test_idle_stages_report_no_wait(self):
        import gh_stack.blender.engine as engine

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        bpy.ops.gh_stack.add_stage(label="Age")
        stage = bpy.context.object.gh_stack.stages[0]
        record = bpy.context.object.gh_stack
        self.assertEqual(engine.waiting_seconds(record.stack_id, stage.key), 0.0)
        self.assertEqual(
            engine.waiting_seconds("no-such-stack", "no-such-stage"),
            0.0,
            "an unknown stage must not look like a wait",
        )

    def test_a_stamp_ages(self):
        import gh_stack.blender.engine as engine

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        bpy.ops.gh_stack.add_stage(label="AgeB")
        stage = bpy.context.object.gh_stack.stages[0]
        record = bpy.context.object.gh_stack
        engine._state_since[(record.stack_id, stage.key)] = time.monotonic() - 4.0
        self.assertGreaterEqual(engine.waiting_seconds(record.stack_id, stage.key), 3.9)
        # A new registration throws the stamps away: a stamp from a previous
        # session would age a stage that is not waiting at all.
        engine._state_since.clear()
        self.assertEqual(engine.waiting_seconds(record.stack_id, stage.key), 0.0)


if __name__ == "__main__":
    unittest.main()
