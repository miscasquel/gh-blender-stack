"""Parameter UI plumbing that can be checked headless."""

import unittest
from types import SimpleNamespace
from unittest import mock

import bpy

STAGE_KEY = "s5-test"

BASE_PARAMS = [
    {
        "id": "f1",
        "label": "Float",
        "type": "float",
        "default": 1.0,
        "min": 0.0,
        "max": 10.0,
        "step": 0.1,
    },
    {"id": "i1", "label": "Int", "type": "int", "default": 2, "min": 0, "max": 5},
    {"id": "b1", "label": "Bool", "type": "bool", "default": False},
    {
        "id": "v1",
        "label": "Vector",
        "type": "vector3",
        "default": [0.0, 1.0, 2.0],
    },
    {
        "id": "c1",
        "label": "Colour",
        "type": "color",
        "default": [1.0, 0.5, 0.0],
    },
    {
        "id": "ch1",
        "label": "Mode",
        "type": "value_list",
        "default": 1,
        "values": ["Ribbon", "Panel", "Grid"],
    },
    {"id": "t1", "label": "Note", "type": "text", "default": "hello"},
]

# One transition per storage family. Each source is incompatible with its
# target, so a correct implementation must replace the property.
MIGRATIONS = [
    (
        {"id": "mig", "label": "Migrate", "type": "int", "default": 1, "min": 0, "max": 5},
        3,
        {
            "id": "mig",
            "label": "Migrate",
            "type": "vector3",
            "default": [1.0, 2.0, 3.0],
        },
        [1.0, 2.0, 3.0],
    ),
    (
        {
            "id": "mig",
            "label": "Migrate",
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "max": 5.0,
            "step": 0.1,
        },
        2.5,
        {
            "id": "mig",
            "label": "Migrate",
            "type": "color",
            "default": [0.25, 0.5, 0.75],
        },
        [0.25, 0.5, 0.75],
    ),
    (
        {
            "id": "mig",
            "label": "Migrate",
            "type": "float",
            "default": 1.0,
            "min": 0.0,
            "max": 5.0,
            "step": 0.1,
        },
        2.5,
        {
            "id": "mig",
            "label": "Migrate",
            "type": "value_list",
            "default": 1,
            "values": ["A", "B"],
        },
        1,
    ),
    (
        {"id": "mig", "label": "Migrate", "type": "int", "default": 1, "min": 0, "max": 5},
        3,
        {"id": "mig", "label": "Migrate", "type": "text", "default": "hi"},
        "hi",
    ),
    (
        {"id": "mig", "label": "Migrate", "type": "int", "default": 1, "min": 0, "max": 5},
        3,
        {"id": "mig", "label": "Migrate", "type": "bool", "default": True},
        True,
    ),
    (
        {
            "id": "mig",
            "label": "Migrate",
            "type": "vector3",
            "default": [0.0, 0.0, 0.0],
        },
        [1.0, 2.0, 3.0],
        {"id": "mig", "label": "Migrate", "type": "int", "default": 4, "min": 0, "max": 9},
        4,
    ),
    (
        {
            "id": "mig",
            "label": "Migrate",
            "type": "color",
            "default": [0.0, 0.0, 0.0],
        },
        [0.5, 0.5, 0.5],
        {
            "id": "mig",
            "label": "Migrate",
            "type": "float",
            "default": 0.5,
            "min": 0.0,
            "max": 1.0,
            "step": 0.1,
        },
        0.5,
    ),
    (
        {
            "id": "mig",
            "label": "Migrate",
            "type": "value_list",
            "default": 0,
            "values": ["A", "B"],
        },
        1,
        {
            "id": "mig",
            "label": "Migrate",
            "type": "float",
            "default": 1.5,
            "min": 0.0,
            "max": 2.0,
            "step": 0.1,
        },
        1.5,
    ),
    (
        {"id": "mig", "label": "Migrate", "type": "text", "default": "old"},
        "old",
        {
            "id": "mig",
            "label": "Migrate",
            "type": "float",
            "default": 1.5,
            "min": 0.0,
            "max": 2.0,
            "step": 0.1,
        },
        1.5,
    ),
    (
        {"id": "mig", "label": "Migrate", "type": "bool", "default": False},
        True,
        {
            "id": "mig",
            "label": "Migrate",
            "type": "float",
            "default": 1.5,
            "min": 0.0,
            "max": 2.0,
            "step": 0.1,
        },
        1.5,
    ),
]


def param_path(name):
    """The RNA path for a custom property, as keyframe_insert/driver_add want it."""
    return '["%s"]' % name


