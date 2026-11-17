"""Layer B: the live path — depsgraph triggers, Edit Mode, reconnect and undo."""

import os
import tempfile
import time
import unittest

import bpy
import bmesh
import numpy as np

from harness import (
    SCALE,
    free_port,
    helper_max,
    isolate_data_root,
    pump,
    report_of,
    settle,
    start_fake,
    stop_fake,
)
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import apply as engine_apply
from stageloom.blender.engine import messages as engine_messages
from stageloom.blender.engine import request as engine_request
from stageloom.blender.engine import triggers as engine_triggers
from stageloom.blender.engine import lifecycle as engine_lifecycle
from stageloom.blender import bridge, server


def first_x(report):
    first = report.get("received_first") or [None]
    return first[0]


class LiveSyncTestCase(unittest.TestCase):
    """A cube, one stage, one fake client on a private port."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-live-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Live"
        bpy.ops.stageloom.add_stage(label="Live")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.obj[stack_mod.param_prop_name(cls.stage.key, SCALE)] = 1.0

        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_live_%s.json" % cls.__name__)
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(cls.stage.key, cls.report, port=cls.port)

        def settled():
            return cls.stage.state == "ok" and report_of(cls.report).get("responses", 0) >= 1

        # settle re-arms lost requests and names the failure here.
        settle(engine, stack_mod, [cls.obj], settled)

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake)
        bpy.ops.preferences.addon_disable(module="stageloom")

    def edit_mode(self):
        bpy.context.view_layer.objects.active = self.obj
        self.obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")

    def leave_edit_mode(self):
        if self.obj.mode == "EDIT":
            bpy.ops.object.mode_set(mode="OBJECT")


class EditModeTests(LiveSyncTestCase):
    def tearDown(self):
        self.leave_edit_mode()

    def test_01_an_edit_mode_stroke_reaches_grasshopper(self):
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        # The cube's first vertex starts at x = -1.
        before = first_x(report_of(self.report))
        self.assertIsNotNone(before)

        self.edit_mode()
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += 5.0
        bmesh.update_edit_mesh(self.obj.data)

        # No mode exit, no manual tag: the timer drains, flushes and sends.
        def reached():
            report = report_of(self.report)
            x = first_x(report)
            return (
                self.stage.state == "ok"
                and x is not None
                and abs(x - (before + 5.0)) < 0.05
            )

        self.assertTrue(
            pump(seconds=15.0, until=reached),
            "an Edit Mode stroke never reached Grasshopper (state=%s, error=%r, report=%s)"
            % (self.stage.state, self.stage.error, report_of(self.report)),
        )
        self.assertEqual(self.obj.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")
        self.assertEqual(self.stage.state, "ok", self.stage.error)


class RevertedEditTests(LiveSyncTestCase):
    """A cancel, undo or reverted parameter still reaches Grasshopper.

    A transform is cancelled by restoring the BMesh and tagging it; undo
    restores the mesh and the saved stage record from its step; a slider returns
    to a value used before. The client still holds the intermediate state, so
    the reverted request must go out even though its request id appeared
    earlier in the session.
    """

    def tearDown(self):
        self.leave_edit_mode()

    def settle(self):
        return pump(
            seconds=15.0,
            until=lambda: not ENGINE.dirty and self.stage.state == "ok",
        )

    def set_first_x(self, value):
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x = value
        bmesh.update_edit_mesh(self.obj.data)

    def test_01_a_cancelled_transform_snaps_grasshopper_back(self):
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        self.edit_mode()
        self.assertTrue(self.settle(), "entering Edit Mode never settled")
        base = first_x(report_of(self.report))
        self.assertIsNotNone(base)
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.verts.ensure_lookup_table()
        original = float(bm.verts[0].co.x)

        self.set_first_x(original + 5.0)
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report)) is not None
                and abs(first_x(report_of(self.report)) - (base + 5.0)) < 0.05,
            ),
            "the moved vertex never reached Grasshopper (report=%s)"
            % report_of(self.report),
        )
        self.assertTrue(self.settle(), "the moved state never settled")
        sends = ENGINE.stats()["sends"]

        # Esc: the operator restores the BMesh to its pre-transform values and
        # tags the mesh; the reverted state must reach Grasshopper again.
        self.set_first_x(original)
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report)) is not None
                and abs(first_x(report_of(self.report)) - base) < 0.05
                and self.stage.state == "ok"
                and not ENGINE.dirty,
            ),
            "a cancelled transform did not reach Grasshopper; it still holds the "
            "moved geometry (report=%s)" % report_of(self.report),
        )
        self.assertGreater(
            ENGINE.stats()["sends"],
            sends,
            "the revert did not reach Grasshopper",
        )
        self.assertEqual(self.obj.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")
        self.assertEqual(self.stage.state, "ok", self.stage.error)

    def test_02_a_reverted_parameter_reaches_grasshopper(self):
        name = self.stack_mod.param_prop_name(self.stage.key, SCALE)
        self.obj[name] = 2.0
        ENGINE.mark_object_dirty(self.obj)
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: abs(helper_max(self.obj, self.stage.key) - 2.0) < 0.01
                and self.stage.state == "ok",
            ),
            "the moved parameter never applied (helper max %r)"
            % helper_max(self.obj, self.stage.key),
        )

        refs = report_of(self.report).get("ref", 0)
        self.obj[name] = 1.0
        ENGINE.mark_object_dirty(self.obj)
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: abs(helper_max(self.obj, self.stage.key) - 1.0) < 0.01
                and self.stage.state == "ok"
                and report_of(self.report).get("ref", 0) > refs,
            ),
            "a reverted parameter did not reach Grasshopper "
            "(report=%s, helper max %r)"
            % (report_of(self.report), helper_max(self.obj, self.stage.key)),
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)

    def test_03_undo_reasserts_the_client_geometry_bookkeeping(self):
        # Undo rolls the saved stage record back to its undo step, including
        # `geom_hash`; the connected client still holds the moved geometry. The
        # undo handler must put the session truth back, so the first request is
        # a full for what GH actually holds instead of a ref it must refuse.
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        self.edit_mode()
        self.assertTrue(self.settle(), "entering Edit Mode never settled")
        base = first_x(report_of(self.report))
        self.assertIsNotNone(base)
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.verts.ensure_lookup_table()
        original = float(bm.verts[0].co.x)
        before_edit_hash = self.stage.geom_hash
        before_edit_rid = self.stage.rid

        self.set_first_x(original + 5.0)
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report)) is not None
                and abs(first_x(report_of(self.report)) - (base + 5.0)) < 0.05,
            ),
            "the moved vertex never reached Grasshopper (report=%s)"
            % report_of(self.report),
        )
        self.assertTrue(self.settle(), "the moved state never settled")
        report = report_of(self.report)
        fulls = report.get("full", 0)
        refusals = report.get("refusals", 0)

        # What Ctrl+Z restores: the BMesh, and the stage fields from the step.
        self.set_first_x(original)
        self.stage.geom_hash = before_edit_hash
        self.stage.rid = before_edit_rid
        engine_triggers._on_undo()

        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report)) is not None
                and abs(first_x(report_of(self.report)) - base) < 0.05
                and self.stage.state == "ok"
                and not ENGINE.dirty,
            ),
            "the undo never reached Grasshopper (report=%s)" % report_of(self.report),
        )
        self.assertGreaterEqual(
            report_of(self.report).get("full", 0),
            fulls + 1,
            "the undo sent a ref for geometry Grasshopper did not hold",
        )
        self.assertEqual(
            report_of(self.report).get("refusals", 0),
            refusals,
            "the undo made Grasshopper refuse a stale ref (report=%s)"
            % report_of(self.report),
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)


class MultiObjectEditTests(unittest.TestCase):
    """Every object in Edit Mode must be flushed, not only the active one.

    Blender supports multi-object editing: `bpy.context.edit_object` is only the
    *active* edit object, and every other selected object carries its own BMesh
    edits next to it. The old flush copied one object per tick, so a stroke on a
    non-active stack object never changed the datablock the prefix clone read:
    the geometry hash stayed put and the pass looked like no edit at all. A
    non-stack object being active had the same effect one step earlier — the
    flush early-returned before it looked at any other edit object.

    Two stack objects (A, B) and one plain object (C) live in the scene; a
    stroke lands on B, the object that is *not* active. The fake for B's stage
    is the witness: it must receive the changed coordinate while the mode is
    still open.
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-live-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

        cls.obj_a = cls._stack_object("MultiA", "A")
        cls.stage_a = cls.obj_a.gh_stack.stages[0]
        cls.obj_b = cls._stack_object("MultiB", "B")
        cls.stage_b = cls.obj_b.gh_stack.stages[0]

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj_c = bpy.context.object
        cls.obj_c.name = "PlainC"

        cls.report_a = os.path.join(tempfile.gettempdir(), "fake_gh_multi_a.json")
        cls.report_b = os.path.join(tempfile.gettempdir(), "fake_gh_multi_b.json")
        for path in (cls.report_a, cls.report_b):
            if os.path.exists(path):
                os.remove(path)
        # Both stages share the port: each fake answers only its own stage.
        cls.fake_a = start_fake(cls.stage_a.key, cls.report_a, port=cls.port)
        cls.fake_b = start_fake(cls.stage_b.key, cls.report_b, port=cls.port)

        def settled():
            return (
                cls.stage_a.state == "ok"
                and cls.stage_b.state == "ok"
                and report_of(cls.report_a).get("responses", 0) >= 1
                and report_of(cls.report_b).get("responses", 0) >= 1
            )

        # The same transition window the single-object suite documents: settle,
        # re-arming quiet requests, and fail here if the fakes never answer.
        settle(engine, stack_mod, [cls.obj_a, cls.obj_b], settled)

    @classmethod
    def _stack_object(cls, name, label):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = name
        bpy.ops.stageloom.add_stage(label=label)
        return obj

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake_a)
        stop_fake(cls.fake_b)
        bpy.ops.preferences.addon_disable(module="stageloom")

    def tearDown(self):
        self.leave_edit_mode()

    def enter_edit(self, objects, active):
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        bpy.ops.object.mode_set(mode="EDIT")

    def leave_edit_mode(self):
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

    def settle(self):
        """Let the entry tag and every answer land, so a test measures only the
        stroke it makes: both stages at rest and nothing marked dirty."""
        return pump(
            seconds=15.0,
            until=lambda: not ENGINE.dirty
            and self.stage_a.state == "ok"
            and self.stage_b.state == "ok",
        )

    def stroke(self, obj, delta):
        """Move the first vertex of `obj` by `delta` in x, in the BMesh.

        The datablock is deliberately not touched: only `update_from_editmode`
        may copy the edit into it, which is exactly what the engine has to do.
        """
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += delta
        bmesh.update_edit_mesh(obj.data)

    def flush_lines(self):
        return [
            line
            for line in ENGINE.recent_activity(200)
            if line.startswith("flushed Edit Mode data")
        ]

    def test_01_a_stroke_on_the_non_active_edit_object_reaches_gh(self):
        self.enter_edit([self.obj_a, self.obj_b], active=self.obj_a)
        self.assertTrue(self.settle(), "entering Edit Mode never settled")
        before = first_x(report_of(self.report_b))
        self.assertIsNotNone(before)
        sends = ENGINE.stats()["sends"]

        self.stroke(self.obj_b, 5.0)

        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report_b)) is not None
                and abs(first_x(report_of(self.report_b)) - (before + 5.0)) < 0.05,
            ),
            "a stroke on the non-active Edit Mode object never reached Grasshopper "
            "(report=%s)" % report_of(self.report_b),
        )
        self.assertGreater(ENGINE.stats()["sends"], sends)
        self.assertTrue(
            any(
                "flushed Edit Mode data for %s" % self.obj_b.name in line
                for line in self.flush_lines()
            ),
            "the non-active object carried no flush line: %s" % self.flush_lines(),
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage_b.state == "ok"),
            "the stage never settled (state=%s, error=%s)"
            % (self.stage_b.state, self.stage_b.error),
        )
        self.assertEqual(self.obj_b.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")
        self.assertEqual(self.stage_b.state, "ok", self.stage_b.error)

    def test_02_a_stack_object_behind_a_non_stack_active_is_flushed(self):
        self.enter_edit([self.obj_b, self.obj_c], active=self.obj_c)
        self.assertTrue(self.settle(), "entering Edit Mode never settled")
        before = first_x(report_of(self.report_b))
        self.assertIsNotNone(before)

        self.stroke(self.obj_b, 5.0)

        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report_b)) is not None
                and abs(first_x(report_of(self.report_b)) - (before + 5.0)) < 0.05,
            ),
            "a stack object in Edit Mode was ignored because the active object "
            "has no stack (report=%s)" % report_of(self.report_b),
        )
        self.assertEqual(self.obj_c.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")

    def test_03_push_flushes_the_target_before_it_builds_the_request(self):
        self.enter_edit([self.obj_a, self.obj_b], active=self.obj_a)
        self.assertTrue(self.settle(), "entering Edit Mode never settled")
        report = report_of(self.report_b)
        before = first_x(report)
        self.assertIsNotNone(before)
        full_before = report.get("full", 0)

        self.stroke(self.obj_b, 3.0)
        # Deliberately no pump here: Push itself must copy the edit into the
        # datablock, or the request goes out as a `ref` with the stale hash and
        # Grasshopper never hears about the stroke.
        self.engine.push(self.obj_b)

        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report_b)) is not None
                and abs(first_x(report_of(self.report_b)) - (before + 3.0)) < 0.05,
            ),
            "Push sent a ref with the stale hash instead of the edited geometry "
            "(report=%s)" % report_of(self.report_b),
        )
        self.assertGreaterEqual(
            report_of(self.report_b).get("full", 0),
            full_before + 1,
            "Push did not send the geometry in full (report=%s)" % report_of(self.report_b),
        )
        self.assertEqual(self.obj_b.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")

    def test_04_an_idle_edit_mode_session_does_not_flush(self):
        self.enter_edit([self.obj_a, self.obj_b], active=self.obj_a)
        self.assertTrue(self.settle(), "entering Edit Mode never settled")

        before = len(self.flush_lines())
        for _ in range(10):
            engine_triggers._timer()
            time.sleep(0.01)

        self.assertEqual(
            len(self.flush_lines()),
            before,
            "an idle Edit Mode session kept copying meshes: %s" % self.flush_lines(),
        )
        self.assertFalse(ENGINE.dirty, "an idle pass left objects dirty")


class MultiObjectGhEditTests(unittest.TestCase):
    """A Grasshopper-side edit that changes two stages reaches both objects.

    The reported failure is the Grasshopper half of this path: a request-driven
    solve recomputes a dependent Stage Out through a shared source but only the
    requested stage answers, so Blender keeps the second object's old output.
    This suite is the receiving contract the plugin fix relies on: two stack
    objects with distinct keys, two fake clients, and an unsolicited answer for
    each stage carrying that stage's own current rid. Blender must apply both.
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-live-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

        cls.obj_a = cls._stack_object("GhEditA", "A")
        cls.stage_a = cls.obj_a.gh_stack.stages[0]
        cls.obj_b = cls._stack_object("GhEditB", "B")
        cls.stage_b = cls.obj_b.gh_stack.stages[0]

        cls.report_a = os.path.join(tempfile.gettempdir(), "fake_gh_edit_a.json")
        cls.report_b = os.path.join(tempfile.gettempdir(), "fake_gh_edit_b.json")
        for path in (cls.report_a, cls.report_b):
            if os.path.exists(path):
                os.remove(path)
        cls.fake_a = start_fake(cls.stage_a.key, cls.report_a, port=cls.port)
        cls.fake_b = start_fake(cls.stage_b.key, cls.report_b, port=cls.port)

        def settled():
            return (
                cls.stage_a.state == "ok"
                and cls.stage_b.state == "ok"
                and report_of(cls.report_a).get("responses", 0) >= 1
                and report_of(cls.report_b).get("responses", 0) >= 1
            )

        settle(engine, stack_mod, [cls.obj_a, cls.obj_b], settled)

    @classmethod
    def _stack_object(cls, name, label):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.object
        obj.name = name
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.stageloom.add_stage(label=label)
        stage = obj.gh_stack.stages[0]
        obj[cls.stack_mod.param_prop_name(stage.key, SCALE)] = 1.0
        return obj

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake_a)
        stop_fake(cls.fake_b)
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _send_changed_answer(self, obj, stage, scale):
        """One Stage Out answer for its current rid, as a GH-side edit sends:
        no request is in flight, and the output differs from the applied one."""
        import stageloom.blender.bridge as bridge

        verts = np.array(
            [[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1]], dtype=np.float32
        ) * scale
        sections = {
            "mesh.verts": verts,
            "mesh.face_counts": np.array([4], dtype=np.uint32),
            "mesh.face_indices": np.array([0, 1, 2, 3], dtype=np.uint32),
        }
        header = {
            "type": "response",
            "stage": stage.key,
            "rid": stage.rid,
            "out_hash": "sha256:gh-edit-%s-%s" % (stage.key, scale),
            "status": "ok",
            "warnings": [],
        }
        bridge.post_incoming(header, sections)

    def test_01_one_gh_edit_applies_to_every_affected_stage(self):
        self.assertEqual(self.stage_a.state, "ok", self.stage_a.error)
        self.assertEqual(self.stage_b.state, "ok", self.stage_b.error)
        self.assertAlmostEqual(helper_max(self.obj_a, self.stage_a.key), 1.0, places=3)
        self.assertAlmostEqual(helper_max(self.obj_b, self.stage_b.key), 1.0, places=3)

        # The fixed Stage Out sends the dependent stage's changed answer too.
        self._send_changed_answer(self.obj_a, self.stage_a, 2.0)
        self._send_changed_answer(self.obj_b, self.stage_b, 3.0)
        engine_messages._handle_incoming()

        self.assertAlmostEqual(
            helper_max(self.obj_a, self.stage_a.key), 2.0, places=3,
            msg="the requested stage's changed answer did not apply",
        )
        self.assertAlmostEqual(
            helper_max(self.obj_b, self.stage_b.key), 3.0, places=3,
            msg="the dependent stage's changed answer did not apply to its object",
        )


class PendingUpdateTests(LiveSyncTestCase):
    def test_01_a_tagged_edit_is_drained_before_the_pass(self):
        # The write tags the depsgraph; nothing has evaluated yet (no
        # `view_layer.update()` here). One timer pass must deliver it to the
        # handler and send — a clone earlier in the pass would otherwise mute
        # the handler and lose the edit.
        sends = ENGINE.stats()["sends"]
        self.obj.data.vertices[0].co.x += 1.0
        engine_triggers._timer()
        self.assertGreater(
            ENGINE.stats()["sends"],
            sends,
            "a pending depsgraph update was swallowed by the pass",
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: abs(first_x(report_of(self.report)) - 0.0) < 0.05),
            "the drained edit never reached Grasshopper (report=%s)" % report_of(self.report),
        )

    def test_02_an_all_dirty_pass_does_not_drain_twice(self):
        """The clone's own evaluation sees a pending edit when every stack is
        queued; forcing a second evaluation first is redundant and is the cost
        the viewport feels during a drag."""
        self.assertTrue(pump(seconds=15.0, until=lambda: self.stage.state == "ok"))
        ENGINE.dirty.clear()
        before = first_x(report_of(self.report))
        sends = ENGINE.stats()["sends"]
        drains = []
        real_background = engine_triggers._in_background
        real_drain = engine_triggers._drain_depsgraph
        engine_triggers._in_background = lambda: False
        engine_triggers._drain_depsgraph = lambda: drains.append(True)
        try:
            engine_lifecycle._mark_dirty_all()
            self.obj.data.vertices[0].co.x += 1.0
            # Deliberately no `view_layer.update()`: the edit is still pending.
            engine_triggers._timer()
            self.assertEqual(drains, [], "the all-dirty pass forced a redundant evaluation")
            self.assertGreater(ENGINE.stats()["sends"], sends, "the pending edit was not sent")
            self.assertTrue(
                pump(
                    seconds=15.0,
                    until=lambda: first_x(report_of(self.report)) is not None
                    and abs(first_x(report_of(self.report)) - (before + 1.0)) < 0.05,
                ),
                "the pending edit never reached Grasshopper (report=%s)"
                % report_of(self.report),
            )
        finally:
            engine_triggers._in_background = real_background
            engine_triggers._drain_depsgraph = real_drain


class ReconnectVerifyTests(LiveSyncTestCase):
    def test_01_a_reconnect_asks_again(self):
        pump(seconds=10.0, until=lambda: self.stage.state == "ok")
        self.assertTrue(self.stage.rid, "the stage has no delivered request to verify")

        sends = ENGINE.stats()["sends"]
        engine_messages._on_client_hello(
            {"doc": "verify.gh", "stages": [{"key": self.stage.key}]}
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: ENGINE.stats()["sends"] > sends),
            "a reconnect did not ask Grasshopper again",
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the verification request was never answered (report=%s)" % report_of(self.report),
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)


class EvaluatedHeadTests(LiveSyncTestCase):
    """No fake, no timer: the prefix read itself must be the evaluated object."""

    def test_01_shape_keys_are_part_of_a_head_of_stack_prefix(self):

        key = self.obj.shape_key_add(name="Basis")
        self.assertIsNotNone(key)
        moved = self.obj.shape_key_add(name="Moved")
        moved.data[0].co.x += 5.0
        moved.value = 1.0
        bpy.context.view_layer.update()
        engine_lifecycle._forget_prefix(self.obj, self.stage.key)

        record = self.stack_mod.stack_of(self.obj)
        sections, _hash, _attributes, _definitions = engine_request._prefix_sections(
            self.obj, record, self.stage
        )
        verts = dict((name, values) for name, _dtype, values in sections).get("mesh.verts")
        self.assertIsNotNone(verts, "the prefix carried no mesh.verts")
        x = float(np.asarray(verts).reshape(-1, 3)[0][0])
        self.assertAlmostEqual(
            x, 4.0, places=2,
            msg="a head-of-stack prefix read the base mesh, not the evaluated shape key (x=%r)" % x,
        )


class GuardWatchdogTests(LiveSyncTestCase):
    """A guard left up by a dead pass must not mute the engine."""

    def tearDown(self):
        self.leave_edit_mode()

    def test_01_a_stuck_guard_is_cleared_and_the_edit_still_sends(self):
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        sends = ENGINE.stats()["sends"]

        # A guard a dead pass left up. No tick is running, so this is the wedge.
        ENGINE.evaluating = True
        ENGINE.guard_owner = (self.obj.name, self.stage.key)
        ENGINE.evaluating_since = time.monotonic()

        # An edit made while the guard is up: the handler must drop it (that is
        # the mute) and say so in the trace.
        epoch = ENGINE.prefix_epoch
        self.obj.data.vertices[0].co.x += 2.0
        bpy.context.view_layer.update()
        self.assertEqual(ENGINE.prefix_epoch, epoch, "a dropped update still moved the epoch")
        self.assertTrue(
            any("guard up" in line for line in ENGINE.recent_activity(50)),
            "the skipped update left no trace: %s" % ENGINE.recent_activity(50),
        )

        # One tick must notice the guard, clear it, explain it, and pick up the
        # edit that was dropped.
        engine_triggers._timer()
        self.assertFalse(ENGINE.evaluating, "the watchdog did not clear the guard")
        watch = [line for line in ENGINE.recent_activity(50) if "guard watchdog" in line]
        self.assertEqual(len(watch), 1, "no single watchdog line: %s" % ENGINE.recent_activity(50))
        self.assertIn(self.obj.name, watch[0])
        self.assertIn(self.stage.key, watch[0])
        self.assertGreater(
            ENGINE.stats()["sends"],
            sends,
            "the dropped edit was not sent after the watchdog cleared the guard",
        )
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the recovered stage never settled (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )

    def test_02_a_cleanup_raise_clears_the_guard_without_the_watchdog(self):
        import stageloom.blender.bridge as bridge

        self.assertEqual(self.stage.state, "ok", self.stage.error)
        self.edit_mode()
        watchdog_lines = sum(
            "guard watchdog" in line for line in ENGINE.recent_activity(200)
        )

        real_drop = engine_request._drop_owned_data

        def failing_drop(id_block):
            raise RuntimeError("test: cleanup fault")

        engine_request._drop_owned_data = failing_drop
        try:
            bm = bmesh.from_edit_mesh(self.obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts[0].co.x += 4.0
            bmesh.update_edit_mesh(self.obj.data)
            # One pass, driven directly: the cleanup fault must fail the pass
            # but the guard must already be down when `_timer` returns.
            ENGINE.mark_object_dirty(self.obj)
            engine_triggers._timer()
        finally:
            engine_request._drop_owned_data = real_drop

        self.assertFalse(
            ENGINE.evaluating, "a cleanup raise left the prefix guard up"
        )
        self.assertEqual(
            sum("guard watchdog" in line for line in ENGINE.recent_activity(200)),
            watchdog_lines,
            "the watchdog, not the nested finally, cleared the guard",
        )
        self.assertIn(
            "cleanup fault",
            bridge.state["error"],
            "the cleanup failure was not recorded: %r" % (bridge.state["error"],),
        )

        # The next edit is processed and reaches Grasshopper: the earlier +4
        # stroke was never sent, so the fake must see the whole current mesh.
        sends = ENGINE.stats()["sends"]
        bm = bmesh.from_edit_mesh(self.obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.x += 1.0
        bmesh.update_edit_mesh(self.obj.data)
        bpy.context.view_layer.update()
        self.assertTrue(
            pump(seconds=15.0, until=lambda: ENGINE.stats()["sends"] > sends),
            "the first edit after a cleanup failure was not sent",
        )
        # The pump's pass flushed Edit Mode into the datablock, so the expected
        # value is readable here (the class's earlier tests moved the vertex
        # too; an absolute would depend on test order).
        expected = float(self.obj.data.vertices[0].co.x)
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: first_x(report_of(self.report)) is not None
                and abs(first_x(report_of(self.report)) - expected) < 0.05,
            ),
            "the edit after recovery never reached Grasshopper (report=%s)"
            % report_of(self.report),
        )
        self.assertEqual(self.obj.mode, "EDIT", "the test left Edit Mode; the point was to stay in it")


class BatchIsolationTests(LiveSyncTestCase):
    """A bad frame must not eat the messages behind it.

    `_handle_incoming` drains the queue into a list, so a raise in `_apply` must
    not abort the pass. The failing message is reported, the stage leaves
    `waiting`, is marked dirty and is armed to ask again; the next message in the
    same batch is still handled.
    """

    def test_01_one_bad_response_does_not_eat_the_batch(self):
        import stageloom.blender.bridge as bridge
        import stageloom.blender.server as server

        engine = self.engine
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        record = self.stack_mod.stack_of(self.obj)

        # Put the stage in flight without letting the real client answer, so
        # the rid belongs to a wait this test controls.
        real_send = server.SERVER.send
        server.SERVER.send = lambda frame: True
        try:
            engine.request_again(self.obj)
            engine_triggers._timer()
        finally:
            server.SERVER.send = real_send
        self.assertEqual(self.stage.state, "waiting", self.stage.error)
        pending = ENGINE.pending_of(record.stack_id, self.stage.key)
        self.assertTrue(pending, "the swallowed send recorded no pending request")
        rid = next(iter(pending))

        sections = {
            "mesh.verts": np.array(
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32
            ),
            "mesh.face_counts": np.array([3], dtype=np.uint32),
            "mesh.face_indices": np.array([0, 1, 2], dtype=np.uint32),
        }
        response = {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "out_hash": "test-poisoned-frame",
            "status": "ok",
            "error": None,
            "warnings": [],
        }

        real_apply = engine_apply._apply
        calls = {"n": 0}

        def failing_apply(
            obj, stage, payload, attributes=None, definitions=None, topology=None
        ):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("test: apply fault")
            return real_apply(
                obj, stage, payload, attributes, definitions, topology=topology
            )

        engine_apply._apply = failing_apply
        try:
            bridge.post_incoming(response, sections)
            # The second message of the same batch must still be handled.
            bridge.post_incoming(
                {"type": "hello", "doc": "batch.gh", "stages": [{"key": self.stage.key}]},
                {},
            )
            engine_messages._handle_incoming()
        finally:
            engine_apply._apply = real_apply

        self.assertEqual(calls["n"], 1, "the poisoned frame was applied twice")
        self.assertIn(
            "apply fault",
            bridge.state["error"],
            "the failed message was not recorded: %r" % (bridge.state["error"],),
        )
        self.assertTrue(
            any("client hello" in line for line in ENGINE.recent_activity(50)),
            "the message after the failure was dropped: %s" % ENGINE.recent_activity(50),
        )
        self.assertFalse(
            ENGINE.pending_of(record.stack_id, self.stage.key),
            "the stage was left waiting with an empty pending map",
        )
        self.assertNotEqual(
            self.stage.state,
            self.stack_mod.STATE_WAITING,
            "the failed response left the stage in waiting",
        )
        self.assertIn(self.obj.name, ENGINE.dirty, "the failed stage was not marked dirty")
        self.assertIn(
            (record.stack_id, self.stage.key),
            ENGINE.verify,
            "the failed answer did not arm a new request",
        )

        # The next pass asks again, and the real client answers.
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the stage never recovered after the apply failure (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )
        self.assertGreaterEqual(ENGINE.stats()["applies"], 1)


class StallRenameTests(LiveSyncTestCase):
    """A rename between send and stall check must not hide a lost answer.

    The old stall set was keyed by the name recorded at send time; renaming the
    object made `bpy.data.objects.get(old_name)` fail, the name was discarded,
    and the lost request never got its retry nudge until the next send. The
    scan now walks the scene's stack objects and their pending entries.
    """

    def test_01_a_renamed_object_still_gets_its_stall_nudge(self):
        import stageloom.blender.server as server

        engine = self.engine
        prefs = bpy.context.preferences.addons["stageloom"].preferences
        original_name = self.obj.name
        original_timeout = prefs.timeout
        real_send = server.SERVER.send
        try:
            self.assertEqual(self.stage.state, "ok", self.stage.error)
            record = self.stack_mod.stack_of(self.obj)

            # A request whose answer never comes: swallow the socket write.
            server.SERVER.send = lambda frame: True
            engine.request_again(self.obj)
            engine_triggers._timer()
            self.assertEqual(self.stage.state, "waiting", self.stage.error)
            pending = ENGINE.pending_of(record.stack_id, self.stage.key)
            self.assertTrue(pending, "the swallowed send recorded no pending request")

            # Rename after the send: a name captured then is now stale.
            self.obj.name = original_name + "Renamed"
            self.assertIn("Renamed", self.obj.name)

            prefs.timeout = 1.0
            for key in list(pending):
                pending[key] = time.monotonic() - 5.0
            ENGINE.dirty.discard(self.obj.name)
            ENGINE.last_stall_check = 0.0
            engine_triggers._nudge_stalled_waits()
            self.assertIn(
                self.obj.name,
                ENGINE.dirty,
                "the renamed object missed the stall nudge",
            )

            # Restore the socket and recover: the pass must not leave the stage
            # waiting on a request nobody can answer. (The rename also makes
            # the pass rebuild its plumbing, which forgets the wait itself —
            # either way, the stage settles.)
            server.SERVER.send = real_send
            self.assertTrue(
                pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
                "the stage did not recover after the rename and nudge (state=%s, error=%s)"
                % (self.stage.state, self.stage.error),
            )

            # A lost answer under the *new* name, no rename this time: the nudge
            # must reach `_process_stage`'s timeout, which resends in full
            # (the timeout cannot know what GH still holds).
            record = self.stack_mod.stack_of(self.obj)
            server.SERVER.send = lambda frame: True
            engine.request_again(self.obj)
            engine_triggers._timer()
            self.assertEqual(self.stage.state, "waiting", self.stage.error)
            pending = ENGINE.pending_of(record.stack_id, self.stage.key)
            self.assertTrue(pending, "the swallowed send recorded no pending request")
            for key in list(pending):
                pending[key] = time.monotonic() - 5.0
            ENGINE.dirty.discard(self.obj.name)
            ENGINE.last_stall_check = 0.0
            engine_triggers._nudge_stalled_waits()
            self.assertIn(self.obj.name, ENGINE.dirty)

            server.SERVER.send = real_send
            sends = ENGINE.stats()["sends"]
            self.assertTrue(
                pump(seconds=15.0, until=lambda: ENGINE.stats()["sends"] > sends),
                "the timed-out stage did not retry",
            )
            self.assertTrue(
                pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
                "the full retry never landed (state=%s, error=%s)"
                % (self.stage.state, self.stage.error),
            )
            self.assertGreaterEqual(
                report_of(self.report).get("full", 0),
                2,
                "the timeout retry did not resend the geometry in full",
            )
        finally:
            server.SERVER.send = real_send
            prefs.timeout = original_timeout
            if self.obj.name != original_name:
                self.obj.name = original_name

    def test_02_a_rename_keeps_the_same_helpers_and_node_group(self):
        import stageloom.blender.helpers as helpers
        import stageloom.blender.nodes as nodes

        self.assertEqual(self.stage.state, "ok", self.stage.error)
        record = self.stack_mod.stack_of(self.obj)
        helper = helpers.helper_for(self.obj, self.stage.key, "mesh")
        self.assertIsNotNone(helper)
        prefix = helpers.helper_name(self.obj, self.stage.key)
        before = {
            item.name
            for item in list(bpy.data.objects) + list(bpy.data.collections)
            if item.name == prefix or item.name.startswith(prefix + ".")
        }
        group = nodes.modifier_of(self.obj, self.stage).node_group
        original_name = self.obj.name
        renamed = original_name + "HelperStable"

        try:
            self.obj.name = renamed
            ENGINE.mark_object_dirty(self.obj)
            self.assertTrue(
                pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
                "the renamed stage never settled (state=%s, error=%s)"
                % (self.stage.state, self.stage.error),
            )

            self.assertIs(
                helpers.helper_for(self.obj, self.stage.key, "mesh"),
                helper,
                "the rename created a second helper set",
            )
            after = {
                item.name
                for item in list(bpy.data.objects) + list(bpy.data.collections)
                if item.name == prefix or item.name.startswith(prefix + ".")
            }
            self.assertEqual(after, before, "the rename created or orphaned plumbing")
            self.assertFalse(
                any(
                    item.name.startswith("SL %s." % original_name)
                    for item in list(bpy.data.objects) + list(bpy.data.collections)
                ),
                "an object-name-based plumbing set was left behind",
            )
            self.assertIs(
                nodes.modifier_of(self.obj, self.stage).node_group,
                group,
                "the rename rebuilt the stage group",
            )
            self.assertEqual(
                self.obj.gh_stack.stack_id,
                record.stack_id,
                "the rename changed the stack identity",
            )
        finally:
            self.obj.name = original_name



class WhyNoSendTests(LiveSyncTestCase):
    """A pass that saw the scene move and sent nothing must say why.

    Freeze is explained once per decision — a line per tick would bury the rest
    of the ring. A request that is outstanding *and young* is coalesced: the
    pass returns before reading the prefix, one explanation per wait, and the
    supersede after the soft retry does the work once.
    """

    def settle(self):
        """Unfreeze, force a request and wait for the answer: each test starts
        from a restful stage whether or not the one before it left a decision
        recorded."""
        self.stage.frozen = False
        self.engine.request_again(self.obj)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the stage never settled (state=%s, error=%s)" % (self.stage.state, self.stage.error),
        )

    def lines(self, needle):
        """This stage's trace lines only: the ring survives across classes."""
        return [
            line
            for line in ENGINE.recent_activity(200)
            if needle in line and self.stage.key in line
        ]

    def test_01_a_frozen_stage_explains_the_silence_once(self):
        engine = self.engine
        self.assertEqual(self.stage.state, "ok", self.stage.error)
        self.stage.frozen = True
        try:
            ENGINE.mark_object_dirty(self.obj)
            engine_triggers._timer()
            self.assertEqual(
                len(self.lines("no send (frozen)")),
                1,
                "no frozen explanation: %s" % ENGINE.recent_activity(50),
            )
            ENGINE.mark_object_dirty(self.obj)
            engine_triggers._timer()
            self.assertEqual(
                len(self.lines("no send (frozen)")),
                1,
                "the frozen explanation repeated per tick: %s" % ENGINE.recent_activity(50),
            )
        finally:
            self.stage.frozen = False

        # A send re-arms the explanation: freezing again is a new decision.
        sends = ENGINE.stats()["sends"]
        engine.request_again(self.obj)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: ENGINE.stats()["sends"] > sends),
            "an unfrozen stage did not send",
        )
        self.stage.frozen = True
        try:
            ENGINE.mark_object_dirty(self.obj)
            engine_triggers._timer()
            self.assertEqual(
                len(self.lines("no send (frozen)")),
                2,
                "the second freeze left no explanation: %s" % ENGINE.recent_activity(50),
            )
        finally:
            self.stage.frozen = False

    def test_02_a_pending_request_is_coalesced_then_superseded(self):
        import stageloom.blender.server as server

        engine = self.engine
        self.settle()
        name = self.stack_mod.param_prop_name(self.stage.key, SCALE)

        # Swallow the request instead of sending it: the stage now believes an
        # answer is in flight, which is the state the live loop waits in while
        # Grasshopper solves.
        real_send = server.SERVER.send
        server.SERVER.send = lambda frame: True
        try:
            engine.request_again(self.obj)
            engine_triggers._timer()
            self.assertEqual(self.stage.state, "waiting", self.stage.error)
            self.assertEqual(
                self.lines("no send (waiting on rid"),
                [],
                "a sent request was reported as a silence",
            )

            # Edits inside the soft-retry window are coalesced: the pass
            # returns before it reads the prefix, so no clone is paid and no
            # second request goes out.
            clones = ENGINE.stats()["prefix_clones"]
            sends = ENGINE.stats()["sends"]
            for value in (2.0, 3.0, 4.0):
                self.obj[name] = value
                ENGINE.mark_object_dirty(self.obj)
                engine_triggers._timer()
            self.assertEqual(
                ENGINE.stats()["prefix_clones"], clones,
                "a pass while waiting re-evaluated the prefix",
            )
            self.assertEqual(ENGINE.stats()["sends"], sends, "a second request went out while waiting")
            self.assertEqual(self.stage.state, "waiting")
            self.assertEqual(
                len(self.lines("no send (waiting on rid")),
                1,
                "the coalesced wait left no explanation: %s" % self.lines("no send"),
            )

            # After the soft retry the pass supersedes the stale request with
            # the current state, and the trace says so.
            deadline = time.time() + 6.0
            while time.time() < deadline and ENGINE.stats()["sends"] == sends:
                ENGINE.mark_object_dirty(self.obj)
                engine_triggers._timer()
                time.sleep(0.05)
            self.assertGreater(
                ENGINE.stats()["sends"], sends, "the stale request was never superseded"
            )
            self.assertTrue(
                any("superseded" in line for line in ENGINE.recent_activity(50)),
                "the supersede left no trace: %s" % ENGINE.recent_activity(50),
            )
        finally:
            server.SERVER.send = real_send

        # Recover: forget the swallowed waits and let the real client answer.
        engine.request_again(self.obj)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the swallowed wait never recovered (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )
        self.assertTrue(
            pump(
                seconds=15.0,
                until=lambda: abs(helper_max(self.obj, self.stage.key) - 4.0) < 0.01,
            ),
            "the recovered value never applied",
        )


