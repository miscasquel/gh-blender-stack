"""Layer B: upstream stage dependencies, and later the chain bake.

Run with:

    scripts/test-blender

W2.2: a stage depends on the stages before it on its own object and on the
stages of objects or collections its prefix reads, transitively. A downstream
consumer is not an upstream dependency, even when it reads the selected stage.
The bake tests (W2.1) and the offline render acceptance (W2.3) extend this file.
"""

import os
import sys
import tempfile
import time
import unittest

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BLENDER_PKG = os.path.join(REPO, "blender")
for path in (BLENDER_PKG, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_duplicates import enable, make_stack  # noqa: E402
from test_playback_sync import (  # noqa: E402
    pump,
    requests_of,
    start_fake,
    stop_fake,
)

from stageloom.blender.engine.dependencies import stage_dependencies  # noqa: E402

PORT = 9159


def _cube(name, calc_uvs=True):
    bpy.ops.mesh.primitive_cube_add(size=2.0, calc_uvs=calc_uvs)
    obj = bpy.context.object
    obj.name = name
    return obj


def _add_stage(obj, label):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.stageloom.add_stage(label=label)
    return obj.gh_stack.stages[-1]


def _reading_group(name, reference):
    """A Geometry Nodes group that reads an Object or a Collection."""
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket(
        "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
    )
    if isinstance(reference, bpy.types.Collection):
        node = group.nodes.new("GeometryNodeCollectionInfo")
        node.inputs["Collection"].default_value = reference
        output = node.outputs["Instances"]
    else:
        node = group.nodes.new("GeometryNodeObjectInfo")
        node.inputs["Object"].default_value = reference
        node.transform_space = "ORIGINAL"
        output = node.outputs["Geometry"]
    group_out = group.nodes.new("NodeGroupOutput")
    group.links.new(output, group_out.inputs["Geometry"])
    return group


def _read_before(obj, name, reference):
    """Insert a reading modifier so a stage added next sits after it."""
    modifier = obj.modifiers.new(name, "NODES")
    modifier.node_group = _reading_group(name + "Group", reference)
    obj.modifiers.active = modifier
    return modifier


class DependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.helpers, cls.nodes, cls.props, cls.stack_mod, cls.bridge = enable()

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        # Every test owns its scene: remove the user objects and the staging
        # plumbing they link, then build the few objects the test names.
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    def _assert_stages(self, pairs, expected):
        self.assertEqual(len(pairs), len(expected), "dependency count differs")
        for (owner, stage), (want_owner, want_stage) in zip(pairs, expected):
            self.assertEqual(
                owner.as_pointer(),
                want_owner.as_pointer(),
                "dependency object differs",
            )
            self.assertEqual(
                stage.as_pointer(),
                want_stage.as_pointer(),
                "dependency stage differs",
            )

    def test_01_same_object_chain_is_upstream_only(self):
        obj = _cube("Chain")
        stage_a = _add_stage(obj, "A")
        bevel = obj.modifiers.new("MidBevel", "BEVEL")
        obj.modifiers.active = bevel
        stage_b = _add_stage(obj, "B")

        self._assert_stages(stage_dependencies(obj, stage_b), [(obj, stage_a)])
        self.assertEqual(
            stage_dependencies(obj, stage_a),
            [],
            "the first stage depends on a downstream stage",
        )

    def test_02_a_prefix_object_reference_reaches_its_stage(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder")
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_03_transitive_object_references_are_walked(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        middle = _cube("Middle")
        _read_before(middle, "ReadsSource", source)
        middle_stage = _add_stage(middle, "Middle")
        holder = _cube("Holder")
        _read_before(holder, "ReadsMiddle", middle)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage),
            [(middle, middle_stage), (source, source_stage)],
        )

    def test_04_a_collection_reference_contributes_its_members(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        collection = bpy.data.collections.new("DependencySources")
        bpy.context.scene.collection.children.link(collection)
        collection.objects.link(source)
        holder = _cube("Holder")
        _read_before(holder, "ReadsCollection", collection)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_05_a_downstream_consumer_is_not_upstream(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder")
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")

        dependencies = stage_dependencies(source, source_stage)
        self.assertEqual(
            dependencies, [], "a stage that reads the selected stage was included"
        )
        self.assertNotIn(
            holder_stage.key,
            [stage.key for _obj, stage in dependencies],
            "the downstream consumer stage was included",
        )

    def test_06_an_unrelated_object_is_absent(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder")
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")
        _unrelated, unrelated_stage = make_stack("Unrelated", "Unrelated", params=None)

        dependencies = stage_dependencies(holder, holder_stage)
        self._assert_stages(dependencies, [(source, source_stage)])
        self.assertNotIn(
            unrelated_stage.key,
            [stage.key for _obj, stage in dependencies],
            "an unrelated object's stage was included",
        )

    def test_07_a_cycle_terminates_and_returns_each_stage_once(self):
        first = _cube("First")
        second = _cube("Second")
        _read_before(first, "ReadsSecond", second)
        first_stage = _add_stage(first, "First")
        _read_before(second, "ReadsFirst", first)
        second_stage = _add_stage(second, "Second")

        first_dependencies = stage_dependencies(first, first_stage)
        second_dependencies = stage_dependencies(second, second_stage)
        self._assert_stages(first_dependencies, [(second, second_stage)])
        self._assert_stages(second_dependencies, [(first, first_stage)])
        self.assertEqual(
            len(first_dependencies),
            len(set((owner.as_pointer(), stage.key) for owner, stage in first_dependencies)),
            "the cycle returned a stage twice",
        )

    def test_08_an_intermediate_object_without_a_stage_is_traversed(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        middle = _cube("Middle")
        _read_before(middle, "ReadsSource", source)
        holder = _cube("Holder")
        _read_before(holder, "ReadsMiddle", middle)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_09_nested_node_groups_are_walked(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        inner = _reading_group("InnerReadsSource", source)
        outer = bpy.data.node_groups.new("OuterReadsSource", "GeometryNodeTree")
        outer.interface.new_socket(
            "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
        group_node = outer.nodes.new("GeometryNodeGroup")
        group_node.node_tree = inner
        group_out = outer.nodes.new("NodeGroupOutput")
        outer.links.new(group_node.outputs["Geometry"], group_out.inputs["Geometry"])

        holder = _cube("Holder")
        modifier = holder.modifiers.new("ReadsNested", "NODES")
        modifier.node_group = outer
        holder.modifiers.active = modifier
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_10_a_geometry_nodes_modifier_object_input_is_read(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        group = bpy.data.node_groups.new("InputReadsSource", "GeometryNodeTree")
        group.interface.new_socket(
            "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
        )
        group.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketObject")
        group_in = group.nodes.new("NodeGroupInput")
        info = group.nodes.new("GeometryNodeObjectInfo")
        group.links.new(group_in.outputs["Source"], info.inputs["Object"])
        group_out = group.nodes.new("NodeGroupOutput")
        group.links.new(info.outputs["Geometry"], group_out.inputs["Geometry"])

        holder = _cube("Holder")
        modifier = holder.modifiers.new("ReadsInput", "NODES")
        modifier.node_group = group
        for key in modifier.properties.inputs.keys():
            modifier.properties.inputs[key]["value"] = source
        holder.modifiers.active = modifier
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_11_a_native_modifier_object_property_is_read(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder")
        boolean = holder.modifiers.new("ReadsSource", "BOOLEAN")
        boolean.object = source
        holder.modifiers.active = boolean
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )

    def test_12_a_generator_stage_has_no_upstream_stages(self):
        source, _source_stage = make_stack("Source", "Source", params=None)
        obj = _cube("Generator")
        _read_before(obj, "ReadsSource", source)
        _add_stage(obj, "Earlier")
        generator = _add_stage(obj, "Generator")
        generator.input_mode = self.stack_mod.INPUT_GENERATOR

        self.assertEqual(
            stage_dependencies(obj, generator),
            [],
            "a generator stage depends on a prefix it never reads",
        )

    def test_13_a_referenced_objects_post_stage_modifier_is_walked(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        target, target_stage = make_stack("Target", "Target", params=None)
        boolean = source.modifiers.new("ReadsTarget", "BOOLEAN")
        boolean.object = target
        holder = _cube("Holder")
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage),
            [(source, source_stage), (target, target_stage)],
        )

    def test_14_sl_named_user_data_is_not_plumbing(self):
        source, source_stage = make_stack("SL Source", "Source", params=None)
        collection = bpy.data.collections.new("SL User Sources")
        bpy.context.scene.collection.children.link(collection)
        collection.objects.link(source)
        holder = _cube("Holder")
        _read_before(holder, "ReadsCollection", collection)
        holder_stage = _add_stage(holder, "Holder")

        self._assert_stages(
            stage_dependencies(holder, holder_stage), [(source, source_stage)]
        )


class BakeFreezeTests(unittest.TestCase):
    """W2.1: Bake stage freezes its upstream closure; Clear Bake undoes it.

    These drive the real operator, the real native Bake node and the real
    add-on server with one fake Grasshopper client per stage. The native bake
    steps the scene frames headlessly, so the gate is exercised for real rather
    than through a bookkeeping seam.
    """

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        import stageloom.blender.bridge as bridge
        import stageloom.blender.engine as engine

        cls.bridge = bridge
        cls.engine = engine
        bpy.context.preferences.addons["stageloom"].preferences.data_root = tempfile.mkdtemp(
            prefix="stageloom-bake-freeze-"
        )
        if not engine.start_server(PORT):
            raise AssertionError("the add-on server did not start")
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        bpy.ops.preferences.addon_disable(module="stageloom")

    def setUp(self):
        engine = self.engine
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for cache in (engine._cache, engine._cache_bytes, engine._prefix_memo):
            cache.clear()
        for name in ("_pending", "_sent", "_verify", "_dirty", "_force_read"):
            getattr(engine, name).clear()
        engine._playback_sync = False
        engine._bake_sync = False
        engine._bake_progress = None
        self.fakes = []
        self.reports = {}
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_end = 3
        scene.frame_set(1)
        scene.sync_mode = "NONE"
        self.blend_dir = tempfile.mkdtemp(prefix="stageloom-bake-freeze-blend-")

    def tearDown(self):
        for fake in self.fakes:
            stop_fake(fake)
        self.fakes = []
        if not pump(seconds=5.0, until=lambda: not self.bridge.state["clients"]):
            raise AssertionError("the bake fakes never disconnected")
        self.engine._playback_sync = False
        self.engine._bake_sync = False
        self.engine._bake_progress = None

    def _start_fake(self, stage, *extra):
        report = os.path.join(
            tempfile.gettempdir(), "fake_gh_bake_%s.json" % stage.key
        )
        if os.path.exists(report):
            os.remove(report)
        self.fakes.append(
            start_fake(stage.key, report, "--delay-ms", "10", *extra, port=PORT)
        )
        self.reports[stage.key] = report

    def _wait_clients(self, count):
        deadline = time.time() + 10.0
        while time.time() < deadline and self.bridge.state["clients"] < count:
            time.sleep(0.02)
        if self.bridge.state["clients"] < count:
            raise AssertionError("the bake fakes never connected")

    def _settle(self, *stages):
        if not pump(
            seconds=15.0, until=lambda: all(stage.state == "ok" for stage in stages)
        ):
            raise AssertionError(
                "the stages never settled: %r"
                % [(stage.key, stage.state, stage.error) for stage in stages]
            )

    def _save(self):
        bpy.ops.wm.save_as_mainfile(
            filepath=os.path.join(self.blend_dir, "bake.blend")
        )

    def _same_object_chain(self):
        obj = _cube("BakeChain", calc_uvs=False)
        stage_a = _add_stage(obj, "A")
        bevel = obj.modifiers.new("BakeBevel", "BEVEL")
        bevel.width = 0.1
        obj.modifiers.active = bevel
        stage_b = _add_stage(obj, "B")
        return obj, stage_a, stage_b

    def _bake(self, obj, index):
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        return bpy.ops.stageloom.bake(index=index)

    def _clear(self, obj, index):
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        return bpy.ops.stageloom.clear_bake(index=index)

    def test_01_same_object_bake_freezes_upstream_and_clear_undoes_it(self):
        obj, stage_a, stage_b = self._same_object_chain()
        self._start_fake(stage_a)
        self._start_fake(stage_b)
        self._wait_clients(2)
        self._settle(stage_a, stage_b)
        before = requests_of(self.reports[stage_a.key])
        self._save()

        self.assertEqual(self._bake(obj, 1), {"FINISHED"})
        self.assertTrue(stage_b.baked, "the baked stage is not marked baked")
        self.assertTrue(stage_b.frozen, "the baked stage is not frozen")
        token = stage_b.bake_token
        self.assertEqual(len(token), 16, "the bake did not mint a token")
        self.assertTrue(stage_a.frozen, "the upstream stage was not frozen")
        self.assertEqual(
            stage_a.frozen_by_bake, token, "the upstream stage carries another token"
        )
        self.assertFalse(stage_a.baked, "an upstream stage is marked baked")
        self.assertEqual(stage_a.state, "frozen")
        self.assertGreater(
            requests_of(self.reports[stage_a.key]),
            before,
            "the upstream stage did not run live during the bake",
        )

        self.assertEqual(self._clear(obj, 1), {"FINISHED"})
        self.assertFalse(stage_b.baked)
        self.assertFalse(stage_b.frozen)
        self.assertFalse(stage_a.frozen, "Clear Bake left the upstream frozen")
        self.assertEqual(stage_b.bake_token, "")
        self.assertEqual(stage_a.frozen_by_bake, "")

    def test_02_a_cross_object_upstream_is_frozen_and_unfrozen(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder", calc_uvs=False)
        _read_before(holder, "ReadsSource", source)
        holder_stage = _add_stage(holder, "Holder")
        self._start_fake(source_stage)
        self._start_fake(holder_stage)
        self._wait_clients(2)
        self._settle(source_stage, holder_stage)
        self._save()

        self.assertEqual(self._bake(holder, 0), {"FINISHED"})
        token = holder_stage.bake_token
        self.assertTrue(holder_stage.baked)
        self.assertTrue(source_stage.frozen, "the referenced stage was not frozen")
        self.assertEqual(source_stage.frozen_by_bake, token)

        self.assertEqual(self._clear(holder, 0), {"FINISHED"})
        self.assertFalse(source_stage.frozen, "Clear Bake left the source frozen")
        self.assertEqual(source_stage.frozen_by_bake, "")
        self.assertEqual(holder_stage.bake_token, "")

    def test_03_a_downstream_stage_stays_live(self):
        obj, stage_a, stage_b = self._same_object_chain()
        self._start_fake(stage_a)
        self._start_fake(stage_b)
        self._wait_clients(2)
        self._settle(stage_a, stage_b)
        self._save()

        self.assertEqual(self._bake(obj, 0), {"FINISHED"})
        self.assertTrue(stage_a.baked)
        self.assertTrue(stage_a.frozen)
        self.assertFalse(stage_b.baked, "the downstream stage was baked")
        self.assertFalse(stage_b.frozen, "the downstream stage was frozen")
        self.assertEqual(stage_b.frozen_by_bake, "")

    def test_04_a_pre_frozen_upstream_stays_frozen_and_unmarked(self):
        obj, stage_a, stage_b = self._same_object_chain()
        self._start_fake(stage_a)
        self._start_fake(stage_b)
        self._wait_clients(2)
        self._settle(stage_a, stage_b)
        stage_a.frozen = True
        self.engine.freeze(obj, stage_a)
        self._save()

        self.assertEqual(self._bake(obj, 1), {"FINISHED"})
        token = stage_b.bake_token
        self.assertTrue(stage_a.frozen, "the user's freeze was lost")
        self.assertEqual(
            stage_a.frozen_by_bake, "", "a pre-frozen stage was marked by the bake"
        )
        self.assertNotEqual(token, "")

        self.assertEqual(self._clear(obj, 1), {"FINISHED"})
        self.assertTrue(stage_a.frozen, "Clear Bake unfroze a pre-frozen stage")
        self.assertEqual(stage_a.frozen_by_bake, "")
        self.assertFalse(stage_b.baked)

    def test_05_a_failed_bake_freezes_nothing_and_reports_the_error(self):
        obj, stage_a, stage_b = self._same_object_chain()
        # The first request settles the stage; the next one lands inside the
        # native bake and fails the solve.
        self._start_fake(stage_a, "--fail-after", "2")
        self._start_fake(stage_b)
        self._wait_clients(2)
        self._settle(stage_a, stage_b)
        self._save()

        # The operator reports the failure as an ERROR; Blender raises it here
        # instead of returning a result, which is exactly the visible failure.
        with self.assertRaises(RuntimeError) as caught:
            self._bake(obj, 1)
        self.assertIn("bake failed", str(caught.exception))
        self.assertFalse(stage_b.baked, "a failed bake left the stage baked")
        self.assertFalse(stage_b.frozen, "a failed bake left the stage frozen")
        self.assertEqual(stage_b.bake_token, "", "a failed bake left a token")
        self.assertFalse(stage_a.frozen, "a failed bake froze an upstream stage")
        self.assertEqual(stage_a.frozen_by_bake, "")
        self.assertGreater(
            self.engine.stats["playback_failures"], 0, "the failure was not counted"
        )
        self.assertTrue(
            any(
                line.startswith("bake:") and "fake solve failure" in line
                for line in self.engine._trace
            ),
            "the failed bake left no visible error: %r" % (list(self.engine._trace),),
        )

    def test_06_a_rebake_replaces_the_old_upstream_token(self):
        obj, stage_a, stage_b = self._same_object_chain()
        self._start_fake(stage_a)
        self._start_fake(stage_b)
        self._wait_clients(2)
        self._settle(stage_a, stage_b)
        self._save()

        self.assertEqual(self._bake(obj, 1), {"FINISHED"})
        old_token = stage_b.bake_token
        self.assertTrue(stage_a.frozen)
        self.assertEqual(stage_a.frozen_by_bake, old_token)

        self.assertEqual(self._bake(obj, 1), {"FINISHED"})
        new_token = stage_b.bake_token
        self.assertNotEqual(new_token, old_token, "the rebake kept the old token")
        self.assertTrue(stage_a.frozen, "the rebake did not refreeze the upstream")
        self.assertEqual(stage_a.frozen_by_bake, new_token)

        self.assertEqual(self._clear(obj, 1), {"FINISHED"})
        self.assertFalse(stage_a.frozen)
        self.assertEqual(stage_a.frozen_by_bake, "")

    def test_07_an_unsettled_upstream_aborts_before_the_native_bake(self):
        obj, stage_a, stage_b = self._same_object_chain()
        self._start_fake(stage_a)
        self._start_fake(stage_b)
        self._wait_clients(2)
        self._settle(stage_a, stage_b)
        self._save()

        for state in ("error", "waiting", "unbound"):
            with self.subTest(state=state):
                stage_a.state = state
                with self.assertRaises(RuntimeError) as caught:
                    self._bake(obj, 1)
                self.assertIn("not ready", str(caught.exception))
                self.assertFalse(stage_b.baked)
                self.assertFalse(stage_b.frozen)

        stage_a.state = "ok"
        for state in ("error", "waiting", "unbound"):
            with self.subTest(selected=state):
                stage_b.state = state
                with self.assertRaises(RuntimeError) as caught:
                    self._bake(obj, 1)
                self.assertIn("not ready", str(caught.exception))
                self.assertFalse(stage_b.baked)
        stage_b.state = "ok"

        pending_key = (obj.gh_stack.stack_id, stage_a.key)
        self.engine._pending[pending_key] = {"rid": 0.0}
        try:
            with self.assertRaises(RuntimeError) as caught:
                self._bake(obj, 1)
            self.assertIn("request pending", str(caught.exception))
        finally:
            self.engine._pending.pop(pending_key, None)
        self.assertFalse(stage_b.baked)

        selected_pending = (obj.gh_stack.stack_id, stage_b.key)
        self.engine._pending[selected_pending] = {"rid": 0.0}
        try:
            with self.assertRaises(RuntimeError) as caught:
                self._bake(obj, 1)
            self.assertIn("request pending", str(caught.exception))
        finally:
            self.engine._pending.pop(selected_pending, None)
        self.assertFalse(stage_b.baked)

    def test_08_a_split_stage_drops_the_old_bake_markers(self):
        obj, stage_a, _stage_b = self._same_object_chain()
        stage_a.bake_token = "old-token"
        stage_a.frozen_by_bake = "upstream-token"
        old_key = stage_a.key

        new_key = self.engine._split_stage(obj, stage_a)

        self.assertNotEqual(new_key, old_key)
        self.assertEqual(stage_a.key, new_key)
        self.assertEqual(stage_a.bake_token, "", "a split kept the old bake token")
        self.assertEqual(
            stage_a.frozen_by_bake, "", "a split kept the old freeze marker"
        )