def param_name(pid):
    """The custom property name for one parameter of the test stage."""
    return "gh/%s/%s" % (STAGE_KEY, pid)


class ParamUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.bridge as bridge
        import stageloom.blender.prefs as prefs
        import stageloom.blender.props as props
        import stageloom.blender.stack as stack_mod

        cls.bridge = bridge
        cls.prefs = prefs
        cls.props = props
        cls.stack_mod = stack_mod

    def setUp(self):
        # Every test gets its own object and schema: a mutation in one test can
        # no longer hide a failure in another (the old shared class fixture had
        # test_08b remove every parameter test_09 then reused).
        bpy.context.scene.frame_set(1)
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        self.obj = bpy.context.object
        self.obj.name = "ParamUi_%s" % self._testMethodName
        record = self.stack_mod.ensure_stack(self.obj)
        self.stage = record.stages.add()
        self.stage.key = STAGE_KEY
        self.stage.label = "Params"
        self.apply(BASE_PARAMS)

    # ------------------------------------------------------------- helpers

    def apply(self, params):
        self.props.apply_schema(self.obj, self.stage, params)

    def name(self, pid):
        return param_name(pid)

    def entry(self, pid):
        for entry in self.stage.params:
            if entry.param_id == pid:
                return entry
        self.fail("no parameter %r in %r" % (pid, [e.param_id for e in self.stage.params]))

    def plain_value(self, pid):
        value = self.obj[self.name(pid)]
        return list(value) if hasattr(value, "to_list") else value

    def ui(self, pid):
        return self.obj.id_properties_ui(self.name(pid)).as_dict()

    def paths(self):
        return [fcurve.data_path for _bag, fcurve in self.props._fcurve_slots(self.obj)]

    def drivers(self):
        animation = getattr(self.obj, "animation_data", None)
        if animation is None:
            return []
        return [driver.data_path for driver in animation.drivers]

    def key(self, pid, value, frame=1):
        name = self.name(pid)
        self.obj[name] = value
        self.obj.keyframe_insert(data_path=param_path(name), frame=frame)
        return name

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
        # The bounds are stored independently, not behind one has_range flag.
        self.assertTrue(self.entry("f1").has_min)
        self.assertTrue(self.entry("f1").has_max)
        self.assertFalse(self.entry("v1").has_min)
        self.assertFalse(self.entry("v1").has_max)

    def test_02_min_only_uses_the_type_maximum(self):
        cases = [
            ("min_float", "float", 2.5, 1e30),
            ("min_int", "int", 2, 2 ** 31 - 1),
        ]
        for pid, param_type, declared, expected_max in cases:
            with self.subTest(param_type=param_type):
                self.apply(
                    [
                        {
                            "id": pid,
                            "label": "Min only",
                            "type": param_type,
                            "default": 0,
                            "min": declared,
                            "step": 0.1,
                        }
                    ]
                )
                entry = self.entry(pid)
                self.assertTrue(entry.has_min)
                self.assertFalse(entry.has_max)
                ui = self.ui(pid)
                self.assertEqual(ui["min"], declared)
                if isinstance(expected_max, float):
                    self.assertGreater(ui["max"], expected_max)
                else:
                    self.assertEqual(ui["max"], expected_max)
                self.assertNotEqual(ui["max"], 1.0)
                schema = self.props.schema_of(self.stage)[0]
                self.assertEqual(schema["min"], declared)
                self.assertIsNone(schema["max"])

    def test_03_max_only_uses_the_type_minimum(self):
        cases = [
            ("max_float", "float", 7.5, -1e30),
            ("max_int", "int", 4, -(2 ** 31)),
        ]
        for pid, param_type, declared, expected_min in cases:
            with self.subTest(param_type=param_type):
                self.apply(
                    [
                        {
                            "id": pid,
                            "label": "Max only",
                            "type": param_type,
                            "default": 0,
                            "max": declared,
                            "step": 0.1,
                        }
                    ]
                )
                entry = self.entry(pid)
                self.assertFalse(entry.has_min)
                self.assertTrue(entry.has_max)
                ui = self.ui(pid)
                self.assertEqual(ui["max"], declared)
                if isinstance(expected_min, float):
                    self.assertLess(ui["min"], expected_min)
                else:
                    self.assertEqual(ui["min"], expected_min)
                self.assertNotEqual(ui["min"], 0.0)
                schema = self.props.schema_of(self.stage)[0]
                self.assertIsNone(schema["min"])
                self.assertEqual(schema["max"], declared)

    def test_04_max_zero_is_kept(self):
        for pid, param_type in (("zero_float", "float"), ("zero_int", "int")):
            with self.subTest(param_type=param_type):
                self.apply(
                    [
                        {
                            "id": pid,
                            "label": "At zero",
                            "type": param_type,
                            "default": 0,
                            "max": 0,
                            "step": 0.1,
                        }
                    ]
                )
                entry = self.entry(pid)
                self.assertTrue(entry.has_max)
                self.assertEqual(entry.max, 0.0)
                ui = self.ui(pid)
                self.assertEqual(ui["max"], 0)
                self.assertNotEqual(ui["max"], 1)
                self.assertEqual(self.props.schema_of(self.stage)[0]["max"], 0.0)

    def test_05_absent_sides_clear_a_previously_set_range(self):
        pid = "range"
        base = {
            "id": pid,
            "label": "Range",
            "type": "float",
            "default": 1.0,
            "step": 0.1,
        }
        self.apply([dict(base, min=1.0, max=2.0)])
        self.assertEqual(self.ui(pid)["min"], 1.0)
        self.assertEqual(self.ui(pid)["max"], 2.0)

        # Keep min only: the old max must go back to the type maximum, not stay
        # 2.0 and not become the invented 1.0.
        self.apply([dict(base, min=1.0)])
        ui = self.ui(pid)
        self.assertEqual(ui["min"], 1.0)
        self.assertGreater(ui["max"], 1e30)
        self.assertFalse(self.entry(pid).has_max)

        # Keep max only: the old min must go back to the type minimum.
        self.apply([dict(base, max=3.0)])
        ui = self.ui(pid)
        self.assertLess(ui["min"], -1e30)
        self.assertEqual(ui["max"], 3.0)
        self.assertFalse(self.entry(pid).has_min)

        # No bounds at all: both sides are cleared.
        self.apply([base])
        ui = self.ui(pid)
        self.assertLess(ui["min"], -1e30)
        self.assertGreater(ui["max"], 1e30)
        schema = self.props.schema_of(self.stage)[0]
        self.assertIsNone(schema["min"])
        self.assertIsNone(schema["max"])

    def test_06_one_sided_bounds_are_schema_identity(self):
        params = [
            {
                "id": "sig",
                "label": "Signature",
                "type": "float",
                "default": 0.0,
                "min": 2.0,
                "step": 0.1,
            }
        ]
        self.apply(params)
        self.assertEqual(
            self.props.schema_signature(self.stage),
            self.props.incoming_signature(params),
        )
        # A driven default is not declaration identity.
        next_frame = [dict(params[0], default=9.0)]
        self.assertEqual(
            self.props.schema_signature(self.stage),
            self.props.incoming_signature(next_frame),
        )
        # A one-sided range change is.
        changed_min = [dict(params[0], min=3.0)]
        self.assertNotEqual(
            self.props.schema_signature(self.stage),
            self.props.incoming_signature(changed_min),
        )
        added_max = [dict(params[0], max=8.0)]
        self.assertNotEqual(
            self.props.schema_signature(self.stage),
            self.props.incoming_signature(added_max),
        )
        # Same number, different declared side: still a real change.
        moved_side = [
            {
                "id": "sig",
                "label": "Signature",
                "type": "float",
                "default": 0.0,
                "max": 2.0,
                "step": 0.1,
            }
        ]
        self.assertNotEqual(
            self.props.schema_signature(self.stage),
            self.props.incoming_signature(moved_side),
        )

    # -------------------------------------------------------------- keyframes

    def test_07_float_keyframes_interpolate(self):
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

    def test_08_fcurves_use_the_layered_action_api(self):
        # Blender 5.2 has no action.fcurves; keyframes live under
        # layers/strips/channelbags.
        obj = self.obj
        name = self.name("f1")
        self.key("f1", 2.0)
        action = obj.animation_data.action
        self.assertIsNotNone(action)
        paths = []
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for fcurve in bag.fcurves:
                        paths.append(fcurve.data_path)
        self.assertTrue(any(name in path for path in paths), paths)

    def test_09_int_and_choice_hold_with_constant_interpolation(self):
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
                        if name in fcurve.data_path:
                            for point in fcurve.keyframe_points:
                                point.interpolation = "CONSTANT"

        bpy.context.scene.frame_set(5)
        # Constant interpolation holds the previous key, not a blend.
        self.assertEqual(obj[name], 0)
        bpy.context.scene.frame_set(12)
        self.assertEqual(obj[name], 4)

    def test_10_vector_keyframes_per_component(self):
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
                        if name in fcurve.data_path:
                            indices.add(fcurve.array_index)
        self.assertEqual(indices, {0, 1, 2}, indices)

        bpy.context.scene.frame_set(6)
        value = obj[name]
        self.assertAlmostEqual(value[0], 5.0, places=4)
        self.assertAlmostEqual(value[1], 10.0, places=4)
        self.assertAlmostEqual(value[2], 15.0, places=4)

    def test_11_colour_keyframes(self):
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

    def test_12_driver_on_a_custom_prop(self):
        obj = self.obj
        name = self.name("f1")
        driver = obj.driver_add(param_path(name))
        driver.driver.type = "SCRIPTED"
        driver.driver.expression = "frame * 0.5"
        bpy.context.scene.frame_set(4)
        self.assertAlmostEqual(obj[name], 2.0, places=5)
        bpy.context.scene.frame_set(8)
        self.assertAlmostEqual(obj[name], 4.0, places=5)

    # ----------------------------------------------------------------- choice

    def test_13_value_list_dropdown_proxy(self):
        entry = self.entry("ch1")
        # Dynamic enum items come from the callback; bl_rna does not evaluate it
        # without a UI context, so call it the way Blender would.
        items = self.stack_mod._value_items(entry, None)
        self.assertEqual([i[1] for i in items], ["Ribbon", "Panel", "Grid"])

        entry.value_index = "2"
        # The update callback writes the custom property, which is the real value.
        self.assertEqual(self.obj[self.name("ch1")], 2)

    def test_14_out_of_range_value_list_index_is_clamped(self):
        name = self.name("ch1")
        self.obj[name] = 7  # as a stale value from a shorter choice list
        self.apply(
            [
                {
                    "id": "ch1",
                    "label": "Mode",
                    "type": "value_list",
                    "default": 1,
                    "values": ["Ribbon", "Panel", "Grid"],
                }
            ]
        )
        self.assertEqual(self.obj[name], 2)  # clamped to the last valid index
        # The entry survives the refresh (reconciled, not rebuilt) and the
        # dropdown proxy accepts the clamped index.
        entry = self.entry("ch1")
        self.assertEqual(entry.values, "Ribbon\nPanel\nGrid")
        entry.value_index = str(int(self.obj[name]))
        self.assertEqual(entry.value_index, "2")

    # -------------------------------------------------------------- migrations

    def assert_migrated(self, name, entry_type, expected):
        value = self.plain_value("mig")
        self.assertEqual(value, expected)
        self.assertEqual(self.entry("mig").type, entry_type)
        self.assertFalse(
            any(name in path for path in self.paths()),
            "the old keyframes survived the type change: %r" % (self.paths(),),
        )

    def test_15_int_to_float_migrates_value_and_animation(self):
        self.apply(
            [
                {
                    "id": "mig",
                    "label": "Migrate",
                    "type": "int",
                    "default": 2,
                    "min": 0,
                    "max": 5,
                }
            ]
        )
        name = self.key("mig", 3)
        self.obj.driver_add(param_path(name))
        self.bridge.state["notice"] = ""

        self.apply(
            [
                {
                    "id": "mig",
                    "label": "Migrate",
                    "type": "float",
                    "default": 1.25,
                    "min": 0.0,
                    "max": 5.0,
                    "step": 0.1,
                }
            ]
        )
        self.assertIsInstance(self.obj[name], float)
        self.assertEqual(self.obj[name], 1.25)
        self.assert_migrated(name, "float", 1.25)
        self.assertNotIn(param_path(name), self.drivers())
        self.assertIn("changed type to float", self.bridge.state.get("notice", ""))

    def test_16_float_to_int_migrates_value_and_keys(self):
        self.apply(
            [
                {
                    "id": "mig",
                    "label": "Migrate",
                    "type": "float",
                    "default": 1.0,
                    "min": 0.0,
                    "max": 5.0,
                    "step": 0.1,
                }
            ]
        )
        name = self.key("mig", 2.75)
        self.bridge.state["notice"] = ""

        self.apply(
            [
                {
                    "id": "mig",
                    "label": "Migrate",
                    "type": "int",
                    "default": 2,
                    "min": 0,
                    "max": 5,
                }
            ]
        )
        self.assertIsInstance(self.obj[name], int)
        self.assertFalse(isinstance(self.obj[name], bool))
        self.assert_migrated(name, "int", 2)
        self.assertIn("changed type to int", self.bridge.state.get("notice", ""))

    def test_17_every_type_target_migrates(self):
        for source, source_value, target, expected in MIGRATIONS:
            with self.subTest(source=source["type"], target=target["type"]):
                self.apply([source])
                name = self.name("mig")
                self.obj[name] = source_value
                if source["type"] != "text":
                    self.obj.keyframe_insert(data_path=param_path(name), frame=1)
                self.bridge.state["notice"] = ""

                self.apply([target])
                self.assertEqual(self.entry("mig").type, target["type"])
                self.assertEqual(self.plain_value("mig"), expected)
                if source["type"] != "text":
                    self.assert_migrated(name, target["type"], expected)
                else:
                    self.assertFalse(any(name in path for path in self.paths()))
                self.assertIn(
                    "changed type to %s" % target["type"],
                    self.bridge.state.get("notice", ""),
                )

    def test_18_same_type_refresh_keeps_values_and_keyframes(self):
        values = {
            "f1": 3.5,
            "i1": 4,
            "b1": True,
            "v1": [4.0, 5.0, 6.0],
            "c1": [0.2, 0.4, 0.6],
            "ch1": 2,
            "t1": "world",
        }
        for pid, value in values.items():
            if pid != "t1":
                self.key(pid, value)
            else:
                self.obj[self.name(pid)] = value

        refreshed = []
        for param in BASE_PARAMS:
            changed = dict(param)
            changed["default"] = {
                "float": 99.0,
                "int": 99,
                "bool": True,
                "vector3": [9.0, 9.0, 9.0],
                "color": [0.0, 0.0, 0.0],
                "value_list": 0,
                "text": "ignored",
            }[param["type"]]
            if param["id"] == "f1":
                # A same-type declaration change (range/step) is not a migration.
                changed["max"] = 20.0
                changed["step"] = 0.5
            refreshed.append(changed)
        self.apply(refreshed)

        for pid, value in values.items():
            self.assertEqual(self.plain_value(pid), value)
        for pid in ("f1", "i1", "b1", "v1", "c1", "ch1"):
            path = param_path(self.name(pid))
            self.assertIn(path, self.paths(), "same-type refresh removed %s" % path)
        self.assertEqual(len(self.stage.params), len(BASE_PARAMS))

    # ------------------------------------------------------------ schema ids

    def test_19_duplicate_ids_reuse_one_entry(self):
        duplicate = [
            {"id": "dup", "label": "First", "type": "float", "default": 1.0},
            {"id": "dup", "label": "Second", "type": "float", "default": 2.0},
        ]
        self.apply(duplicate)
        entries = [entry for entry in self.stage.params if entry.param_id == "dup"]
        self.assertEqual(len(entries), 1, "duplicate ids created two schema entries")
        self.assertEqual(entries[0].label, "Second")
        self.assertEqual(self.obj[self.name("dup")], 2.0)

        # A later heartbeat with the same duplicate list still updates the one
        # entry and leaves the property (and its keyframe) alone.
        self.key("dup", 4.0)
        self.apply(duplicate)
        entries = [entry for entry in self.stage.params if entry.param_id == "dup"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.obj[self.name("dup")], 4.0)
        self.assertIn(param_path(self.name("dup")), self.paths())

    # ---------------------------------------------------------- UI failures

    def test_20_rejected_ui_update_is_logged_and_range_cleared(self):
        entry = SimpleNamespace(
            param_id="ui",
            label="UI",
            type="int",
            has_min=True,
            has_max=False,
            min=2.0,
            max=0.0,
            step=0.01,
            values="",
        )

        class FakeUi:
            def __init__(self):
                self.calls = []

            def update(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    raise TypeError("range rejected")

        class FakeObj:
            def __init__(self):
                self.ui = FakeUi()

            def id_properties_ui(self, _name):
                return self.ui

        fake = FakeObj()
        with mock.patch.object(self.prefs, "log") as log:
            self.props._set_ui(fake, "gh/s5-test/ui", entry)

        self.assertTrue(log.called, "a rejected UI update was swallowed silently")
        messages = [str(call.args[0]) for call in log.call_args_list if call.args]
        self.assertTrue(any("rejected" in message for message in messages), messages)
        self.assertGreaterEqual(len(fake.ui.calls), 2)
        fallback = fake.ui.calls[-1]
        self.assertEqual(fallback["min"], -(2 ** 31))
        self.assertEqual(fallback["max"], 2 ** 31 - 1)

    def test_21_text_property_round_trips(self):
        obj = self.obj
        name = self.name("t1")
        obj[name] = "changed"
        self.assertEqual(obj[name], "changed")
        # Text is not animatable by design; no keyframe test.


if __name__ == "__main__":
    unittest.main()