class SessionResetTests(LiveSyncTestCase):
    """A wedged guard must not survive a new session.

    No socket work is needed here, so `start_server` is stubbed: `register()`
    would otherwise stop the class's server (it follows the port preference,
    not the test port) and strand the fake client.
    """

    def test_01_register_clears_a_wedged_guard(self):
        engine = self.engine
        real_start = engine.start_server
        engine.unregister()
        engine.start_server = lambda port=None: False
        try:
            ENGINE.evaluating = True
            ENGINE.guard_owner = ("Wedged", self.stage.key)
            ENGINE.evaluating_since = time.monotonic()
            ENGINE.ticking = True
            ENGINE.runtime(*("Wedged", self.stage.key)).sent = ("rid", "hash")
            ENGINE.runtime(*("Wedged", self.stage.key)).pending = {"rid": time.monotonic()}
            ENGINE.runtime(*("Wedged", self.stage.key)).last_frame = 1.0
            ENGINE.runtime("Wedged", self.stage.key).pull_permit = True
            bridge.post_incoming(
                {
                    "type": "response",
                    "stage": self.stage.key,
                    "rid": "sha256:queued-before-register",
                    "status": "ok",
                    "out_hash": "queued-before-register",
                },
                {},
            )

            engine.register()
            self.assertFalse(ENGINE.evaluating, "register() kept a wedged prefix guard")
            self.assertIsNone(ENGINE.guard_owner)
            self.assertIsNone(ENGINE.evaluating_since)
            self.assertFalse(ENGINE.ticking, "register() kept the previous session's tick flag")
            self.assertIsNone(
                getattr(ENGINE.peek_runtime("Wedged", self.stage.key), "sent", None),
                "register() kept the previous session's client state",
            )
            self.assertFalse(
                ENGINE.is_pending("Wedged", self.stage.key),
                "register() kept a pending request",
            )
            self.assertIsNone(
                ENGINE.peek_runtime("Wedged", self.stage.key),
                "register() kept a delivered frame",
            )
            self.assertFalse(
                getattr(
                    ENGINE.peek_runtime("Wedged", self.stage.key), "pull_permit", False
                ),
                "register() kept a Pull permit",
            )
            self.assertEqual(
                bridge.INCOMING.qsize(),
                0,
                "register() kept a frame from the previous session",
            )

            # A retry of register() (a partial failure is the reason it can run
            # again) must not append the same handler twice.
            handlers = len(bpy.app.handlers.depsgraph_update_post)
            undos = len(bpy.app.handlers.undo_post)
            redos = len(bpy.app.handlers.redo_post)
            ENGINE.registered = False
            engine.register()
            self.assertEqual(
                len(bpy.app.handlers.depsgraph_update_post),
                handlers,
                "a second register() duplicated the depsgraph handler",
            )
            self.assertEqual(
                len(bpy.app.handlers.undo_post),
                undos,
                "a second register() duplicated the undo handler",
            )
            self.assertEqual(
                len(bpy.app.handlers.redo_post),
                redos,
                "a second register() duplicated the redo handler",
            )
        finally:
            engine.start_server = real_start
            if not ENGINE.registered:
                engine.register()

    def test_02_a_file_load_clears_a_wedged_guard(self):
        ENGINE.evaluating = True
        ENGINE.guard_owner = ("Wedged", self.stage.key)
        ENGINE.evaluating_since = time.monotonic()
        ENGINE.ticking = True
        ENGINE.runtime(*("Wedged", self.stage.key)).sent = ("rid", "hash")

        engine_triggers._on_load()
        self.assertFalse(ENGINE.evaluating, "_on_load() kept a wedged prefix guard")
        self.assertIsNone(ENGINE.guard_owner)
        self.assertIsNone(ENGINE.evaluating_since)
        self.assertFalse(ENGINE.ticking, "_on_load() kept the previous session's tick flag")
        self.assertIsNone(
            getattr(ENGINE.peek_runtime("Wedged", self.stage.key), "sent", None),
            "_on_load() kept the previous session's client state",
        )


