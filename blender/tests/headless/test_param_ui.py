"""S5: parameter UI plumbing that can be checked headless.

Keyframes, constant interpolation, per-component array keyframes, drivers, and
the choice dropdown's proxy property. The visual decorators (the keyframe diamond
and driver badge) are a GUI check; these tests prove the underlying properties
behave.

Run with scripts/test-blender.
"""

import os
import sys
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if os.path.join(REPO, "blender") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "blender"))


def param_path(name):
    """The RNA path for a custom property, as keyframe_insert/driver_add want it."""
    return '["%s"]' % name


def param_name(pid):
    """The custom property name for one parameter of the test stage."""
    return "gh/s5-test/%s" % pid


class ParamUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.props as props
        import stageloom.blender.stack as stack_mod

        cls.stack_mod = stack_mod
        cls.props = props

        bpy.ops.mesh.primitive_cube_add(size=2.0)
        cls.obj = bpy.context.object
        record = stack_mod.ensure_stack(cls.obj)
        stage = record.stages.add()
        stage.key = "s5-test"
        stage.label = "S5"
        cls.stage = stage
        props.apply_schema(
            cls.obj,
            stage,
            [
                {"id": "f1", "label": "Float", "type": "float", "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.1},
                {"id": "i1", "label": "Int", "type": "int", "default": 2, "min": 0, "max": 5},
                {"id": "b1", "label": "Bool", "type": "bool", "default": False},
                {"id": "v1", "label": "Vector", "type": "vector3", "default": [0.0, 1.0, 2.0]},
                {"id": "c1", "label": "Colour", "type": "color", "default": [1.0, 0.5, 0.0]},
                {"id": "ch1", "label": "Mode", "type": "value_list", "default": 1, "values": ["Ribbon", "Panel", "Grid"]},
                {"id": "t1", "label": "Note", "type": "text", "default": "hello"},
            ],
        )
        cls.name = staticmethod(param_name)

    # ------------------------------------------------------------- properties

    def test_01_types_and_ranges(self):
        obj = self.obj
        self.assertIsInstance(obj[self.name("f1")], float)
        self.assertIsInstance(obj[self.name("i1")], int)
        self.assertIsInstance(obj[self.name("b1")], bool)
        self.assertEqual(len(obj[self.name("v1")]), 3)
        self.assertEqual(len(obj[self.name("c1")]), 3)
        self.assertEqual(obj[self.name("t1")], "hello")
        ui = obj.id_properties_ui(self.name("f1"))
        self.assertEqual(ui.as_dict()["min"], 0.0)
        self.assertEqual(ui.as_dict()["max"], 10.0)
        self.assertEqual(obj.id_properties_ui(self.name("c1")).as_dict()["subtype"], "COLOR")
        self.assertEqual(obj.id_properties_ui(self.name("v1")).as_dict()["subtype"], "XYZ")
        # A parameter with no declared range must not be constrained.
        self.assertLess(obj.id_properties_ui(self.name("v1")).as_dict()["min"], -1e30)
        self.assertGreater(obj.id_properties_ui(self.name("v1")).as_dict()["max"], 1e30)

    # -------------------------------------------------------------- keyframes

    def test_02_float_keyframes_interpolate(self):
        obj = self.obj
        name = self.name("f1")
        bpy.context.scene.frame_set(1)
        obj[name] = 1.0
        obj.keyframe_insert(data_path=param_path(name), frame=1)
        bpy.context.scene.frame_set(10)
        obj[name] = 5.0
        obj.keyframe_insert(data_path=param_path(name), frame=10)

        bpy.context.scene.frame_set(5)
        value = obj[name]
        self.assertGreater(value, 2.5)
        self.assertLess(value, 3.5)
        self.assertNotEqual(value, 1.0)

    def test_03_fcurves_use_the_layered_action_api(self):
        # The plan's note: Blender 5.2 has no action.fcurves; keyframes live under
        # layers/strips/channelbags.
        obj = self.obj
        action = obj.animation_data.action
        self.assertIsNotNone(action)
        paths = []
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for fcurve in bag.fcurves:
                        paths.append(fcurve.data_path)
        self.assertTrue(any(self.name("f1") in p for p in paths), paths)

    def test_04_int_and_choice_hold_with_constant_interpolation(self):
        obj = self.obj
        name = self.name("i1")
        bpy.context.scene.frame_set(1)
        obj[name] = 0
        obj.keyframe_insert(data_path=param_path(name), frame=1)
        bpy.context.scene.frame_set(10)
        obj[name] = 4
        obj.keyframe_insert(data_path=param_path(name), frame=10)

        for layer in obj.animation_data.action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for fcurve in bag.fcurves:
                        if self.name("i1") in fcurve.data_path:
                            for kp in fcurve.keyframe_points:
                                kp.interpolation = "CONSTANT"

        bpy.context.scene.frame_set(5)
        # Constant interpolation holds the previous key, not a blend.
        self.assertEqual(obj[name], 0)
        bpy.context.scene.frame_set(12)
        self.assertEqual(obj[name], 4)

    def test_05_vector_keyframes_per_component(self):
        obj = self.obj
        name = self.name("v1")
        bpy.context.scene.frame_set(1)
        obj[name] = [0.0, 0.0, 0.0]
        obj.keyframe_insert(data_path=param_path(name), frame=1)
        bpy.context.scene.frame_set(11)
        obj[name] = [10.0, 20.0, 30.0]
        obj.keyframe_insert(data_path=param_path(name), frame=11)

        indices = set()
        for layer in obj.animation_data.action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for fcurve in bag.fcurves:
                        if self.name("v1") in fcurve.data_path:
                            indices.add(fcurve.array_index)
        self.assertEqual(indices, {0, 1, 2}, indices)

        bpy.context.scene.frame_set(6)
        value = obj[name]
        self.assertAlmostEqual(value[0], 5.0, places=4)
        self.assertAlmostEqual(value[1], 10.0, places=4)
        self.assertAlmostEqual(value[2], 15.0, places=4)

    def test_06_colour_keyframes(self):
        obj = self.obj
        name = self.name("c1")
        bpy.context.scene.frame_set(1)
        obj[name] = [0.0, 0.0, 0.0]
        obj.keyframe_insert(data_path=param_path(name), frame=1)
        bpy.context.scene.frame_set(9)
        obj[name] = [1.0, 1.0, 1.0]
        obj.keyframe_insert(data_path=param_path(name), frame=9)
        bpy.context.scene.frame_set(5)
        value = obj[name]
        self.assertAlmostEqual(value[0], 0.5, places=4)

    def test_07_driver_on_a_custom_prop(self):
        obj = self.obj
        name = self.name("f1")
        # Clear keyframes so the driver is the only thing acting on it.
        if obj.animation_data is not None and obj.animation_data.action is not None:
            action = obj.animation_data.action
            for layer in list(action.layers):
                for strip in list(layer.strips):
                    for bag in list(strip.channelbags):
                        for fcurve in list(bag.fcurves):
                            if self.name("f1") in fcurve.data_path:
                                bag.fcurves.remove(fcurve)

        driver = obj.driver_add(param_path(name))
        driver.driver.type = "SCRIPTED"
        driver.driver.expression = "frame * 0.5"
        bpy.context.scene.frame_set(4)
        self.assertAlmostEqual(obj[name], 2.0, places=5)
        bpy.context.scene.frame_set(8)
        self.assertAlmostEqual(obj[name], 4.0, places=5)

    # ----------------------------------------------------------------- choice

    def test_08_value_list_dropdown_proxy(self):
        stage = self.stage
        entry = None
        for e in stage.params:
            if e.param_id == "ch1":
                entry = e
        self.assertIsNotNone(entry)
        # Dynamic enum items come from the callback; bl_rna does not evaluate it
        # without a UI context, so call it the way Blender would.
        items = self.stack_mod._value_items(entry, None)
        self.assertEqual([i[1] for i in items], ["Ribbon", "Panel", "Grid"])

        entry.value_index = "2"
        # The update callback writes the custom property, which is the real value.
        self.assertEqual(self.obj[self.name("ch1")], 2)

    def test_08b_out_of_range_value_list_index_is_clamped(self):
        stage = self.stage
        entry = None
        for e in stage.params:
            if e.param_id == "ch1":
                entry = e
        name = self.name("ch1")
        self.obj[name] = 7  # as a stale value from a shorter choice list
        self.props.apply_schema(
            self.obj,
            stage,
            [{"id": "ch1", "label": "Mode", "type": "value_list", "default": 1,
              "values": ["Ribbon", "Panel", "Grid"]}],
        )
        self.assertEqual(self.obj[name], 2)  # clamped to the last valid index
        # The entry survives the refresh (reconciled, not rebuilt) and the
        # dropdown proxy accepts the clamped index.
        entry = None
        for e in stage.params:
            if e.param_id == "ch1":
                entry = e
        self.assertEqual(entry.values, "Ribbon\nPanel\nGrid")
        entry.value_index = str(int(self.obj[name]))
        self.assertEqual(entry.value_index, "2")

    def test_09_type_change_migrates_the_property(self):
        """A parameter whose GH type changed must not break the engine: the
        property is recreated from the new default, and a stale value that cannot
        be cast falls back to the default instead of raising."""
        obj = self.obj
        stage = self.stage
        props = self.props
        name = self.name("f1")

        # Pretend GH changed "f1" from float to vector3.
        props.apply_schema(
            obj,
            stage,
            [{"id": "f1", "label": "Float", "type": "vector3", "default": [1.0, 2.0, 3.0]}],
        )
        self.assertEqual(len(obj[name]), 3)
        self.assertEqual(list(obj[name]), [1.0, 2.0, 3.0])

        # A value that does not fit (as if keyframed before the change) is dropped
        # from the cast, and the schema default is used.
        obj[name] = 0.5
        values = props.cast_values(obj, stage)
        self.assertEqual(values["f1"], (1.0, 2.0, 3.0))

        # The old keys and drivers cannot drive the new type: they are removed,
        # and the panel is told.
        import stageloom.blender.bridge as bridge

        self.assertIn("changed type to vector3", bridge.state.get("notice", ""))

        # Put it back for the later tests.
        obj[name] = 3.0
        obj.keyframe_insert(data_path=param_path(name), frame=1)
        props.apply_schema(
            obj,
            stage,
            [{"id": "f1", "label": "Float", "type": "vector3", "default": [0.0, 0.0, 0.0]}],
        )
        props.apply_schema(
            obj,
            stage,
            [{"id": "f1", "label": "Float", "type": "float", "default": 1.0, "min": 0.0, "max": 10.0}],
        )
        self.assertIsInstance(obj[name], float)
        # The keyframe from before the change is gone with the old property.
        paths = []
        if obj.animation_data is not None and obj.animation_data.action is not None:
            for layer in obj.animation_data.action.layers:
                for strip in layer.strips:
                    for bag in strip.channelbags:
                        for fcurve in bag.fcurves:
                            paths.append(fcurve.data_path)
        self.assertFalse(any(name in p for p in paths), paths)

    def test_10_text_property_round_trips(self):
        obj = self.obj
        name = self.name("t1")
        obj[name] = "changed"
        self.assertEqual(obj[name], "changed")
        # Text is not animatable by design (plan §7); no keyframe test.


if __name__ == "__main__":
    unittest.main()
