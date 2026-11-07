"""Layer B, multi-stage link safety: what a response is allowed to do.

The live 2026-11-07 session had two independent stage pairs. The first stage's
request triggered a document-wide Grasshopper solve, so the second Stage Out
ran before it had ever been asked for anything and sent an empty `ok` response
with `rid=""`. Blender's cache stored it as `<stage>/.bin`, and a stage whose
`rid` was also empty could apply it and clear its helpers.

These checks pin the response gate:
1. an empty rid is ignored: no cache entry, no pending cleared, no apply;
2. an `ok` response without an output hash is ignored;
3. a real error evicts a cached success for the same rid;
4. a frozen stage stores the answer but keeps its applied output;
5. a disabled stage ignores the answer.
"""

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