class DiagnosticsTimingTests(LiveSyncTestCase):
    """Diagnostics: trigger health and stage timings."""

    def test_01_a_round_trip_leaves_timings_and_a_heartbeat(self):
        record = self.stack_mod.stack_of(self.obj)
        timings = ENGINE.stage_timings(record.stack_id, self.stage.key)
        for field in ("prefix_ms", "encode_ms", "rtt_ms", "apply_ms"):
            self.assertIsNotNone(
                timings.get(field), "%s missing after a round trip: %s" % (field, timings)
            )
            self.assertGreaterEqual(timings[field], 0.0)
            self.assertLess(timings[field], 60000.0)
        # The helper hands out a copy: the panel can never write engine state.
        timings["rtt_ms"] = -1.0
        self.assertNotEqual(
            ENGINE.stage_timings(record.stack_id, self.stage.key)["rtt_ms"],
            -1.0,
            "stage_timings handed out the engine's live dict",
        )

        health = ENGINE.trigger_health()
        self.assertIsNotNone(health["trigger_age"], "no depsgraph update was ever heard")
        self.assertIsNotNone(health["send_age"])
        self.assertIsNotNone(health["apply_age"])
        self.assertGreaterEqual(health["trigger_count"], 1)
        self.assertGreaterEqual(health["trigger_age"], 0.0)


