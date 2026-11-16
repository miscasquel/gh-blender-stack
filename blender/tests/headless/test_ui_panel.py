"""Layer B: the N-panel draws on this Blender build.

The engine tests never draw the panel, so an icon name this Blender build does
not register reaches the UI only at redraw time — and one bad icon aborts the
whole panel, hiding every stage after it. These tests check every icon the panel
can emit against the running build and draw a real stage with a recording
layout, including the failure path of one broken stage.
"""

import ast
import types
import unittest

import bpy


class FakeLayout:
    """The subset of UILayout the panel uses.

    Icons are checked as they are emitted: a name Blender does not know is what
    raises in a real draw. Container calls return this same layout, which is all
    the panel needs (boxes, rows and columns only group their contents).
    """

    icon_names = frozenset()

    def __init__(self):
        self.labels = []
        self.icons = []
        self.operators = []
        self.props = []
        self.use_property_split = False
        self.use_property_decorate = False
        self.index = -1

    def _icon(self, icon):
        if icon is None:
            return
        self.icons.append(icon)
        if icon not in self.icon_names:
            raise ValueError("icon %r is not registered in this Blender build" % (icon,))

    def row(self):
        return self

    def box(self):
        return self

    def column(self):
        return self

    def label(self, text="", icon=None):
        self._icon(icon)
        self.labels.append(text)

    def operator(self, idname, text="", icon=None, **kwargs):
        self._icon(icon)
        self.operators.append((idname, text))
        return types.SimpleNamespace(index=self.index)

    def prop(self, holder, attr, text=None, **kwargs):
        # Resolve the way a real draw does: reading a dynamic enum (the value
        # list proxy) must be a pure read and must not raise.
        if isinstance(attr, str) and not attr.startswith("["):
            getattr(holder, attr)
        self.props.append((holder, attr, text))
        return None


class PanelHarness:
    """Bind the real panel methods to a recording layout, without registering."""

    def __init__(self, layout, panel):
        self.layout = layout
        # Panel methods are plain functions on the class; bind them to this
        # harness so `self.draw_stage`/`self.draw_param` resolve exactly as they
        # do in Blender. Leaving draw_param unbound made the stage's parameters
        # raise AttributeError, which the per-stage guard hid as "panel failed
        # to draw": the tests must bind it too.
        self.draw = panel.draw.__get__(self)
        self.draw_stage = panel.draw_stage.__get__(self)
        self.draw_param = panel.draw_param.__get__(self)


class PanelIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        from stageloom.blender import ui

        cls.ui = ui

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_every_module_icon_constant_is_registered(self):
        checked = []
        for name, value in vars(self.ui).items():
            if not name.endswith("_ICON") or not isinstance(value, str):
                continue
            checked.append(name)
            self.assertIn(
                value,
                self.ui._ICON_NAMES,
                "%s uses an icon this Blender build does not register" % name,
            )
        self.assertIn("_GENERATOR_ICON", checked)
        self.assertIn("_STAGE_ICON", checked)
        self.assertIn("_BAKE_ICON", checked)

    def test_02_every_state_icon_is_registered(self):
        for state, icon in self.ui._STATE_ICONS.items():
            self.assertIn(
                icon,
                self.ui._ICON_NAMES,
                "the %s state icon is not registered in this Blender build" % state,
            )

    def test_03_every_literal_icon_argument_is_registered(self):
        with open(self.ui.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), self.ui.__file__)
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "icon":
                    continue
                if not isinstance(keyword.value, ast.Constant):
                    continue
                icon = keyword.value.value
                if isinstance(icon, str) and icon not in self.ui._ICON_NAMES:
                    missing.append((icon, keyword.value.lineno))
        self.assertEqual(
            missing,
            [],
            "the panel passes icons this Blender build does not register",
        )


