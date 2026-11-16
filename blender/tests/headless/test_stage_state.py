"""Layer B: explicit stage states, flag precedence and wait diagnostics.

Run with:

    scripts/test-blender

Pinned here: every stage-state write goes through ``engine._set_state``; the
``enabled``/``frozen`` flags decide ``disabled``/``frozen``; ``conflict``
outranks both; and ``waiting_seconds`` is zero as soon as no request is in
flight. The duplicate-key integration path is covered in test_duplicates.py.
"""

import time
import unittest

import bpy

from harness import isolate_data_root


class StageStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-state-"))
        import stageloom.blender.engine as engine
        from stageloom.blender import bridge
        from stageloom.blender import stack as stack_mod

        cls.engine = engine
        cls.stack_mod = stack_mod
        cls.bridge = bridge
        bridge.state.update(connected=False, clients=0, listening=False)
        engine._deactivate()
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "State"
        bpy.context.view_layer.objects.active = cls.obj
        cls.obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="State")
        cls.stage = cls.obj.gh_stack.stages[0]
        cls.key = (cls.obj.gh_stack.stack_id, cls.stage.key)
        # Tests drive the pass directly; no timer or handler should do work
        # behind a transition assertion.
        engine._deactivate()
        engine._dirty.clear()

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        stage = self.stage
        self.engine._pending.pop(self.key, None)
        self.engine._state_since.pop(self.key, None)
        self.engine._stage_conflicts.discard(stage.key)
        self.engine._dirty.clear()
        stage.enabled = True
        stage.frozen = False
        stage.rid = ""
        stage.error = ""
        self.engine._set_state(stage, self.stack_mod.STATE_IDLE)

    def test_01_legal_transitions(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        engine._pending[self.key] = {"rid": "one"}
        self.assertEqual(
            engine._set_state(stage, state.STATE_WAITING), state.STATE_WAITING
        )
        self.assertEqual(engine._set_state(stage, state.STATE_OK), state.STATE_OK)
        self.assertEqual(engine._set_state(stage, state.STATE_ERROR), state.STATE_ERROR)
        self.assertEqual(engine._set_state(stage, state.STATE_IDLE), state.STATE_IDLE)
        self.assertEqual(engine._set_state(stage, state.STATE_UNBOUND), state.STATE_UNBOUND)
        engine._pending[self.key] = {"rid": "two"}
        self.assertEqual(engine._set_state(stage, state.STATE_WAITING), state.STATE_WAITING)
        self.assertEqual(
            engine._set_state(stage, state.STATE_DISCONNECTED),
            state.STATE_DISCONNECTED,
        )

    def test_02_flag_states_refuse_transients(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        stage.enabled = False
        self.assertEqual(engine._set_state(stage, state.STATE_IDLE), state.STATE_DISABLED)
        for target in (
            state.STATE_OK,
            state.STATE_ERROR,
            state.STATE_WAITING,
            state.STATE_UNBOUND,
            state.STATE_DISCONNECTED,
        ):
            self.assertEqual(engine._set_state(stage, target), state.STATE_DISABLED, target)

        stage.enabled = True
        stage.frozen = True
        for target in (
            state.STATE_IDLE,
            state.STATE_OK,
            state.STATE_UNBOUND,
            state.STATE_DISCONNECTED,
        ):
            self.assertEqual(engine._set_state(stage, target), state.STATE_FROZEN, target)
        engine._pending[self.key] = {"rid": "pull"}
        self.assertEqual(engine._set_state(stage, state.STATE_WAITING), state.STATE_WAITING)

    def test_03_conflict_outranks_disabled_and_frozen(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        stage.enabled = False
        stage.frozen = True
        engine._stage_conflicts.add(stage.key)
        try:
            for target in (
                state.STATE_IDLE,
                state.STATE_OK,
                state.STATE_ERROR,
                state.STATE_WAITING,
            ):
                self.assertEqual(engine._set_state(stage, target), state.STATE_CONFLICT)
            self.assertEqual(
                engine._set_state(stage, state.STATE_ERROR, error="duplicate key detail"),
                state.STATE_CONFLICT,
            )
            self.assertEqual(stage.error, "duplicate key detail")
        finally:
            engine._stage_conflicts.discard(stage.key)

    def test_04_unfreeze_settles_to_disabled_or_conflict(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        stage.enabled = False
        stage.frozen = True
        engine.freeze(self.obj, stage)
        self.assertEqual(stage.state, state.STATE_DISABLED)
        stage.frozen = False
        engine.unfreeze(self.obj, stage)
        self.assertEqual(stage.state, state.STATE_DISABLED, "unfreeze hid the disabled flag")

        stage.enabled = True
        engine._stage_conflicts.add(stage.key)
        try:
            engine.unfreeze(self.obj, stage)
            self.assertEqual(stage.state, state.STATE_CONFLICT)
        finally:
            engine._stage_conflicts.discard(stage.key)

    def test_05_frozen_error_stays_frozen_and_records_the_error(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        stage.frozen = True
        engine._set_state(stage, state.STATE_FROZEN)
        self.assertEqual(
            engine._set_state(stage, state.STATE_ERROR, error="solver failed"),
            state.STATE_FROZEN,
        )
        self.assertEqual(stage.error, "solver failed")

    def test_06_a_response_error_on_a_frozen_stage_keeps_frozen(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        rid = "sha256:" + "a" * 64
        stage.frozen = True
        stage.rid = rid
        engine._set_state(stage, state.STATE_FROZEN)
        engine._pending[self.key] = {rid: time.monotonic()}
        engine._handle_response(
            {
                "type": "response",
                "stage": stage.key,
                "rid": rid,
                "status": "error",
                "error": {"code": "solver", "message": "bad graph"},
            },
            {},
        )
        self.assertEqual(stage.state, state.STATE_FROZEN, "the frozen state was lost")
        self.assertEqual(stage.error, "bad graph")
        self.assertNotIn(self.key, engine._pending)
        self.assertEqual(engine.waiting_seconds(*self.key), 0.0)

    def test_07_conflict_sends_and_applies_nothing(self):
        engine = self.engine
        stage = self.stage
        engine._stage_conflicts.add(stage.key)
        real_prefix = engine._prefix_sections
        real_apply = engine._apply
        engine._prefix_sections = lambda *args: (_ for _ in ()).throw(
            AssertionError("a conflicted stage read its prefix")
        )
        engine._apply = lambda *args: (_ for _ in ()).throw(
            AssertionError("a conflicted stage applied an answer")
        )
        try:
            outcome = engine._process_stage(self.obj, self.obj.gh_stack, stage)
            self.assertEqual(outcome, "idle")
            engine._handle_response(
                {
                    "type": "response",
                    "stage": stage.key,
                    "rid": "sha256:" + "b" * 64,
                    "status": "ok",
                    "out_hash": "sha256:" + "c" * 64,
                },
                {},
            )
        finally:
            engine._prefix_sections = real_prefix
            engine._apply = real_apply
            engine._stage_conflicts.discard(stage.key)

    def test_08_waiting_requires_an_in_flight_request(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        engine._set_state(stage, state.STATE_IDLE)
        self.assertEqual(
            engine._set_state(stage, state.STATE_WAITING),
            state.STATE_IDLE,
            "waiting was accepted without a request",
        )
        stage.state = state.STATE_WAITING
        self.assertEqual(
            engine._set_state(stage, state.STATE_WAITING),
            state.STATE_IDLE,
            "a stale waiting state was left in place",
        )

        engine._pending[self.key] = {"rid": "live"}
        self.assertEqual(
            engine._set_state(stage, state.STATE_WAITING), state.STATE_WAITING
        )
        self.assertGreater(engine.waiting_seconds(*self.key), 0.0)
        engine._pending.pop(self.key, None)
        self.assertEqual(engine._set_state(stage, state.STATE_OK), state.STATE_OK)
        self.assertEqual(engine.waiting_seconds(*self.key), 0.0)
        self.assertNotIn(self.key, engine._state_since)

    def test_09_waiting_seconds_clears_on_error_and_idle(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        for target in (state.STATE_ERROR, state.STATE_IDLE):
            engine._pending[self.key] = {"rid": target}
            engine._set_state(stage, state.STATE_WAITING)
            self.assertGreater(engine.waiting_seconds(*self.key), 0.0)
            engine._pending.pop(self.key, None)
            self.assertEqual(engine._set_state(stage, target), target)
            self.assertEqual(
                engine.waiting_seconds(*self.key),
                0.0,
                "a settled stage reported a wait: %s" % target,
            )
            self.assertNotIn(self.key, engine._state_since)

    def test_10_unknown_states_and_contradictory_flag_targets_are_refused(self):
        engine = self.engine
        state = self.stack_mod
        stage = self.stage
        engine._set_state(stage, state.STATE_IDLE)
        with self.assertRaises(ValueError):
            engine._set_state(stage, "thinking")
        self.assertEqual(stage.state, state.STATE_IDLE)

        self.assertEqual(engine._set_state(stage, state.STATE_DISABLED), state.STATE_IDLE)
        self.assertEqual(engine._set_state(stage, state.STATE_FROZEN), state.STATE_IDLE)
        self.assertEqual(engine._set_state(stage, state.STATE_CONFLICT), state.STATE_IDLE)

    def test_11_the_test_only_waiting_object_set_is_gone(self):
        self.assertFalse(hasattr(self.engine, "_waiting_objects"))


if __name__ == "__main__":
    unittest.main()