class SupersedeTests(unittest.TestCase):
    """A slow answer must not swallow the edits that follow it.

    After SOFT_RETRY_SECONDS a pass supersedes the stale request with the
    current state; the slow fake's echo proves the newest value is what gets
    answered and applied.
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-live-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.port = free_port()
        engine.start_server(cls.port)
        time.sleep(0.5)

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Slow"
        bpy.ops.stageloom.add_stage(label="Slow")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.obj[stack_mod.param_prop_name(cls.stage.key, SCALE)] = 1.0

        cls.report = os.path.join(tempfile.gettempdir(), "fake_gh_live_supersede.json")
        if os.path.exists(cls.report):
            os.remove(cls.report)
        cls.fake = start_fake(
            cls.stage.key, cls.report, "--delay-ms", "3000", port=cls.port
        )

        def settled():
            return cls.stage.state == "ok" and report_of(cls.report).get("responses", 0) >= 1

        settle(engine, stack_mod, [cls.obj], settled)

    @classmethod
    def tearDownClass(cls):
        stop_fake(cls.fake)
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_a_new_value_supersedes_a_slow_request(self):
        name = self.stack_mod.param_prop_name(self.stage.key, SCALE)
        self.obj[name] = 2.0
        ENGINE.mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=5.0, until=lambda: self.stage.state == "waiting"),
            "the slow fake never left a request outstanding",
        )
        sends = ENGINE.stats()["sends"]
        pump(seconds=0.4)
        self.assertEqual(
            ENGINE.stats()["sends"], sends, "a request went out before the soft retry"
        )
        self.obj[name] = 5.0
        ENGINE.mark_object_dirty(self.obj)
        self.assertTrue(
            pump(seconds=6.0, until=lambda: ENGINE.stats()["sends"] > sends),
            "the newer value never superseded the in-flight request",
        )
        self.assertTrue(
            pump(
                seconds=20.0,
                until=lambda: abs(helper_max(self.obj, self.stage.key) - 5.0) < 0.01,
            ),
            "the newest value never applied (helper max %r)"
            % helper_max(self.obj, self.stage.key),
        )
        self.assertEqual(self.stage.state, "ok", self.stage.error)


class SelfHealingTests(LiveSyncTestCase):
    """The bookkeeping around a failed or lost request must not gate recovery."""

    def test_01_a_standalone_error_releases_the_pending_gate(self):
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)
        rid = "sha256:standalone-error"
        ENGINE.runtime(*key).pending = {rid: time.monotonic()}
        self.stage.rid = rid
        self.stage.state = "waiting"

        engine_messages._handle_error(
            {
                "type": "error",
                "code": "unknown_stage",
                "stage": self.stage.key,
                "rid": rid,
                "message": "no claim",
            }
        )

        self.assertFalse(ENGINE.pending_of(*key), "the error left a pending request behind")
        self.assertEqual(self.stage.state, "error")
        self.assertIn("unknown_stage", self.stage.error)

    def test_02_an_unknown_ref_resend_asks_again(self):
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)
        engine_messages._resend_in_full(self.obj, record, self.stage, "test")

        self.assertIn(key, ENGINE.verify, "the promised full resend was not armed")
        self.assertEqual(self.stage.geom_hash, "")
        self.assertIn(self.obj.name, ENGINE.dirty)

    def test_03_a_pull_permit_survives_an_undelivered_send(self):
        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)

        self.stage.frozen = True
        engine.freeze(self.obj, self.stage)
        engine.request_again(self.obj, self.stage)
        self.assertTrue(ENGINE.runtime(*key).pull_permit)

        real_send = server.SERVER.send
        server.SERVER.send = lambda frame: False
        try:
            ENGINE.mark_object_dirty(self.obj)
            engine_triggers._timer()
        finally:
            server.SERVER.send = real_send
        self.assertTrue(
            ENGINE.runtime(*key).pull_permit,
            "the Pull permit was spent by a send that never left",
        )

        ENGINE.mark_object_dirty(self.obj)
        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: not ENGINE.runtime(*key).pull_permit,
            ),
            "the armed Pull never went out after the socket recovered",
        )
        self.stage.frozen = False
        engine.unfreeze(self.obj, self.stage)

    def test_04_a_frozen_pull_with_no_answer_settles_and_stops_dirtying(self):
        import stageloom.blender.server as server

        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)
        self.stage.frozen = True
        engine.freeze(self.obj, self.stage)
        real_send = server.SERVER.send
        server.SERVER.send = lambda frame: True
        try:
            engine.request_again(self.obj, self.stage)
            ENGINE.mark_object_dirty(self.obj)
            engine_triggers._timer()
        finally:
            server.SERVER.send = real_send
        self.assertTrue(ENGINE.is_pending(*key), "the Pull was not recorded as in flight")
        self.assertEqual(self.stage.state, self.stack_mod.STATE_WAITING)
        self.assertTrue(ENGINE.runtime(*key).pull_permit)

        # The answer never lands; the permit deadline is what must settle it.
        ENGINE.runtime(*key).pull_deadline = time.monotonic() - 0.01
        ENGINE.last_stall_check = 0.0
        ENGINE.dirty.clear()
        try:
            engine_triggers._nudge_stalled_waits()
        finally:
            self.stage.frozen = False
        self.assertFalse(ENGINE.is_pending(*key))
        self.assertFalse(ENGINE.runtime(*key).pull_permit)
        self.assertFalse(ENGINE.runtime(*key).pull_deadline is not None)
        self.assertEqual(self.stage.state, self.stack_mod.STATE_FROZEN)
        self.assertIn("Pull timed out", self.stage.error)
        self.assertNotIn(self.obj.name, ENGINE.dirty)

        ENGINE.last_stall_check = 0.0
        engine_triggers._nudge_stalled_waits()
        self.assertNotIn(
            self.obj.name,
            ENGINE.dirty,
            "a lost Pull dirtied the object once a second after it had settled",
        )

        engine.unfreeze(self.obj, self.stage)
        engine.request_again(self.obj, self.stage)
        self.assertTrue(
            pump(seconds=15.0, until=lambda: self.stage.state == "ok"),
            "the stage did not recover after the lost Pull (state=%s, error=%s)"
            % (self.stage.state, self.stage.error),
        )

    def test_05_a_client_leaving_between_the_check_and_the_send_is_not_recorded(self):
        import stageloom.blender.bridge as bridge
        import stageloom.blender.server as server

        engine = self.engine
        record = self.stack_mod.stack_of(self.obj)
        key = (record.stack_id, self.stage.key)
        stop_fake(type(self).fake)
        type(self).fake = None
        self.assertTrue(
            pump(seconds=5.0, until=lambda: not bridge.state["clients"]),
            "the fake never disconnected",
        )
        ENGINE.clear_runtimes()
        ENGINE.clear_runtimes()
        ENGINE.verify.clear()
        ENGINE.clear_runtimes()
        ENGINE.dirty.clear()
        previous = (bridge.state["clients"], bridge.state["connected"])
        # The stale observation the old send() trusted: a client count with no
        # socket behind it, the exact window between its check and its enqueue.
        bridge.state["clients"] = 1
        bridge.state["connected"] = True
        try:
            self.assertFalse(
                server.SERVER.send(b"a frame with no socket"),
                "send claimed a frame was carried with no client socket",
            )
            undelivered = ENGINE.stats()["undelivered"]
            engine.request_again(self.obj, self.stage)
            ENGINE.mark_object_dirty(self.obj)
            engine_triggers._timer()
            self.assertEqual(
                ENGINE.stats()["undelivered"],
                undelivered + 1,
                "the request path counted an undelivered frame as sent",
            )
            self.assertFalse(
                ENGINE.pending_of(*key),
                "an undelivered frame armed a pending answer wait",
            )
            self.assertFalse(ENGINE.runtime(*
                key).sent is not None, "an undelivered frame recorded a sent request"
            )
            self.assertEqual(self.stage.state, self.stack_mod.STATE_DISCONNECTED)
        finally:
            bridge.state["clients"], bridge.state["connected"] = previous
            type(self).fake = start_fake(
                self.stage.key, self.report, port=self.port
            )
            self.assertTrue(
                pump(seconds=10.0, until=lambda: bridge.state["clients"] >= 1),
                "the restarted fake never connected",
            )
            settle(
                engine,
                self.stack_mod,
                [self.obj],
                lambda: self.stage.state == "ok",
            )



class HashTrustTests(LiveSyncTestCase):
    """An emptied helper with an unchanged applied hash must be re-applied.

    There is no frame store to replay, so the repair is a fresh request; the
    answer carries the same output hash and must still be applied over the
    empty helper."""

    def test_01_a_cleared_helper_is_reapplied_on_request(self):
        import stageloom.blender.helpers as helpers

        engine = self.engine
        helper = helpers.helper_for(self.obj, self.stage.key, "mesh")
        self.assertIsNotNone(helper)
        self.assertGreater(len(helper.data.vertices), 0)
        applies = ENGINE.stats()["applies"]

        helper.data.clear_geometry()
        engine.request_again(self.obj, self.stage)
        self.assertTrue(
            pump(seconds=10.0, until=lambda: ENGINE.stats()["applies"] > applies),
            "the frame was skipped over an emptied helper",
        )
        helper = helpers.helper_for(self.obj, self.stage.key, "mesh")
        self.assertGreater(len(helper.data.vertices), 0, "the helper stayed empty")
        self.assertEqual(self.stage.state, "ok", self.stage.error)


if __name__ == "__main__":
    unittest.main()
