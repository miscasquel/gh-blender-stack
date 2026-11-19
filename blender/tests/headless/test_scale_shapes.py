"""Layer B: the T10c scale shapes, as structure rather than timing.

The full timing run is ``scripts/scale`` (a local/release check). This module
keeps the same three shapes in the normal headless suite so a structural
regression fails CI without running the timing evidence:

- one object with **200 stage records**: record/modifier order, unique keys,
  a 200-key index with one owner and no conflicts, a 200-row panel draw that
  does not rebuild the index, and a no-client pass that reads no prefix;
- one **100,000-triangle** mesh (250 x 200 quads): read, fresh apply and an
  in-place apply whose topology re-read is made to raise;
- one small definition on **10,000 instance rows**: one definition, 10,000
  row transforms, a 10,000-point row cloud, one evaluated reference and group
  reuse on the repeated apply.

These are shape proofs, not new latency budgets. The existing suite already
covers mixed n-gon reads, the topology token and the instance grammar at small
sizes; this module only adds the scale dimension.
"""

import contextlib
import math
import os
import sys
import types
import unittest
from unittest import mock

import bpy
import numpy as np

from test_instances import _points, _quad
from test_ui_panel import FakeLayout, PanelHarness

from stageloom.blender import geometry_io, helpers, nodes
from stageloom.blender import stack as stack_mod
from stageloom.blender import ui
from stageloom.blender.engine import ENGINE
from stageloom.blender.engine import apply as engine_apply
from stageloom.blender.engine import lifecycle
from stageloom.blender.engine import request as engine_request
from stageloom.blender.geometry_io import mesh as geometry_mesh

TOKEN = "sha256:" + "0" * 64


@contextlib.contextmanager
def quiet_stdout():
    """Hide the operator's 200 ``Info: Added stage ...`` report lines.

    Blender writes those reports straight to file descriptor 1, so redirecting
    ``sys.stdout`` is not enough. A test failure still reaches stderr.
    """
    try:
        sys.stdout.flush()
        saved = os.dup(1)
    except (AttributeError, OSError):
        yield
        return
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
        yield
    finally:
        try:
            sys.stdout.flush()
        except (AttributeError, ValueError):
            pass
        os.dup2(saved, 1)
        os.close(saved)


