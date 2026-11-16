"""Layer B: multi-stage response gating and link safety."""

import time
import unittest

import bpy
import numpy as np

from harness import isolate_data_root


class ResponseValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-multistage-"))
        import stageloom.blender.engine as engine
        import stageloom.blender.stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "MultiStage"
        bpy.ops.stageloom.add_stage(label="Multi")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.record = stack_mod.stack_of(cls.obj)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        self.engine._pending.clear()
        self.engine._sent.clear()
        self.engine._verify.clear()
        self.engine._pull_permits.clear()
        self.engine._pull_deadlines.clear()
        self.stage.enabled = True
        self.stage.frozen = False
        self.stage.state = self.stack_mod.STATE_OK
        self.stage.rid = ""
        self.stage.applied_out_hash = ""
        self.stage.error = ""

    def _frame(self, rid, out_hash):
        sections = {
            "mesh.verts": np.array(
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                dtype=np.float32,
            ),
            "mesh.face_counts": np.array([3], dtype=np.uint32),
            "mesh.face_indices": np.array([0, 1, 2], dtype=np.uint32),
        }
        header = {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "out_hash": out_hash,
            "status": "ok",
            "warnings": [],
        }
        return header, sections

    def test_01_empty_rid_is_ignored(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:current"
        self.stage.rid = rid
        self.stage.applied_out_hash = "sha256:applied"
        key = (self.record.stack_id, self.stage.key)
        self.engine._pending[key] = {rid: time.monotonic()}

        header = {
            "type": "response",
            "stage": self.stage.key,
            "rid": "",
            "out_hash": "sha256:empty",
            "status": "ok",
            "warnings": [],
        }
        bridge.post_incoming(header, {})
        self.engine._handle_incoming()

        self.assertIn(
            rid,
            self.engine._pending_of(self.record.stack_id, self.stage.key),
            "the empty-rid response cleared the real pending request",
        )
        self.assertEqual(self.stage.applied_out_hash, "sha256:applied")
        self.assertEqual(self.stage.state, self.stack_mod.STATE_OK)

    def test_02_ok_without_output_hash_is_ignored(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:missing-hash"
        self.stage.rid = rid
        header = {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "status": "ok",
            "warnings": [],
        }
        bridge.post_incoming(header, {})
        self.engine._handle_incoming()

        self.assertEqual(self.stage.applied_out_hash, "")

    def test_03_an_error_answers_and_records_the_failure(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:failing"
        self.stage.rid = rid
        key = (self.record.stack_id, self.stage.key)
        self.engine._pending[key] = {rid: time.monotonic()}

        error = {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "status": "error",
            "error": {"code": "gh_solve_failed", "message": "boom"},
            "warnings": [],
        }
        bridge.post_incoming(error, {})
        self.engine._handle_incoming()

        self.assertNotIn(key, self.engine._pending, "the error left the request pending")
        self.assertEqual(self.stage.state, self.stack_mod.STATE_ERROR)
        self.assertIn("boom", self.stage.error)

    def test_04_frozen_stage_does_not_apply(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:frozen"
        # Freeze parks the stage by clearing its request id.
        self.stage.rid = ""
        self.stage.frozen = True
        self.stage.state = self.stack_mod.STATE_FROZEN
        self.stage.applied_out_hash = "sha256:before"
        header, sections = self._frame(rid, "sha256:after")
        bridge.post_incoming(header, sections)
        self.engine._handle_incoming()

        self.assertEqual(
            self.stage.applied_out_hash,
            "sha256:before",
            "a frozen stage applied a response",
        )
        self.assertEqual(self.stage.state, self.stack_mod.STATE_FROZEN)

    def test_05_disabled_stage_does_not_apply(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:disabled"
        self.stage.rid = rid
        self.stage.enabled = False
        self.stage.applied_out_hash = "sha256:before"
        header, sections = self._frame(rid, "sha256:after")
        bridge.post_incoming(header, sections)
        self.engine._handle_incoming()

        self.assertEqual(
            self.stage.applied_out_hash,
            "sha256:before",
            "a disabled stage applied a response",
        )

    def test_06_a_stale_error_keeps_the_newer_pending_request(self):
        import stageloom.blender.bridge as bridge

        old_rid = "sha256:stale-error"
        new_rid = "sha256:newer-pending"
        key = (self.record.stack_id, self.stage.key)
        self.stage.rid = new_rid
        self.stage.state = self.stack_mod.STATE_WAITING
        self.engine._pending[key] = {new_rid: time.monotonic()}

        error = {
            "type": "response",
            "stage": self.stage.key,
            "rid": old_rid,
            "status": "error",
            "error": {"code": "gh_solve_failed", "message": "stale boom"},
            "warnings": [],
        }
        bridge.post_incoming(error, {})
        self.engine._handle_incoming()

        self.assertIn(
            new_rid,
            self.engine._pending_of(self.record.stack_id, self.stage.key),
            "a stale error cleared the newer pending request",
        )
        self.assertEqual(self.stage.rid, new_rid)
        self.assertEqual(self.stage.state, self.stack_mod.STATE_WAITING)
        self.assertNotIn("stale boom", self.stage.error)

    def test_07_frozen_stage_keeps_frozen_on_error(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:frozen-error"
        self.stage.frozen = True
        self.stage.state = self.stack_mod.STATE_FROZEN
        self.stage.rid = rid
        key = (self.record.stack_id, self.stage.key)
        self.engine._pending[key] = {rid: time.monotonic()}

        error = {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "status": "error",
            "error": {"code": "gh_solve_failed", "message": "frozen boom"},
            "warnings": [],
        }
        bridge.post_incoming(error, {})
        self.engine._handle_incoming()

        self.assertEqual(
            self.stage.state,
            self.stack_mod.STATE_FROZEN,
            "an error unfroze the stage",
        )
        self.assertIn("frozen boom", self.stage.error)

    def test_09_error_without_pending_is_ignored(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:idle-last-rid"
        self.stage.rid = rid
        self.stage.state = self.stack_mod.STATE_OK

        error = {
            "type": "response",
            "stage": self.stage.key,
            "rid": rid,
            "status": "error",
            "error": {"code": "gh_solve_failed", "message": "unsolicited boom"},
            "warnings": [],
        }
        bridge.post_incoming(error, {})
        self.engine._handle_incoming()

        self.assertEqual(self.stage.state, self.stack_mod.STATE_OK)
        self.assertEqual(self.stage.error, "")

    def test_08_disabled_stage_forgets_pending(self):
        import stageloom.blender.nodes as nodes

        rid = "sha256:disabled-pending"
        key = (self.record.stack_id, self.stage.key)
        self.engine._pending[key] = {rid: time.monotonic()}
        self.engine._sent[key] = (rid, "sha256:geom")
        self.stage.rid = rid
        self.stage.state = self.stack_mod.STATE_WAITING

        modifier = nodes.modifier_of(self.obj, self.stage)
        self.assertIsNotNone(modifier)
        toggles = (
            modifier.show_viewport,
            modifier.show_render,
            modifier.show_in_editmode,
        )
        modifier.show_viewport = False
        modifier.show_render = False
        modifier.show_in_editmode = False
        try:
            outcome = self.engine._process_stage(self.obj, self.record, self.stage)
        finally:
            (
                modifier.show_viewport,
                modifier.show_render,
                modifier.show_in_editmode,
            ) = toggles

        self.assertEqual(outcome, "idle")
        self.assertEqual(self.stage.state, self.stack_mod.STATE_DISABLED)
        self.assertNotIn(
            key,
            self.engine._pending,
            "the disabled stage kept its in-flight request pending",
        )
        self.assertNotIn(
            key,
            self.engine._sent,
            "the disabled stage kept its sent request bookkeeping",
        )
        self.assertEqual(
            self.stage.rid,
            "",
            "the disabled stage kept the sent request id",
        )

    def test_10_load_clears_a_saved_request_id_before_verify(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:saved-rid"
        self.stage.frozen = False
        self.stage.rid = rid
        self.stage.state = self.stack_mod.STATE_OK
        self.stage.applied_out_hash = "sha256:before-load"
        self.stage.applied_topology = "sha256:" + "e" * 64
        header, sections = self._frame(rid, "sha256:after-load")

        self.engine._on_load()

        self.assertEqual(
            self.stage.rid,
            "",
            "a non-frozen stage kept the request id saved with the file",
        )
        self.assertEqual(
            self.stage.applied_topology,
            "",
            "a load kept a topology token from the old session",
        )
        # A response was already queued when the file opened. It is for a
        # request the load no longer waits for, so it must be dropped: the load
        # has to ask Grasshopper again first.
        bridge.post_incoming(header, sections)
        self.engine._handle_incoming()
        self.assertEqual(
            self.stage.applied_out_hash,
            "sha256:before-load",
            "a response queued at load applied before the verify request",
        )

    def test_11_load_clears_a_pull_permit_armed_before_save(self):
        key = (self.record.stack_id, self.stage.key)
        self.stage.frozen = True
        self.stage.state = self.stack_mod.STATE_FROZEN
        self.stage.rid = "sha256:saved-frozen-rid"
        self.engine._pull_permits.add(key)
        self.engine._pull_deadlines[key] = time.monotonic() + 30.0

        self.engine._on_load()

        self.assertEqual(self.stage.rid, "")
        self.assertNotIn(
            key,
            self.engine._pull_permits,
            "a Pull armed before the save survived the load",
        )
        self.assertNotIn(
            key,
            self.engine._pull_deadlines,
            "a Pull deadline armed before the save survived the load",
        )
        self.assertEqual(
            self.engine._process_stage(self.obj, self.record, self.stage),
            "idle",
            "the saved frozen stage sent the old Pull after reopening",
        )
        self.assertEqual(self.stage.state, self.stack_mod.STATE_FROZEN)

    def test_12_a_stale_standalone_error_keeps_the_newer_pending_request(self):
        import stageloom.blender.bridge as bridge

        old_rid = "sha256:stale-standalone"
        new_rid = "sha256:newer-standalone"
        key = (self.record.stack_id, self.stage.key)
        self.stage.rid = new_rid
        self.stage.state = self.stack_mod.STATE_WAITING
        self.engine._pending[key] = {new_rid: time.monotonic()}

        bridge.post_incoming(
            {
                "type": "error",
                "code": "unknown_stage",
                "stage": self.stage.key,
                "rid": old_rid,
                "message": "stale no claim",
            },
            {},
        )
        self.engine._handle_incoming()

        self.assertIn(
            new_rid,
            self.engine._pending_of(self.record.stack_id, self.stage.key),
            "a stale standalone error cleared the newer pending request",
        )
        self.assertEqual(self.stage.rid, new_rid)
        self.assertEqual(self.stage.state, self.stack_mod.STATE_WAITING)
        self.assertEqual(self.stage.error, "", "a stale error reached the stage row")

    def test_16_undo_clears_the_applied_topology(self):
        self.stage.applied_topology = "sha256:" + "f" * 64
        self.engine._on_undo()
        self.assertEqual(
            self.stage.applied_topology,
            "",
            "an undo kept a topology token from the undone step",
        )

    def test_13_an_uncorrelated_error_keeps_the_pending_wait(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:uncorrelated-pending"
        key = (self.record.stack_id, self.stage.key)
        self.stage.rid = rid
        self.stage.state = self.stack_mod.STATE_WAITING
        self.engine._pending[key] = {rid: time.monotonic()}

        bridge.post_incoming(
            {
                "type": "error",
                "code": "unknown_stage",
                "stage": self.stage.key,
                "message": "no request id",
            },
            {},
        )
        self.engine._handle_incoming()

        self.assertIn(
            rid,
            self.engine._pending_of(self.record.stack_id, self.stage.key),
            "an error without a rid cancelled the newer pending request",
        )
        self.assertEqual(self.stage.state, self.stack_mod.STATE_WAITING)
        self.assertEqual(self.stage.error, "")

    def test_14_a_stale_failed_response_keeps_the_newer_pending_request(self):
        old_rid = "sha256:stale-failed"
        new_rid = "sha256:newer-failed"
        key = (self.record.stack_id, self.stage.key)
        self.stage.rid = new_rid
        self.stage.state = self.stack_mod.STATE_WAITING
        self.engine._pending[key] = {new_rid: time.monotonic()}

        self.engine._report_message_failure(
            {"type": "response", "stage": self.stage.key, "rid": old_rid},
            RuntimeError("stale frame failure"),
        )
        self.engine._clear_failed_response(self.stage.key, old_rid)

        self.assertIn(
            new_rid,
            self.engine._pending_of(self.record.stack_id, self.stage.key),
            "a stale failed response consumed the newer pending request",
        )
        self.assertEqual(self.stage.rid, new_rid)
        self.assertEqual(self.stage.state, self.stack_mod.STATE_WAITING)
        self.assertNotIn(
            key,
            self.engine._verify,
            "a stale failed response armed a resend for the newer request",
        )

        # The failure for the request actually in flight still recovers it.
        self.engine._clear_failed_response(self.stage.key, new_rid)
        self.assertFalse(
            self.engine._pending_of(self.record.stack_id, self.stage.key),
            "the current failed response was not consumed",
        )
        self.assertEqual(self.stage.state, self.stack_mod.STATE_IDLE)
        self.assertIn(key, self.engine._verify)
        self.assertIn(self.obj.name, self.engine._dirty)

    def test_15_a_frozen_pull_with_no_answer_settles(self):
        rid = "sha256:lost-pull"
        key = (self.record.stack_id, self.stage.key)
        self.stage.frozen = True
        self.stage.state = self.stack_mod.STATE_WAITING
        self.stage.rid = rid
        self.engine._pending[key] = {rid: time.monotonic() - 100.0}
        self.engine._arm_pull(key)
        self.engine._pull_deadlines[key] = time.monotonic() - 0.01
        self.engine._dirty.clear()
        self.engine._last_stall_check = 0.0
        try:
            self.engine._nudge_stalled_waits()
        finally:
            self.stage.frozen = False

        self.assertNotIn(
            key, self.engine._pending, "the lost Pull left the request pending"
        )
        self.assertNotIn(
            key, self.engine._pull_permits, "the lost Pull left its permit armed"
        )
        self.assertNotIn(
            key, self.engine._pull_deadlines, "the lost Pull left its deadline behind"
        )
        self.assertEqual(self.stage.state, self.stack_mod.STATE_FROZEN)
        self.assertEqual(self.stage.rid, "")
        self.assertIn("Pull timed out", self.stage.error)
        self.assertNotIn(
            self.obj.name,
            self.engine._dirty,
            "the expired Pull dirtied the object",
        )

        # An idle second scan must stay quiet: the once-a-second dirty loop this
        # deadline removes is what made a lost Pull cost a pass per second.
        self.engine._last_stall_check = 0.0
        self.engine._nudge_stalled_waits()
        self.assertNotIn(
            self.obj.name,
            self.engine._dirty,
            "the settled Pull kept dirtying the object",
        )