class PanelDrawTests(unittest.TestCase):
    """Draw real stages: the generator label, and one stage failing safely."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        from stageloom.blender import ui

        cls.ui = ui
        FakeLayout.icon_names = ui._ICON_NAMES

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Panel"
        bpy.context.view_layer.objects.active = cls.obj
        cls.obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="Generator")
        cls.generator = cls.obj.gh_stack.stages[0]
        cls.generator.input_mode = "generator"
        bpy.ops.stageloom.add_stage(label="Prefix")
        cls.prefix = cls.obj.gh_stack.stages[1]

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _draw(self):
        layout = FakeLayout()
        PanelHarness(layout, self.ui.STAGELOOM_PT_panel).draw(
            types.SimpleNamespace(object=self.obj)
        )
        return layout

    def test_01_a_generator_stage_draws_with_the_build_icons(self):
        layout = self._draw()
        self.assertIn(
            "Generator (no Stage In)",
            layout.labels,
            "the generator stage did not draw its label",
        )
        for stage in (self.generator, self.prefix):
            self.assertIn(stage.label, layout.labels)
            self.assertIn(stage.key, layout.labels)

    def test_02_a_failing_stage_does_not_hide_the_next(self):
        original = self.ui._state_icon

        def failing(stage):
            if stage.key == self.generator.key:
                raise RuntimeError("deliberate draw failure")
            return original(stage)

        self.ui._state_icon = failing
        try:
            layout = self._draw()
        finally:
            self.ui._state_icon = original
            self.ui._draw_failures.clear()

        self.assertTrue(
            any("panel failed to draw" in text for text in layout.labels),
            "the failed stage did not render an error row: %r" % (layout.labels,),
        )
        self.assertIn(
            "Prefix",
            layout.labels,
            "a failing stage hid the stages after it",
        )
        self.assertIn(self.prefix.key, layout.labels)

    def test_03_response_and_input_warnings_draw_distinctly(self):
        original_warnings = self.prefix.warnings
        original_input = self.prefix.input_warnings
        self.prefix.warnings = "Grasshopper warning"
        self.prefix.input_warnings = "2 Catmull-Rom curves were skipped"
        try:
            layout = self._draw()
            self.assertNotEqual(self.prefix.warnings, self.prefix.input_warnings)
        finally:
            self.prefix.warnings = original_warnings
            self.prefix.input_warnings = original_input

        self.assertIn("Grasshopper warning", layout.labels)
        self.assertIn("Input conversion notes", layout.labels)
        self.assertIn("2 Catmull-Rom curves were skipped", layout.labels)

    def test_04_clear_bake_draws_the_irreversible_warning(self):
        from stageloom.blender import ops

        self.generator.baked = True
        try:
            layout = self._draw()
        finally:
            self.generator.baked = False
        self.assertIn(
            ops.CLEAR_BAKE_WARNING,
            layout.labels,
            "the panel does not warn that Clear Bake is permanent",
        )

    def test_05_a_repeated_draw_failure_is_logged_once_and_cleared_on_recovery(self):
        from unittest import mock

        ui = self.ui
        ui._draw_failures.clear()
        original = ui._state_icon

        def failing(stage):
            if stage.key == self.generator.key:
                raise RuntimeError("deliberate draw failure")
            return original(stage)

        ui._state_icon = failing
        try:
            with mock.patch.object(ui.prefs, "log") as log:
                self._draw()
                self._draw()
                self.assertEqual(
                    log.call_count,
                    1,
                    "a per-redraw failure was logged more than once:\n%r"
                    % (log.call_args_list,),
                )
                # Recovery clears the throttle: the same failure after a
                # successful draw is a new event and is logged again.
                ui._state_icon = original
                self._draw()
                self.assertNotIn(self.generator.key, ui._draw_failures)
                ui._state_icon = failing
                self._draw()
                self.assertEqual(log.call_count, 2)
        finally:
            ui._state_icon = original
            ui._draw_failures.clear()

    def test_06_start_server_is_reachable_while_offline(self):
        from stageloom.blender import bridge

        original = (bridge.state["listening"], bridge.state["connected"])
        try:
            bridge.state["listening"] = False
            bridge.state["connected"] = False
            layout = FakeLayout()
            PanelHarness(layout, self.ui.STAGELOOM_PT_panel).draw(
                types.SimpleNamespace(object=None)
            )
            self.assertIn(
                "stageloom.start_server",
                [idname for idname, _text in layout.operators],
                "the panel does not offer Start Server while the server is off",
            )

            bridge.state["listening"] = True
            layout = FakeLayout()
            PanelHarness(layout, self.ui.STAGELOOM_PT_panel).draw(
                types.SimpleNamespace(object=None)
            )
            self.assertNotIn(
                "stageloom.start_server",
                [idname for idname, _text in layout.operators],
                "Start Server was offered while the server was already listening",
            )
        finally:
            bridge.state["listening"], bridge.state["connected"] = original


class PanelParameterTests(unittest.TestCase):
    """Parameter rows are actually drawn, and drawing them writes nothing."""

    PARAMS = [
        {
            "id": "f1",
            "label": "Float",
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "max": 10.0,
            "step": 0.1,
        },
        {
            "id": "ch1",
            "label": "Mode",
            "type": "value_list",
            "default": 0,
            "values": ["Ribbon", "Panel", "Grid"],
        },
    ]

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        from stageloom.blender import props, stack as stack_mod, ui

        cls.ui = ui
        cls.props_mod = props
        cls.stack_mod = stack_mod
        FakeLayout.icon_names = ui._ICON_NAMES

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "PanelParams"
        bpy.context.view_layer.objects.active = cls.obj
        cls.obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="Parameters")
        cls.stage = cls.obj.gh_stack.stages[0]
        props.apply_schema(cls.obj, cls.stage, cls.PARAMS)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def _draw(self):
        layout = FakeLayout()
        PanelHarness(layout, self.ui.STAGELOOM_PT_panel).draw(
            types.SimpleNamespace(object=self.obj)
        )
        return layout

    def _entry(self, param_id):
        for entry in self.stage.params:
            if entry.param_id == param_id:
                return entry
        self.fail("no parameter %r in %r" % (param_id, self.stage.params))

    def test_01_parameter_rows_are_drawn(self):
        layout = self._draw()
        attrs = [attr for _holder, attr, _text in layout.props]
        float_path = '["%s"]' % self.stack_mod.param_prop_name(
            self.stage.key, "f1"
        )
        self.assertIn(
            float_path,
            attrs,
            "the float parameter row was not drawn: %r" % (attrs,),
        )
        self.assertIn(
            "value_index",
            attrs,
            "the value list dropdown row was not drawn: %r" % (attrs,),
        )
        self.assertFalse(
            any("panel failed to draw" in text for text in layout.labels),
            "drawing the parameters failed: %r" % (layout.labels,),
        )
        # The label editor is part of every stage row.
        self.assertTrue(
            any(
                getattr(holder, "key", None) == self.stage.key and attr == "label"
                for holder, attr, _text in layout.props
            ),
            "the stage label editor was not drawn",
        )

    def test_02_draw_reads_the_value_list_but_never_writes_or_tags(self):
        name = self.stack_mod.param_prop_name(self.stage.key, "ch1")
        entry = self._entry("ch1")
        self.obj[name] = 99  # stale index from a shorter value list
        calls = []
        original = self.stack_mod.notify_param_changed
        self.stack_mod.notify_param_changed = lambda obj: calls.append(obj)
        try:
            self.assertEqual(entry.value_index, "0", "a stale index did not read safely")
            self._draw()
        finally:
            self.stack_mod.notify_param_changed = original

        self.assertEqual(
            self.obj[name],
            99,
            "draw clamped the parameter's custom property",
        )
        self.assertEqual(entry.value_index, "0", "draw wrote the dropdown index")
        self.assertEqual(calls, [], "draw tagged the depsgraph")

        # Schema application is the owner of the clamp, outside draw.
        self.props_mod.apply_schema(self.obj, self.stage, self.PARAMS)
        self.assertEqual(self.obj[name], 2, "schema application did not clamp")
        self.assertEqual(entry.value_index, "2")
        self.obj[name] = 0


class StageLabelTests(unittest.TestCase):
    """Labels are editable and unique by default; keys/helpers/requests are not."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        from stageloom.blender import ui

        cls.ui = ui
        FakeLayout.icon_names = ui._ICON_NAMES

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        self.obj = bpy.context.object
        self.obj.name = "Labels_%s" % self._testMethodName
        bpy.context.view_layer.objects.active = self.obj
        self.obj.select_set(True)

    def test_01_new_stages_get_unique_numbered_labels(self):
        bpy.ops.stageloom.add_stage()
        bpy.ops.stageloom.add_stage()
        labels = [stage.label for stage in self.obj.gh_stack.stages]
        self.assertEqual(labels, ["Stage 1", "Stage 2"])
        keys = [stage.key for stage in self.obj.gh_stack.stages]
        self.assertEqual(len(set(keys)), 2, "the default stages share a key")

    def test_02_an_explicit_label_is_kept(self):
        bpy.ops.stageloom.add_stage(label="Explicit")
        self.assertEqual(self.obj.gh_stack.stages[0].label, "Explicit")

    def test_03_editing_the_label_keeps_key_helper_and_request(self):
        import stageloom.blender.helpers as helpers

        bpy.ops.stageloom.add_stage()
        stage = self.obj.gh_stack.stages[0]
        key = stage.key
        stage.rid = "rid-unchanged"
        helper = helpers.ensure_helper(self.obj, key)
        helper_name = helper.name

        stage.label = "Renamed"

        self.assertEqual(stage.key, key, "editing the label changed the key")
        self.assertEqual(stage.rid, "rid-unchanged", "editing the label changed the request id")
        self.assertIs(
            helpers.helper_for(self.obj, key, "mesh"),
            helper,
            "editing the label lost the helper",
        )
        self.assertIn(helper_name, bpy.data.objects, "editing the label renamed the helper")

        layout = FakeLayout()
        PanelHarness(layout, self.ui.STAGELOOM_PT_panel).draw(
            types.SimpleNamespace(object=self.obj)
        )
        self.assertIn("Renamed", layout.labels, "the panel did not draw the new label")
        self.assertIn(key, layout.labels, "the panel did not keep the key visible")
        self.assertTrue(
            any(
                getattr(holder, "key", None) == key and attr == "label"
                for holder, attr, _text in layout.props
            ),
            "the stage label editor was not drawn",
        )


