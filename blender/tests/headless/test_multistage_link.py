"""Layer B: multi-stage response gating and link safety."""

import os
import sys
import tempfile
import time
import unittest

import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
if BLENDER_PKG not in sys.path:
    sys.path.insert(0, BLENDER_PKG)


class ResponseValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        bpy.context.preferences.addons["stageloom"].preferences.data_root = (
            tempfile.mkdtemp(prefix="stageloom-multistage-")
        )
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
        self.engine._cache.clear()
        self.engine._cache_bytes.clear()
        self.engine._pending.clear()
        self.engine._sent.clear()
        self.engine._pull_permits.clear()
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
        raw = self.engine.codec.encode_frame(
            header,
            [
                ("mesh.verts", "f32", sections["mesh.verts"]),
                ("mesh.face_counts", "u32", sections["mesh.face_counts"]),
                ("mesh.face_indices", "u32", sections["mesh.face_indices"]),
            ],
        )
        return header, sections, raw

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

        self.assertIsNone(
            self.engine._cached_response(self.record, self.stage, ""),
            "an empty-rid response was cached",
        )
        store = self.engine._store_for(self.record, self.stage)
        self.assertFalse(
            os.path.exists(store.index.path_for("")),
            "an empty-rid response reached the disk store",
        )
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

        self.assertIsNone(
            self.engine._cached_response(self.record, self.stage, rid),
            "an ok response without an output hash was cached",
        )
        self.assertEqual(self.stage.applied_out_hash, "")

    def test_03_error_evicts_a_cached_success(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:cached-before-error"
        self.stage.rid = rid
        header, sections, raw = self._frame(rid, "sha256:before")
        self.engine._remember(self.record, self.stage, rid, header, sections, raw)
        self.engine._store_frame(self.record, self.stage, rid, header, sections, raw)
        key = (self.record.stack_id, self.stage.key)
        self.engine._pending[key] = {rid: time.monotonic()}
        self.assertIsNotNone(
            self.engine._cached_response(self.record, self.stage, rid),
            "the test did not seed a cached success",
        )

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

        self.assertIsNone(
            self.engine._cached_response(self.record, self.stage, rid),
            "the cached success survived the error and would replay",
        )
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
        header, sections, raw = self._frame(rid, "sha256:after")
        bridge.post_incoming(header, sections, raw)
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
        header, sections, raw = self._frame(rid, "sha256:after")
        bridge.post_incoming(header, sections, raw)
        self.engine._handle_incoming()

        self.assertEqual(
            self.stage.applied_out_hash,
            "sha256:before",
            "a disabled stage applied a response",
        )

    def test_06_stale_error_keeps_the_cached_frame_and_newer_pending(self):
        import stageloom.blender.bridge as bridge

        old_rid = "sha256:cached-before-stale-error"
        new_rid = "sha256:newer-pending"
        header, sections, raw = self._frame(old_rid, "sha256:old")
        self.engine._remember(self.record, self.stage, old_rid, header, sections, raw)
        self.engine._store_frame(self.record, self.stage, old_rid, header, sections, raw)

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

        self.assertIsNotNone(
            self.engine._cached_response(self.record, self.stage, old_rid),
            "a stale error evicted a good cached frame",
        )
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
        header, sections, raw = self._frame(rid, "sha256:good")
        self.engine._remember(self.record, self.stage, rid, header, sections, raw)
        self.engine._store_frame(self.record, self.stage, rid, header, sections, raw)

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

        self.assertIsNotNone(
            self.engine._cached_response(self.record, self.stage, rid),
            "an unsolicited error evicted an idle stage's cached frame",
        )
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

    def test_10_load_clears_a_live_stage_request_id_before_verify(self):
        import stageloom.blender.bridge as bridge

        rid = "sha256:saved-live-rid"
        self.stage.frozen = False
        self.stage.rid = rid
        self.stage.state = self.stack_mod.STATE_OK
        self.stage.applied_out_hash = "sha256:before-load"
        header, sections, raw = self._frame(rid, "sha256:after-load")

        self.engine._on_load()

        self.assertEqual(
            self.stage.rid,
            "",
            "a non-frozen stage kept the request id saved with the file",
        )
        # A response was already queued when the file opened. It must be
        # cached, not applied: the load has to verify with Grasshopper first.
        bridge.post_incoming(header, sections, raw)
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

        self.engine._on_load()

        self.assertEqual(self.stage.rid, "")
        self.assertNotIn(
            key,
            self.engine._pull_permits,
            "a Pull armed before the save survived the load",
        )
        self.assertEqual(
            self.engine._process_stage(self.obj, self.record, self.stage),
            "idle",
            "the saved frozen stage sent the old Pull after reopening",
        )
        self.assertEqual(self.stage.state, self.stack_mod.STATE_FROZEN)
