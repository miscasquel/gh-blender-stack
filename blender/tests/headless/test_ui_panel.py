"""Layer B: the N-panel draws on this Blender build.

The engine tests never draw the panel, so an icon name this Blender build does
not register reaches the UI only at redraw time — and one bad icon aborts the
whole panel, hiding every stage after it. These tests check every icon the panel
can emit against the running build and draw a real stage with a recording
layout, including the failure path of one broken stage.
"""

import ast
import os
import sys
import types
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if os.path.join(REPO, "blender") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "blender"))


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
        return types.SimpleNamespace(index=self.index)

    def prop(self, holder, attr, text=None, **kwargs):
        return None


class PanelHarness:
    """Bind the real panel methods to a recording layout, without registering."""

    def __init__(self, layout, panel):
        self.layout = layout
        # Panel methods are plain functions on the class; bind them to this
        # harness so `self.draw_stage` resolves exactly as it does in Blender.
        self.draw = panel.draw.__get__(self)
        self.draw_stage = panel.draw_stage.__get__(self)


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


if __name__ == "__main__":
    unittest.main()