def triangle_grid(triangles):
    """An exact quad grid of ``triangles`` triangles (the count is even)."""
    quads = triangles // 2
    columns = math.isqrt(quads)
    while columns > 1 and quads % columns:
        columns -= 1
    rows = quads // columns
    if columns * rows * 2 != triangles:
        raise AssertionError("cannot factor %d quads into a grid" % quads)
    xs = np.linspace(0.0, 10.0, columns + 1, dtype=np.float32)
    ys = np.linspace(0.0, 10.0, rows + 1, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    verts = np.stack(
        [gx.reshape(-1), gy.reshape(-1), np.zeros(gx.size, dtype=np.float32)], axis=1
    )
    i = np.arange(columns, dtype=np.int64)[:, None]
    j = np.arange(rows, dtype=np.int64)[None, :]
    a = (i * (rows + 1) + j).reshape(-1)
    quads_array = np.stack(
        [a, a + rows + 1, a + rows + 2, a + 1], axis=1
    ).astype(np.int32)
    return verts, quads_array


def mesh_object(name, verts, quads):
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set(
        "co", np.ascontiguousarray(verts, dtype=np.float32).reshape(-1)
    )
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set(
        "vertex_index", np.ascontiguousarray(quads, dtype=np.int32).reshape(-1)
    )
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set(
        "loop_start", np.arange(0, quads.size, 4, dtype=np.int32)
    )
    mesh.update(calc_edges=True)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def by_name(sections):
    return {name: np.asarray(values) for name, _dtype, values in sections}


def mesh_instance_holder(name, points_obj, source):
    """A mesh anchor for Instance on Points: ``add_stage`` needs a stack type.

    ``test_instances._instance_holder`` uses an Empty, which is read-only in
    that suite; a stage needs a mesh/curve/point-cloud anchor, exactly like the
    benchmark's holder.
    """
    group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.interface.new_socket(
        "Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry"
    )
    output = group.nodes.new("NodeGroupOutput")
    iono = group.nodes.new("GeometryNodeInstanceOnPoints")
    source_info = group.nodes.new("GeometryNodeObjectInfo")
    source_info.inputs["Object"].default_value = source
    source_info.transform_space = "ORIGINAL"
    points_info = group.nodes.new("GeometryNodeObjectInfo")
    points_info.inputs["Object"].default_value = points_obj
    points_info.transform_space = "ORIGINAL"
    group.links.new(source_info.outputs["Geometry"], iono.inputs["Instance"])
    group.links.new(points_info.outputs["Geometry"], iono.inputs["Points"])
    group.links.new(iono.outputs["Instances"], output.inputs["Geometry"])
    holder = bpy.data.objects.new(name, bpy.data.meshes.new(name + "-base"))
    bpy.context.scene.collection.objects.link(holder)
    holder.modifiers.new("Instances", "NODES").node_group = group
    return holder


class ScaleShapeTests(unittest.TestCase):
    """The 200-stage, 100k-triangle and 10k-instance shapes."""

    def setUp(self):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.preferences.addon_enable(module="stageloom")
        ENGINE.dirty.clear()
        ENGINE.clear_runtimes()
        ENGINE.verify.clear()

    def tearDown(self):
        try:
            bpy.ops.preferences.addon_disable(module="stageloom")
        except (RuntimeError, AttributeError):
            pass

    def test_01_two_hundred_stages_keep_order_and_index(self):
        verts = np.array(
            [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], dtype=np.float32
        )
        quads = np.array([[0, 1, 2, 3]], dtype=np.int32)
        obj = mesh_object("scale-stages", verts, quads)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)

        count = 200
        labels = ["Scale %03d" % (index + 1) for index in range(count)]
        with quiet_stdout():
            for label in labels:
                bpy.ops.stageloom.add_stage(label=label)

        record = stack_mod.stack_of(obj)
        self.assertIsNotNone(record)
        self.assertEqual(len(record.stages), count)
        self.assertEqual([stage.label for stage in record.stages], labels)
        keys = [stage.key for stage in record.stages]
        self.assertEqual(len(set(keys)), count, "stage keys are not unique")
        self.assertEqual(len(obj.modifiers), count)

        ENGINE.index_dirty = True
        lifecycle._ensure_index()
        self.assertEqual(len(ENGINE.stage_index), count)
        self.assertEqual(len(ENGINE.stack_objects), 1)
        self.assertIs(ENGINE.stack_objects[0], obj)
        self.assertEqual(len(ENGINE.stage_owners), count)
        self.assertEqual(len(ENGINE.stage_claims), count)
        self.assertEqual(ENGINE.stage_conflicts, set())
        for key in keys:
            self.assertIs(ENGINE.stage_owners[key], obj)
            self.assertEqual(len(ENGINE.stage_index[key]), 1)
            self.assertIs(ENGINE.stage_index[key][0], obj)

        ordered = ENGINE.ordered_stages(obj, record)
        self.assertEqual([stage.key for stage in ordered], keys)

        FakeLayout.icon_names = ui._ICON_NAMES
        rebuilds = ENGINE.stats()["index_rebuilds"]
        layout = FakeLayout()
        PanelHarness(layout, ui.STAGELOOM_PT_panel).draw(
            types.SimpleNamespace(object=obj)
        )
        self.assertTrue(
            all(key in layout.labels for key in keys),
            "the panel omitted a stage key",
        )
        self.assertNotIn(
            "panel failed to draw",
            "\n".join(layout.labels),
            "the panel failed to draw a stage",
        )
        self.assertEqual(
            sum(1 for idname, _text in layout.operators if idname == "stageloom.pull"),
            count,
        )
        self.assertEqual(
            ENGINE.stats()["index_rebuilds"],
            rebuilds,
            "the panel rebuilt the stage index",
        )

        ENGINE.dirty.clear()
        clones = ENGINE.stats()["prefix_clones"]
        self.assertFalse(lifecycle._process_object(obj))
        self.assertEqual(ENGINE.stats()["prefix_clones"], clones)
        self.assertTrue(
            all(
                stage.state in (stack_mod.STATE_UNBOUND, stack_mod.STATE_DISCONNECTED)
                for stage in record.stages
            )
        )

    def test_02_one_hundred_thousand_triangles_apply_in_place(self):
        triangles = 100_000
        verts, quads = triangle_grid(triangles)
        obj = mesh_object("scale-triangles", verts, quads)
        mesh = obj.data
        mesh.calc_loop_triangles()
        self.assertEqual(len(quads), triangles // 2)
        self.assertEqual(len(verts), 50_451)
        self.assertEqual(len(mesh.loop_triangles), triangles)

        sections = geometry_io.mesh_to_sections(mesh)
        flat = by_name(sections)
        self.assertEqual(list(flat), ["mesh.verts", "mesh.face_counts", "mesh.face_indices"])
        self.assertEqual(flat["mesh.verts"].shape, (len(verts), 3))
        self.assertEqual(len(flat["mesh.face_counts"]), len(quads))
        self.assertEqual(int(flat["mesh.face_counts"].sum()), len(quads) * 4)

        target = bpy.data.meshes.new("scale-triangles-applied")
        try:
            geometry_io.sections_to_mesh(target, sections)
            target.calc_loop_triangles()
            self.assertEqual(len(target.vertices), len(verts))
            self.assertEqual(len(target.polygons), len(quads))
            self.assertEqual(len(target.loop_triangles), triangles)

            moved = np.array(flat["mesh.verts"], dtype=np.float32, copy=True)
            moved = moved.reshape(-1, 3)
            moved[:, 0] += 0.5
            payload = [("mesh.verts", "f32", moved.reshape(-1))]
            payload.extend(
                (name, dtype, values)
                for name, dtype, values in sections
                if name != "mesh.verts"
            )
            before_x = float(target.vertices[0].co.x)
            with mock.patch.object(
                geometry_mesh,
                "_topology_matches",
                side_effect=AssertionError("the in-place scale path re-read topology"),
            ):
                geometry_io.sections_to_mesh(
                    target, payload, topology=TOKEN, previous=TOKEN
                )
            target.calc_loop_triangles()
            self.assertAlmostEqual(
                float(target.vertices[0].co.x), before_x + 0.5, places=5
            )
            self.assertEqual(len(target.polygons), len(quads))
            self.assertEqual(len(target.loop_triangles), triangles)
        finally:
            bpy.data.meshes.remove(target)

    def test_03_ten_thousand_instance_rows_apply_as_one_definition(self):
        count = 10_000
        source = _quad("scale-source")
        positions = [
            [float(index % 100) * 2.0, float(index // 100) * 2.0, 0.0]
            for index in range(count)
        ]
        points = _points("scale-points", positions)
        holder = mesh_instance_holder("scale-holder", points, source)
        bpy.context.view_layer.objects.active = holder
        holder.select_set(True)
        with quiet_stdout():
            bpy.ops.stageloom.add_stage(label="Instances")
        stage = holder.gh_stack.stages[-1]
        record = stack_mod.stack_of(holder)

        ENGINE.prefix_memo.clear()
        ENGINE.prefix_batch.clear()
        ENGINE.prefix_batch_built = False
        sections, _geom_hash, attributes, definitions = (
            engine_request._prefix_sections(holder, record, stage)
        )
        flat = by_name(sections)

        self.assertEqual(definitions, [{"name": "scale-source"}])
        self.assertEqual(len(flat["inst.ref_def"]), count)
        self.assertEqual(set(int(value) for value in flat["inst.ref_def"]), {0})
        matrices = np.asarray(flat["inst.transforms"], dtype=np.float32).reshape(
            -1, 16
        )
        self.assertEqual(matrices.shape, (count, 16))
        self.assertEqual(
            [float(matrices[0][3]), float(matrices[0][7]), float(matrices[0][11])],
            [0.0, 0.0, 0.0],
        )
        self.assertEqual(
            [float(matrices[-1][3]), float(matrices[-1][7]), float(matrices[-1][11])],
            [198.0, 198.0, 0.0],
        )
        self.assertEqual(flat["idef.0.mesh.verts"].shape, (4, 3))
        self.assertEqual(len(flat["idef.0.mesh.face_counts"]), 1)
        self.assertFalse(
            any(name.startswith("idef.0.inst.") for name in flat),
            "a single-part definition gained a nested row namespace",
        )

        engine_apply._apply(
            holder,
            stage,
            flat,
            attributes,
            definitions=definitions,
            topology=TOKEN,
        )
        clouds = helpers.instance_clouds(holder, stage.key)
        self.assertEqual(sorted(clouds), [0])
        self.assertEqual(len(clouds[0].data.points), count)
        definitions_map = helpers.definition_collections(holder, stage.key)
        self.assertEqual(sorted(definitions_map), [0])
        definition_helper = bpy.data.objects[
            helpers.definition_name(holder, stage.key, 0, "mesh")
        ]
        self.assertEqual(len(definition_helper.data.vertices), 4)

        group = bpy.data.node_groups[nodes.group_name(stage.key)]
        stage.applied_topology = TOKEN
        matrices[:, 3] += 1.0
        payload = dict(flat)
        payload["inst.transforms"] = matrices.reshape(-1)
        engine_apply._apply(
            holder,
            stage,
            payload,
            attributes,
            definitions=definitions,
            topology=TOKEN,
        )
        self.assertIs(
            bpy.data.node_groups[nodes.group_name(stage.key)],
            group,
            "the repeated 10k apply rebuilt the node group",
        )
        co = np.empty(len(clouds[0].data.points) * 3, dtype=np.float32)
        clouds[0].data.points.foreach_get("co", co)
        self.assertAlmostEqual(float(co.reshape(-1, 3)[0][0]), 1.0, places=5)

        bpy.context.view_layer.update()
        geometry = holder.evaluated_get(
            bpy.context.evaluated_depsgraph_get()
        ).evaluated_geometry()
        evaluated_points = geometry.instances_pointcloud()
        self.assertIsNotNone(evaluated_points)
        self.assertEqual(len(evaluated_points.points), count)
        references = geometry.instance_references()
        self.assertEqual(len(references), 1)
        self.assertIsNotNone(references[0].mesh)
        self.assertEqual(len(references[0].mesh.vertices), 4)


if __name__ == "__main__":
    unittest.main()