class OperatorAffordanceTests(unittest.TestCase):
    """Undo flags, polls and the linked-library refusal."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        from stageloom.blender import ops

        cls.ops = ops
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        cls.obj.name = "Affordances"
        bpy.context.view_layer.objects.active = cls.obj
        cls.obj.select_set(True)
        bpy.ops.stageloom.add_stage(label="Affordances")

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def test_01_engine_session_operators_do_not_claim_undo(self):
        for cls in (
            self.ops.STAGELOOM_OT_pull,
            self.ops.STAGELOOM_OT_push,
            self.ops.STAGELOOM_OT_freeze,
        ):
            self.assertNotIn(
                "UNDO",
                cls.bl_options,
                "%s claims an undo step the engine cannot restore" % cls.bl_idname,
            )
        for cls in (
            self.ops.STAGELOOM_OT_add_stage,
            self.ops.STAGELOOM_OT_resolve_duplicate_key,
            self.ops.STAGELOOM_OT_key_all,
            self.ops.STAGELOOM_OT_purge_orphans,
        ):
            self.assertIn(
                "UNDO",
                cls.bl_options,
                "%s changes data but does not offer undo" % cls.bl_idname,
            )
        for cls in (self.ops.STAGELOOM_OT_bake, self.ops.STAGELOOM_OT_clear_bake):
            self.assertNotIn(
                "UNDO",
                cls.bl_options,
                "%s claims undo safety for native bake data" % cls.bl_idname,
            )

    def test_02_stage_operators_poll_and_refuse_invalid_contexts(self):
        no_object = types.SimpleNamespace(object=None)
        self.assertFalse(self.ops.STAGELOOM_OT_add_stage.poll(no_object))
        self.assertFalse(self.ops.STAGELOOM_OT_freeze.poll(no_object))

        record = types.SimpleNamespace(stages=[types.SimpleNamespace()])
        obj = types.SimpleNamespace(type="MESH", library=None, gh_stack=record)
        context = types.SimpleNamespace(object=obj)
        self.assertTrue(self.ops.STAGELOOM_OT_add_stage.poll(context))
        for cls in (
            self.ops.STAGELOOM_OT_pull,
            self.ops.STAGELOOM_OT_push,
            self.ops.STAGELOOM_OT_freeze,
            self.ops.STAGELOOM_OT_resolve_duplicate_key,
            self.ops.STAGELOOM_OT_key_all,
            self.ops.STAGELOOM_OT_purge_orphans,
            self.ops.STAGELOOM_OT_bake,
            self.ops.STAGELOOM_OT_clear_bake,
            self.ops.STAGELOOM_OT_copy_key,
        ):
            self.assertTrue(cls.poll(context), "%s refused a valid context" % cls.bl_idname)

        empty = types.SimpleNamespace(
            type="MESH", library=None, gh_stack=types.SimpleNamespace(stages=[])
        )
        self.assertFalse(
            self.ops.STAGELOOM_OT_freeze.poll(types.SimpleNamespace(object=empty)),
            "a stack with no stages was accepted",
        )
        self.assertFalse(
            self.ops.STAGELOOM_OT_add_stage.poll(
                types.SimpleNamespace(object=types.SimpleNamespace(type="SURFACE", library=None))
            ),
            "a surface object was accepted",
        )

    def test_03_linked_objects_refuse_writes_but_allow_copy_key(self):
        record = types.SimpleNamespace(stages=[types.SimpleNamespace()])
        linked = types.SimpleNamespace(type="MESH", library=object(), gh_stack=record)
        context = types.SimpleNamespace(object=linked)
        self.assertFalse(
            self.ops.STAGELOOM_OT_add_stage.poll(context),
            "add_stage accepted a linked-library object",
        )
        for cls in (
            self.ops.STAGELOOM_OT_pull,
            self.ops.STAGELOOM_OT_push,
            self.ops.STAGELOOM_OT_freeze,
            self.ops.STAGELOOM_OT_resolve_duplicate_key,
            self.ops.STAGELOOM_OT_key_all,
            self.ops.STAGELOOM_OT_purge_orphans,
            self.ops.STAGELOOM_OT_bake,
            self.ops.STAGELOOM_OT_clear_bake,
        ):
            self.assertFalse(
                cls.poll(context),
                "%s accepted a linked-library object" % cls.bl_idname,
            )
        self.assertTrue(
            self.ops.STAGELOOM_OT_copy_key.poll(context),
            "copying the key of a linked stage is a read and should be allowed",
        )

    def test_04_an_out_of_range_row_cancels_cleanly(self):
        self.assertEqual(bpy.ops.stageloom.pull(index=99), {"CANCELLED"})
        self.assertEqual(bpy.ops.stageloom.freeze(index=99), {"CANCELLED"})


if __name__ == "__main__":
    unittest.main()
