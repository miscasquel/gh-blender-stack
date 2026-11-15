"""Layer B: upstream stage dependencies, and later the chain bake.

Run with:

    scripts/test-blender

W2.2: a stage depends on the stages before it on its own object and on the
stages of objects or collections its prefix reads, transitively. A downstream
consumer is not an upstream dependency, even when it reads the selected stage.
The bake tests (W2.1) and the offline render acceptance (W2.3) extend this file.
"""

import hashlib
import os
import shutil
import tempfile
import time
import unittest

import bpy
import numpy as np

from harness import (
    SCALE,
    free_port,
    isolate_data_root,
    keyframe_scale,
    pump,
    requests_of,
    start_fake,
    stop_fake,
)
from test_duplicates import enable, make_stack  # noqa: E402
from stageloom.blender.engine.dependencies import stage_dependencies  # noqa: E402


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


def _reading_group(name, reference, join=False):
    """A Geometry Nodes group that reads an Object or a Collection.

    With ``join`` the read geometry is added to the prefix the modifier
    receives, instead of replacing it, so a stage after it sees both.
    """
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket(
        "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
    )
    if join:
        group.interface.new_socket(
            "Geometry", in_out="INPUT", socket_type="NodeSocketGeometry"
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
    if join:
        group_in = group.nodes.new("NodeGroupInput")
        join_node = group.nodes.new("GeometryNodeJoinGeometry")
        group.links.new(group_in.outputs["Geometry"], join_node.inputs["Geometry"])
        group.links.new(output, join_node.inputs["Geometry"])
        output = join_node.outputs["Geometry"]
    group_out = group.nodes.new("NodeGroupOutput")
    group.links.new(output, group_out.inputs["Geometry"])
    return group


def _read_before(obj, name, reference, join=False):
    """Insert a reading modifier so a stage added next sits after it."""
    modifier = obj.modifiers.new(name, "NODES")
    modifier.node_group = _reading_group(name + "Group", reference, join=join)
    obj.modifiers.active = modifier
    return modifier


def _mesh_geometry(mesh):
    """A stable (vertex count, coordinate hash, max coordinate) for a mesh."""
    count = len(mesh.vertices)
    if not count:
        return (0, hashlib.sha256(b"").hexdigest(), 0.0)
    co = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    return (
        count,
        hashlib.sha256(co.tobytes()).hexdigest(),
        float(np.abs(co).max()),
    )


def _evaluated_geometry(obj):
    """The object's evaluated output, through every modifier in the stack."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    if mesh is None:
        return (0, "", 0.0)
    try:
        return _mesh_geometry(mesh)
    finally:
        evaluated.to_mesh_clear()


def _helper_mesh_geometry(obj, stage_key):
    """The mesh a stage's helper holds: its last applied Grasshopper output."""
    from stageloom.blender import helpers

    helper = helpers.helper_for(obj, stage_key, "mesh")
    if helper is None:
        raise AssertionError("the stage has no mesh helper")
    return _mesh_geometry(helper.data)


class DependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.helpers, cls.nodes, cls.props, cls.stack_mod, cls.bridge = enable(cls)

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


class _BakeFixture(unittest.TestCase):
    """The real operator, native Bake node and add-on server, one fake client
    per stage. The native bake steps the scene frames headlessly, so the gate
    is exercised for real rather than through a bookkeeping seam."""

    @classmethod
    def setUpClass(cls):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        cls.enterClassContext(isolate_data_root("stageloom-bake-freeze-"))
        import stageloom.blender.bridge as bridge
        import stageloom.blender.engine as engine
        import stageloom.blender.nodes as nodes

        cls.bridge = bridge
        cls.engine = engine
        cls.nodes = nodes
        cls.port = free_port()
        if not engine.start_server(cls.port):
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
        self.addCleanup(shutil.rmtree, self.blend_dir, ignore_errors=True)

    def tearDown(self):
        self._stop_fakes()
        self.engine._playback_sync = False
        self.engine._bake_sync = False
        self.engine._bake_progress = None

    def _stop_fakes(self):
        for fake in self.fakes:
            stop_fake(fake)
        self.fakes = []
        if not pump(seconds=5.0, until=lambda: not self.bridge.state["clients"]):
            raise AssertionError("the bake fakes never disconnected")

    def _start_fake(self, stage, *extra):
        report = os.path.join(
            tempfile.gettempdir(), "fake_gh_bake_%s.json" % stage.key
        )
        if os.path.exists(report):
            os.remove(report)
        self.fakes.append(
            start_fake(stage.key, report, "--delay-ms", "10", *extra, port=self.port)
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

    def _frame_geometry(self, obj, frame):
        """Evaluate ``obj`` at ``frame`` through the depsgraph, no client pass."""
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        return _evaluated_geometry(obj)

    def _helper_geometry(self, obj, stage):
        return _helper_mesh_geometry(obj, stage.key)


class BakeFreezeTests(_BakeFixture):
    """W2.1: Bake stage freezes its upstream closure; Clear Bake undoes it."""

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


class OfflineBakeAcceptanceTests(_BakeFixture):
    """W2.3: the baked chain renders offline and survives save/reload.

    The selected stage has both kinds of upstream: A is earlier on the same
    object, C is another object read through Object Info. B's own parameter is
    keyframed 1/2/3, so the bake covers its range; A and C stay at one value.
    That is the honest static-upstream shape: the baked stage has per-frame
    data, the frozen upstream objects do not animate.
    """

    def _stage(self, obj, key):
        stage = next((item for item in obj.gh_stack.stages if item.key == key), None)
        self.assertIsNotNone(stage, "stage %s is missing after the reload" % key)
        return stage

    def _native_entry(self, modifier):
        return next(
            (
                item
                for item in modifier.bakes
                if item.node is not None
                and item.node.bl_idname == "GeometryNodeBake"
            ),
            None,
        )

    def test_01_offline_chain_bake_survives_save_reload_and_clear(self):
        source, source_stage = make_stack("Source", "Source", params=None)
        holder = _cube("Holder", calc_uvs=False)
        holder_stage_a = _add_stage(holder, "A")
        bevel = holder.modifiers.new("BakeBevel", "BEVEL")
        bevel.width = 0.1
        holder.modifiers.active = bevel
        _read_before(holder, "ReadsSource", source, join=True)
        selected = _add_stage(holder, "B")

        self.assertEqual(
            [
                (owner.name, stage.key)
                for owner, stage in stage_dependencies(holder, selected)
            ],
            [(holder.name, holder_stage_a.key), (source.name, source_stage.key)],
            "the chain lacks its same-object or cross-object upstream stage",
        )

        for stage in (source_stage, holder_stage_a, selected):
            self._start_fake(stage)
        self._wait_clients(3)
        self._settle(source_stage, holder_stage_a, selected)
        self.assertTrue(
            pump(
                seconds=10.0,
                until=lambda: any(
                    param.param_id == SCALE for param in selected.params
                ),
            ),
            "the fake never published the selected stage's parameter",
        )

        scene = bpy.context.scene
        scene.frame_set(1)
        keyframe_scale(holder, selected, [1.0, 2.0, 3.0])
        scene.frame_set(1)

        upstream_before = {
            "A": self._helper_geometry(holder, holder_stage_a),
            "C": self._helper_geometry(source, source_stage),
        }
        for name, geometry in upstream_before.items():
            self.assertGreater(geometry[0], 0, "the %s helper is empty" % name)
            self.assertAlmostEqual(
                geometry[2], 1.0, places=3, msg="the %s helper has the wrong scale" % name
            )

        modifier = self.nodes.modifier_of(holder, selected)
        self.assertIsNotNone(modifier)
        group = bpy.data.node_groups[self.nodes.group_name(selected.key)]
        self.assertTrue(
            any(node.bl_idname == "GeometryNodeBake" for node in group.nodes),
            "the selected stage has no native Bake node",
        )
        modifier.bake_target = "PACKED"
        self.assertEqual(modifier.bake_target, "PACKED")
        self._save()
        blend = os.path.join(self.blend_dir, "bake.blend")

        self.assertEqual(self._bake(holder, 1), {"FINISHED"})
        self.assertTrue(selected.baked, "the selected stage is not baked")
        self.assertTrue(selected.frozen, "the selected stage is not frozen")
        token = selected.bake_token
        self.assertEqual(len(token), 16, "the bake did not mint a token")
        for stage in (holder_stage_a, source_stage):
            self.assertTrue(stage.frozen, "an upstream stage was not frozen")
            self.assertEqual(
                stage.frozen_by_bake, token, "an upstream stage carries another token"
            )
            self.assertFalse(stage.baked, "an upstream stage is marked baked")

        # Save again: the packed native bake and the freeze bookkeeping are only
        # durable once the file is written after the bake.
        self._save()
        self.assertTrue(os.path.exists(blend))

        sends = self.engine.stats["sends"]
        self._stop_fakes()
        baked = {}
        for frame in (1, 2, 3):
            baked[frame] = self._frame_geometry(holder, frame)
            self.engine._timer()  # the offline engine pass must not ask anyone
            self.assertEqual(
                self.engine.stats["sends"], sends, "evaluation sent a request"
            )
        self.assertEqual(
            len({geometry[1] for geometry in baked.values()}),
            3,
            "the baked stage did not hold per-frame output: %r" % (baked,),
        )
        self.assertEqual(
            [round(baked[frame][2], 3) for frame in (1, 2, 3)],
            [1.0, 2.0, 3.0],
            "the baked stage did not cover its keyed range",
        )

        for frame in (1, 2, 3):
            self.assertEqual(
                self._frame_geometry(holder, frame),
                baked[frame],
                "the baked stage changed on a second evaluation",
            )
            # The upstream objects are static: no per-frame assertion here.
            self.assertEqual(
                self._helper_geometry(holder, holder_stage_a),
                upstream_before["A"],
                "the frozen same-object upstream changed",
            )
            self.assertEqual(
                self._helper_geometry(source, source_stage),
                upstream_before["C"],
                "the frozen cross-object upstream changed",
            )
            self.assertEqual(self.engine.stats["sends"], sends)

        keys = (source_stage.key, holder_stage_a.key, selected.key)
        bpy.ops.wm.open_mainfile(filepath=blend)

        holder = bpy.data.objects["Holder"]
        source = bpy.data.objects["Source"]
        source_stage = self._stage(source, keys[0])
        holder_stage_a = self._stage(holder, keys[1])
        selected = self._stage(holder, keys[2])
        self.assertTrue(selected.baked, "the reload lost the baked marker")
        self.assertTrue(selected.frozen, "the reload lost the selected freeze")
        self.assertEqual(selected.bake_token, token, "the reload lost the bake token")
        for stage in (holder_stage_a, source_stage):
            self.assertTrue(stage.frozen, "the reload lost an upstream freeze")
            self.assertEqual(
                stage.frozen_by_bake, token, "the reload lost an upstream marker"
            )
            self.assertFalse(stage.baked)
        self.assertFalse(
            self.bridge.state["connected"], "a client survived the offline check"
        )

        modifier = self.nodes.modifier_of(holder, selected)
        self.assertIsNotNone(modifier)
        self.assertEqual(
            modifier.bake_target,
            "PACKED",
            "the reloaded modifier lost its packed bake target",
        )
        entry = self._native_entry(modifier)
        self.assertIsNotNone(entry, "the native bake entry is missing after the reload")
        self.assertEqual(
            getattr(entry, "directory", ""),
            "",
            "the bake is a disk bake, not the packed bake the test asked for",
        )

        sends = self.engine.stats["sends"]
        for frame in (1, 2, 3):
            self.assertEqual(
                self._frame_geometry(holder, frame),
                baked[frame],
                "the packed bake did not survive save/reload at frame %d" % frame,
            )
            self.engine._timer()
            self.assertEqual(
                self._helper_geometry(holder, holder_stage_a),
                upstream_before["A"],
                "the reloaded same-object upstream changed",
            )
            self.assertEqual(
                self._helper_geometry(source, source_stage),
                upstream_before["C"],
                "the reloaded cross-object upstream changed",
            )
            self.assertEqual(
                self.engine.stats["sends"], sends, "a reloaded evaluation sent a request"
            )

        self.assertEqual(self._clear(holder, 1), {"FINISHED"})
        self.assertFalse(selected.baked, "Clear Bake left the stage baked")
        self.assertFalse(selected.frozen, "Clear Bake left the stage frozen")
        self.assertFalse(
            holder_stage_a.frozen, "Clear Bake left the same-object upstream frozen"
        )
        self.assertFalse(
            source_stage.frozen, "Clear Bake left the cross-object upstream frozen"
        )
        self.assertEqual(selected.bake_token, "")
        self.assertEqual(selected.frozen_by_bake, "")
        for stage in (holder_stage_a, source_stage):
            self.assertEqual(stage.frozen_by_bake, "", "a freeze marker survived Clear Bake")

        sends = self.engine.stats["sends"]
        self._start_fake(source_stage)
        self._start_fake(holder_stage_a)
        self._start_fake(selected)
        self._wait_clients(3)
        self._settle(source_stage, holder_stage_a, selected)
        self.assertGreater(
            self.engine.stats["sends"],
            sends,
            "the live chain did not request again after Clear Bake",
        )
        for key in keys:
            self.assertGreater(
                requests_of(self.reports[key]),
                0,
                "stage %s never asked Grasshopper again" % key,
            )
